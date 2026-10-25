"""
Telegram interface to the desk. Stdlib only.

  text           → Desk.handle (DMs: direct; groups: conference, only when @mentioned,
                   a /command, or a reply to the bot)
  voice / audio  → blob store → VoiceStudio STT → Desk.handle(audio_blob=…) → text replies,
                   plus a voice note per reply in the agent's voice (TTS) when VoiceStudio is up
  document       → caption "/ingest TICKER KIND PUBLISHED [AS_OF]" → pipeline.ingest in the background

Security: only TELEGRAM_ALLOWED_USER_IDS are served. Anyone else is ignored silently, and
recorded as a 'message_refused' ledger entry (ids only, never content). Every message in and
out goes through the desk, which writes it to the ledger.
"""
import json
import os
import signal
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from grinos import voice
from grinos.desk import COMPANIES_DIR, Desk
from grinos.env import secret
from grinos.ledger import Ledger
from grinos.pipeline import ingest
from grinos.schemas import Actor

API = "https://api.telegram.org"
MAX_LEN = 4000
MAX_DOC_BYTES = 2_000_000   # a text filing larger than this is unbounded LLM spend; send an extract
SYSTEM = Actor(kind="system", name="telegram", version="1")
LABELS = {
    "mid-cycle-analyst": "🧮 Analyst:", "verifier": "🔎 Verifier:", "decision-model": "🧭 Decision:",
    "config-auditor": "📋 Auditor:", "claim-extractor": "📑 Extractor:", "system": "⚙️",
}
SOURCE_KINDS = {"10-K", "10-Q", "transcript", "broker", "call", "news", "other"}

SETUP = """Telegram is not configured. To set it up:
  1. In Telegram, open @BotFather → /newbot → follow the prompts → copy the bot token.
  2. Open @userinfobot → it replies with your numeric user id.
  3. export TELEGRAM_BOT_TOKEN=<token>
     export TELEGRAM_ALLOWED_USER_IDS=<your id>[,<other ids>]
  4. grinos telegram
Only allowlisted users are ever answered; the allowlist is required."""


def split(text: str, n: int = MAX_LEN) -> list[str]:
    """Split on line breaks where possible, hard-cut otherwise."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > n:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:n])
            line = line[n:]
        if len(cur) + len(line) > n:
            out.append(cur)
            cur = ""
        cur += line
    if cur or not out:
        out.append(cur)
    return out


class TelegramAPI:
    def __init__(self, token: str):
        self.token = token

    def _call(self, method: str, params: dict | None = None, timeout: float = 60) -> Any:
        req = urllib.request.Request(f"{API}/bot{self.token}/{method}",
                                     data=json.dumps(params or {}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {data.get('description')}")
        return data["result"]

    def get_me(self) -> dict:
        return self._call("getMe")

    def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params, timeout=timeout + 15)

    def send_message(self, chat_id: int, text: str, reply_to: int | None = None) -> dict:
        params = {"chat_id": chat_id, "text": text}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self._call("sendMessage", params)

    def send_voice(self, chat_id: int, audio: bytes, caption: str = "") -> dict:
        boundary = uuid.uuid4().hex
        fields = {"chat_id": str(chat_id), "caption": caption[:1000]}
        body = b"".join(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
            for k, v in fields.items())
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"voice\"; filename=\"reply.ogg\"\r\n"
                 f"Content-Type: audio/ogg\r\n\r\n").encode() + audio + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(f"{API}/bot{self.token}/sendVoice", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())["result"]

    def get_file(self, file_id: str) -> dict:
        return self._call("getFile", {"file_id": file_id})

    def download(self, file_path: str) -> bytes:
        with urllib.request.urlopen(f"{API}/file/bot{self.token}/{file_path}", timeout=120) as r:
            return r.read()


class Bot:
    def __init__(self, ledger: Ledger, api, desk: Desk, allowed_ids: set[int],
                 bot_username: str = "", bot_id: int | None = None, model: str = "sonnet",
                 background: bool = True):
        self.ledger, self.api, self.desk, self.allowed = ledger, api, desk, allowed_ids
        self.bot_username, self.bot_id, self.model, self.background = bot_username.lower(), bot_id, model, background
        self.offset_path = ledger.root / "telegram_offset"
        self._refused: dict[int | None, float] = {}   # uid → last time a refusal was logged

    # --- offset ---------------------------------------------------------------

    def load_offset(self) -> int | None:
        try:
            return int(self.offset_path.read_text().strip())
        except (OSError, ValueError):
            return None

    def save_offset(self, update_id: int) -> None:
        self.offset_path.write_text(str(update_id + 1))

    # --- dispatch -------------------------------------------------------------

    def handle_update(self, update: dict) -> None:
        msg = update.get("message")
        if not msg:
            return
        user = msg.get("from") or {}
        chat = msg.get("chat") or {}
        uid, chat_id = user.get("id"), chat.get("id")
        if chat_id is None:                 # nothing to answer or attribute
            return
        if uid not in self.allowed:
            # Logged at most once per sender per hour, so spam can't grow the ledger.
            now = time.monotonic()
            if now - self._refused.get(uid, -1e9) > 3600:
                self._refused[uid] = now
                self.ledger.append(f"telegram:{chat_id}", "message_refused", SYSTEM,
                                   {"channel": "telegram", "user_id": uid, "chat_id": chat_id})
            return
        # Stable numeric id first: usernames can be changed or reassigned.
        name = f"tg:{uid}" + (f"({user['username']})" if user.get("username") else "")
        group = chat.get("type") in {"group", "supergroup"}

        if msg.get("document"):
            self._document(msg, name, chat_id)
        elif msg.get("voice") or msg.get("audio"):
            if group and not self._addressed(msg, msg.get("caption", "")):
                return
            self._voice(msg, name, chat_id, group)
        elif "text" in msg:
            text = msg["text"]
            if group and not self._addressed(msg, text):
                return
            if group and self.bot_username:
                text = text.replace(f"@{self.bot_username}", "").replace(f"@{self.bot_username.upper()}", "").strip()
                if text.startswith("/"):  # "/pending@bot" → "/pending"
                    head, _, rest = text.partition(" ")
                    text = (head.split("@")[0] + (" " + rest if rest else "")).strip()
            replies = self.desk.handle(text, user=name, channel="telegram", thread=str(chat_id), conference=group)
            self._send_replies(chat_id, replies, msg.get("message_id"))

    def _addressed(self, msg: dict, text: str) -> bool:
        if text.strip().startswith("/"):
            return True
        if self.bot_username and f"@{self.bot_username}" in text.lower():
            return True
        reply = msg.get("reply_to_message") or {}
        return bool(self.bot_id and (reply.get("from") or {}).get("id") == self.bot_id)

    def _send_replies(self, chat_id: int, replies: list[dict], reply_to: int | None = None) -> None:
        for r in replies:
            label = LABELS.get(r["agent"], f"{r['agent']}:")
            for part in split(f"{label} {r['text']}"):
                self.api.send_message(chat_id, part, reply_to)

    # --- voice ----------------------------------------------------------------

    def _voice(self, msg: dict, name: str, chat_id: int, group: bool) -> None:
        media = msg.get("voice") or msg.get("audio") or {}
        audio = self.api.download(self.api.get_file(media["file_id"])["file_path"])
        blob = self.ledger.put_blob(audio)
        try:
            transcript = voice.stt(audio, "voice.ogg")
        except voice.VoiceUnavailable as exc:
            self.ledger.append(f"telegram:{chat_id}", "speech_failed", SYSTEM,
                               {"channel": "telegram", "audio_blob": blob, "error": str(exc)[:300]},
                               refs=[f"blob:{blob}"])
            self.api.send_message(chat_id, "⚙️ Voice notes need VoiceStudio running on the Mac "
                                           "(http://127.0.0.1:3900). Your recording is saved; text messages work now.")
            return
        self.api.send_message(chat_id, f"🎙️ heard: {transcript}", msg.get("message_id"))
        replies = self.desk.handle(transcript, user=name, channel="telegram", thread=str(chat_id),
                                   audio_blob=blob, conference=group)
        self._send_replies(chat_id, replies)
        voices = voice.voice_map(self.ledger.root)
        for r in replies:
            try:
                speech = voice.tts(r["text"], voices.get(r["agent"], "default"), fmt="opus")
            except voice.VoiceUnavailable:
                self.api.send_message(chat_id, "⚙️ (VoiceStudio unavailable for spoken replies; text above.)")
                return
            sblob = self.ledger.put_blob(speech)
            self.ledger.append(f"telegram:{chat_id}", "speech", SYSTEM,
                               {"channel": "telegram", "reply_hash": r.get("ledger_hash"), "agent": r["agent"],
                                "audio_blob": sblob}, refs=[f"blob:{sblob}"])
            self.api.send_voice(chat_id, speech, LABELS.get(r["agent"], r["agent"]))

    # --- documents ------------------------------------------------------------

    def _document(self, msg: dict, name: str, chat_id: int) -> None:
        parts = (msg.get("caption") or "").split()
        usage = "Send a text filing with caption: /ingest TICKER KIND PUBLISHED [AS_OF]  e.g. /ingest ACME 10-K 2026-02-20"
        if not parts or parts[0].split("@")[0] != "/ingest" or len(parts) < 4:
            self.api.send_message(chat_id, f"⚙️ {usage}")
            return
        ticker, kind, published = parts[1].upper(), parts[2], parts[3]
        try:
            pub = date.fromisoformat(published)
            as_of = date.fromisoformat(parts[4]) if len(parts) > 4 else date.today()
        except ValueError:
            self.api.send_message(chat_id, f"⚙️ Dates must be YYYY-MM-DD. {usage}")
            return
        if kind not in SOURCE_KINDS:
            self.api.send_message(chat_id, f"⚙️ KIND must be one of {', '.join(sorted(SOURCE_KINDS))}.")
            return
        doc = msg["document"]
        if (doc.get("file_size") or 0) > MAX_DOC_BYTES:
            self.api.send_message(chat_id, f"⚙️ That file is over {MAX_DOC_BYTES // 1_000_000} MB. "
                                           "Send a text extract of the relevant sections.")
            return
        data = self.api.download(self.api.get_file(doc["file_id"])["file_path"])
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            self.api.send_message(chat_id, "⚙️ I can only ingest text files (UTF-8: .txt, .md, .htm). "
                                           "Convert PDFs to text first.")
            return
        self.ledger.append(f"telegram:{chat_id}", "command", Actor(kind="human", name=name, version="-"),
                           {"channel": "telegram", "command": "ingest", "arg": " ".join(parts[1:]),
                            "file_name": doc.get("file_name"), "result": "started"})
        meta = {"uri": f"telegram://{chat_id}/{doc.get('file_name', doc['file_id'])}", "kind": kind,
                "company": ticker, "published_at": pub}
        cfg = COMPANIES_DIR / f"{ticker.lower()}.yaml"
        config = yaml.safe_load(cfg.read_text()) if cfg.exists() else None
        self.api.send_message(chat_id, f"⚙️ ingesting {doc.get('file_name', 'file')} for {ticker}… "
                                       "(extract → decide → verify; I'll post the summary)")

        def work():
            try:
                s = ingest(self.ledger, data, meta, None, as_of, config, model=self.model)
                v = [c["verdict"] for c in s["claims"]]
                text = (f"⚙️ {s['run_id']}: {len(v)} claims — verified {v.count('verified')}, "
                        f"rejected {v.count('rejected')}, needs_human {v.count('needs_human')}, "
                        f"discarded {v.count(None)}. /pending to review, /analyse {ticker} next.")
            except Exception as exc:  # report to the user, never kill the bot
                text = f"⚙️ ingest failed: {exc}"
            for part in split(text):
                self.api.send_message(chat_id, part)

        if self.background:
            threading.Thread(target=work, daemon=True).start()
        else:
            work()

    # --- loop -----------------------------------------------------------------

    def poll_forever(self, stop: threading.Event) -> None:
        backoff = 1
        while not stop.is_set():
            try:
                updates = self.api.get_updates(self.load_offset())
                backoff = 1
            except (urllib.error.URLError, TimeoutError, ConnectionError, RuntimeError) as exc:
                print(f"telegram: poll error {exc}; retrying in {backoff}s")
                stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for u in updates:
                try:
                    self.handle_update(u)
                except Exception as exc:  # one bad update must not stall the queue
                    print(f"telegram: update {u.get('update_id')} failed: {exc}")
                self.save_offset(u["update_id"])


def parse_allowed(raw: str | None) -> set[int]:
    return {int(x) for x in (raw or "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}


def run(ledger: Ledger, model: str = "sonnet") -> int:
    token = secret("TELEGRAM_BOT_TOKEN")
    allowed = parse_allowed(os.environ.get("TELEGRAM_ALLOWED_USER_IDS"))
    if not token or not allowed:
        print(SETUP)
        return 2
    api = TelegramAPI(token)
    me = api.get_me()
    bot = Bot(ledger, api, Desk(ledger, model=model), allowed, me.get("username", ""), me.get("id"), model)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    print(f"telegram: @{me.get('username')} serving {len(allowed)} allowlisted user(s); Ctrl-C to stop")
    bot.poll_forever(stop)
    print("telegram: stopped")
    return 0


def pair(env_path: Path, minutes: int = 10, api_factory=None) -> int:
    """
    One-time pairing: print a fresh code, wait for it in a private chat with the bot, and set
    TELEGRAM_ALLOWED_USER_IDS in .env to exactly that sender. The code proves the sender is you.
    """
    import secrets
    token = secret("TELEGRAM_BOT_TOKEN")
    if not token:
        print(SETUP)
        return 2
    api = (api_factory or TelegramAPI)(token)
    code = "grin-" + secrets.token_hex(3)
    me = api.get_me()
    print(f"Send this in a private chat with @{me['username']} within {minutes} minutes:\n\n    {code}\n", flush=True)
    deadline, offset = time.time() + minutes * 60, None
    while time.time() < deadline:
        for u in api.get_updates(offset, timeout=30):
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            sender, chat = m.get("from") or {}, m.get("chat") or {}
            if chat.get("type") == "private" and code in (m.get("text") or ""):
                lines = [ln for ln in env_path.read_text().splitlines()
                         if not ln.startswith("TELEGRAM_ALLOWED_USER_IDS=")] if env_path.exists() else []
                env_path.write_text("\n".join(lines + [f"TELEGRAM_ALLOWED_USER_IDS={sender['id']}"]) + "\n")
                api.get_updates(offset, timeout=0)   # acknowledge, so the bot won't replay these
                api.send_message(chat["id"], "✅ Paired. You're the only allowlisted user for Grin OS. Send /help.")
                print(f"paired: user {sender['id']} (@{sender.get('username')}). Start the bot with: grinos telegram")
                return 0
    print("timed out; run grinos telegram-pair again")
    return 1

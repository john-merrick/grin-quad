"""Telegram bot with a fake API and stubbed agents — no network."""
from datetime import date
from pathlib import Path

import pytest
import yaml

from grinos import analyst, decide, telegram, verify, voice
from grinos.desk import Desk
from grinos.ledger import Ledger
from grinos.pipeline import ingest
from grinos.proposals import analyse
from grinos.telegram import Bot, split

ROOT = Path(__file__).parents[2]
CONFIG = yaml.safe_load((ROOT / "evals/config-auditor/cases/clean_base/acme.yaml").read_text())
SOURCE = b"Gross margin was 33.0% of net sales in fiscal 2025."
META = {"uri": "sec://ACME/10-K", "kind": "10-K", "company": "ACME", "published_at": date(2026, 2, 1)}
CLAIM = {"metric": "gross_margin", "period": "FY2025", "value": 0.33, "unit": "pct",
         "quote": "Gross margin was 33.0% of net sales in fiscal 2025", "statement": "ACME FY2025 GM 33.0%."}
ME, BOT_ID = 42, 999


class FakeAPI:
    def __init__(self, files=None):
        self.sent, self.voices, self.files = [], [], files or {}

    def send_message(self, chat_id, text, reply_to=None):
        self.sent.append((chat_id, text))

    def send_voice(self, chat_id, audio, caption=""):
        self.voices.append((chat_id, audio, caption))

    def get_file(self, file_id):
        return {"file_path": file_id}

    def download(self, file_path):
        return self.files[file_path]


def fake_agents(name, message, schema, model="sonnet", timeout=600, system_prompt=None):
    if name == "desk-router":
        return {"agents": ["mid-cycle-analyst"], "why": "t"}, {}
    if name == "decision-model":
        return {"action": "verify", "nature": "structural", "materiality": "high", "rationale": "s"}, {}
    if name == "verifier":
        return {"verdict": "verified", "reasons": [{"code": "supported", "detail": "s"}]}, {}
    if name == "claim-extractor":
        return {"claims": [CLAIM]}, {}
    return {"reply": f"{name} reply", "cites": []}, {}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(decide, "call_agent", fake_agents)
    monkeypatch.setattr(verify, "call_agent", fake_agents)
    led = Ledger(tmp_path)
    ingest(led, SOURCE, META, [CLAIM], date(2026, 3, 1), CONFIG)
    return led


def _bot(ledger, api=None):
    api = api or FakeAPI()
    return Bot(ledger, api, Desk(ledger, call=fake_agents), {ME}, "grinbot", BOT_ID, background=False), api


def _msg(text=None, uid=ME, chat_type="private", chat_id=ME, **extra):
    m = {"message_id": 1, "from": {"id": uid, "username": "isaac"}, "chat": {"id": chat_id, "type": chat_type}}
    if text is not None:
        m["text"] = text
    return {"update_id": 10, "message": m | extra}


def test_allowlisted_text_gets_labelled_reply(ledger):
    bot, api = _bot(ledger)
    bot.handle_update(_msg("what's ACME's margin?"))
    assert api.sent == [(ME, "🧮 Analyst: mid-cycle-analyst reply")]
    msgs = [e for e in ledger.entries() if e["stage"] == "message"]
    assert msgs[-2]["actor"]["name"] == "tg:42(isaac)" and msgs[-2]["payload"]["thread"] == str(ME)


def test_stranger_is_ignored_and_recorded_without_content(ledger):
    bot, api = _bot(ledger)
    bot.handle_update(_msg("/approve p_x yes", uid=7, chat_id=7))
    assert api.sent == []
    refused = [e for e in ledger.entries() if e["stage"] == "message_refused"]
    assert refused[-1]["payload"] == {"channel": "telegram", "user_id": 7, "chat_id": 7}
    assert not any(e["stage"] in {"message", "command"} for e in ledger.entries())


def test_group_needs_mention_and_runs_as_conference(ledger, monkeypatch):
    bot, api = _bot(ledger)
    bot.handle_update(_msg("chatting among ourselves", chat_type="group", chat_id=-5))
    assert api.sent == []
    seen = {}
    real = bot.desk.handle
    monkeypatch.setattr(bot.desk, "handle", lambda *a, **k: seen.update(k) or real(*a, **k))
    bot.handle_update(_msg("@grinbot what changed?", chat_type="group", chat_id=-5))
    assert seen["conference"] is True and api.sent and api.sent[0][0] == -5
    bot.handle_update(_msg("/pending@grinbot", chat_type="supergroup", chat_id=-5))
    assert "Nothing pending" in api.sent[-1][1] or "•" in api.sent[-1][1]


def test_approve_via_telegram_records_channel(ledger, monkeypatch):
    cid = ledger.verified_claims("ACME")[0]["claim_id"]
    monkeypatch.setattr(analyst, "call_agent", lambda *a, **k: ({"proposals": [
        {"field": "mid_cycle_ebit", "new_value": 146.4, "nature": "structural", "rationale": "r", "cites": [cid]}],
        "no_change": ""}, {}))
    pid = analyse(ledger, "ACME", CONFIG, "acme.yaml")["proposals"][0]["proposal_id"]
    bot, api = _bot(ledger)
    bot.handle_update(_msg(f"/approve {pid} private label is structural"))
    assert f"{pid} approved" in api.sent[-1][1]
    appr = [e for e in ledger.entries() if e["stage"] == "approval"][-1]
    assert appr["payload"]["channel"] == "telegram" and appr["actor"]["name"] == "tg:42(isaac)"


def test_voice_note_transcribed_answered_and_spoken(ledger, monkeypatch):
    monkeypatch.setattr(voice, "stt", lambda audio, name: "what is the margin")
    monkeypatch.setattr(voice, "tts", lambda text, v, fmt="mp3": b"OGG" + text.encode())
    bot, api = _bot(ledger, FakeAPI({"f1": b"voice-bytes"}))
    bot.handle_update(_msg(voice={"file_id": "f1"}))
    assert api.sent[0][1] == "🎙️ heard: what is the margin"
    assert api.sent[1][1].startswith("🧮 Analyst:")
    assert len(api.voices) == 1 and api.voices[0][1].startswith(b"OGG")
    inbound = [e for e in ledger.entries() if e["stage"] == "message" and e["actor"]["kind"] == "human"][-1]
    assert ledger.get_blob(inbound["payload"]["audio_blob"]) == b"voice-bytes"
    speech = [e for e in ledger.entries() if e["stage"] == "speech"][-1]
    assert speech["payload"]["reply_hash"] and ledger.audit() == []


def test_voice_without_voicestudio_falls_back_to_text(ledger, monkeypatch):
    def down(*a, **k):
        raise voice.VoiceUnavailable("no server")
    monkeypatch.setattr(voice, "stt", down)
    bot, api = _bot(ledger, FakeAPI({"f1": b"v"}))
    bot.handle_update(_msg(voice={"file_id": "f1"}))
    assert len(api.sent) == 1 and "VoiceStudio" in api.sent[0][1] and "text" in api.sent[0][1]
    assert any(e["stage"] == "speech_failed" for e in ledger.entries())


def test_document_ingest(ledger, monkeypatch):
    monkeypatch.setattr(telegram, "ingest", lambda *a, **k: ingest(*a, **k))
    import grinos.extract as extract
    monkeypatch.setattr(extract, "call_agent", fake_agents)
    bot, api = _bot(ledger, FakeAPI({"d1": SOURCE, "bin": b"\xff\xfe\x00"}))
    bot.handle_update(_msg(document={"file_id": "d1", "file_name": "acme.txt"}, caption="/ingest ACME 10-K 2026-02-01 2026-03-01"))
    assert "ingesting" in api.sent[0][1] and "verified 1" in api.sent[-1][1]
    bot.handle_update(_msg(document={"file_id": "bin", "file_name": "x.pdf"}, caption="/ingest ACME 10-K 2026-02-01"))
    assert "only ingest text" in api.sent[-1][1]
    bot.handle_update(_msg(document={"file_id": "d1"}, caption="hello"))
    assert "/ingest TICKER" in api.sent[-1][1]


def test_split_long_messages():
    parts = split("a" * 9000)
    assert [len(p) for p in parts] == [4000, 4000, 1000]
    assert split("x\n" * 3) == ["x\nx\nx\n"]


def test_offset_persistence(ledger):
    bot, _ = _bot(ledger)
    assert bot.load_offset() is None
    bot.save_offset(10)
    assert _bot(ledger)[0].load_offset() == 11


def test_run_without_env_prints_setup(ledger, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
    assert telegram.run(ledger) == 2
    assert "@BotFather" in capsys.readouterr().out
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert telegram.run(ledger) == 2   # allowlist is mandatory


def test_pairing_allowlists_only_the_sender_of_the_code(tmp_path, monkeypatch, capsys):
    import re

    from grinos import telegram as tg
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    env = tmp_path / ".env"
    env.write_text("NOTION_TOKEN=op://x/y/z\nTELEGRAM_ALLOWED_USER_IDS=1\n")

    class PairAPI:
        def __init__(self, token):
            self.sent, self.calls = [], 0

        def get_me(self):
            return {"username": "alpha_bot"}

        def get_updates(self, offset, timeout=30):
            self.calls += 1
            if self.calls > 1:
                return []
            code = re.search(r"grin-[0-9a-f]{6}", capsys.readouterr().out).group(0)
            return [
                {"update_id": 1, "message": {"from": {"id": 666}, "chat": {"id": 666, "type": "private"}, "text": "hi"}},
                {"update_id": 2, "message": {"from": {"id": 777}, "chat": {"id": -5, "type": "group"}, "text": code}},
                {"update_id": 3, "message": {"from": {"id": 42, "username": "isaac"},
                                             "chat": {"id": 42, "type": "private"}, "text": code}},
            ]

        def send_message(self, chat_id, text, reply_to=None):
            self.sent.append((chat_id, text))

    assert tg.pair(env, minutes=1, api_factory=PairAPI) == 0
    assert env.read_text() == "NOTION_TOKEN=op://x/y/z\nTELEGRAM_ALLOWED_USER_IDS=42\n"

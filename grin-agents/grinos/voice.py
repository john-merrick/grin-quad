"""
VoiceStudio client (local, http://127.0.0.1:3900 by default; VOICESTUDIO_URL to override).
Uses its OpenAI-compatible routes:

  POST /v1/audio/speech          {"model", "voice", "input", "response_format"}  → audio bytes
  POST /v1/audio/transcriptions  multipart file                                   → {"text"}
  GET  /v1/audio/voices                                                           → voice list
  GET  /.well-known/voicestudio-speech                                            → discovery

Each agent speaks in its own voice. Create designed voices in VoiceStudio named as in
DEFAULT_VOICES (or edit var/voices.json); unknown names fall back to "default".
"""
import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = os.environ.get("VOICESTUDIO_URL", "http://127.0.0.1:3900")
DEFAULT_VOICES = {
    "mid-cycle-analyst": "grin-analyst",
    "verifier": "grin-verifier",
    "decision-model": "grin-decision",
    "config-auditor": "grin-auditor",
    "claim-extractor": "grin-extractor",
    "system": "default",
}


class VoiceUnavailable(RuntimeError):
    pass


def _open(req, timeout):
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise VoiceUnavailable(f"VoiceStudio not reachable at {BASE}: {exc}") from exc


def health(timeout: float = 2.0) -> dict | None:
    try:
        with _open(urllib.request.Request(f"{BASE}/.well-known/voicestudio-speech"), timeout) as r:
            return json.loads(r.read())
    except (VoiceUnavailable, ValueError):
        return None


def voices(timeout: float = 5.0) -> list[dict]:
    with _open(urllib.request.Request(f"{BASE}/v1/audio/voices"), timeout) as r:
        data = json.loads(r.read())
    return data.get("voices", data.get("data", data)) if isinstance(data, dict) else data


def voice_map(root: Path | None = None) -> dict[str, str]:
    path = (root or Path(__file__).parents[1] / "var") / "voices.json"
    try:
        return DEFAULT_VOICES | json.loads(path.read_text())
    except (OSError, ValueError):
        return dict(DEFAULT_VOICES)


def tts(text: str, voice: str = "default", fmt: str = "mp3", model: str = "tts-1", timeout: float = 120) -> bytes:
    """fmt: mp3 (browser), opus (Telegram voice notes), wav."""
    def call(v):
        body = json.dumps({"model": model, "voice": v, "input": text, "response_format": fmt}).encode()
        req = urllib.request.Request(f"{BASE}/v1/audio/speech", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with _open(req, timeout) as r:
            return r.read()
    try:
        return call(voice)
    except urllib.error.HTTPError as exc:
        if voice != "default" and exc.code in (400, 404, 409):
            return call("default")   # agent voice not created yet
        raise VoiceUnavailable(f"TTS failed: {exc.code} {exc.read()[:200]!r}") from exc


def stt(audio: bytes, filename: str = "audio.webm", model: str = "whisper-1", timeout: float = 300) -> str:
    boundary = uuid.uuid4().hex
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson\r\n".encode(),
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
         f"Content-Type: application/octet-stream\r\n\r\n").encode() + audio + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(f"{BASE}/v1/audio/transcriptions", data=b"".join(parts), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with _open(req, timeout) as r:
            return json.loads(r.read())["text"].strip()
    except urllib.error.HTTPError as exc:
        raise VoiceUnavailable(f"STT failed: {exc.code} {exc.read()[:200]!r}") from exc

"""Dashboard + voice room server: token guard, host guard, actions in the ledger, room turn, blob allowlist."""
import json
import threading
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest
import yaml

from grinos import analyst, decide, verify, voice
from grinos.ledger import Ledger
from grinos.pipeline import ingest
from grinos.proposals import analyse
from grinos.web.server import make_server

ROOT = Path(__file__).parents[2]
CONFIG = yaml.safe_load((ROOT / "evals/config-auditor/cases/clean_base/acme.yaml").read_text())
SOURCE = b"Gross margin was 33.0% of net sales in fiscal 2025, up from 32.6% in fiscal 2024."
META = {"uri": "sec://ACME/10-K/FY2025", "kind": "10-K", "company": "ACME", "published_at": date(2026, 2, 1)}
CLAIM = {"metric": "gross_margin", "period": "FY2025", "value": 0.33, "unit": "pct",
         "quote": "Gross margin was 33.0% of net sales in fiscal 2025", "statement": "ACME FY2025 GM 33.0%."}


def fake_desk_call(name, message, schema, model="sonnet", timeout=600, system_prompt=None):
    if name == "desk-router":
        return {"agents": ["mid-cycle-analyst", "verifier"], "why": "t"}, {}
    return {"reply": f"{name}: gross margin was thirty-three percent.", "cites": []}, {"cost_usd": 0}


@pytest.fixture
def app(tmp_path, monkeypatch):
    def fake(name, message, schema, model="sonnet", timeout=600, system_prompt=None):
        if name == "decision-model":
            return {"action": "verify", "nature": "structural", "materiality": "high", "rationale": "s"}, {}
        return {"verdict": "verified", "reasons": [{"code": "supported", "detail": "s"}]}, {}
    monkeypatch.setattr(decide, "call_agent", fake)
    monkeypatch.setattr(verify, "call_agent", fake)
    ledger = Ledger(tmp_path)
    ingest(ledger, SOURCE, META, [CLAIM], date(2026, 3, 1), CONFIG)
    cid = ledger.verified_claims("ACME")[0]["claim_id"]
    monkeypatch.setattr(analyst, "call_agent", lambda *a, **k: (
        {"proposals": [{"field": "mid_cycle_ebit", "new_value": 146.4, "nature": "structural",
                        "rationale": "private label is structural", "cites": [cid]}], "no_change": ""}, {}))
    pid = analyse(ledger, "ACME", CONFIG, "acme.yaml")["proposals"][0]["proposal_id"]

    httpd, state = make_server(ledger, 0, desk_call=fake_desk_call, user="tester")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield {"base": base, "token": state.token, "ledger": ledger, "pid": pid, "cid": cid, "httpd": httpd}
    httpd.shutdown()
    httpd.server_close()


def req(app, path, body=None, token=True, raw=None, ctype="application/json", host=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": ctype} if data is not None else {}
    if token and data is not None:
        headers["X-Grin-Token"] = app["token"]
    if host:
        headers["Host"] = host
    r = urllib.request.Request(app["base"] + path, data=data, headers=headers,
                               method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def j(resp):
    return json.loads(resp[1])


def test_binds_loopback_only(app):
    assert app["httpd"].server_address[0] == "127.0.0.1"


def test_pages_render_with_token(app):
    for path in ("/", "/room"):
        status, body, ctype = req(app, path)
        assert status == 200 and "text/html" in ctype
        assert app["token"].encode() in body and b"__GRIN_TOKEN__" not in body


def test_foreign_host_is_refused(app):
    assert req(app, "/api/audit", host="evil.example.com")[0] == 403


def test_post_without_token_is_refused(app):
    status, body, _ = req(app, "/api/approve", {"proposal_id": app["pid"], "note": "x"}, token=False)
    assert status == 403
    assert not any(e["stage"] == "approval" for e in app["ledger"].entries())


def test_read_apis(app):
    runs = j(req(app, "/api/runs"))
    run = next(r for r in runs if r["source"])
    assert run["claims"] == 1 and run["verified"] == 1
    steps = j(req(app, f"/api/run/{run['run_id']}"))
    assert [s["stage"] for s in steps][:2] == ["source", "claim"]
    trail = j(req(app, f"/api/trail/{app['cid']}"))
    assert {s["stage"] for s in trail} >= {"source", "claim", "decision", "verdict"}
    pend = j(req(app, "/api/pending"))
    assert [p["proposal_id"] for p in pend["proposals"]] == [app["pid"]]
    props = j(req(app, "/api/proposals"))
    assert props[0]["status"] == "pending" and props[0]["ev_change_pct"] > 0
    assert j(req(app, "/api/audit"))["ok"] is True
    evals = j(req(app, "/api/evals"))
    assert any(e["agent"] == "verifier" for e in evals)


def test_approve_flow_records_dashboard_channel(app):
    assert req(app, "/api/approve", {"proposal_id": app["pid"], "note": ""})[0] == 400   # note required
    status, body, _ = req(app, "/api/approve", {"proposal_id": app["pid"], "note": "agree"})
    assert status == 200
    appr = [e for e in app["ledger"].entries() if e["stage"] == "approval"]
    assert len(appr) == 1 and appr[0]["payload"]["channel"] == "dashboard"
    assert appr[0]["actor"] == {"kind": "human", "name": "tester", "version": "-", "model": None}
    assert req(app, "/api/approve", {"proposal_id": app["pid"], "note": "again"})[0] == 400  # already decided
    pub = j(req(app, "/api/publish", {"proposal_id": app["pid"]}))          # LongGamma model sync: prepare only,
    assert pub["pushed"] is False and "NOTION_TOKEN" in pub["message"]       # which reads Notion, so needs the token
    assert not any(e["stage"] in ("lg_pushed", "published") for e in app["ledger"].entries())


def test_resolve_validates(app):
    assert req(app, "/api/resolve", {"claim_id": app["cid"], "verdict": "maybe", "note": "x"})[0] == 400
    assert req(app, "/api/resolve", {"claim_id": "c_000000000000", "verdict": "verified", "note": "x"})[0] == 400
    assert req(app, "/api/resolve", {"claim_id": app["cid"], "verdict": "rejected", "note": "x"})[0] == 200


def test_desk_endpoint(app):
    out = j(req(app, "/api/desk", {"text": "how is ACME gross margin?", "conference": True}))
    assert [r["agent"] for r in out["replies"]] == ["mid-cycle-analyst", "verifier"]
    msgs = [e for e in app["ledger"].entries() if e["stage"] == "message"]
    assert msgs[0]["payload"]["channel"] == "room" and msgs[0]["actor"]["name"] == "tester"


def test_room_turn_falls_back_when_voicestudio_down(app, monkeypatch):
    def down(*a, **k):
        raise voice.VoiceUnavailable("not reachable")
    monkeypatch.setattr(voice, "stt", down)
    status, body, _ = req(app, "/api/room/turn", raw=b"\x1aE\xdf\xa3webm", ctype="audio/webm")
    assert status == 503 and json.loads(body)["fallback"] == "text"


def test_room_turn_with_voice_and_blob_allowlist(app, monkeypatch):
    monkeypatch.setattr(voice, "stt", lambda audio, filename="x", **k: "everyone, how is ACME?")
    monkeypatch.setattr(voice, "tts", lambda text, v="default", fmt="mp3", **k: b"ID3fake-mp3-" + text.encode()[:20])
    out = j(req(app, "/api/room/turn", raw=b"\x1aE\xdf\xa3webm", ctype="audio/webm"))
    assert out["transcript"] == "everyone, how is ACME?"
    assert len(out["replies"]) == 2 and all(r["audio_url"] for r in out["replies"])
    status, body, ctype = req(app, out["replies"][0]["audio_url"])
    assert status == 200 and body.startswith(b"ID3fake") and ctype == "audio/mpeg"

    stages = [e["stage"] for e in app["ledger"].entries()]
    assert stages.count("speech") == 2
    inbound = next(e for e in app["ledger"].entries() if e["stage"] == "message" and e["actor"]["kind"] == "human")
    assert inbound["payload"]["audio_blob"] and req(app, f"/api/blob/{inbound['payload']['audio_blob']}")[0] == 200

    # The source document blob exists in the store but is not voice audio: never served.
    src = next(e for e in app["ledger"].entries() if e["stage"] == "source")["payload"]["source"]["blob"]
    assert req(app, f"/api/blob/{src}")[0] == 404
    assert req(app, "/api/blob/../../etc")[0] == 404
    assert app["ledger"].audit() == []


def test_room_turn_without_tts_returns_text(app, monkeypatch):
    monkeypatch.setattr(voice, "stt", lambda audio, filename="x", **k: "hello")
    def down(*a, **k):
        raise voice.VoiceUnavailable("tts down")
    monkeypatch.setattr(voice, "tts", down)
    out = j(req(app, "/api/room/turn", raw=b"webm", ctype="audio/webm"))
    assert out["tts"] is False and all(r["audio_url"] is None for r in out["replies"])


def test_valuation_endpoints_read_without_advancing_and_decide_with_the_token(app):
    from grinos import sec
    from grinos import valuation as V
    from grinos.tests import sec_fixture as F
    raw = yaml.safe_load((ROOT / "examples/valuation/acme.yaml").read_text())
    top = {k: raw.pop(k) for k in ("ticker", "valuation_date", "price", "currency")}
    r = V.start(app["ledger"], V.Request(**top, assumptions=raw), sec.fixture_getter(F.responses()))
    n = sum(1 for _ in app["ledger"].entries())
    rows = j(req(app, "/api/valuations"))
    assert rows[0]["status"] == "blocked"
    view = j(req(app, f"/api/valuation/{r['run_id']}"))
    assert view["status"]["step"] == "adjustments" and sum(1 for _ in app["ledger"].entries()) == n   # GET is read-only
    adj = view["steps"]["adjustments"]["blocking"][0]["id"]
    body = {"run_id": r["run_id"], "target": adj, "decision": "approve", "note": "one-off"}
    assert req(app, "/api/valuation/decide", body, token=False)[0] == 403
    out = j(req(app, "/api/valuation/decide", body))
    assert out["status"] == "complete"
    d = next(e for e in app["ledger"].entries() if e["stage"] == "valuation_decision")
    assert d["actor"]["name"] == "tester" and d["payload"]["channel"] == "dashboard"
    assert "Analysis only" in j(req(app, f"/api/valuation/{r['run_id']}"))["memo"]

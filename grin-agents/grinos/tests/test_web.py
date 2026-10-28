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
    if name == "orchestrator":
        return {"reply": "Asking the verifier.", "actions": [],
                "consults": [{"agent": "verifier", "question": "is the margin in the source?"}]}, {"cost_usd": 0}
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
    assert status == 400 and b"not confirmed in review" in body                          # cites unreviewed
    assert req(app, "/api/review", {"target_id": app["cid"], "context_id": app["pid"], "decision": "confirmed",
                                    "note": ""})[0] == 200
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
    assert [r["agent"] for r in out["replies"]] == ["orchestrator"]
    assert "verifier reports: verifier: gross margin" in out["replies"][0]["text"]
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
    assert len(out["replies"]) == 1 and all(r["audio_url"] for r in out["replies"])
    status, body, ctype = req(app, out["replies"][0]["audio_url"])
    assert status == 200 and body.startswith(b"ID3fake") and ctype == "audio/mpeg"

    stages = [e["stage"] for e in app["ledger"].entries()]
    assert stages.count("speech") == 1
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


def test_review_popup_data_highlights_the_exact_source_span(app):
    d = j(req(app, f"/api/proposal/{app['pid']}"))
    (c,) = d["cites"]
    assert c["claim_id"] == app["cid"] and not d["all_confirmed"] and d["review_state"] == {app["cid"]: None}
    ex = c["excerpt"]
    assert ex["found"] and ex["match"] == "Gross margin was 33.0% of net sales in fiscal 2025"
    assert SOURCE.decode()[ex["raw_start"]:ex["raw_end"]] == ex["match"] and ex["before"] == ""
    assert c["source"]["edgar_url"] is None                     # only sec.gov URIs become links
    status, body, ctype = req(app, f"/api/source/{c['source']['blob']}?start=0&end=12")
    assert status == 200 and body == b"Gross margin" and ctype.startswith("text/plain")


def test_source_route_serves_only_recorded_sources(app):
    blob = app["ledger"].put_blob(b"private audio or anything else")
    assert req(app, f"/api/source/{blob}")[0] == 404
    assert req(app, "/api/claim/c_000000000000")[0] == 404 and req(app, "/api/proposal/p_0000000000")[0] == 404


def test_disputing_a_cite_can_reject_the_claim_and_feedback_is_recorded(app):
    assert req(app, "/api/review", {"target_id": app["cid"], "context_id": app["pid"], "decision": "disputed",
                                    "note": ""})[0] == 400                                   # a dispute needs a note
    assert req(app, "/api/review", {"target_id": app["cid"], "context_id": app["pid"], "decision": "disputed",
                                    "note": "that is FY2024, not FY2025", "reject_claim": True})[0] == 200
    assert app["ledger"].verified_claims("ACME") == []                                       # out of the evidence
    assert req(app, "/api/review", {"target_id": app["cid"], "context_id": app["pid"], "decision": "confirmed",
                                    "note": ""})[0] == 400                                   # first review is final
    assert req(app, "/api/feedback", {"target_kind": "proposal", "target_id": app["pid"], "kind": "perspective",
                                      "text": "pricing power is overstated", "company": "ACME"})[0] == 200
    assert j(req(app, f"/api/proposal/{app['pid']}"))["feedback"][0]["text"] == "pricing power is overstated"
    assert req(app, "/api/review", {"target_id": app["cid"], "context_id": app["pid"], "decision": "confirmed"},
               token=False)[0] == 403


def test_ops_view_reports_traced_agent_calls(app, monkeypatch, tmp_path):
    from grinos import trace
    monkeypatch.setenv("GRINOS_HOME", str(tmp_path))
    with trace.span("verifier", kind="generation", model="sonnet", prompt_version="v1") as g:
        g.end(cost_usd=0.01, usage={"input_tokens": 10, "output_tokens": 1})
    o = j(req(app, "/api/ops"))
    assert o["last_7_days"][0]["agent"] == "verifier" and o["last_7_days"][0]["calls"] == 1
    assert o["trace_url"] is None


def test_promotions_are_shown_with_their_diff_and_decided_with_a_note(app, monkeypatch, tmp_path):
    from grinos import improve
    from grinos.schemas import Actor
    agents = tmp_path / "agents"
    agents.mkdir()
    body = "You verify.\n- Never guess."
    (agents / "verifier.md").write_text("---\nname: verifier\n---\n\n" + body + "\n")
    monkeypatch.setattr(improve, "AGENTS_DIR", agents)
    L = app["ledger"]
    new = body + "\n- Check signs."
    L.append("improve:verifier:x", "prompt_selected", Actor(kind="system", name="t", version="1"),
             {"promotion_id": "pr_1", "agent": "verifier", "parent_version": improve.prompt_version(body),
              "cand_version": improve.prompt_version(new), "prompt_blob": L.put_blob(new.encode()), "rationale": "signs",
              "baseline": {"per_case": {"s": 0.0}}, "candidate": {"per_case": {"s": 1.0}},
              "failing_cases_before": 1, "failing_cases_after": 0})
    (v,) = j(req(app, "/api/promotions"))
    assert "+- Check signs." in v["diff"] and v["cases"] == [{"id": "s", "before": 0.0, "after": 1.0}] and not v["stale"]
    assert req(app, "/api/promote", {"promotion_id": "pr_1", "decision": "approve", "note": ""})[0] == 400
    assert req(app, "/api/promote", {"promotion_id": "pr_1", "decision": "approve", "note": "yes"})[0] == 200
    assert "- Check signs." in (agents / "verifier.md").read_text() and j(req(app, "/api/promotions")) == []


def test_agent_pages_drill_down_from_agent_to_run_to_case_to_trace(app, monkeypatch, tmp_path):
    from grinos import trace
    from grinos.web import agents_view
    monkeypatch.setattr(agents_view, "RESULTS", tmp_path)
    monkeypatch.setenv("GRINOS_HOME", str(tmp_path))
    run = tmp_path / "verifier" / "results" / "20261029-090000_agent_sonnet"
    run.mkdir(parents=True)
    (run / "report.json").write_text(json.dumps({"summary": {"passed": True, "false_accept_rate": 0.0, "cost_usd": 0.3,
                                                             "gates": {"false_accept_rate": True}},
                                                 "cases": [{"id": "c1", "repeat": 0, "case_pass": True, "verdict": "verified"},
                                                           {"id": "c2", "repeat": 0, "case_pass": False, "verdict": "verified"}]}))
    (run / "c2.r0.json").write_text(json.dumps({"case": {"id": "c2"}, "result": {"verdict": "verified"}, "grade": {"ok": False}}))
    with trace.span("verifier", kind="generation", model="sonnet", prompt_version="v1", input="claim?") as g:
        g.end(output={"verdict": "verified"}, cost_usd=0.01)

    agents = j(req(app, "/api/agents"))
    assert {a["agent"] for a in agents} >= {"verifier", "orchestrator", "prompt-optimiser"}
    d = j(req(app, "/api/agent/verifier"))
    assert d["model"] and d["prompt"] and d["runs"][0]["failed"] == 1 and d["calls"][0]["trace_id"] == g.trace_id
    assert d["optimizable"]["tools"] is True and "financial-statements" in [x["name"] for x in d["library"]]
    rd = j(req(app, f"/api/agent/verifier/run/{d['runs'][0]['id']}"))
    assert [(r["id"], r["case_pass"], r["file"]) for r in rd["rows"]] == [("c1", True, None), ("c2", False, "c2.r0.json")]
    case = j(req(app, f"/api/agent/verifier/run/{d['runs'][0]['id']}/case/c2.r0.json"))
    assert case["grade"] == {"ok": False}
    (rec,) = j(req(app, f"/api/trace/{g.trace_id}"))
    assert rec["input"] == "claim?"
    assert req(app, "/api/agent/verifier/run/eval:..%2F..%2Fetc")[0] == 404
    assert req(app, "/api/agent/nobody")[0] == 404
    assert req(app, "/api/agent/verifier/optimize/run", {"plan_id": "op_0000000000", "variant_ids": ["v_1"]})[0] == 400


def test_cases_over_time_and_adding_a_case_from_a_claim(app, monkeypatch, tmp_path):
    import sys
    from pathlib import Path

    from grinos.web import agents_view
    sys.path.insert(0, str(Path(__file__).parents[2] / "evals"))
    import added
    data = tmp_path / "added"
    for d in set(added.EVAL_DIRS.values()):
        (data / d).mkdir(parents=True)
    monkeypatch.setattr(added, "HERE", data)
    monkeypatch.setattr(agents_view, "RESULTS", tmp_path)
    for name, passes in (("20261020-090000_agent_sonnet", {"c1": True, "c2": True}),
                         ("20261027-090000_agent_sonnet", {"c1": True, "c2": False})):
        run = tmp_path / "verifier" / "results" / name
        run.mkdir(parents=True)
        (run / "report.json").write_text(json.dumps({"summary": {"passed": True}, "cases": [
            {"id": k, "repeat": 0, "case_pass": v} for k, v in passes.items()]}))
        (run / "c2.r0.json").write_text(json.dumps({"case": {"id": "c2"}, "result": {"verdict": "verified" if passes["c2"] else "rejected"}}))
    m = j(req(app, "/api/agent/verifier/cases"))
    status = {c["id"]: (c["status"], c["results"]) for c in m["cases"]}
    assert status["c2"] == ("new failure", [1.0, 0.0]) and status["c1"][0] == "passing"
    h = j(req(app, "/api/agent/verifier/case/c2"))
    assert [r["passed"] for r in h["history"]] == [[False], [True]] and h["history"][0]["files"] == ["c2.r0.json"]

    t = j(req(app, f"/api/agent/verifier/case_template?from=claim:{app['cid']}"))
    assert t["case"]["claim"]["metric"] == "gross_margin" and "Gross margin was 33.0%" in t["case"]["source_text"]
    case = t["case"] | {"id": "margin_ok", "kind": "valid"}
    status_code, body, _ = req(app, "/api/agent/verifier/cases", {"case": case, "source": f"claim:{app['cid']}"})
    assert status_code == 200 and j((status_code, body))["id"] == "added_margin_ok"
    assert added.cases("verifier")[0]["source_text"].startswith("Gross margin")
    assert any(e["stage"] == "eval_case_added" for e in app["ledger"].entries())
    assert req(app, "/api/agent/verifier/cases", {"case": {"id": "bad", "kind": "judgment"}})[0] == 400
    assert req(app, "/api/agent/memo-writer/case_template")[0] == 404

"""The orchestrator's hard barriers, and one full turn through the desk with stub agents."""
from datetime import date
from pathlib import Path

import pytest

from grinos import analyst, decide, verify
from grinos import desk as desk_mod
from grinos import orchestrator as O
from grinos.desk import WORD_JOINER, Desk
from grinos.ledger import Ledger
from grinos.pipeline import ingest
from grinos.tests.test_publish import CLAIM, CONFIG, META, SOURCE

ROOT = Path(__file__).parents[2]


def test_check_plan_keeps_allowed_work_and_drops_the_rest():
    plan = {"actions": [{"action": "start_analysis", "ticker": "acme", "why": ""},
                        {"action": "start_coverage", "ticker": "TSLA", "why": ""},
                        {"action": "start_coverage", "ticker": "ACME", "as_of": "next week", "why": ""},
                        {"action": "start_eval", "agent": "verifier", "why": ""},
                        {"action": "start_eval", "agent": "intern", "why": ""},
                        {"action": "suggest_command", "command": "/approve p_1 ok", "why": ""},
                        {"action": "suggest_command", "command": "/rm -rf", "why": ""},
                        {"action": "suggest_command", "command": "approve it", "why": ""},
                        {"action": "status", "run_id": "cov_x", "why": ""}],
            "consults": [{"agent": "verifier", "question": "q"}, {"agent": "intern", "question": "q"},
                         {"agent": "decision-model", "question": " "}]}
    ok, consults, problems = O.check_plan(plan, ["ACME"], {"cov_x"})
    assert [(a["action"], a.get("ticker") or a.get("agent") or a.get("command") or a.get("run_id")) for a in ok] == [
        ("start_analysis", "ACME"), ("start_eval", "verifier"), ("suggest_command", "/approve p_1 ok"),
        ("status", "cov_x")]
    assert [c["agent"] for c in consults] == ["verifier"] and len(problems) == 7


def test_unsourced_numbers_are_removed_from_the_reply():
    reply, bad = O.strip_invented("Gross margin was 44.8%. The target is $312. Let me check.", "gm 44.8% in Q3")
    assert reply == "Gross margin was 44.8%. Let me check." and bad == {"312"}
    assert O.invented_numbers("I'll start 2 runs", "") == set()            # single digits are fine


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(decide, "call_agent", lambda *a, **k: (
        {"action": "verify", "nature": "structural", "materiality": "high", "rationale": "s"}, {}))
    monkeypatch.setattr(verify, "call_agent", lambda *a, **k: (
        {"verdict": "verified", "reasons": [{"code": "supported", "detail": "s"}]}, {}))
    monkeypatch.setattr(analyst, "call_agent", lambda *a, **k: ({"proposals": [], "no_change": "nothing new"}, {}))
    monkeypatch.setattr(desk_mod, "COMPANIES_DIR", ROOT / "evals/config-auditor/cases/clean_base")
    L = Ledger(tmp_path)
    ingest(L, SOURCE, META, [CLAIM], date(2026, 3, 1), CONFIG)
    return L


def _desk(ledger, plan):
    calls = []

    def fake(name, message, schema, model="sonnet", timeout=600, system_prompt=None):
        calls.append((name, message))
        if name == "orchestrator":
            return plan, {"cost_usd": 0}
        return {"reply": f"{name}: the 33.0% is in the 10-K.", "cites": []}, {"cost_usd": 0}
    return Desk(ledger, call=fake), calls


def test_a_turn_starts_work_consults_and_answers_as_one_voice(ledger):
    d, calls = _desk(ledger, {"reply": "Starting the analyst on ACME and asking the verifier.", "actions": [
        {"action": "start_analysis", "ticker": "ACME", "why": "asked"},
        {"action": "start_coverage", "ticker": "TSLA", "why": "asked"}],
        "consults": [{"agent": "verifier", "question": "Is the 33.0% gross margin in the 10-K?"}]})
    (r,) = d.handle("analyse ACME and check the 33.0% margin", "isaac", "telegram", thread="42")
    assert r["agent"] == "orchestrator" and "analyst started on ACME" in r["text"]
    assert "verifier reports: verifier: the 33.0% is in the 10-K." in r["text"]
    assert "TSLA" not in r["text"]                                               # dropped: no config
    plan = [e for e in ledger.entries() if e["stage"] == "orchestrator_plan"][-1]["payload"]
    assert any("TSLA" in p for p in plan["problems"]) and plan["consulted"] == ["verifier"]
    assert any(e["stage"] == "analysis_done" for e in ledger.entries()) and ledger.audit() == []
    assert "<known_tickers>\nACME" in calls[0][1]


def test_isaacs_decisions_are_only_ever_suggested(ledger):
    d, _ = _desk(ledger, {"reply": "That's your call.", "actions": [
        {"action": "suggest_command", "command": "/approve p_0123456789 the SG&A evidence is solid", "why": "his"}],
        "consults": []})
    (r,) = d.handle("approve p_0123456789, the SG&A evidence is solid", "isaac", "telegram")
    assert f"/{WORD_JOINER}approve p_0123456789" in r["text"]                    # can't be tapped as a command
    assert not any(e["stage"] == "approval" for e in ledger.entries())


def test_a_failed_plan_call_still_answers(ledger):
    d, _ = _desk(ledger, None)
    (r,) = d.handle("hello", "isaac", "dashboard")
    assert "couldn't plan" in r["text"]

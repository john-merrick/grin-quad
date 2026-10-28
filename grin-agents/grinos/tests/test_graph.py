"""The research graph is a deterministic view of the ledger: rebuildable, gate-respecting, traceable."""
import sqlite3
from datetime import date
from pathlib import Path

import pytest
import yaml

from grinos import analyst, decide, graph, verify
from grinos.desk import Desk
from grinos.graph import Graph, agree, normalise_period, why
from grinos.ledger import Ledger
from grinos.pipeline import human_verdict, ingest
from grinos.proposals import analyse, decide_proposal
from grinos.tests.review_helpers import confirm_all

ROOT = Path(__file__).parents[2]
CONFIG = yaml.safe_load((ROOT / "evals/config-auditor/cases/clean_base/acme.yaml").read_text())
K10 = (b"Net sales for fiscal 2025 were $1,126.0 million. Gross margin was 33.0% of net sales in fiscal 2025."
       b" CEO transition costs were $8.3 million in fiscal 2025.")
Q1 = (b"Fiscal 2025 net sales were $1.1 billion. Fiscal 2025 net sales were restated to $1,090.0 million."
      b" Bonus accrual was $2.0 million in fiscal 2025.")


def _claim(metric, value, unit, quote, period="FY2025"):
    return {"metric": metric, "period": period, "value": value, "unit": unit, "quote": quote, "statement": quote}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    def fake(name, message, schema, model="sonnet", timeout=600):
        if name == "decision-model":
            return {"action": "verify", "nature": "structural", "materiality": "high", "rationale": "s"}, {}
        # the verifier rejects anything mentioning "restated", so it must never confirm or conflict
        claim = message.split("<claim>")[1].split("</claim>")[0]
        verdict = "rejected" if "restated" in claim else "verified"
        return {"verdict": verdict, "reasons": [{"code": "supported", "detail": "s"}]}, {}
    monkeypatch.setattr(decide, "call_agent", fake)
    monkeypatch.setattr(verify, "call_agent", fake)
    led = Ledger(tmp_path)
    ingest(led, K10, {"uri": "sec://ACME/10-K", "kind": "10-K", "company": "ACME", "published_at": date(2026, 2, 1)},
           [_claim("revenue", 1126.0, "usd_m", "Net sales for fiscal 2025 were $1,126.0 million"),
            _claim("gross_margin", 0.33, "pct", "Gross margin was 33.0% of net sales in fiscal 2025"),
            _claim("other", 8.3, "usd_m", "CEO transition costs were $8.3 million in fiscal 2025")],
           date(2026, 6, 1), CONFIG)
    ingest(led, Q1, {"uri": "sec://ACME/8-K", "kind": "other", "company": "ACME", "published_at": date(2026, 5, 1)},
           [_claim("revenue", 1100.0, "usd_m", "Fiscal 2025 net sales were $1.1 billion"),
            _claim("revenue", 1090.0, "usd_m", "Fiscal 2025 net sales were restated to $1,090.0 million"),
            _claim("other", 2.0, "usd_m", "Bonus accrual was $2.0 million in fiscal 2025")],
           date(2026, 6, 1), CONFIG)
    return led


def _id(ledger, quote_start):
    return next(e["payload"]["claim_id"] for e in ledger.entries()
                if e["stage"] == "claim" and e["payload"]["claim"]["quote"].startswith(quote_start))


def test_rebuild_is_deterministic(ledger, tmp_path):
    a, b = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
    graph.write(graph.project(ledger), a)
    graph.write(graph.project(ledger), b)
    assert list(sqlite3.connect(a).iterdump()) == list(sqlite3.connect(b).iterdump())


def test_same_fact_confirms_with_rounding_and_rejected_claims_never_count(ledger):
    g = Graph.open(ledger)
    first, rounded = _id(ledger, "Net sales for fiscal"), _id(ledger, "Fiscal 2025 net sales were $1.1")
    restated = _id(ledger, "Fiscal 2025 net sales were restated")
    assert [n["id"] for n in g.out(f"claim:{rounded}", "CONFIRMS")] == [f"claim:{first}"]   # $1.1bn ≈ 1,126.0
    assert g.node(f"claim:{restated}")["status"] == "rejected"
    assert g.conflicts("ACME") == []                          # a rejected claim is not evidence


def test_verified_disagreement_is_a_conflict(ledger):
    restated = _id(ledger, "Fiscal 2025 net sales were restated")
    human_verdict(ledger, restated, "verified", "isaac", "checked the restatement note")
    [(later, earlier)] = Graph.open(ledger).conflicts("ACME")
    assert (later["id"], earlier["id"]) == (f"claim:{restated}", f"claim:{_id(ledger, 'Net sales for fiscal')}")
    assert "contradicted by" in why(Graph.open(ledger), _id(ledger, "Net sales for fiscal"))


def test_other_metric_asserts_no_fact(ledger):
    g = Graph.open(ledger)
    assert g.out(f"claim:{_id(ledger, 'CEO transition costs')}", "ASSERTS") == []
    assert g.conflicts() == []                                # $8.3M and $2.0M are not "the same fact"


def test_graph_rebuilds_when_the_ledger_moves_on(ledger, monkeypatch):
    before = Graph.open(ledger).stats()["edges"].get("CITES", 0)
    monkeypatch.setattr(analyst, "call_agent", lambda *a, **k: ({"proposals": [
        {"field": "gross_margin", "new_value": [0.33] * 5, "nature": "structural", "rationale": "mix shift",
         "cites": [_id(ledger, "Gross margin was 33.")]}], "no_change": ""}, {}))
    pid = analyse(ledger, "ACME", CONFIG, "acme.yaml")["proposals"][0]["proposal_id"]
    confirm_all(ledger, pid)
    decide_proposal(ledger, pid, "approve", "isaac", "private label is structural")
    g = Graph.open(ledger)
    assert g.stats()["edges"]["CITES"] == before + 1
    out = why(g, "ACME gross_margin")
    assert f"{pid} approved by isaac" in out and "Gross margin was 33.0%" in out and "sec://ACME/10-K" in out


def test_why_parses_claims_facts_and_usage(ledger):
    g = Graph.open(ledger)
    cid = _id(ledger, "Net sales for fiscal")
    assert "confirmed by" in why(g, cid) and "sec://ACME/10-K" in why(g, cid)
    assert "Net sales for fiscal 2025" in why(g, "acme revenue fy2025")
    assert why(g, "").startswith("Usage") and "No conflicting" in why(g, "ACME conflicts")
    assert "nothing in the ledger" in why(g, "ACME stability_score")


def test_why_is_a_logged_desk_command(ledger):
    out = Desk(ledger, call=lambda *a, **k: ({}, {})).handle("/why ACME conflicts", "isaac", "telegram")
    assert "No conflicting" in out[0]["text"]
    assert [e["stage"] for e in ledger.entries()][-1] == "command" and ledger.audit() == []


@pytest.mark.parametrize("raw,want", [("Q2 FY2026", "Q2 FY2026"), ("q2 2026", "Q2 FY2026"),
                                      ("FY2025", "FY FY2025"), ("6M FY2026", "H1 FY2026"), ("TTM", "TTM")])
def test_normalise_period(raw, want):
    assert normalise_period(raw) == want


@pytest.mark.parametrize("a,b,same", [(1800.0, 1822.938, True), (1822.938, 1784.53, False),
                                      (0.297, 0.297, True), (0.297, 0.30, True), (0.294, 0.297, False)])
def test_agree_allows_rounding_only(a, b, same):
    assert agree(a, b) is same


def test_valuations_trace_value_per_share_to_accessions(tmp_path):
    from grinos import sec
    from grinos import valuation as V
    from grinos.tests import sec_fixture as F
    ledger = Ledger(tmp_path)
    raw = yaml.safe_load((ROOT / "examples/valuation/acme.yaml").read_text())
    top = {k: raw.pop(k) for k in ("ticker", "valuation_date", "price", "currency")}
    r = V.start(ledger, V.Request(**top, assumptions=raw), sec.fixture_getter(F.responses()))
    V.decide(ledger, r["run_id"], r["blocking"][0]["id"], "approve", "isaac", "one-off")
    V.resume(ledger, r["run_id"])
    g = Graph.open(ledger)
    out = why(g, r["run_id"])
    assert "complete" in out and "base $31.02" in out and "decision adj_" in out
    assert "debt: us-gaap:LongTermDebtNoncurrent at 2026-03-31" in out and F.Q1[0] in out
    assert all(f["filed"] <= "2026-06-30" for f in g.out(f"valuation:{r['run_id']}", "USES"))
    assert r["run_id"] in why(g, "ACME valuations")

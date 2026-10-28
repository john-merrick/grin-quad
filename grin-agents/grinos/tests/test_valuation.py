"""The valuation workflow, offline: routing, review gates, versions, lookahead, memo."""
import copy
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from grinos import decide as decide_mod
from grinos import sec, verify
from grinos import valuation as V
from grinos.ledger import Ledger
from grinos.pipeline import ingest
from grinos.tests import sec_fixture as F

ROOT = Path(__file__).parents[2]
ASSUMPTIONS = ROOT / "examples/valuation/acme.yaml"


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path)


def request(**over) -> V.Request:
    raw = yaml.safe_load(ASSUMPTIONS.read_text()) | over
    top = {k: raw.pop(k) for k in ("ticker", "valuation_date", "price", "currency")}
    return V.Request(**top, assumptions=raw)


def getter(**kw):
    return sec.fixture_getter(F.responses(**kw))


def run_to_end(ledger, req, get=None, who="isaac"):
    r = V.start(ledger, req, get or getter())
    for _ in range(5):
        if r["status"] != "blocked":
            return r
        for b in r["blocking"]:
            V.decide(ledger, r["run_id"], b["id"], "approve" if b["kind"] == "adjustment" else "accept", who, "ok")
        r = V.resume(ledger, r["run_id"])
    return r


def test_full_offline_run_completes_with_three_scenarios_and_audits(ledger):
    r = run_to_end(ledger, request())
    assert r["status"] == "complete"
    v = {k: s["value_per_share"] for k, s in r["out"]["valuation"]["scenarios"].items()}
    assert v["bear"] < v["base"] < v["bull"]
    assert r["out"]["valuation"]["scenarios"]["base"]["value_per_share"] == pytest.approx(Decimal("31.02"), abs=Decimal("0.01"))
    assert ledger.audit() == []
    memo = Path(r["out"]["memo"]["path"]).read_text()
    assert V.__doc__ and "Analysis only" in memo and "0001234567-26-000010" in memo
    for fid in set(__import__("re").findall(r"\[(x_[0-9a-f]{12})\]", memo)):
        assert f"| {fid} |" in memo                            # every cited fact is in the appendix


def test_rerun_resumes_the_same_run_without_new_entries(ledger):
    r = run_to_end(ledger, request())
    n = sum(1 for _ in ledger.entries())
    again = V.start(ledger, request(), getter())
    assert again["run_id"] == r["run_id"] and again["status"] == "complete"
    assert sum(1 for _ in ledger.entries()) == n               # nothing changed, nothing appended


def test_facts_filed_after_the_valuation_date_are_excluded(ledger):
    r = run_to_end(ledger, request())
    bal = r["out"]["reconcile"]["reported"]["balance"]
    assert bal["date"] == "2026-03-31" and bal["cash"]["value"] == Decimal(55)      # not the Q2 70
    assert all(f["filed"] <= "2026-06-30" for f in r["out"]["reconcile"]["reported"]["facts"].values())
    later = run_to_end(ledger, request(valuation_date="2026-08-15"))
    assert later["out"]["reconcile"]["reported"]["balance"]["cash"]["value"] == Decimal(70)


def test_restated_figures_use_the_latest_filing_and_keep_the_original(ledger):
    rec = run_to_end(ledger, request())["out"]["reconcile"]["reported"]
    fy24 = rec["annual"][-2]["revenue"]
    fact = rec["facts"][fy24["fact_ids"][0]]
    assert fy24["value"] == Decimal(1060) and fact["superseded"][0]["value"] == str(1058 * F.M)
    assert fact["accn"] == F.TEN_K[2025][0] and fact["url"].endswith("-index.html")


def test_banks_route_to_unsupported_sector(ledger):
    r = V.start(ledger, request(ticker="BANK"), getter())
    assert r["status"] == "unsupported" and "unsupported sector" in r["out"]["resolve"]["reason"]
    assert "valuation" not in r["out"]


def test_terminal_growth_at_or_above_wacc_is_invalid(ledger):
    scen = copy.deepcopy(yaml.safe_load(ASSUMPTIONS.read_text())["scenarios"])
    scen["base"] |= {"terminal_growth": 0.20}
    r = run_to_end(ledger, request(scenarios=scen))
    assert r["status"] == "invalid" and r["step"] == "forecast"
    assert any("must be below WACC" in e for e in r["out"]["forecast"]["errors"])


def test_missing_material_data_needs_a_sourced_value_never_an_accept(ledger):
    facts = F.acme_companyfacts()
    for c in ("LongTermDebtNoncurrent", "LongTermDebtCurrent"):
        del facts["facts"]["us-gaap"][c]
    r = V.start(ledger, request(), getter(companyfacts=facts))
    assert r["status"] == "blocked" and r["step"] == "reconcile"
    [item] = [b for b in r["blocking"] if b["item"] == "debt"]
    with pytest.raises(ValueError, match="never accept a gap"):
        V.decide(ledger, r["run_id"], item["id"], "accept", "isaac", "fine")
    V.decide(ledger, r["run_id"], item["id"], "value", "isaac", "10-Q note 7: term loan 250", value=Decimal(250))
    r = V.resume(ledger, r["run_id"])
    assert r["out"]["reconcile"]["analyst_inputs"]["debt"]["value"] == Decimal(250)
    for b in r["blocking"]:
        V.decide(ledger, r["run_id"], b["id"], "approve", "isaac", "ok")
    r = V.resume(ledger, r["run_id"])
    assert r["status"] == "complete"
    assert not next(c for c in r["out"]["validate"]["checks"] if c["id"] == "analyst_inputs")["ok"]


def test_newest_filing_missing_from_xbrl_is_routed_to_review(ledger):
    sub = F.acme_submissions(extra=[("10-Q", "0001234567-26-000027", "2026-06-01", "2026-04-30")])
    r = V.start(ledger, request(), getter(submissions=sub))
    assert r["status"] == "blocked" and r["step"] == "retrieve" and "not yet in SEC" in r["blocking"][0]["detail"]


def test_adjustments_need_approval_and_never_change_reported_figures(ledger):
    req = request(adjustments=[{"item": "operating_income", "amount": 2.0, "rationale": "legal settlement",
                                "source": "https://example.com/10-K#note12"}])
    r = V.start(ledger, req, getter())
    assert r["step"] == "adjustments" and len(r["blocking"]) == 2
    [rule] = [b for b in r["blocking"] if "restructuring" in b["detail"]]
    [analyst] = [b for b in r["blocking"] if "legal settlement" in b["detail"]]
    V.decide(ledger, r["run_id"], rule["id"], "approve", "isaac", "restructuring is one-off")
    V.decide(ledger, r["run_id"], analyst["id"], "reject", "isaac", "settlements recur here")
    r = V.resume(ledger, r["run_id"])
    adj = r["out"]["adjustments"]["normalised"][-1]
    assert adj["reported_operating_income"] == Decimal("112.6") and adj["approved_adjustments"] == Decimal(6)
    assert adj["normalised_operating_income"] == Decimal("118.6")
    assert r["out"]["reconcile"]["reported"]["annual"][-1]["operating_income"]["value"] == Decimal("112.6")


def test_decisions_are_validated_and_first_is_final(ledger):
    r = V.start(ledger, request(), getter())
    adj = r["blocking"][0]["id"]
    with pytest.raises(ValueError, match="takes"):
        V.decide(ledger, r["run_id"], adj, "accept", "isaac", "x")
    with pytest.raises(ValueError, match="note"):
        V.decide(ledger, r["run_id"], adj, "approve", "isaac", " ")
    V.decide(ledger, r["run_id"], adj, "approve", "isaac", "ok")
    with pytest.raises(ValueError, match="already decided"):
        V.decide(ledger, r["run_id"], adj, "reject", "mallory", "no")


def test_changed_inputs_or_new_filings_make_a_new_version(ledger):
    first = run_to_end(ledger, request())["run_id"]
    assert run_to_end(ledger, request(price=65))["run_id"] != first
    facts = F.acme_companyfacts()                         # a late 10-K/A filed before the valuation date
    facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
        F._row(1127.0, "0001234567-26-000040", "2026-06-15", "10-K/A", "2025-12-31", "2025-01-01", fy=2025))
    newer = V.start(ledger, request(), getter(companyfacts=facts))      # cached snapshot: same version
    assert newer["run_id"] == first
    snap = V.snapshot(ledger, "ACME", date(2026, 6, 30), getter(companyfacts=facts), refresh=True)
    rid = V.run_id_for(request(), snap)
    r = V.start(ledger, request(), getter(companyfacts=facts))
    assert r["run_id"] == rid != first
    req_entry = next(e for e in ledger.entries() if e["run_id"] == rid and e["stage"] == "valuation_request")
    assert req_entry["payload"]["supersedes"] == first


def test_evidence_sources_are_checked_against_the_gate_and_the_date(ledger, monkeypatch):
    monkeypatch.setattr(decide_mod, "call_agent", lambda *a, **k: (
        {"action": "verify", "nature": "structural", "materiality": "high", "rationale": "s"}, {}))
    monkeypatch.setattr(verify, "call_agent", lambda *a, **k: (
        {"verdict": "verified", "reasons": [{"code": "supported", "detail": "s"}]}, {}))
    claim = {"metric": "customer_retention", "period": "FY2025", "value": 0.94, "unit": "pct",
             "quote": "retention was 94%", "statement": "ACME retention 94%."}
    for published in (date(2026, 3, 1), date(2026, 9, 1)):
        ingest(ledger, f"In fiscal 2025 retention was 94%. ({published})".encode(),
               {"uri": f"x://{published}", "kind": "other", "company": "ACME", "published_at": published},
               [claim], date(2026, 10, 1))
    early, late = [c["claim_id"] for c in ledger.verified_claims("ACME")]
    narrative = {"drivers": [{"text": "sticky", "source": early}, {"text": "later", "source": late},
                             {"text": "made up", "source": "c_000000000000"}, {"text": "bare"}],
                 "risks": [], "disconfirming": []}
    r = V.start(ledger, request(narrative=narrative), getter())
    for b in r["blocking"]:
        V.decide(ledger, r["run_id"], b["id"], "approve", "isaac", "ok")
    r = V.resume(ledger, r["run_id"])
    assert r["step"] == "evidence" and r["blocking"][0]["id"] == "r_disconfirm"
    items = r["out"]["evidence"]["sections"]["drivers"]
    assert [i["verified"] for i in items] == [True, False, False, False]
    assert "after the valuation date" in items[1]["problem"] and items[3]["problem"] == "no source"


def test_unsupported_ticker_is_reported(ledger):
    r = V.start(ledger, request(ticker="NOPE"), getter())
    assert r["status"] == "unsupported" and "not in the SEC ticker list" in r["out"]["resolve"]["reason"]


def test_runs_lists_status_and_values(ledger):
    r = V.start(ledger, request(), getter())
    [row] = V.runs(ledger)
    assert (row["run_id"], row["status"], row["step"], row["blocking"]) == (r["run_id"], "blocked", "adjustments", [r["blocking"][0]["id"]])
    run_to_end(ledger, request())
    [row] = V.runs(ledger)
    assert row["status"] == "complete" and set(row["value_per_share"]) == {"bear", "base", "bull"}


def test_valuation_from_the_desk_is_logged_and_needs_a_human_decision(ledger):
    from grinos.desk import Desk
    (ledger.root / "assumptions").mkdir()
    (ledger.root / "assumptions" / "acme.yaml").write_text(ASSUMPTIONS.read_text())
    desk = Desk(ledger, call=lambda *a, **k: ({}, {}), sec_get=getter())
    out = desk.handle("/value ACME", "tg:42(isaac)", "telegram")[0]["text"]
    run_id, adj = out.split()[0], next(w for w in out.split() if w.startswith("adj_"))
    assert "blocked at adjustments" in out
    done = desk.handle(f"/vdecide {run_id} {adj} approve restructuring is one-off", "tg:42(isaac)", "telegram")[0]["text"]
    assert "complete" in done and "base $31.02" in done
    d = next(e for e in ledger.entries() if e["stage"] == "valuation_decision")
    assert (d["actor"]["kind"], d["actor"]["name"]) == ("human", "tg:42(isaac)")
    assert sum(e["stage"] == "command" for e in ledger.entries()) == 2 and ledger.audit() == []
    assert "/⁠value" in __import__("grinos.desk", fromlist=["neutralise"]).neutralise("try /value ACME")


def test_narrative_is_validated_traced_and_never_redrafted_on_resume(ledger):
    from grinos import narrative as N
    calls = []

    def fake(name, message, schema, model="sonnet"):
        calls.append(name)
        if len(calls) == 1:
            return {"paragraphs": [{"text": "Buy: worth $99.", "cites": ["scenario:base"]}]}, {"cost_usd": 0.01}
        return {"paragraphs": [
            {"text": "The base case is below the market price.", "cites": ["scenario:base", "reverse_dcf"]},
            {"text": "A further restatement would weaken the base year.", "cites": ["disconfirming:1"]}]}, {}
    def narrate(ctx):
        return N.narrate(ctx, call=fake)
    r = V.start(ledger, request(), getter(), narrate=narrate)
    V.decide(ledger, r["run_id"], r["blocking"][0]["id"], "approve", "isaac", "one-off")
    r = V.resume(ledger, r["run_id"], narrate=narrate)
    assert r["status"] == "complete" and calls == ["memo-writer", "memo-writer"]
    trace = next(e for e in ledger.entries() if e["stage"] == "valuation_narrative")
    assert trace["actor"]["name"] == "memo-writer" and "recommendation language" in " ".join(trace["payload"]["attempts"][0]["problems"])
    assert "[disconfirming:1]" in Path(r["out"]["memo"]["path"]).read_text()
    V.resume(ledger, r["run_id"], narrate=narrate)
    assert len(calls) == 2                                    # checkpointed: no third call

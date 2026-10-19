"""Proposal → approval → Notion publication, with stubbed agents and a fake Notion API."""
from datetime import date
from pathlib import Path

import pytest
import yaml

from grinos import analyst, decide, notion, verify
from grinos.ledger import Ledger
from grinos.notion import NotPublishable
from grinos.pipeline import ingest
from grinos.proposals import analyse, decide_proposal, pending

ROOT = Path(__file__).parents[2]
CONFIG = yaml.safe_load((ROOT / "evals/config-auditor/cases/clean_base/acme.yaml").read_text())
SOURCE = b"Gross margin was 33.0% of net sales in fiscal 2025, up from 32.6% in fiscal 2024."
META = {"uri": "sec://ACME/10-K/FY2025", "kind": "10-K", "company": "ACME", "published_at": date(2026, 2, 1)}
CLAIM = {"metric": "gross_margin", "period": "FY2025", "value": 0.33, "unit": "pct",
         "quote": "Gross margin was 33.0% of net sales in fiscal 2025", "statement": "ACME FY2025 GM 33.0%."}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    def fake(name, message, schema, model="sonnet", timeout=600):
        if name == "decision-model":
            return {"action": "verify", "nature": "structural", "materiality": "high", "rationale": "s"}, {}
        return {"verdict": "verified", "reasons": [{"code": "supported", "detail": "s"}]}, {}
    monkeypatch.setattr(decide, "call_agent", fake)
    monkeypatch.setattr(verify, "call_agent", fake)
    led = Ledger(tmp_path)
    ingest(led, SOURCE, META, [CLAIM], date(2026, 3, 1), CONFIG)
    return led


def _stub_analyst(monkeypatch, proposals):
    monkeypatch.setattr(analyst, "call_agent",
                        lambda *a, **k: ({"proposals": proposals, "no_change": ""}, {"cost_usd": 0}))


def _cid(ledger):
    return ledger.verified_claims("ACME")[0]["claim_id"]


def test_valid_proposal_flows_to_publication(ledger, monkeypatch):
    _stub_analyst(monkeypatch, [{"field": "mid_cycle_ebit", "new_value": 146.4, "nature": "structural",
                                 "rationale": "r", "cites": [_cid(ledger)]}])
    res = analyse(ledger, "ACME", CONFIG, "acme.yaml")
    pid = res["proposals"][0]["proposal_id"]
    assert res["proposals"][0]["ok"] and res["proposals"][0]["impact"]["ev_change_pct"] > 0
    assert [p["proposal"]["payload"]["proposal_id"] for p in pending(ledger)] == [pid]

    with pytest.raises(NotPublishable, match="not been approved"):
        notion.build_publication(ledger, pid)
    decide_proposal(ledger, pid, "approve", "isaac", "agree: private label is structural")
    pub = notion.build_publication(ledger, pid)
    assert [r["Field"] for r in pub["valuation_log"]] == ["Sustainable EBIT"]
    assert pub["valuation_log"][0]["Old Value"] == 112.6 and pub["valuation_log"][0]["New Value"] == 146.4
    assert pub["research_note"]["Valuation Impact"] == "Up"
    body = " ".join(t for _, t in pub["research_note"]["body"])
    assert "Approved by isaac" in body and _cid(ledger) in body

    calls = []

    def fake_api(method, path, body, token):
        calls.append((method, path, body))
        if path.endswith("/query"):
            return {"results": [{"id": "company-page"}]}
        return {"id": f"page-{len(calls)}", "url": f"https://notion.so/{len(calls)}"}

    notion.prepare(ledger, pid)
    out = notion.push(ledger, pid, token="t", request=fake_api)
    assert out["company_page"] == "company-page" and len(out["valuation_log"]) == 1
    log_body = calls[-1][2]
    assert log_body["properties"]["Rationale Note"]["relation"] == [{"id": out["research_note"]["id"]}]
    assert log_body["parent"]["data_source_id"] == notion.DS["valuation_log"]
    with pytest.raises(NotPublishable, match="already published"):
        notion.push(ledger, pid, token="t", request=fake_api)
    assert ledger.audit() == []


def test_unverified_citation_fails_check_and_cannot_be_approved(ledger, monkeypatch):
    _stub_analyst(monkeypatch, [{"field": "gross_margin", "new_value": 0.33, "nature": "structural",
                                 "rationale": "r", "cites": ["c_made_up"]}])
    pid = analyse(ledger, "ACME", CONFIG, "acme.yaml")["proposals"][0]["proposal_id"]
    assert pending(ledger) == []
    with pytest.raises(ValueError, match="failed its checks"):
        decide_proposal(ledger, pid, "approve", "isaac", "x")


@pytest.mark.parametrize("field,value,why", [
    ("discount_rate", 0.07, "not an analyst-editable driver"),
    ("gross_margin", [0.33, 0.33], "needs 5 values"),
    ("gross_margin", 33.0, "introduces config check"),
    ("duration_score", 11, "invalid"),
])
def test_bad_proposals_are_caught(ledger, monkeypatch, field, value, why):
    _stub_analyst(monkeypatch, [{"field": field, "new_value": value, "nature": "structural",
                                 "rationale": "r", "cites": [_cid(ledger)]}])
    r = analyse(ledger, "ACME", CONFIG, "acme.yaml")["proposals"][0]
    assert not r["ok"] and any(why in p for p in r["problems"]), r["problems"]


def test_rejected_proposal_never_publishes(ledger, monkeypatch):
    _stub_analyst(monkeypatch, [{"field": "tax_rate", "new_value": 0.21, "nature": "structural",
                                 "rationale": "r", "cites": [_cid(ledger)]}])
    pid = analyse(ledger, "ACME", CONFIG, "acme.yaml")["proposals"][0]["proposal_id"]
    decide_proposal(ledger, pid, "reject", "isaac", "one year is not enough")
    with pytest.raises(NotPublishable):
        notion.build_publication(ledger, pid)


def test_tax_change_maps_to_model_rebuild(ledger, monkeypatch):
    _stub_analyst(monkeypatch, [{"field": "tax_rate", "new_value": 0.21, "nature": "structural",
                                 "rationale": "r", "cites": [_cid(ledger)]}])
    pid = analyse(ledger, "ACME", CONFIG, "acme.yaml")["proposals"][0]["proposal_id"]
    decide_proposal(ledger, pid, "approve", "isaac", "ok")
    assert [r["Field"] for r in notion.build_publication(ledger, pid)["valuation_log"]] == ["Model Rebuild"]


def test_tampered_outbox_refuses_push(ledger, monkeypatch):
    _stub_analyst(monkeypatch, [{"field": "mid_cycle_ebit", "new_value": 146.4, "nature": "structural",
                                 "rationale": "r", "cites": [_cid(ledger)]}])
    pid = analyse(ledger, "ACME", CONFIG, "acme.yaml")["proposals"][0]["proposal_id"]
    decide_proposal(ledger, pid, "approve", "isaac", "ok")
    path = notion.prepare(ledger, pid)
    path.write_text(path.read_text().replace("146.4", "999.9"))
    with pytest.raises(NotPublishable, match="differs"):
        notion.push(ledger, pid, token="t", request=lambda *a: {"results": []})


def test_registry_rows_mark_live_only_when_gated(tmp_path):
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: does a\n---\nbody")
    (tmp_path / "b.md").write_text("---\nname: b\ndescription: does b\n---\nbody")
    rows = notion.registry_rows(tmp_path, {"a": "Live"})
    assert [(r["Agent"], r["Status"]) for r in rows] == [("a", "Live"), ("b", "Planned")]


def test_push_refuses_company_missing_from_notion(ledger, monkeypatch):
    _stub_analyst(monkeypatch, [{"field": "mid_cycle_ebit", "new_value": 146.4, "nature": "structural",
                                 "rationale": "r", "cites": [_cid(ledger)]}])
    pid = analyse(ledger, "ACME", CONFIG, "acme.yaml")["proposals"][0]["proposal_id"]
    decide_proposal(ledger, pid, "approve", "isaac", "ok")
    notion.prepare(ledger, pid)
    calls = []

    def fake_api(method, path, body, token):
        calls.append(path)
        return {"results": []}
    with pytest.raises(NotPublishable, match="no row with Ticker"):
        notion.push(ledger, pid, token="t", request=fake_api)
    assert calls == [f"/data_sources/{notion.DS['companies']}/query"]   # nothing was created


def test_check_access_flags_missing_properties():
    def fake_api(method, path, body, token):
        if path == "/users/me":
            return {"name": "bot"}
        if path.endswith("/query"):
            return {"results": [{"properties": {"Ticker": {"rich_text": [{"plain_text": "ACME"}]}}}]}
        ds = next(k for k, v in notion.DS.items() if v in path)
        props = {p: {} for p in notion.WRITES[ds]}
        if ds == "valuation_log":
            props.pop("Scenario")
        return {"title": [{"plain_text": ds}], "properties": props}
    out = notion.check_access(token="t", request=fake_api)
    assert not out["data_sources"]["valuation_log"]["ok"] and "Scenario" in out["data_sources"]["valuation_log"]["error"]
    assert out["data_sources"]["research_notes"]["ok"] and out["company_tickers"] == ["ACME"]


def test_rerun_supersedes_older_proposals(ledger, monkeypatch):
    prop = {"field": "mid_cycle_ebit", "new_value": 146.4, "nature": "structural", "rationale": "r",
            "cites": [_cid(ledger)]}
    _stub_analyst(monkeypatch, [prop])
    old = analyse(ledger, "ACME", CONFIG, "acme.yaml")["proposals"][0]["proposal_id"]
    _stub_analyst(monkeypatch, [prop | {"new_value": 150.0}])
    new = analyse(ledger, "ACME", CONFIG, "acme.yaml")["proposals"][0]["proposal_id"]
    assert [p["proposal"]["payload"]["proposal_id"] for p in pending(ledger)] == [new]
    with pytest.raises(ValueError, match="superseded"):
        decide_proposal(ledger, old, "approve", "isaac", "n")
    decide_proposal(ledger, old, "reject", "isaac", "n")   # rejecting an old one is still allowed

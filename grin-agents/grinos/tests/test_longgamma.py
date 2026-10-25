"""Completed research → LongGamma, against a fake Notion: the skill contracts, enforced in code."""
from datetime import date
from pathlib import Path

import pytest
import yaml

from grinos import analyst, decide, sec, verify
from grinos import longgamma as LG
from grinos import valuation as V
from grinos.ledger import Ledger
from grinos.notion import NotPublishable
from grinos.pipeline import ingest
from grinos.proposals import analyse, decide_proposal
from grinos.tests import sec_fixture as F

ROOT = Path(__file__).parents[2]
CONFIG = yaml.safe_load((ROOT / "evals/config-auditor/cases/clean_base/acme.yaml").read_text())
SOURCE = b"Operating income was $30.0 million in the second quarter of fiscal 2026. Gross margin was 33.0% in Q2 2026."
CLAIMS = [{"metric": "ebit", "period": "Q2 FY2026", "value": 30.0, "unit": "usd_m",
           "quote": "Operating income was $30.0 million in the second quarter of fiscal 2026", "statement": "ACME Q2 EBIT $30M."},
          {"metric": "gross_margin", "period": "Q2 FY2026", "value": 0.33, "unit": "pct",
           "quote": "Gross margin was 33.0% in Q2 2026", "statement": "ACME Q2 GM 33.0%."}]
# The Schema Contract's model-sync write list for Companies, plus the two bookkeeping fields.
MODEL_SYNC_WRITES = set(LG.DRIVER_FIELDS) | {"ROIC", "FCF Yield", "Revision Momentum 3m", "Model Synced", "Updated By"}
FORMULAS = {"PT Bear", "PT Base", "PT Bull", "PW Target", "Upside %", "Exp Return %", "EV m", "Size Band", "Urgent"}


class FakeNotion:
    def __init__(self, companies=None):
        self.companies = companies or {}               # ticker → {"id", "props": {name: number}}
        self.pages, self.calls, self.thesis = {}, [], {"ACME": "thesis-1"}

    def __call__(self, method, path, body, token):
        self.calls.append((method, path, body))
        if path.endswith("/query") and DS_of(path) == "companies":
            t = body["filter"]["rich_text"]["equals"]
            return {"results": [{"id": self.companies[t]["id"]}] if t in self.companies else []}
        if path.endswith("/query") and DS_of(path) == "research_notes":
            if "and" in body["filter"]:                # latest Investment Thesis for a company
                cid = body["filter"]["and"][0]["relation"]["contains"]
                hit = [t for k, t in self.thesis.items() if self.companies.get(k, {}).get("id") == cid]
                return {"results": [{"id": hit[0]}] if hit else []}
            title = body["filter"]["title"]["equals"]
            return {"results": [{"id": i} for i, p in self.pages.items()
                                if p["properties"].get("Title", {}).get("title", [{}])[0].get("text", {}).get("content") == title]}
        if path.endswith("/query") and DS_of(path) == "tasks":
            title = body["filter"]["title"]["equals"]
            return {"results": [{"id": i} for i, p in self.pages.items()
                                if p["properties"].get("Task", {}).get("title", [{}])[0].get("text", {}).get("content") == title]}
        if method == "GET" and path == "/users/me":
            return {"id": "bot"}
        if method == "GET" and path.startswith("/pages/") and path.split("/")[-1] in self.pages:
            return {"last_edited_by": {"id": self.pages[path.split("/")[-1]].get("_edited_by", "bot")}}
        if method == "GET" and path.startswith("/blocks/"):
            pid = path.split("/")[2]
            return {"results": [{"id": f"{pid}-b{i}"} for i in range(len(self.pages[pid].get("children", [])))],
                    "has_more": False}
        if method == "DELETE" and path.startswith("/blocks/"):
            pid, i = path.split("/")[-1].rsplit("-b", 1)
            self.pages[pid]["children"][int(i)] = None
            return {}
        if method == "PATCH" and path.startswith("/blocks/") and path.endswith("/children"):
            page = self.pages[path.split("/")[2]]
            page["children"] = [b for b in page.get("children", []) if b is not None] + body["children"]
            return {}
        if method == "GET" and path.startswith("/pages/"):
            cid = path.split("/")[-1]
            c = next(c for c in self.companies.values() if c["id"] == cid)
            return {"properties": {k: {"number": v} for k, v in c["props"].items()}}
        if method == "POST" and path == "/pages":
            pid = f"page-{len(self.pages) + 1}"
            self.pages[pid] = body
            return {"id": pid, "url": f"https://notion.so/{pid}"}
        if method == "PATCH":
            return {"id": path.split("/")[-1]}
        raise AssertionError(f"unexpected {method} {path}")

    def created(self, ds):
        return [p for p in self.pages.values() if p["parent"]["data_source_id"] == LG.DS[ds]]

    def patches(self, page_id):
        return [b for m, p, b in self.calls if m == "PATCH" and p == f"/pages/{page_id}"]


def DS_of(path):
    return next(k for k, v in LG.DS.items() if v in path)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(decide, "call_agent", lambda *a, **k: (
        {"action": "verify", "nature": "structural", "materiality": "high", "rationale": "s"}, {}))
    monkeypatch.setattr(verify, "call_agent", lambda *a, **k: (
        {"verdict": "verified", "reasons": [{"code": "supported", "detail": "s"}]}, {}))
    return Ledger(tmp_path)


def acme(**props):
    return {"ACME": {"id": "co-acme", "props": {"EBIT FY1": 100.0, "Net Debt m": 195.0, "Shares m": 39.8} | props}}


def ingested(ledger):
    return ingest(ledger, SOURCE, {"uri": "https://sec.gov/acme-q2", "kind": "other", "company": "ACME",
                                   "published_at": date(2026, 7, 30)}, CLAIMS, date(2026, 8, 1), CONFIG)["run_id"]


# --- results-transcript --------------------------------------------------------------------------

def test_results_note_follows_the_contract_and_never_touches_drivers(ledger, monkeypatch):
    run = ingested(ledger)
    cid = ledger.verified_claims("ACME")[0]["claim_id"]
    monkeypatch.setattr(analyst, "call_agent", lambda *a, **k: ({"proposals": [
        {"field": "mid_cycle_ebit", "new_value": 146.4, "nature": "structural", "rationale": "mix", "cites": [cid]}],
        "no_change": ""}, {}))
    analyse(ledger, "ACME", CONFIG, "acme.yaml")
    fake = FakeNotion(acme())
    p = LG.prepare(ledger, "results", run, token="t", request=fake, today="2026-08-01")
    n = p["note"]
    assert n["Title"] == "ACME · Results · Q2 FY2026" and n["Type"] == "Results" and n["Author"] == "Agent"
    assert n["Valuation Impact"] == "Up" and 0 < n["Val Impact %"] < 1           # a decimal, not ×100
    assert "Thesis Impact" not in n                                            # Isaac's call: left unset
    assert any("PROPOSAL" in t for _, t in n["body"]) and p["feeds_into"] == "thesis-1"
    assert any("run-rate 120.0 (quarter × 4" in t and "+20.0%" in t for _, t in n["body"])   # vs EBIT FY1 100
    assert p["task"]["Task"] == "Update model: ACME after Q2 FY2026" and p["task"]["Due"] == "2026-08-03"
    out = LG.push(ledger, f"lg_results_{run}", token="t", request=fake)
    assert len(fake.created("research_notes")) == 1 and len(fake.created("tasks")) == 1
    assert not fake.created("valuation_log") and not fake.patches("co-acme")   # never Companies / Valuation Log
    note = fake.created("research_notes")[0]
    assert note["properties"]["Feeds Into"] == {"relation": [{"id": "thesis-1"}]}
    assert any(b["type"] == "to_do" for b in note["children"])
    assert out["research_note"]["id"] and ledger.audit() == []
    with pytest.raises(NotPublishable, match="already in Notion"):
        LG.push(ledger, f"lg_results_{run}", token="t", request=fake)


def test_results_without_analysis_proposes_nothing_and_creates_no_task(ledger):
    run = ingested(ledger)
    p = LG.prepare(ledger, "results", run, token="t", request=FakeNotion(acme()))
    assert p["note"]["Valuation Impact"] == "None" and p["task"] is None
    assert any("has not run on this evidence" in t for _, t in p["note"]["body"])


def test_search_before_create_updates_an_existing_note(ledger):
    run = ingested(ledger)
    fake = FakeNotion(acme())
    fake.pages["old"] = {"parent": {"data_source_id": LG.DS["research_notes"]},
                         "properties": {"Title": {"title": [{"text": {"content": "ACME · Results · Q2 FY2026"}}]}}}
    LG.prepare(ledger, "results", run, token="t", request=fake)
    out = LG.push(ledger, f"lg_results_{run}", token="t", request=fake)
    assert out["research_note"] == {"id": "old", "url": None, "updated": True, "body": "kept"} and fake.patches("old")


def test_a_company_missing_from_notion_stops_with_initiate_coverage(ledger):
    run = ingested(ledger)
    with pytest.raises(NotPublishable, match="longgamma-initiate-coverage"):
        LG.prepare(ledger, "results", run, token="t", request=FakeNotion({}))


# --- model-sync ----------------------------------------------------------------------------------

def complete_valuation(ledger, **assumptions):
    raw = yaml.safe_load((ROOT / "examples/valuation/acme.yaml").read_text()) | assumptions
    top = {k: raw.pop(k) for k in ("ticker", "valuation_date", "price", "currency")}
    r = V.start(ledger, V.Request(**top, assumptions=raw), sec.fixture_getter(F.responses()))
    V.decide(ledger, r["run_id"], r["blocking"][0]["id"], "approve", "isaac", "one-off")
    return V.resume(ledger, r["run_id"])


def test_valuation_drivers_reproduce_the_dcf_through_notions_formula(ledger):
    r = complete_valuation(ledger)
    d = LG.drivers_from_valuation(V.view(ledger, r["run_id"]))
    bridge = r["out"]["valuation"]["bridge"]
    net_debt = float(bridge["debt"] - bridge["cash"] + bridge["preferred"] + bridge["minority_interest"])
    for s in LG.SCENARIOS:
        notion_pt = (d[f"EBIT FY2 {s}"] * d[f"Target Multiple {s}"] - net_debt) / float(bridge["diluted_shares"])
        assert notion_pt == pytest.approx(d["_dcf_value_per_share"][s], abs=0.01)
    assert d["Sustainable EBIT"] == pytest.approx(118.6) and LG.validate(d) == []


def test_sync_needs_your_go_with_a_rationale_then_writes_only_contract_fields(ledger):
    r = complete_valuation(ledger)
    fake = FakeNotion(acme(**{"EBIT FY1": 120.0, "Net Debt m": 195.0}))
    p = LG.prepare(ledger, "sync", r["run_id"], token="t", request=fake, today="2026-07-01")
    lg = f"lg_sync_{r['run_id']}"
    assert "EBIT FY2 Base" in p["plan"]["changed"] and "Model Rebuild" in [x["Field"] for x in p["plan"]["valuation_log"]]
    with pytest.raises(NotPublishable, match="needs your go"):
        LG.push(ledger, lg, token="t", request=fake)
    with pytest.raises(NotPublishable, match="rationale is required"):
        LG.go(ledger, lg, "isaac", "  ")
    LG.go(ledger, lg, "isaac", "Normalised margin holds; restructuring was a one-off.")
    out = LG.push(ledger, lg, token="t", request=fake)
    [patch] = fake.patches("co-acme")
    assert set(patch["properties"]) <= MODEL_SYNC_WRITES and not set(patch["properties"]) & FORMULAS
    assert patch["properties"]["Updated By"] == {"select": {"name": "Agent"}}
    note = fake.created("research_notes")[0]
    assert note["properties"]["Type"] == {"select": {"name": "Thesis Update"}}
    assert note["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == \
        "Normalised margin holds; restructuring was a one-off."                    # his words, verbatim
    logs = fake.created("valuation_log")
    assert logs and all(x["properties"]["Rationale Note"] == {"relation": [{"id": out["rationale_note"]["id"]}]} for x in logs)
    assert all(x["properties"]["Author"] == {"select": {"name": "Agent"}} for x in logs)
    titles = {x["properties"]["Change"]["title"][0]["text"]["content"] for x in logs}
    assert "ACME · EBIT FY2 Base" in titles and "ACME · EBIT FY1" in titles
    assert ledger.audit() == []


def test_changes_under_half_a_percent_are_ignored():
    d = {"EBIT FY1": 100.4, "EBIT FY3": 130.0}
    plan = LG.sync_plan("ACME", d, {"values": {"EBIT FY1": 100.0, "EBIT FY3": 120.0}}, "2026-07-01")
    assert list(plan["changed"]) == ["EBIT FY3"]


def test_big_pw_target_moves_need_explicit_confirmation(ledger):
    r = complete_valuation(ledger)
    cur = {f"EBIT FY2 {s}": 10.0 for s in LG.SCENARIOS} | {f"Target Multiple {s}": 5.0 for s in LG.SCENARIOS}
    fake = FakeNotion(acme(**cur))
    p = LG.prepare(ledger, "sync", r["run_id"], token="t", request=fake)
    assert p["plan"]["needs_confirm"]
    with pytest.raises(NotPublishable, match="--confirm ACME"):
        LG.go(ledger, f"lg_sync_{r['run_id']}", "isaac", "rebuilt the model")
    LG.go(ledger, f"lg_sync_{r['run_id']}", "isaac", "rebuilt the model", confirm="ACME")


@pytest.mark.parametrize("d,problem", [
    ({"Prob Bear": 0.2, "Prob Base": 0.5, "Prob Bull": 0.2}, "sum to 1.00"),
    ({"Prob Bear": 25, "Prob Base": 50, "Prob Bull": 25}, "decimals"),
    ({"EBIT FY2 Bear": 10, "EBIT FY2 Base": 9, "EBIT FY2 Bull": 12}, "Bear ≤ Base ≤ Bull"),
])
def test_validation_stops_bad_drivers(d, problem):
    assert any(problem in p for p in LG.validate(d))


def test_pw_target_matches_the_notion_formula_with_default_probabilities():
    d = {"EBIT FY2 Bear": 80.0, "EBIT FY2 Base": 100.0, "EBIT FY2 Bull": 120.0,
         "Target Multiple Bear": 10.0, "Target Multiple Base": 12.0, "Target Multiple Bull": 14.0}
    # (800-200)/10=60, (1200-200)/10=100, (1680-200)/10=148 → .25·60 + .5·100 + .25·148 = 102
    assert LG.pw_target(d, 200.0, 10.0) == 102.0


def test_an_approved_proposal_syncs_through_the_same_contract(ledger, monkeypatch):
    ingested(ledger)
    cid = ledger.verified_claims("ACME")[0]["claim_id"]
    monkeypatch.setattr(analyst, "call_agent", lambda *a, **k: ({"proposals": [
        {"field": "mid_cycle_ebit", "new_value": 146.4, "nature": "structural", "rationale": "mix", "cites": [cid]}],
        "no_change": ""}, {}))
    pid = analyse(ledger, "ACME", CONFIG, "acme.yaml")["proposals"][0]["proposal_id"]
    with pytest.raises(NotPublishable, match="not been approved"):
        LG.prepare(ledger, "sync", pid, token="t", request=FakeNotion(acme()))
    decide_proposal(ledger, pid, "approve", "isaac", "private label is structural")
    p = LG.prepare(ledger, "sync", pid, token="t", request=FakeNotion(acme()))
    assert p["plan"]["changed"]["Sustainable EBIT"][1] == pytest.approx(146.4)


def test_pending_lists_completed_research_until_it_is_pushed(ledger):
    run = ingested(ledger)
    assert [x["id"] for x in LG.pending(ledger)] == [f"lg_results_{run}"]
    fake = FakeNotion(acme())
    LG.prepare(ledger, "results", run, token="t", request=fake)
    assert LG.pending(ledger)[0]["state"] == "prepared"
    LG.push(ledger, f"lg_results_{run}", token="t", request=fake)
    assert LG.pending(ledger) == []


def _texts(page):
    return [b[b["type"]]["rich_text"][0]["text"]["content"] for b in page["children"]]


def test_refresh_rebuilds_our_draft_and_never_duplicates_the_task(ledger, monkeypatch):
    run = ingested(ledger)
    cid = ledger.verified_claims("ACME")[0]["claim_id"]

    def analyst_says(value):
        monkeypatch.setattr(analyst, "call_agent", lambda *a, **k: ({"proposals": [
            {"field": "mid_cycle_ebit", "new_value": value, "nature": "structural", "rationale": "mix", "cites": [cid]}],
            "no_change": ""}, {}))
        analyse(ledger, "ACME", CONFIG, "acme.yaml")

    analyst_says(146.4)
    fake = FakeNotion(acme())
    lg = f"lg_results_{run}"
    LG.prepare(ledger, "results", run, token="t", request=fake)
    LG.push(ledger, lg, token="t", request=fake)
    with pytest.raises(NotPublishable, match="--refresh"):
        LG.prepare(ledger, "results", run, token="t", request=fake)

    analyst_says(150.0)                                    # a re-run: the note must show the new proposal
    LG.prepare(ledger, "results", run, token="t", request=fake, refresh=True)
    assert [x["id"] for x in LG.pending(ledger)] == [lg]
    out = LG.push(ledger, lg, token="t", request=fake)
    assert out["research_note"]["body"] == "replaced" and out["task"]["existing"]
    note = fake.created("research_notes")[0]
    assert any("→ 150.0" in t for t in _texts(note)) and not any("→ 146.4" in t for t in _texts(note))
    assert len(fake.created("research_notes")) == 1 and len(fake.created("tasks")) == 1
    assert LG.pending(ledger) == [] and ledger.audit() == []
    with pytest.raises(NotPublishable, match="refreshed"):
        LG.prepare(ledger, "sync", "p_x", token="t", request=fake, refresh=True)


def test_refresh_keeps_isaacs_edits_and_appends_the_update(ledger, monkeypatch):
    run = ingested(ledger)
    fake = FakeNotion(acme())
    LG.prepare(ledger, "results", run, token="t", request=fake)
    LG.push(ledger, f"lg_results_{run}", token="t", request=fake)
    note = fake.created("research_notes")[0]
    note["_edited_by"] = "isaac"
    before = _texts(note)
    LG.prepare(ledger, "results", run, token="t", request=fake, refresh=True)
    out = LG.push(ledger, f"lg_results_{run}", token="t", request=fake)
    assert out["research_note"]["body"] == "appended" and _texts(note)[:len(before)] == before
    assert any(t.startswith("Update ") for t in _texts(note)[len(before):])


def test_a_recheck_of_an_ingested_source_is_not_new_research(ledger):
    run = ingested(ledger)
    ingest(ledger, SOURCE, {"uri": "https://sec.gov/acme-q2", "kind": "other", "company": "ACME",
                            "published_at": date(2026, 7, 30)}, CLAIMS[:1], date(2026, 8, 1), CONFIG)
    assert [x["id"] for x in LG.pending(ledger)] == [f"lg_results_{run}"]


def test_a_runs_approved_proposals_sync_together_not_one_by_one(ledger, monkeypatch):
    ingested(ledger)
    cid = ledger.verified_claims("ACME")[0]["claim_id"]
    gm = [x + 0.01 for x in CONFIG["gross_margin"]]
    monkeypatch.setattr(analyst, "call_agent", lambda *a, **k: ({"proposals": [
        {"field": "mid_cycle_ebit", "new_value": 146.4, "nature": "structural", "rationale": "mix", "cites": [cid]},
        {"field": "gross_margin", "new_value": gm, "nature": "structural", "rationale": "mix", "cites": [cid]}],
        "no_change": ""}, {}))
    res = analyse(ledger, "ACME", CONFIG, "acme.yaml")
    run = res["run_id"]
    ebit, margin = (r["proposal_id"] for r in res["proposals"])
    for pid in (ebit, margin):
        decide_proposal(ledger, pid, "approve", "isaac", "agree")
    with pytest.raises(NotPublishable, match=f"grinos capture sync {run}"):
        LG.prepare(ledger, "sync", ebit, token="t", request=FakeNotion(acme()))
    assert [x["id"] for x in LG.pending(ledger) if x["kind"] == "sync"] == [f"lg_sync_{run}"]

    p = LG.prepare(ledger, "sync", run, token="t", request=FakeNotion(acme()))
    alone = {r["proposal_id"]: r["impact"] for r in res["proposals"]}
    assert p["drivers"]["Sustainable EBIT"] == pytest.approx(146.4)                      # the EBIT proposal …
    assert p["drivers"]["EBIT FY1"] == pytest.approx(round(alone[margin]["after"]["ebit_path"][0], 4))  # … and the margin one
    assert sorted(p["drivers"]["_proposals"]) == sorted([ebit, margin])

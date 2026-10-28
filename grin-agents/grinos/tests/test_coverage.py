"""Coverage: XBRL drivers with provenance, step-by-step human verification, valuation, risks, memo."""
import json
from datetime import date
from pathlib import Path

import pytest

from grinos import coverage as C
from grinos import drivers as D
from grinos import sec, xbrl
from grinos.ledger import Ledger
from grinos.tests import sec_fixture as F

ROOT = Path(__file__).parents[2]
ACME = (ROOT / "evals/config-auditor/cases/clean_base/acme.yaml")
AS_OF = date(2026, 6, 30)


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "led")


@pytest.fixture
def config(tmp_path):
    p = tmp_path / "acme.yaml"
    p.write_text(ACME.read_text().replace("dio: 60.0", "dio: 90.0") + "\n#   DIO: avg 60.0 d\n")
    return p


def _rec():
    view = xbrl.as_of(F.acme_companyfacts(), AS_OF)
    return xbrl.reconcile(view, F.ACME_CIK, history_years=10)


def test_drivers_match_the_hand_checked_fixture_and_cite_their_facts():
    ex = D.extract(_rec(), [], None)
    d = ex["drivers"]
    assert d["gross_margin"]["value"] == pytest.approx(0.30, abs=1e-6)
    assert d["dio"]["value"] == pytest.approx(0.18 * 365, rel=1e-6)
    assert d["dso"]["value"] == pytest.approx(0.12 * 365, rel=1e-6) and d["dpo"]["value"] == pytest.approx(0.09 * 365, rel=1e-6)
    assert d["tax_rate"]["value"] == pytest.approx(0.25, abs=1e-6)
    assert d["mid_cycle_ebit"]["value"] == pytest.approx((90 + 97 + 104 + 108 + 112.6) / 5)
    adj = next(a for a in d["mid_cycle_ebit"]["alternatives"] if a["method"] == "adjusted")
    assert adj["value"] == pytest.approx((90 + 97 + 104 + 108 + 118.6) / 5)        # + the 6.0 restructuring
    facts = _rec()["facts"]
    y24 = next(y for y in d["gross_margin"]["years"] if y["period"] == "FY2024")
    assert y24["inputs"] and all(i["fact_id"] in facts and i["accn"] and i["url"] for i in y24["inputs"])
    assert any(i["restated"] for i in y24["inputs"])                                 # FY2024 revenue was restated
    assert any("only 5 fiscal years" in f for f in d["dio"]["flags"])
    assert ex["bridge"]["net_debt"] == pytest.approx(240 + 10 - 55) and ex["bridge"]["shares"] == pytest.approx(39.8)


def test_drivers_agree_with_distval_normalise():
    from decimal import Decimal

    from distval import normalise
    from distval.schema import Financials
    rec = _rec()
    hist = [Financials(ticker="ACME", fiscal_year=int(r["end"][:4]), filed_date=date(2026, 2, 1),
                       revenue=r["revenue"]["value"], gross_profit=r["revenue"]["value"] - r["cost_of_revenue"]["value"]
                       if not r.get("gross_profit") else r["gross_profit"]["value"],
                       operating_income=r["operating_income"]["value"], d_and_a=Decimal(0), capex=Decimal(0),
                       inventory=r["inventory"]["value"], receivables=r["receivables"]["value"],
                       payables=r["payables"]["value"], net_ppe=Decimal(0), total_debt=Decimal(0), cash=Decimal(0),
                       minority_interest=Decimal(0), shares_diluted=Decimal(1)) for r in rec["annual"]]
    d = D.compute(rec)
    dio, dso, dpo = normalise.avg_wc_days(hist)
    assert (d["dio"]["value"], d["dso"]["value"], d["dpo"]["value"]) == pytest.approx((dio, dso, dpo))
    hist10 = hist * 2                                                              # the helper needs 10 years
    assert d["gross_margin"]["value"] == pytest.approx(normalise.mean_gross_margin(hist10), abs=1e-6)
    assert d["opex_pct_sales"]["value"] == pytest.approx(normalise.mean_opex_pct(hist10), abs=1e-6)


def test_a_config_whose_comment_contradicts_its_value_is_flagged(config):
    import yaml
    d = D.extract(_rec(), [], yaml.safe_load(config.read_text()), config.read_text())["drivers"]["dio"]
    assert d["config_value"] == 90.0 and d["config_comment_value"] == 60.0
    assert any("own comment says avg 60" in f for f in d["flags"])
    assert D.comment_values("#   DIO: avg 83.9 d\n#   DSO: avg  9.3 d\n") == {"dio": 83.9, "dso": 9.3}


def test_claims_corroborate_or_conflict_only_in_the_drivers_own_unit():
    claims = [{"claim_id": "c_a", "claim": {"metric": "gross_margin", "period": "FY2024", "value": 0.301, "unit": "pct",
                                            "statement": "GM 30.1%"}},
              {"claim_id": "c_b", "claim": {"metric": "gross_margin", "period": "FY2023", "value": 0.35, "unit": "pct",
                                            "statement": "GM 35%"}},
              {"claim_id": "c_c", "claim": {"metric": "capex", "period": "FY2024", "value": 23.0, "unit": "usd_m",
                                            "statement": "capex $23M"}},
              {"claim_id": "c_d", "claim": {"metric": "gross_margin", "period": "Q2 FY2024", "value": 0.4, "unit": "pct",
                                            "statement": "a quarter"}}]
    d = D.extract(_rec(), claims, None)["drivers"]
    assert [c["claim_id"] for c in d["gross_margin"]["corroborating_claims"]] == ["c_a"]
    assert [c["claim_id"] for c in d["gross_margin"]["conflicting_claims"]] == ["c_b"]
    assert d["maintenance_capex_pct_sales"]["conflicting_claims"] == []          # $ claim vs % driver: skipped


def _start(ledger, config):
    return C.start(ledger, "ACME", AS_OF, str(config), sec.fixture_getter(F.responses()), who="isaac")


def _through_to_risks(ledger, config):
    r = _start(ledger, config)
    run = r["run_id"]
    assert r["status"] == "blocked" and r["step"] == "sources" and r["blocking"][0]["id"] == "s_confirm"
    C.decide(ledger, run, "s_confirm", "accept", "isaac", "")
    r = C.resume(ledger, run)
    assert r["step"] == "drivers" and {b["id"] for b in r["blocking"]} >= {"d_gross_margin", "d_dio", "d_mid_cycle_ebit"}
    for b in r["blocking"]:
        if b["kind"] == "driver" and b["id"] != "d_dio":
            C.decide(ledger, run, b["id"], "accept", "isaac", "")
        elif b["kind"] == "xbrl_review":
            C.decide(ledger, run, b["id"], "accept", "isaac", "")
    C.decide(ledger, run, "d_dio", "keep", "isaac", "inventory build is deliberate: keep 90 days")
    r = C.resume(ledger, run)
    assert r["step"] == "scenarios" and "base:" in r["blocking"][0]["detail"]
    for k, v in {"s_bear_ebit": 85, "s_bull_ebit": 125, "s_bear_multiple": 8, "s_bull_multiple": 16}.items():
        C.decide(ledger, run, k, "value", "isaac", "my scenario", value=v)
    C.decide(ledger, run, "s_probs", "accept", "isaac", "")
    return run, C.resume(ledger, run)


def test_a_full_run_blocks_for_each_human_step_then_completes(ledger, config):
    run, r = _through_to_risks(ledger, config)
    assert r["step"] == "risks" and {b["id"] for b in r["blocking"]} == {"k_risks", "k_direction", "k_nature"}
    v = r["out"]["valuation"]
    assert v["longgamma"]["Sustainable EBIT"] == pytest.approx((90 + 97 + 104 + 108 + 112.6) / 5, rel=1e-4)
    assert v["config"]["dio"] == 90.0                                              # kept, as decided
    assert v["targets"]["Bear"] < v["targets"]["Base"] < v["targets"]["Bull"]
    for risk, kill in ([("Customer concentration", "top customer above 15% of sales"),
                                      ("Private label erosion", "gross margin below 28% for 4 quarters"),
                                      ("Freight inflation", "freight above 6% of sales for 2 years")]):
        C.record_risk(ledger, "ACME", risk, 3, 4, kill, "isaac")
    C.decide(ledger, run, "k_direction", "value", "isaac", "durable", text="Long")
    C.decide(ledger, run, "k_nature", "value", "isaac", "mix shift", text="Structural")
    r = C.resume(ledger, run)
    assert r["status"] == "complete"
    memo = Path(r["out"]["memo"]["path"]).read_text()
    assert "not a recommendation" in memo and "inventory build is deliberate" in memo and "Kill:" in memo
    assert ledger.audit() == []
    assert C.resume(ledger, run)["status"] == "complete"
    n = len(list(ledger.entries()))
    C.resume(ledger, run)
    assert len(list(ledger.entries())) == n                                      # re-running writes nothing


def test_validation_catches_bad_scenarios_and_a_revision_fixes_it(ledger, config):
    run, _ = _through_to_risks(ledger, config)
    for risk in ("a risk one", "a risk two", "a risk three"):
        C.record_risk(ledger, "ACME", risk, 2, 2, "metric below 10 for 2 years", "isaac")
    C.decide(ledger, run, "k_direction", "value", "isaac", "x", text="Long")
    C.decide(ledger, run, "k_nature", "value", "isaac", "x", text="Mixed")
    C.decide(ledger, run, "s_bear_multiple", "value", "isaac", "typo: meant 8", value=30, revise=True)
    r = C.resume(ledger, run)
    assert r["status"] == "invalid" and any("Bear ≤ Base ≤ Bull" in p for p in r["out"]["validate"]["problems"])
    C.decide(ledger, run, "s_bear_multiple", "value", "isaac", "back to 8", value=8, revise=True)
    assert C.resume(ledger, run)["status"] == "complete"
    revs = [d for d in C.view(ledger, run)["decisions"] if d["target"] == "s_bear_multiple"]
    assert len(revs) == 3 and revs[1]["revises"] is not None and revs[2]["revises"] == revs[1]["seq"]


def test_decisions_are_checked(ledger, config):
    run = _start(ledger, config)["run_id"]
    with pytest.raises(ValueError, match="not an open item"):
        C.decide(ledger, run, "d_dio", "accept", "isaac", "")
    with pytest.raises(ValueError, match="takes"):
        C.decide(ledger, run, "s_confirm", "value", "isaac", "x", value=1)
    C.decide(ledger, run, "s_confirm", "accept", "isaac", "")
    with pytest.raises(ValueError, match="already decided"):
        C.decide(ledger, run, "s_confirm", "accept", "isaac", "")
    with pytest.raises(ValueError, match="can't be revised"):
        C.decide(ledger, run, "s_confirm", "accept", "isaac", "again", revise=True)
    C.resume(ledger, run)
    with pytest.raises(ValueError, match="needs a note"):
        C.decide(ledger, run, "d_dio", "value", "isaac", "", value=60)
    with pytest.raises(ValueError, match="likelihood and impact are 1–5"):
        C.record_risk(ledger, "ACME", "r", 9, 1, "k 1", "isaac")


def test_apply_writes_accepted_drivers_with_provenance_comments(ledger, config):
    run, _ = _through_to_risks(ledger, config)
    for risk in ("risk one", "risk two", "risk three"):
        C.record_risk(ledger, "ACME", risk, 2, 3, "a metric below 5 for 2 years", "isaac")
    C.decide(ledger, run, "k_direction", "value", "isaac", "x", text="Long")
    C.decide(ledger, run, "k_nature", "value", "isaac", "x", text="Structural")
    assert C.resume(ledger, run)["status"] == "complete"
    text = C.config_candidate(ledger, run)
    import yaml
    cfg = yaml.safe_load(text)
    assert cfg["dio"] == 90.0 and cfg["tax_rate"] == pytest.approx(0.25)
    assert "accepted in " + run in text and cfg["gross_margin"] == [0.3] * len(cfg["gross_margin"])
    e = C.apply(ledger, run, "isaac", "adopt the verified drivers")
    assert yaml.safe_load(config.read_text()) == cfg and e["payload"]["before_blob"]


def test_stage_moves_are_recorded_with_notes(ledger):
    assert C.stage(ledger, "ACME") is None
    C.set_stage(ledger, "ACME", "Deep Dive", "isaac", "starting work")
    e = C.set_stage(ledger, "ACME", "Watchlist", "isaac", "valued")
    assert e["payload"]["from"] == "Deep Dive" and C.stage(ledger, "ACME") == "Watchlist"
    with pytest.raises(ValueError):
        C.set_stage(ledger, "ACME", "Bought", "isaac", "x")
    json.dumps(C.runs(ledger))


def test_the_graph_explains_a_driver_down_to_its_filings(ledger, config):
    from grinos.graph import Graph, why
    run, _ = _through_to_risks(ledger, config)
    C.record_risk(ledger, "ACME", "Freight inflation", 3, 4, "freight above 6% of sales for 2 years", "isaac",
                  nature="Cyclical")
    g = Graph.open(ledger)
    out = why(g, "ACME dio")
    assert "from config by isaac: inventory build is deliberate" in out and "XBRL facts" in out
    assert "us-gaap:InventoryNet" in out and F.TEN_K[2025][0] in out
    assert "Freight inflation [Active] L3×I4" in why(g, "ACME risks")

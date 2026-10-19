from decimal import Decimal

import pytest

from distval.fcff import Bridge, Scenario, WaccInputs, dcf, forecast, reverse_dcf, sensitivity, wacc

D = Decimal
NO_CLAIMS = Bridge(debt=D(0), cash=D(0), shares_diluted=D(1))


def scen(**kw):
    base = dict(revenue_growth=0, ebit_margin="0.10", tax_rate=0, net_capex_pct_revenue=0,
                nwc_pct_revenue=0, terminal_growth=0, wacc="0.10")
    return Scenario.build("base", **(base | kw))


def test_flat_perpetuity_values_at_fcff_over_wacc():
    # FCFF = 10 forever at 10% → EV = 100 exactly; forecast PV + terminal PV must add back up.
    r = dcf(scen(), D(100), Bridge(debt=D(20), cash=D(5), shares_diluted=D(10)))
    assert r.enterprise_value == pytest.approx(D(100), abs=D("1e-20"))
    assert r.equity_value == pytest.approx(D(85), abs=D("1e-20")) and r.value_per_share == pytest.approx(D("8.5"))
    assert r.terminal_value == pytest.approx(D(100)) and [y.fcff for y in r.years] == [D(10)] * 5


def test_fcff_components_by_hand():
    s = scen(revenue_growth="0.10", ebit_margin="0.20", tax_rate="0.25", net_capex_pct_revenue="0.02",
             nwc_pct_revenue="0.15")
    rev, ebit, nopat, capex, dnwc = forecast(s, D(100))[0]
    assert (rev, ebit, nopat, capex, dnwc) == (D("110.0"), D("22.00"), D("16.5000"), D("2.20"), D("1.50"))
    y1 = dcf(s, D(100), NO_CLAIMS).years[0]
    assert y1.fcff == D("12.8000") and y1.pv_fcff == pytest.approx(D("12.8") / D("1.1"))


def test_terminal_value_uses_year_six_cash_flow():
    s = scen(terminal_growth="0.02", nwc_pct_revenue="0.10")
    r = dcf(s, D(100), NO_CLAIMS)
    # year 6 revenue 102: NOPAT 10.2, ΔNWC 0.2 → FCFF 10.0; TV = 10 / 0.08
    assert r.terminal_fcff == pytest.approx(D("10.0")) and r.terminal_value == pytest.approx(D(125))


@pytest.mark.parametrize("g,w", [("0.10", "0.10"), ("0.11", "0.10")])
def test_terminal_growth_at_or_above_wacc_is_rejected(g, w):
    with pytest.raises(ValueError, match="must be below WACC"):
        scen(terminal_growth=g, wacc=w)


@pytest.mark.parametrize("bad", [dict(wacc="0"), dict(wacc="0.35"), dict(tax_rate="1.0"), dict(revenue_growth="-1"),
                                 dict(ebit_margin=[0.1] * 4)])
def test_invalid_assumptions_are_rejected(bad):
    with pytest.raises(ValueError):
        scen(**bad)


def test_heavy_terminal_reliance_is_flagged():
    r = dcf(scen(terminal_growth="0.035", wacc="0.07"), D(100), NO_CLAIMS)
    assert r.terminal_share > D("0.75") and any("terminal value is" in f for f in r.flags)
    assert not any("terminal value is" in f for f in dcf(scen(), D(100), NO_CLAIMS).flags)


def test_bridge_subtracts_every_claim_and_adds_cash():
    b = Bridge(debt=D(30), cash=D(10), preferred=D(5), minority_interest=D(3), other_claims=D(2), shares_diluted=D(4))
    assert dcf(scen(), D(100), b).equity_value == pytest.approx(D(70))
    with pytest.raises(ValueError):
        Bridge(debt=D(0), cash=D(0), shares_diluted=D(0))


def test_sensitivity_centre_is_the_base_case_and_invalid_cells_are_none():
    s = scen(terminal_growth="0.02", wacc="0.03")
    grid = sensitivity(s, D(100), NO_CLAIMS)
    assert grid["value_per_share"][2][2] == dcf(s, D(100), NO_CLAIMS).value_per_share
    assert grid["value_per_share"][0][4] is None                     # g 3% vs WACC 2%
    rows = sensitivity(scen(), D(100), NO_CLAIMS)["value_per_share"]
    assert rows[0][2] > rows[2][2] > rows[4][2]                       # higher WACC, lower value


def test_reverse_dcf_recovers_the_growth_and_margin_that_set_the_price():
    s = scen(revenue_growth="0.06", ebit_margin="0.15", tax_rate="0.25", net_capex_pct_revenue="0.01",
             nwc_pct_revenue="0.10", terminal_growth="0.02", wacc="0.09")
    price = dcf(s, D(100), NO_CLAIMS).value_per_share
    rev = reverse_dcf(s, D(100), NO_CLAIMS, price)
    assert rev["implied_revenue_growth"] == pytest.approx(D("0.06"), abs=D("1e-5"))
    assert rev["implied_ebit_margin"] == pytest.approx(D("0.15"), abs=D("1e-5"))
    assert reverse_dcf(s, D(100), NO_CLAIMS, D("1e9"))["implied_revenue_growth"] is None   # out of range


def test_wacc_build_up_and_weights():
    w = wacc(WaccInputs(risk_free=D("0.04"), equity_risk_premium=D("0.05"), beta=D("1.2"),
                        pre_tax_cost_of_debt=D("0.06"), tax_rate=D("0.25")), D(800), D(200))
    assert w["cost_of_equity"] == D("0.100") and w["after_tax_cost_of_debt"] == D("0.0450")
    assert w["debt_weight"] == D("0.2") and w["wacc"] == pytest.approx(D("0.089"))
    assert wacc(WaccInputs(target_debt_weight=D("0.3")), D(1), D(0))["debt_weight"] == D("0.3")

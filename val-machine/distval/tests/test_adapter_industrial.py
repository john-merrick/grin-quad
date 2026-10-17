"""Unit tests and golden case for IndustrialAdapter."""
from decimal import Decimal
from datetime import date

import pytest

from distval.engine import value
from distval.industries.industrial import IndustrialAdapter, IndustrialDrivers
from distval.macro import MacroAssumptions
from distval.schema import EngineInputs


def _make_drivers(**overrides):
    base = dict(
        ticker="EXAM",
        forecast_years=5,
        revenue=[Decimal("500"), Decimal("525"), Decimal("551"),
                 Decimal("579"), Decimal("608")],
        gross_margin=[0.35] * 5,
        opex_pct_sales=[0.20] * 5,
        asset_turnover=1.8,
        maintenance_capex_pct_sales=0.04,
        growth_capex_pct_rev_growth=0.30,
        tax_rate=0.25,
        mid_cycle_ebit=Decimal("75"),
        duration_score=5,
        stability_score=4,
    )
    base.update(overrides)
    return IndustrialDrivers(**base)


_ADAPTER = IndustrialAdapter()
_MACRO = MacroAssumptions(
    sustainable_risk_free_rate=0.04,
    equity_risk_premium=0.05,
    market_avg_multiple=15.0,
)


class TestIndustrialDriversValidation:
    def test_forecast_years_zero_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            IndustrialDrivers(
                ticker="X", forecast_years=0,
                revenue=[], gross_margin=[], opex_pct_sales=[],
                asset_turnover=1.8, maintenance_capex_pct_sales=0.04,
                growth_capex_pct_rev_growth=0.30,
                tax_rate=0.25, mid_cycle_ebit=Decimal("75"),
                duration_score=5, stability_score=5,
            )


class TestAdapterContract:
    def test_returns_engine_inputs(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        assert isinstance(ei, EngineInputs)

    def test_fcf_path_length_matches_forecast_years(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        assert len(ei.fcf_path) == 5

    def test_industry_is_industrial(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        assert ei.industry == "industrial"

    def test_scores_preserved(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers(duration_score=6, stability_score=7))
        assert ei.duration_score == 6
        assert ei.stability_score == 7

    def test_terminal_nopat_is_mid_cycle_nopat(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers(mid_cycle_ebit=Decimal("75"), tax_rate=0.25))
        assert float(ei.terminal_nopat) == pytest.approx(56.25, abs=0.001)

    def test_drivers_schema_returns_industrial_drivers(self):
        assert _ADAPTER.drivers_schema() is IndustrialDrivers


class TestFCFConstruction:
    def test_year1_has_no_growth_capex(self):
        """Cold start: year 1 growth_capex = 0."""
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        # year1: NOPAT - maint_capex only (no growth capex)
        nopat_y1 = float(Decimal("500") * Decimal("0.15") * Decimal("0.75"))
        maint_y1 = float(Decimal("500") * Decimal("0.04"))
        assert float(ei.fcf_path[0]) == pytest.approx(nopat_y1 - maint_y1, abs=0.001)

    def test_year2_includes_growth_capex(self):
        """Year 2 FCF reduced by growth_capex on incremental revenue."""
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        # year2: NOPAT - maint_capex - growth_capex
        nopat_y2 = float(Decimal("525") * Decimal("0.15") * Decimal("0.75"))
        maint_y2 = float(Decimal("525") * Decimal("0.04"))
        growth_y2 = float((Decimal("525") - Decimal("500")) * Decimal("0.30"))
        assert float(ei.fcf_path[1]) == pytest.approx(nopat_y2 - maint_y2 - growth_y2, abs=0.001)

    def test_flat_revenue_no_growth_capex(self):
        """If revenue is flat, growth_capex = 0 every year."""
        flat = _make_drivers(
            forecast_years=3,
            revenue=[Decimal("500")] * 3,
            gross_margin=[0.35] * 3,
            opex_pct_sales=[0.20] * 3,
        )
        ei = _ADAPTER.to_engine_inputs(flat)
        nopat = float(Decimal("500") * Decimal("0.15") * Decimal("0.75"))
        maint = float(Decimal("500") * Decimal("0.04"))
        for fcf in ei.fcf_path:
            assert float(fcf) == pytest.approx(nopat - maint, abs=0.001)


class TestIndustrialGolden:
    """
    Hand-computed golden case using the PRD example YAML values.
    Revenue = [500, 525, 551, 579, 608], GM=35%, opex=20%, maint_capex=4%,
    growth_capex=30% of rev growth, tax=25%, mid_cycle_ebit=75.

    FCF path (hand-computed):
      year1: NOPAT(56.25) - maint(20.0) - growth(0)       = 36.25
      year2: NOPAT(59.0625) - maint(21.0) - growth(7.5)   = 30.5625
      year3: NOPAT(61.9875) - maint(22.04) - growth(7.8)  = 32.1475
      year4: NOPAT(65.1375) - maint(23.16) - growth(8.4)  = 33.5775
      year5: NOPAT(68.40)   - maint(24.32) - growth(8.7)  = 35.38

    terminal_nopat = 75 × 0.75 = 56.25
    terminal_multiple: combined=(5+4)/2=4.5, adj=0.40×(-1)/4.5=-4/45, tm=15×41/45≈13.667
    EV ≈ 630.22
    """

    def test_fcf_path(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        expected = [36.25, 30.5625, 32.1475, 33.5775, 35.38]
        for fcf, exp in zip(ei.fcf_path, expected):
            assert float(fcf) == pytest.approx(exp, abs=0.001)

    def test_terminal_nopat(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        assert float(ei.terminal_nopat) == pytest.approx(56.25, abs=0.001)

    def test_enterprise_value(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        val = value(
            engine_inputs=ei,
            macro=_MACRO,
            net_debt=Decimal("0"),
            minority_interest=Decimal("0"),
            shares_diluted=Decimal("100"),
            price=None,
            as_of=date(2024, 1, 1),
        )
        assert float(val.enterprise_value) == pytest.approx(630.22, abs=0.10)


class TestIndustrialPropertyTests:
    def test_higher_duration_score_increases_value(self):
        low = _ADAPTER.to_engine_inputs(_make_drivers(duration_score=3, stability_score=4))
        high = _ADAPTER.to_engine_inputs(_make_drivers(duration_score=8, stability_score=4))

        val_low = value(engine_inputs=low, macro=_MACRO, net_debt=Decimal("0"),
                        minority_interest=Decimal("0"), shares_diluted=Decimal("100"),
                        price=None, as_of=date(2024, 1, 1))
        val_high = value(engine_inputs=high, macro=_MACRO, net_debt=Decimal("0"),
                         minority_interest=Decimal("0"), shares_diluted=Decimal("100"),
                         price=None, as_of=date(2024, 1, 1))

        assert val_high.equity_value > val_low.equity_value

    def test_higher_stability_score_increases_value(self):
        low = _ADAPTER.to_engine_inputs(_make_drivers(duration_score=5, stability_score=3))
        high = _ADAPTER.to_engine_inputs(_make_drivers(duration_score=5, stability_score=8))

        val_low = value(engine_inputs=low, macro=_MACRO, net_debt=Decimal("0"),
                        minority_interest=Decimal("0"), shares_diluted=Decimal("100"),
                        price=None, as_of=date(2024, 1, 1))
        val_high = value(engine_inputs=high, macro=_MACRO, net_debt=Decimal("0"),
                         minority_interest=Decimal("0"), shares_diluted=Decimal("100"),
                         price=None, as_of=date(2024, 1, 1))

        assert val_high.equity_value > val_low.equity_value

    def test_net_debt_decreases_equity_one_for_one(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        base = value(engine_inputs=ei, macro=_MACRO, net_debt=Decimal("0"),
                     minority_interest=Decimal("0"), shares_diluted=Decimal("100"),
                     price=None, as_of=date(2024, 1, 1))
        more = value(engine_inputs=ei, macro=_MACRO, net_debt=Decimal("100"),
                     minority_interest=Decimal("0"), shares_diluted=Decimal("100"),
                     price=None, as_of=date(2024, 1, 1))
        assert float(base.equity_value - more.equity_value) == pytest.approx(100.0, abs=0.01)

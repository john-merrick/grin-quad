"""Unit tests and golden case for SaaSAdapter."""
from decimal import Decimal
from datetime import date

import pytest

from distval.engine import value
from distval.industries.saas import SaaSAdapter, SaaSDrivers
from distval.macro import MacroAssumptions
from distval.schema import EngineInputs


def _make_drivers(**overrides):
    base = dict(
        ticker="EXAM",
        forecast_years=5,
        arr=[Decimal("200"), Decimal("260"), Decimal("320"),
             Decimal("384"), Decimal("450")],
        nrr=0.115,
        gross_margin=[0.75, 0.76, 0.77, 0.78, 0.78],
        s_and_m_pct_revenue=[0.30, 0.28, 0.25, 0.23, 0.22],
        r_and_d_pct_revenue=[0.18, 0.17, 0.16, 0.15, 0.15],
        g_and_a_pct_revenue=[0.12, 0.11, 0.10, 0.10, 0.09],
        maintenance_capex_pct_revenue=0.02,
        tax_rate=0.25,
        mid_cycle_ebit_margin=0.22,
        mid_cycle_revenue=Decimal("450"),
        duration_score=7,
        stability_score=6,
    )
    base.update(overrides)
    return SaaSDrivers(**base)


_ADAPTER = SaaSAdapter()
_MACRO = MacroAssumptions(
    sustainable_risk_free_rate=0.04,
    equity_risk_premium=0.05,
    market_avg_multiple=15.0,
)


class TestSaaSDriversValidation:
    def test_forecast_years_zero_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SaaSDrivers(
                ticker="X", forecast_years=0,
                arr=[], nrr=0.115,
                gross_margin=[], s_and_m_pct_revenue=[],
                r_and_d_pct_revenue=[], g_and_a_pct_revenue=[],
                maintenance_capex_pct_revenue=0.02,
                tax_rate=0.25, mid_cycle_ebit_margin=0.22,
                mid_cycle_revenue=Decimal("200"),
                duration_score=5, stability_score=5,
            )


class TestAdapterContract:
    def test_returns_engine_inputs(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        assert isinstance(ei, EngineInputs)

    def test_fcf_path_length_matches_forecast_years(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        assert len(ei.fcf_path) == 5

    def test_industry_is_saas(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        assert ei.industry == "saas"

    def test_scores_preserved(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers(duration_score=8, stability_score=9))
        assert ei.duration_score == 8
        assert ei.stability_score == 9

    def test_terminal_nopat_formula(self):
        """terminal_nopat = mid_cycle_revenue × mid_cycle_ebit_margin × (1 - tax_rate)."""
        ei = _ADAPTER.to_engine_inputs(
            _make_drivers(mid_cycle_revenue=Decimal("450"), mid_cycle_ebit_margin=0.22, tax_rate=0.25)
        )
        expected = 450.0 * 0.22 * 0.75
        assert float(ei.terminal_nopat) == pytest.approx(expected, abs=0.001)

    def test_drivers_schema_returns_saas_drivers(self):
        assert _ADAPTER.drivers_schema() is SaaSDrivers


class TestFCFConstruction:
    def test_fcf_equals_nopat_minus_capex(self):
        """SaaS: no working capital → FCF = NOPAT - capex exactly."""
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        # year1: revenue=200, GM=75%, S&M=30%, R&D=18%, G&A=12%, capex=2%
        gross = 200.0 * 0.75
        opex = 200.0 * (0.30 + 0.18 + 0.12)
        nopat = (gross - opex) * 0.75
        capex = 200.0 * 0.02
        assert float(ei.fcf_path[0]) == pytest.approx(nopat - capex, abs=0.001)

    def test_nrr_field_stored(self):
        """NRR is recorded on the drivers schema (used by normalise, not FCF)."""
        drivers = _make_drivers(nrr=0.12)
        assert drivers.nrr == pytest.approx(0.12)


class TestSaaSGolden:
    """
    Hand-computed golden case using the PRD example YAML values.
    ARR = [200, 260, 320, 384, 450].

    FCF path (hand-computed):
      year1: NOPAT(22.5)  - capex(4.0)   = 18.5
      year2: NOPAT(39.0)  - capex(5.2)   = 33.8
      year3: NOPAT(62.4)  - capex(6.4)   = 56.0
      year4: NOPAT(86.4)  - capex(7.68)  = 78.72
      year5: NOPAT(108.0) - capex(9.0)   = 99.0

    terminal_nopat = 450 × 0.22 × 0.75 = 74.25
    terminal_multiple: combined=(7+6)/2=6.5, adj=0.40×(1)/4.5=4/45, tm=15×49/45≈16.333
    EV ≈ 997
    """

    def test_fcf_path(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        expected = [18.5, 33.8, 56.0, 78.72, 99.0]
        for fcf, exp in zip(ei.fcf_path, expected):
            assert float(fcf) == pytest.approx(exp, abs=0.001)

    def test_terminal_nopat(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        assert float(ei.terminal_nopat) == pytest.approx(74.25, abs=0.001)

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
        assert float(val.enterprise_value) == pytest.approx(997.0, abs=0.50)


class TestSaaSPropertyTests:
    def test_higher_duration_score_increases_value(self):
        low = _ADAPTER.to_engine_inputs(_make_drivers(duration_score=3, stability_score=6))
        high = _ADAPTER.to_engine_inputs(_make_drivers(duration_score=9, stability_score=6))

        val_low = value(engine_inputs=low, macro=_MACRO, net_debt=Decimal("0"),
                        minority_interest=Decimal("0"), shares_diluted=Decimal("100"),
                        price=None, as_of=date(2024, 1, 1))
        val_high = value(engine_inputs=high, macro=_MACRO, net_debt=Decimal("0"),
                         minority_interest=Decimal("0"), shares_diluted=Decimal("100"),
                         price=None, as_of=date(2024, 1, 1))

        assert val_high.equity_value > val_low.equity_value

    def test_higher_nrr_not_directly_affecting_engine(self):
        """NRR is stored on the driver schema but not wired into FCF — analyst sets ARR explicitly."""
        ei_low = _ADAPTER.to_engine_inputs(_make_drivers(nrr=0.90))
        ei_high = _ADAPTER.to_engine_inputs(_make_drivers(nrr=1.30))
        # Both produce the same FCF since ARR is provided explicitly (NRR is for normalisation)
        assert ei_low.fcf_path == ei_high.fcf_path

    def test_net_debt_decreases_equity_one_for_one(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        base = value(engine_inputs=ei, macro=_MACRO, net_debt=Decimal("0"),
                     minority_interest=Decimal("0"), shares_diluted=Decimal("100"),
                     price=None, as_of=date(2024, 1, 1))
        more = value(engine_inputs=ei, macro=_MACRO, net_debt=Decimal("100"),
                     minority_interest=Decimal("0"), shares_diluted=Decimal("100"),
                     price=None, as_of=date(2024, 1, 1))
        assert float(base.equity_value - more.equity_value) == pytest.approx(100.0, abs=0.01)

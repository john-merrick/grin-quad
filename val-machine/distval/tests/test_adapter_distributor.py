"""Unit tests for DistributorAdapter."""
from decimal import Decimal

import pytest

from distval.industries.distributor import DistributorAdapter
from distval.schema import DistributorDrivers, EngineInputs


def _make_drivers(**overrides):
    base = dict(
        ticker="TEST",
        forecast_years=3,
        revenue=[Decimal("1000")] * 3,
        gross_margin=[0.30] * 3,
        opex_pct_sales=[0.20] * 3,
        dio=60.0,
        dso=45.0,
        dpo=50.0,
        maintenance_capex_pct_sales=0.02,
        tax_rate=0.25,
        mid_cycle_ebit=Decimal("100"),
        duration_score=5,
        stability_score=5,
    )
    base.update(overrides)
    return DistributorDrivers(**base)


_ADAPTER = DistributorAdapter()


class TestAdapterContract:
    def test_returns_engine_inputs(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        assert isinstance(ei, EngineInputs)

    def test_fcf_path_length_matches_forecast_years(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        assert len(ei.fcf_path) == 3

    def test_industry_is_distributor(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        assert ei.industry == "distributor"

    def test_ticker_preserved(self):
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        assert ei.ticker == "TEST"

    def test_scores_preserved(self):
        drivers = _make_drivers(duration_score=7, stability_score=8)
        ei = _ADAPTER.to_engine_inputs(drivers)
        assert ei.duration_score == 7
        assert ei.stability_score == 8

    def test_terminal_nopat_is_mid_cycle_nopat(self):
        drivers = _make_drivers(mid_cycle_ebit=Decimal("100"), tax_rate=0.25)
        ei = _ADAPTER.to_engine_inputs(drivers)
        assert float(ei.terminal_nopat) == pytest.approx(75.0, abs=0.001)

    def test_drivers_schema_returns_distributor_drivers(self):
        assert _ADAPTER.drivers_schema() is DistributorDrivers


class TestFCFConstruction:
    def test_flat_revenue_yields_equal_fcf(self):
        """With flat revenue, ΔWC = 0 every year → all FCF values identical."""
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        assert all(cf == ei.fcf_path[0] for cf in ei.fcf_path)

    def test_flat_revenue_fcf_value(self):
        """FCF = NOPAT = rev × (GM - opex) × (1-tax) = 1000 × 0.10 × 0.75 = 75."""
        ei = _ADAPTER.to_engine_inputs(_make_drivers())
        assert float(ei.fcf_path[0]) == pytest.approx(75.0, abs=0.001)

    def test_growing_revenue_reduces_fcf_via_wc(self):
        """Revenue growth consumes working capital → FCF < NOPAT in growth years."""
        drivers = _make_drivers(
            forecast_years=2,
            revenue=[Decimal("1000"), Decimal("1100")],
            gross_margin=[0.30, 0.30],
            opex_pct_sales=[0.20, 0.20],
        )
        ei = _ADAPTER.to_engine_inputs(drivers)
        # Year 2 has higher NOPAT but also higher ΔWC, FCF should be lower than year 2 NOPAT
        nopat_y2 = float(Decimal("1100") * Decimal("0.10") * Decimal("0.75"))
        assert float(ei.fcf_path[1]) < nopat_y2

    def test_other_elements_preserved(self):
        drivers = _make_drivers(other_elements={"excess_land": Decimal("50")})
        ei = _ADAPTER.to_engine_inputs(drivers)
        assert ei.other_elements == {"excess_land": Decimal("50")}

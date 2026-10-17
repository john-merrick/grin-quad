"""Tests for schema validation."""
import pytest
from decimal import Decimal
from datetime import date
from pydantic import ValidationError

from distval.schema import Drivers, EngineInputs, Financials, Valuation


def make_drivers(**overrides):
    base = dict(
        ticker="TEST",
        forecast_years=3,
        revenue=[Decimal("1000"), Decimal("1000"), Decimal("1000")],
        gross_margin=[0.30, 0.30, 0.30],
        opex_pct_sales=[0.20, 0.20, 0.20],
        dio=60.0,
        dso=45.0,
        dpo=50.0,
        maintenance_capex_pct_sales=0.02,
        tax_rate=0.25,
        mid_cycle_ebit=Decimal("100"),
        duration_score=6,
        stability_score=5,
    )
    base.update(overrides)
    return base


def test_drivers_valid():
    d = Drivers(**make_drivers())
    assert d.ticker == "TEST"


def test_drivers_revenue_length_mismatch():
    with pytest.raises(ValidationError):
        Drivers(**make_drivers(revenue=[Decimal("1000"), Decimal("1000")]))  # len=2 != 3


def test_drivers_gross_margin_length_mismatch():
    with pytest.raises(ValidationError):
        Drivers(**make_drivers(gross_margin=[0.30]))


def test_drivers_opex_length_mismatch():
    with pytest.raises(ValidationError):
        Drivers(**make_drivers(opex_pct_sales=[0.20, 0.20]))


def test_drivers_duration_score_out_of_range():
    with pytest.raises(ValidationError):
        Drivers(**make_drivers(duration_score=0))
    with pytest.raises(ValidationError):
        Drivers(**make_drivers(duration_score=11))


def test_drivers_stability_score_out_of_range():
    with pytest.raises(ValidationError):
        Drivers(**make_drivers(stability_score=0))
    with pytest.raises(ValidationError):
        Drivers(**make_drivers(stability_score=11))


def test_drivers_tax_rate_implausible():
    with pytest.raises(ValidationError):
        Drivers(**make_drivers(tax_rate=-0.01))
    with pytest.raises(ValidationError):
        Drivers(**make_drivers(tax_rate=1.01))


# --- EngineInputs ---

def make_engine_inputs(**overrides):
    base = dict(
        ticker="TEST",
        forecast_years=3,
        fcf_path=[Decimal("75")] * 3,
        terminal_nopat=Decimal("75"),
        duration_score=6,
        stability_score=5,
    )
    base.update(overrides)
    return base


def test_engine_inputs_valid():
    ei = EngineInputs(**make_engine_inputs())
    assert ei.ticker == "TEST"
    assert ei.industry == "distributor"  # default


def test_engine_inputs_fcf_length_mismatch():
    with pytest.raises(ValidationError):
        EngineInputs(**make_engine_inputs(fcf_path=[Decimal("75")] * 2))  # len=2 != 3


def test_engine_inputs_score_out_of_range():
    with pytest.raises(ValidationError):
        EngineInputs(**make_engine_inputs(duration_score=0))
    with pytest.raises(ValidationError):
        EngineInputs(**make_engine_inputs(stability_score=11))


def test_engine_inputs_industry_preserved():
    ei = EngineInputs(**make_engine_inputs(industry="saas"))
    assert ei.industry == "saas"


def test_distributor_drivers_forecast_years_zero_rejected():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Drivers(**make_drivers(forecast_years=0, revenue=[], gross_margin=[], opex_pct_sales=[]))


# --- Financials ---

def test_financials_valid():
    f = Financials(
        ticker="FAST",
        fiscal_year=2023,
        filed_date=date(2024, 2, 1),
        revenue=Decimal("6981000000"),
        gross_profit=Decimal("3268000000"),
        operating_income=Decimal("1604000000"),
        d_and_a=Decimal("185000000"),
        capex=Decimal("182000000"),
        inventory=Decimal("1538000000"),
        receivables=Decimal("1210000000"),
        payables=Decimal("279000000"),
        net_ppe=Decimal("1240000000"),
        total_debt=Decimal("350000000"),
        cash=Decimal("270000000"),
        minority_interest=Decimal("0"),
        shares_diluted=Decimal("571000000"),
        lifo_reserve=None,
    )
    assert f.ticker == "FAST"

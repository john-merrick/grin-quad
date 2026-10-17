"""
Golden case from PRD Section 7. These numbers must never be edited to
accommodate a refactor — if they change, the engine is broken.
"""
from decimal import Decimal
from datetime import date

import pytest

from distval.engine import value
from distval.industries.distributor import DistributorAdapter
from distval.macro import MacroAssumptions
from distval.schema import Drivers

_AS_OF = date(2024, 1, 1)


MACRO = MacroAssumptions(
    sustainable_risk_free_rate=0.04,
    equity_risk_premium=0.05,
    market_avg_multiple=15.0,
)

DRIVERS = Drivers(
    ticker="GOLDEN",
    forecast_years=5,
    revenue=[Decimal("1000")] * 5,
    gross_margin=[0.30] * 5,
    opex_pct_sales=[0.20] * 5,
    dio=60.0,
    dso=45.0,
    dpo=50.0,
    maintenance_capex_pct_sales=0.02,
    tax_rate=0.25,
    mid_cycle_ebit=Decimal("100"),
    duration_score=6,
    stability_score=5,
)

SHARES = Decimal("100")
NET_DEBT = Decimal("200")
MINORITY_INTEREST = Decimal("0")


def test_golden_discount_rate():
    from distval.engine import discount_rate
    assert discount_rate(MACRO) == pytest.approx(0.09)


def test_golden_terminal_multiple():
    from distval.engine import terminal_multiple
    tm = terminal_multiple(MACRO.market_avg_multiple, DRIVERS.duration_score, DRIVERS.stability_score)
    assert tm == pytest.approx(15.0, abs=0.01)


def test_golden_full_valuation():
    engine_inputs = DistributorAdapter().to_engine_inputs(DRIVERS)
    val = value(
        engine_inputs=engine_inputs,
        macro=MACRO,
        net_debt=NET_DEBT,
        minority_interest=MINORITY_INTEREST,
        shares_diluted=SHARES,
        price=None,
        as_of=_AS_OF,
    )

    # FCF each year = 75.00 (revenue flat → ΔWC = 0)
    for fcf in val.fcf_path:
        assert float(fcf) == pytest.approx(75.0, abs=0.01)

    assert float(val.enterprise_value) == pytest.approx(1022.90, abs=0.01)
    assert float(val.equity_value) == pytest.approx(822.90, abs=0.01)
    assert float(val.value_per_share) == pytest.approx(8.229, abs=0.01)
    assert val.upside_pct is None
    assert val.price is None

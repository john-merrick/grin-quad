"""Tests for normalise.py."""
from decimal import Decimal
from datetime import date

import pytest

from distval.schema import Financials
from distval.normalise import (
    mean_gross_margin,
    detect_flags,
    NormalisationFlags,
)


def _make_fin(
    ticker="TEST",
    fiscal_year=2023,
    revenue=Decimal("1000"),
    gross_profit=Decimal("300"),
    operating_income=Decimal("100"),
    capex=Decimal("20"),
    d_and_a=Decimal("20"),
    inventory=Decimal("200"),
    receivables=Decimal("150"),
    payables=Decimal("80"),
    net_ppe=Decimal("300"),
    total_debt=Decimal("100"),
    cash=Decimal("50"),
    minority_interest=Decimal("0"),
    shares_diluted=Decimal("100"),
    lifo_reserve=None,
) -> Financials:
    return Financials(
        ticker=ticker,
        fiscal_year=fiscal_year,
        filed_date=date(fiscal_year + 1, 2, 1),
        revenue=revenue,
        gross_profit=gross_profit,
        operating_income=operating_income,
        d_and_a=d_and_a,
        capex=capex,
        inventory=inventory,
        receivables=receivables,
        payables=payables,
        net_ppe=net_ppe,
        total_debt=total_debt,
        cash=cash,
        minority_interest=minority_interest,
        shares_diluted=shares_diluted,
        lifo_reserve=lifo_reserve,
    )


def _history(gross_margins, revenues=None):
    if revenues is None:
        revenues = [Decimal("1000")] * len(gross_margins)
    return [
        _make_fin(
            fiscal_year=2014 + i,
            revenue=rev,
            gross_profit=(rev * Decimal(str(gm))).quantize(Decimal("1")),
        )
        for i, (gm, rev) in enumerate(zip(gross_margins, revenues))
    ]


def test_mean_gross_margin_basic():
    history = _history([0.30, 0.32, 0.28, 0.31, 0.29, 0.30, 0.31, 0.30, 0.29, 0.30])
    gm = mean_gross_margin(history)
    assert gm == pytest.approx(0.30, abs=0.001)


def test_mean_gross_margin_requires_10_years():
    history = _history([0.30] * 9)
    with pytest.raises(ValueError, match="10 years"):
        mean_gross_margin(history)


def test_flag_capex_persistently_above_da():
    # capex > d&a for 3+ consecutive years
    fins = [
        _make_fin(fiscal_year=2021, capex=Decimal("30"), d_and_a=Decimal("20")),
        _make_fin(fiscal_year=2022, capex=Decimal("35"), d_and_a=Decimal("21")),
        _make_fin(fiscal_year=2023, capex=Decimal("40"), d_and_a=Decimal("22")),
    ]
    flags = detect_flags(fins)
    assert flags.capex_exceeds_da


def test_no_flag_when_capex_normalises():
    fins = [
        _make_fin(fiscal_year=2021, capex=Decimal("30"), d_and_a=Decimal("20")),
        _make_fin(fiscal_year=2022, capex=Decimal("18"), d_and_a=Decimal("21")),
        _make_fin(fiscal_year=2023, capex=Decimal("20"), d_and_a=Decimal("22")),
    ]
    flags = detect_flags(fins)
    assert not flags.capex_exceeds_da


def test_flag_inventory_growth_exceeds_revenue():
    # Inventory growing 15pp faster than revenue in a year
    fins = [
        _make_fin(fiscal_year=2022, revenue=Decimal("1000"), inventory=Decimal("200")),
        _make_fin(fiscal_year=2023, revenue=Decimal("1050"), inventory=Decimal("240")),
    ]
    flags = detect_flags(fins)
    assert flags.inventory_outpacing_revenue

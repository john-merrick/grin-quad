"""Tests for record.py — identical inputs produce identical hashes."""
import json
import tempfile
from decimal import Decimal
from datetime import date, datetime
from pathlib import Path

from distval.engine import value
from distval.industries.distributor import DistributorAdapter
from distval.macro import MacroAssumptions
from distval.record import write_snapshot
from distval.schema import Drivers


_MACRO = MacroAssumptions()
_DRIVERS = Drivers(
    ticker="SNAP",
    forecast_years=3,
    revenue=[Decimal("1000")] * 3,
    gross_margin=[0.30] * 3,
    opex_pct_sales=[0.20] * 3,
    dio=60.0, dso=45.0, dpo=50.0,
    maintenance_capex_pct_sales=0.02,
    tax_rate=0.25,
    mid_cycle_ebit=Decimal("100"),
    duration_score=5,
    stability_score=5,
)
_ENGINE_INPUTS = DistributorAdapter().to_engine_inputs(_DRIVERS)


def _make_val():
    return value(
        engine_inputs=_ENGINE_INPUTS,
        macro=_MACRO,
        net_debt=Decimal("100"),
        minority_interest=Decimal("0"),
        shares_diluted=Decimal("50"),
        price=None,
        as_of=date(2024, 1, 1),
    )


def test_identical_inputs_produce_identical_hash():
    val1 = _make_val()
    val2 = _make_val()
    assert val1.input_hash == val2.input_hash


def test_snapshot_written_and_readable():
    val = _make_val()
    ts = datetime(2024, 6, 1, 12, 0, 0)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = write_snapshot(val, snapshot_dir=Path(tmpdir), timestamp=ts)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["ticker"] == "SNAP"
        assert data["industry"] == "distributor"
        assert data["input_hash"] == val.input_hash


def test_identical_runs_same_filename():
    val1 = _make_val()
    val2 = _make_val()
    ts = datetime(2024, 6, 1)
    with tempfile.TemporaryDirectory() as tmpdir:
        p1 = write_snapshot(val1, snapshot_dir=Path(tmpdir), timestamp=ts)
        p2 = write_snapshot(val2, snapshot_dir=Path(tmpdir), timestamp=ts)
        assert p1.name == p2.name

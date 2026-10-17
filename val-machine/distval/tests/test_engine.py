"""Property tests for the engine."""
from decimal import Decimal
from datetime import date

import pytest

from distval.engine import value
from distval.macro import MacroAssumptions
from distval.schema import EngineInputs


MACRO = MacroAssumptions()

_BASE = dict(
    ticker="PROP",
    forecast_years=5,
    fcf_path=[Decimal("75")] * 5,
    terminal_nopat=Decimal("75"),
    duration_score=5,
    stability_score=5,
)

_AS_OF = date(2024, 1, 1)


def _val(**overrides):
    inputs = EngineInputs(**{**_BASE, **overrides})
    return value(
        engine_inputs=inputs,
        macro=MACRO,
        net_debt=Decimal("200"),
        minority_interest=Decimal("0"),
        shares_diluted=Decimal("100"),
        price=None,
        as_of=_AS_OF,
    )


def test_higher_duration_score_increases_value():
    low = _val(duration_score=3, stability_score=5)
    high = _val(duration_score=8, stability_score=5)
    assert high.equity_value > low.equity_value


def test_higher_stability_score_increases_value():
    low = _val(duration_score=5, stability_score=3)
    high = _val(duration_score=5, stability_score=8)
    assert high.equity_value > low.equity_value


def test_net_debt_decreases_equity_one_for_one():
    base = _val()
    more_debt = value(
        engine_inputs=EngineInputs(**_BASE),
        macro=MACRO,
        net_debt=Decimal("300"),
        minority_interest=Decimal("0"),
        shares_diluted=Decimal("100"),
        price=None,
        as_of=_AS_OF,
    )
    diff = base.equity_value - more_debt.equity_value
    assert float(diff) == pytest.approx(100.0, abs=0.01)


def test_flat_fcf_path_all_equal():
    val = _val()
    for fcf in val.fcf_path:
        assert float(fcf) == pytest.approx(float(val.fcf_path[0]), abs=0.001)


def test_engine_does_not_import_industries():
    """Seam enforcement: engine.py must not import from distval.industries."""
    import ast
    import pathlib

    engine_src = (pathlib.Path(__file__).parent.parent / "engine.py").read_text()
    tree = ast.parse(engine_src)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("distval.industries"), (
                    f"engine.py imports from distval.industries: {node.module}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("distval.industries"), (
                        f"engine.py imports from distval.industries: {alias.name}"
                    )

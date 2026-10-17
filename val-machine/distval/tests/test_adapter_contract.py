"""
Adapter contract tests — run automatically for every entry in REGISTRY.
Each adapter must produce a valid EngineInputs given a minimal valid drivers dict.
"""
import importlib
from decimal import Decimal

import pytest

from distval.industries.registry import REGISTRY
from distval.schema import EngineInputs

# Minimal valid drivers for each registered industry
_MINIMAL_DRIVERS = {
    "distributor": dict(
        ticker="CONT",
        forecast_years=2,
        revenue=[Decimal("500")] * 2,
        gross_margin=[0.30] * 2,
        opex_pct_sales=[0.20] * 2,
        dio=60.0,
        dso=45.0,
        dpo=50.0,
        maintenance_capex_pct_sales=0.02,
        tax_rate=0.25,
        mid_cycle_ebit=Decimal("75"),
        duration_score=5,
        stability_score=5,
    ),
    "industrial": dict(
        ticker="CONT",
        forecast_years=2,
        revenue=[Decimal("500")] * 2,
        gross_margin=[0.35] * 2,
        opex_pct_sales=[0.20] * 2,
        asset_turnover=1.8,
        maintenance_capex_pct_sales=0.04,
        growth_capex_pct_rev_growth=0.30,
        tax_rate=0.25,
        mid_cycle_ebit=Decimal("75"),
        duration_score=5,
        stability_score=5,
    ),
    "saas": dict(
        ticker="CONT",
        forecast_years=2,
        arr=[Decimal("200")] * 2,
        nrr=0.115,
        gross_margin=[0.75] * 2,
        s_and_m_pct_revenue=[0.30] * 2,
        r_and_d_pct_revenue=[0.18] * 2,
        g_and_a_pct_revenue=[0.10] * 2,
        maintenance_capex_pct_revenue=0.02,
        tax_rate=0.25,
        mid_cycle_ebit_margin=0.22,
        mid_cycle_revenue=Decimal("200"),
        duration_score=5,
        stability_score=5,
    ),
}


def _load_adapter(dotted_path: str):
    module_path, cls_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, cls_name)()


@pytest.mark.parametrize("industry,adapter_path", list(REGISTRY.items()))
class TestAdapterContract:
    def test_produces_engine_inputs(self, industry, adapter_path):
        adapter = _load_adapter(adapter_path)
        drivers_cls = adapter.drivers_schema()
        raw = _MINIMAL_DRIVERS[industry]
        drivers = drivers_cls(**raw)
        ei = adapter.to_engine_inputs(drivers)
        assert isinstance(ei, EngineInputs)

    def test_fcf_path_length_correct(self, industry, adapter_path):
        adapter = _load_adapter(adapter_path)
        drivers_cls = adapter.drivers_schema()
        raw = _MINIMAL_DRIVERS[industry]
        drivers = drivers_cls(**raw)
        ei = adapter.to_engine_inputs(drivers)
        assert len(ei.fcf_path) == raw["forecast_years"]

    def test_industry_field_set(self, industry, adapter_path):
        adapter = _load_adapter(adapter_path)
        drivers_cls = adapter.drivers_schema()
        raw = _MINIMAL_DRIVERS[industry]
        drivers = drivers_cls(**raw)
        ei = adapter.to_engine_inputs(drivers)
        assert ei.industry == industry

    def test_terminal_nopat_positive(self, industry, adapter_path):
        adapter = _load_adapter(adapter_path)
        drivers_cls = adapter.drivers_schema()
        raw = _MINIMAL_DRIVERS[industry]
        drivers = drivers_cls(**raw)
        ei = adapter.to_engine_inputs(drivers)
        assert ei.terminal_nopat > 0

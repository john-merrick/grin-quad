from pathlib import Path

import pytest
import yaml
from config_facts import config_facts

BASE = {
    "ticker": "ACME",
    "forecast_years": 3,
    "revenue": [1000.0, 1040.0, 1080.0],
    "gross_margin": [0.30, 0.30, 0.30],
    "opex_pct_sales": [0.20, 0.20, 0.20],
    "dio": 60.0,
    "dso": 45.0,
    "dpo": 50.0,
    "maintenance_capex_pct_sales": 0.02,
    "tax_rate": 0.25,
    "mid_cycle_ebit": 104.0,
    "duration_score": 6,
    "stability_score": 5,
    "other_elements": {},
}


def _run(tmp_path: Path, name: str = "acme.yaml", **overrides) -> dict:
    cfg = {**BASE, **overrides}
    path = tmp_path / name
    path.write_text(yaml.safe_dump(cfg))
    return config_facts(path)


def _ids(out: dict) -> set[str]:
    return {c["id"] for c in out["checks"]}


def test_clean_config_has_no_checks(tmp_path):
    out = _run(tmp_path)
    assert out["checks"] == []
    assert out["facts"]["ebit_margin"] == [0.1, 0.1, 0.1]
    assert out["facts"]["net_wc_days"] == 55.0
    assert out["facts"]["terminal_multiple"] == pytest.approx(15.0)  # (6+5)/2 = 5.5 → market multiple


def test_output_is_deterministic(tmp_path):
    assert _run(tmp_path) == _run(tmp_path)


def test_rate_written_as_percent(tmp_path):
    out = _run(tmp_path, gross_margin=[0.30, 30.0, 0.30])
    assert "rate_out_of_unit" in _ids(out)


def test_opex_above_gross_margin(tmp_path):
    out = _run(tmp_path, opex_pct_sales=[0.20, 0.35, 0.20])
    assert "non_positive_ebit_margin" in _ids(out)


def test_revenue_unit_error(tmp_path):
    out = _run(tmp_path, revenue=[1000.0, 10400.0, 1080.0])
    assert "revenue_jump" in _ids(out)


def test_wc_days_out_of_range(tmp_path):
    assert "wc_days_out_of_range" in _ids(_run(tmp_path, dio=400.0))


def test_list_length_is_schema_error(tmp_path):
    out = _run(tmp_path, revenue=[1000.0, 1040.0])
    assert any(c["id"] == "schema_invalid" and c["field"] == "revenue" for c in out["checks"])
    assert out["facts"] == {}


def test_score_out_of_range_names_field(tmp_path):
    out = _run(tmp_path, duration_score=11)
    fields = {c["field"] for c in out["checks"] if c["id"] == "schema_invalid"}
    assert "duration_score" in fields


def test_forbidden_macro_key(tmp_path):
    out = _run(tmp_path, discount_rate=0.07)
    assert any(c["id"] == "forbidden_key" and c["field"] == "discount_rate" for c in out["checks"])


def test_unknown_key_is_reported(tmp_path):
    out = _run(tmp_path, other_element={"x": 1})
    assert any(c["id"] == "unknown_key" and c["field"] == "other_element" for c in out["checks"])


def test_ticker_filename_mismatch(tmp_path):
    assert "ticker_filename_mismatch" in _ids(_run(tmp_path, name="acmf.yaml"))


def test_mid_cycle_far_from_forecast_margin(tmp_path):
    assert "mid_cycle_vs_forecast_margin" in _ids(_run(tmp_path, mid_cycle_ebit=160.0))
    assert "mid_cycle_vs_forecast_margin" not in _ids(_run(tmp_path, mid_cycle_ebit=115.0))


def test_unknown_industry(tmp_path):
    out = _run(tmp_path, industry="bank")
    assert "unknown_industry" in _ids(out)

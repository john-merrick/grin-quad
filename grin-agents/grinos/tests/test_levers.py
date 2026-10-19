import typing

from grinos.levers import LEVERS, lever_facts
from grinos.schemas import Metric

CONFIG = {"gross_margin": [0.30] * 5, "opex_pct_sales": [0.20] * 5, "tax_rate": 0.25, "dio": 60.0}


def test_every_metric_has_a_lever_entry():
    assert set(typing.get_args(Metric)) == set(LEVERS)


def test_deviation_against_config_mean():
    f = lever_facts({"metric": "gross_margin", "value": 0.33}, CONFIG)
    assert f["lever"] == "mid_cycle_ebit" and f["deviation"] == 0.1


def test_ebit_margin_compares_to_gm_minus_opex():
    assert lever_facts({"metric": "ebit_margin", "value": 0.10}, CONFIG)["deviation"] == 0.0


def test_guidance_is_not_compared_as_an_actual():
    assert lever_facts({"metric": "guidance_margin", "value": 0.31}, CONFIG)["deviation"] is None


def test_retention_maps_to_multiple():
    assert lever_facts({"metric": "customer_retention", "value": 0.94}, CONFIG)["lever"] == "multiple"

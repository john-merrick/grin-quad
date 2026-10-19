"""
Deterministic checks on an analyst proposal before a human ever sees it, plus its valuation
impact computed by the real distval engine (never by the LLM).

A proposal passes only if:
  - it cites ≥1 claim, and every cited id is in the verified set it was given
  - the field is a real driver for the company's industry and not a macro/methodology key
  - the new value has the right shape (list length = forecast_years) and the edited config
    validates through the industry adapter
  - the edited config raises no new high-severity config_facts check
"""
import copy
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

from config_facts import FORBIDDEN_KEYS, raw_checks  # noqa: E402
from distval.engine import terminal_multiple, value  # noqa: E402
from distval.industries.registry import REGISTRY  # noqa: E402
from distval.loader import _load_adapter_class  # noqa: E402
from distval.macro import DEFAULT_MACRO  # noqa: E402


def _adapter(config: dict):
    return _load_adapter_class(REGISTRY[config.get("industry", "distributor")])()


def engine_view(config: dict, net_debt: float = 0.0, shares: float = 1.0) -> dict:
    adapter = _adapter(config)
    schema = adapter.drivers_schema()
    coerced = adapter.coerce_raw(dict(config))
    drivers = schema(**{k: v for k, v in coerced.items() if k in schema.model_fields})
    ei = adapter.to_engine_inputs(drivers)
    val = value(ei, DEFAULT_MACRO, Decimal(str(net_debt)), Decimal("0"), Decimal(str(shares)), None, date(2000, 1, 1))
    ebit = [float(r) * (g - o) for r, g, o in zip(drivers.revenue, drivers.gross_margin, drivers.opex_pct_sales)]
    return {
        "enterprise_value": float(val.enterprise_value),
        "value_per_share": float(val.value_per_share),
        "terminal_multiple": terminal_multiple(DEFAULT_MACRO.market_avg_multiple,
                                               drivers.duration_score, drivers.stability_score),
        "sustainable_ebit": float(drivers.mid_cycle_ebit),
        "ebit_path": ebit,
    }


def apply(config: dict, field: str, new_value) -> dict:
    out = copy.deepcopy(config)
    out[field] = new_value
    return out


def check_proposal(proposal: dict, config: dict, verified_ids: set[str],
                   net_debt: float = 0.0, shares: float = 1.0) -> dict:
    problems: list[str] = []
    field, new = proposal.get("field"), proposal.get("new_value")
    cites = proposal.get("cites") or []

    if not cites:
        problems.append("cites no evidence")
    unknown = [c for c in cites if c not in verified_ids]
    if unknown:
        problems.append(f"cites claims that are not verified: {unknown}")

    schema_fields = set(_adapter(config).drivers_schema().model_fields)
    if field in FORBIDDEN_KEYS or field not in schema_fields or field in {"ticker", "forecast_years"}:
        problems.append(f"field {field!r} is not an analyst-editable driver")
        return {"ok": False, "problems": problems, "impact": None}

    n = config["forecast_years"]
    if isinstance(config.get(field), list):
        if isinstance(new, (int, float)):
            new = [float(new)] * n   # a scalar for a per-year field means "every year"
        if not (isinstance(new, list) and len(new) == n):
            problems.append(f"{field} needs {n} values")
    proposed = apply(config, field, new)

    try:
        before, after = engine_view(config, net_debt, shares), engine_view(proposed, net_debt, shares)
    except (ValidationError, ValueError, TypeError) as exc:
        problems.append(f"edited config is invalid: {exc}".splitlines()[0])
        return {"ok": False, "problems": problems, "impact": None}

    new_high = {(c["id"], c["field"]) for c in raw_checks(proposed, Path(f"{config['ticker'].lower()}.yaml"))
                if c["severity"] == "high"}
    old_high = {(c["id"], c["field"]) for c in raw_checks(config, Path(f"{config['ticker'].lower()}.yaml"))
                if c["severity"] == "high"}
    for cid, f in sorted(new_high - old_high):
        problems.append(f"introduces config check {cid} on {f}")

    impact = {
        "old_value": config.get(field), "new_value": new,
        "ev_before": round(before["enterprise_value"], 1), "ev_after": round(after["enterprise_value"], 1),
        "ev_change_pct": round(after["enterprise_value"] / before["enterprise_value"] - 1, 4),
        "vps_before": round(before["value_per_share"], 2), "vps_after": round(after["value_per_share"], 2),
        "before": before, "after": after,
    }
    return {"ok": not problems, "problems": problems, "impact": impact, "normalised_value": new}

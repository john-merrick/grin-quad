"""
Deterministic facts + mechanical checks for a distval company config.

    python tools/config_facts.py path/to/<ticker>.yaml

Prints JSON to stdout. Exit code 0 always (findings are data, not failures),
2 only if the file cannot be read or parsed as YAML at all.

This is the "tool" layer under the config-auditor agent: everything here is
arithmetic or rule-based and is unit tested. The agent handles what rules
can't — comments that disagree with values, wrong day-count basis, stale
sources, unjustified scores.
"""
import json
import statistics
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml
from pydantic import ValidationError

from distval.engine import discount_rate, terminal_multiple, value
from distval.industries.registry import REGISTRY
from distval.loader import _load_adapter_class
from distval.macro import DEFAULT_MACRO

# Keys the loader accepts that are not part of any drivers schema.
LOADER_KEYS = {"industry"}

# Macro / methodology parameters that must never appear in a company config
# (README "Design constraints": macro lives in macro.py, no terminal growth).
FORBIDDEN_KEYS = {
    "discount_rate", "wacc", "beta", "cost_of_equity", "terminal_growth",
    "terminal_growth_rate", "risk_free_rate", "equity_risk_premium",
    "market_avg_multiple", "terminal_multiple",
}

RATE_LIST_FIELDS = ("gross_margin", "opex_pct_sales")
WC_DAY_FIELDS = ("dio", "dso", "dpo")

REVENUE_JUMP_LIMIT = 0.30        # |yoy growth| within the forecast
MID_CYCLE_MARGIN_TOLERANCE = 0.30  # relative gap, mid-cycle vs forecast EBIT margin
TV_SHARE_WARN = 0.85


def _check(cid: str, field: str, severity: str, message: str) -> dict:
    return {"id": cid, "field": field, "severity": severity, "message": message}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def raw_checks(raw: dict, path: Path) -> list[dict]:
    """Checks that work on the raw dict, so they still run when the schema fails."""
    checks: list[dict] = []
    industry = raw.get("industry", "distributor")

    ticker = str(raw.get("ticker", ""))
    if ticker and ticker.lower() != path.stem.lower():
        checks.append(_check("ticker_filename_mismatch", "ticker", "medium",
                             f"ticker {ticker!r} does not match filename {path.name!r}"))

    adapter_path = REGISTRY.get(industry)
    if adapter_path is None:
        checks.append(_check("unknown_industry", "industry", "high",
                             f"industry {industry!r} not in registry {sorted(REGISTRY)}"))
        return checks
    schema_fields = set(_load_adapter_class(adapter_path)().drivers_schema().model_fields)

    for key in raw:
        if key in schema_fields or key in LOADER_KEYS:
            continue
        if key in FORBIDDEN_KEYS:
            checks.append(_check("forbidden_key", key, "high",
                                 f"{key!r} is a macro/methodology parameter; it belongs in macro.py, "
                                 "and the loader silently drops it"))
        else:
            checks.append(_check("unknown_key", key, "medium",
                                 f"{key!r} is not a {industry} driver; the loader silently drops it"))

    for field in RATE_LIST_FIELDS:
        vals = raw.get(field)
        if not isinstance(vals, list):
            continue
        for i, v in enumerate(vals):
            n = _num(v)
            if n is not None and not 0.0 <= n <= 1.0:
                checks.append(_check("rate_out_of_unit", field, "high",
                                     f"{field}[{i}] = {v} is outside [0, 1]; rates are decimals"))

    gm, opex = raw.get("gross_margin"), raw.get("opex_pct_sales")
    if isinstance(gm, list) and isinstance(opex, list):
        for i, (g, o) in enumerate(zip(gm, opex)):
            g, o = _num(g), _num(o)
            if g is not None and o is not None and g <= 1 and o <= 1 and g - o <= 0:
                checks.append(_check("non_positive_ebit_margin", "opex_pct_sales", "high",
                                     f"year {i + 1}: gross_margin {g} - opex {o} <= 0"))

    rev = raw.get("revenue")
    if isinstance(rev, list):
        nums = [_num(v) for v in rev]
        for i in range(1, len(nums)):
            a, b = nums[i - 1], nums[i]
            if a and b is not None and abs(b / a - 1) > REVENUE_JUMP_LIMIT:
                checks.append(_check("revenue_jump", "revenue", "high",
                                     f"revenue[{i - 1}]→[{i}]: {a} → {b} ({b / a - 1:+.0%}); unit error?"))

    for field in WC_DAY_FIELDS:
        n = _num(raw.get(field))
        if field in raw and (n is None or not 0 <= n <= 365):
            checks.append(_check("wc_days_out_of_range", field, "medium",
                                 f"{field} = {raw.get(field)} outside [0, 365]"))

    return checks


def derived_facts(raw: dict) -> tuple[dict, list[dict]]:
    """Validate via the real adapter and compute what the engine will see."""
    industry = raw.get("industry", "distributor")
    adapter = _load_adapter_class(REGISTRY[industry])()
    schema = adapter.drivers_schema()
    try:
        coerced = adapter.coerce_raw(dict(raw))
    except (KeyError, TypeError, InvalidOperation) as exc:
        return {}, [_check("schema_invalid", "", "high", f"cannot coerce config: {exc!r}")]
    try:
        drivers = schema(**{k: v for k, v in coerced.items() if k in schema.model_fields})
    except ValidationError as exc:
        checks = []
        for err in exc.errors():
            field = str(err["loc"][0]) if err["loc"] else ""
            if not field:
                # Model-level validators ("revenue must have 5 entries") carry no loc.
                first = err["msg"].removeprefix("Value error, ").split(" ", 1)[0]
                field = first if first in schema.model_fields else ""
            checks.append(_check("schema_invalid", field, "high", err["msg"]))
        return {}, checks

    ei = adapter.to_engine_inputs(drivers)
    checks: list[dict] = []
    facts: dict = {"industry": industry, "fcf_path": [round(float(x), 1) for x in ei.fcf_path]}

    rev = [float(r) for r in drivers.revenue]
    facts["revenue_growth"] = [round(rev[i] / rev[i - 1] - 1, 4) for i in range(1, len(rev))]

    if hasattr(drivers, "gross_margin") and hasattr(drivers, "opex_pct_sales"):
        margins = [g - o for g, o in zip(drivers.gross_margin, drivers.opex_pct_sales)]
        facts["ebit_margin"] = [round(m, 4) for m in margins]
        fwd_margin = statistics.mean(margins)
        mc_margin = float(drivers.mid_cycle_ebit) / statistics.mean(rev)
        facts["mid_cycle_ebit_margin_on_avg_forecast_revenue"] = round(mc_margin, 4)
        facts["avg_forecast_ebit_margin"] = round(fwd_margin, 4)
        if fwd_margin > 0 and abs(mc_margin / fwd_margin - 1) > MID_CYCLE_MARGIN_TOLERANCE:
            checks.append(_check("mid_cycle_vs_forecast_margin", "mid_cycle_ebit", "medium",
                                 f"mid-cycle EBIT implies {mc_margin:.1%} margin on avg forecast revenue "
                                 f"vs {fwd_margin:.1%} forecast EBIT margin"))

    if all(hasattr(drivers, f) for f in WC_DAY_FIELDS):
        facts["net_wc_days"] = round(drivers.dio + drivers.dso - drivers.dpo, 1)

    macro = DEFAULT_MACRO
    tm = terminal_multiple(macro.market_avg_multiple, ei.duration_score, ei.stability_score)
    # Unit balance sheet: EV is all we need, and a fixed date keeps output deterministic.
    val = value(ei, macro, Decimal("0"), Decimal("0"), Decimal("1"), None, date(2000, 1, 1))
    pv_tv = val.terminal_value / (1 + Decimal(str(val.discount_rate))) ** ei.forecast_years
    tv_share = float(pv_tv / val.enterprise_value) if val.enterprise_value else None
    facts.update({
        "discount_rate": discount_rate(macro),
        "terminal_multiple": round(tm, 2),
        "terminal_nopat": round(float(ei.terminal_nopat), 1),
        "enterprise_value": round(float(val.enterprise_value), 1),
        "terminal_value_share_of_ev": round(tv_share, 3) if tv_share is not None else None,
    })
    if tv_share is not None and tv_share > TV_SHARE_WARN:
        checks.append(_check("terminal_value_dominates", "mid_cycle_ebit", "low",
                             f"terminal value is {tv_share:.0%} of EV"))
    return facts, checks


def config_facts(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("config is not a YAML mapping")
    checks = raw_checks(raw, path)
    facts: dict = {}
    if not any(c["id"] == "unknown_industry" for c in checks):
        facts, more = derived_facts(raw)
        checks += more
    return {"file": path.name, "ticker": raw.get("ticker"), "facts": facts, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: config_facts.py <config.yaml>", file=sys.stderr)
        return 2
    try:
        out = config_facts(Path(argv[0]))
    except (OSError, yaml.YAMLError, ValueError) as exc:
        print(json.dumps({"file": argv[0], "error": str(exc)}))
        return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Deterministic half of the decision model: which valuation lever a claim can move, and how far
it sits from the current company config. Mirrors the valuation process:
value = mid-cycle EBIT × multiple(duration, stability), plus FCF path and the equity bridge.
"""
import statistics
from typing import Any

# metric -> (lever, company-config field it speaks to)
LEVERS: dict[str, tuple[str | None, str | None]] = {
    "revenue": ("mid_cycle_ebit", "revenue"),
    "organic_growth": ("mid_cycle_ebit", "revenue"),
    "guidance_revenue": ("mid_cycle_ebit", "revenue"),
    "pricing": ("mid_cycle_ebit", "gross_margin"),
    "gross_margin": ("mid_cycle_ebit", "gross_margin"),
    "guidance_margin": ("mid_cycle_ebit", "gross_margin"),
    "opex_pct_sales": ("mid_cycle_ebit", "opex_pct_sales"),
    "ebit": ("mid_cycle_ebit", "mid_cycle_ebit"),
    "ebit_margin": ("mid_cycle_ebit", "mid_cycle_ebit"),
    "guidance_ebit": ("mid_cycle_ebit", "mid_cycle_ebit"),
    "customer_retention": ("multiple", "duration_score"),
    "market_share": ("multiple", "duration_score"),
    "dio": ("fcf", "dio"),
    "dso": ("fcf", "dso"),
    "dpo": ("fcf", "dpo"),
    "capex": ("fcf", "maintenance_capex_pct_sales"),
    "d_and_a": ("fcf", "maintenance_capex_pct_sales"),
    "tax_rate": ("nopat", "tax_rate"),
    "net_debt": ("bridge", None),
    "shares_diluted": ("bridge", None),
    "macro_rate": ("macro", None),
    "other": (None, None),
}

# Fields where a claimed rate/day-count is directly comparable to the config.
COMPARABLE = {"gross_margin", "opex_pct_sales", "tax_rate", "dio", "dso", "dpo"}


def config_value(config: dict, metric: str) -> float | None:
    if metric == "ebit_margin":
        gm, ox = config.get("gross_margin"), config.get("opex_pct_sales")
        if gm and ox:
            return statistics.mean(g - o for g, o in zip(gm, ox))
        return None
    _, field = LEVERS.get(metric, (None, None))
    if field not in COMPARABLE or field not in config:
        return None
    v = config[field]
    return statistics.mean(v) if isinstance(v, list) else float(v)


def lever_facts(claim: dict, config: dict | None) -> dict:
    lever, field = LEVERS.get(claim["metric"], (None, None))
    facts: dict[str, Any] = {"lever": lever, "config_field": field, "config_value": None, "deviation": None}
    if config and claim.get("value") is not None and not claim["metric"].startswith("guidance_"):
        cv = config_value(config, claim["metric"])
        if cv:
            facts["config_value"] = round(cv, 4)
            facts["deviation"] = round(claim["value"] / cv - 1, 4)
    return facts

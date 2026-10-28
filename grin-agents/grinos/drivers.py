"""
XBRL facts → the distval mid-cycle drivers, every number with its sources. Deterministic: no model
writes a number here.

Each Driver says what it is, how it was computed (a formula in words), which fiscal years went in,
and for every year the exact XBRL facts used (concept, value, period end, accession, filing URL).
It is then compared with:
  - verified claims for the same metric and year (corroborating / conflicting evidence), and
  - the company config's current value and what its own comments say it should be (`pool.yaml`
    says `dio: 140` with a comment "DIO: avg 83.9 d": that contradiction is flagged).

Formulas match distval.normalise (mean_gross_margin, mean_opex_pct, avg_wc_days); working-capital
days are days of REVENUE, as the engine expects.
"""
import re
import statistics
from decimal import Decimal
from typing import Any

MIN_YEARS = 10                      # distval's mid-cycle rule; fewer years is a review item, not a stop
PCT_TOL = 0.005                     # a claim within half a point corroborates a percentage driver
REL_TOL = 0.015                     # within 1.5% for money and days

# driver → (config field, claim metrics that speak to it, unit)
DRIVERS: dict[str, tuple[str, set[str], str]] = {
    "gross_margin": ("gross_margin", {"gross_margin"}, "pct"),
    "opex_pct_sales": ("opex_pct_sales", {"opex_pct_sales"}, "pct"),
    "dio": ("dio", {"dio"}, "days"),
    "dso": ("dso", {"dso"}, "days"),
    "dpo": ("dpo", {"dpo"}, "days"),
    "tax_rate": ("tax_rate", {"tax_rate"}, "pct"),
    "maintenance_capex_pct_sales": ("maintenance_capex_pct_sales", {"capex"}, "pct"),
    "mid_cycle_ebit": ("mid_cycle_ebit", {"ebit"}, "usd_m"),
}


def _v(item: dict | None) -> Decimal | None:
    return item["value"] if item else None


def _inputs(rec: dict, *items: dict | None) -> list[dict]:
    out = []
    for it in items:
        for fid in (it or {}).get("fact_ids", []):
            f = rec["facts"].get(fid, {})
            out.append({"fact_id": fid, "concept": f.get("concept"), "value": f.get("value"), "end": f.get("end"),
                        "start": f.get("start"), "accn": f.get("accn"), "form": f.get("form"),
                        "filed": f.get("filed"), "url": f.get("url"), "restated": bool(f.get("superseded"))})
    return out


def _fy(row: dict) -> str:
    return "FY" + row["end"][:4]


def _year(row: dict, value: float | None, rec: dict, *items: dict | None) -> dict:
    return {"period": _fy(row), "end": row["end"], "value": value, "inputs": _inputs(rec, *items)}


def _gp(row: dict) -> tuple[Decimal | None, list]:
    if row.get("gross_profit"):
        return _v(row["gross_profit"]), [row["gross_profit"]]
    rev, cogs = _v(row.get("revenue")), _v(row.get("cost_of_revenue"))
    if rev is not None and cogs is not None:
        return rev - cogs, [row["revenue"], row["cost_of_revenue"]]
    return None, []


def _per_year(rec: dict, fn) -> list[dict]:
    out = []
    for row in rec["annual"]:
        value, items = fn(row)
        if value is not None:
            out.append(_year(row, float(value), rec, *items))
    return out


def _ratio(num, den) -> Decimal | None:
    return num / den if num is not None and den not in (None, 0) else None


def compute(rec: dict) -> dict[str, dict]:
    """The raw drivers from reconciled XBRL: value, method, formula, years with their facts."""
    def gm(row):
        gp, items = _gp(row)
        return _ratio(gp, _v(row.get("revenue"))), items + [row.get("revenue")]

    def opex(row):
        gp, items = _gp(row)
        oi = _v(row.get("operating_income"))
        return (_ratio(gp - oi, _v(row.get("revenue"))) if gp is not None and oi is not None else None,
                items + [row.get("operating_income"), row.get("revenue")])

    def days(name):
        def f(row):
            r = _ratio(_v(row.get(name)), _v(row.get("revenue")))
            return (r * 365 if r is not None else None), [row.get(name), row.get("revenue")]
        return f

    def tax(row):
        return _ratio(_v(row.get("income_tax")), _v(row.get("pretax_income"))), [row.get("income_tax"),
                                                                               row.get("pretax_income")]

    def capex(row):
        return _ratio(_v(row.get("capex")), _v(row.get("revenue"))), [row.get("capex"), row.get("revenue")]

    def dna(row):
        return _ratio(_v(row.get("d_and_a")), _v(row.get("revenue"))), [row.get("d_and_a"), row.get("revenue")]

    def oi(row):
        return _v(row.get("operating_income")), [row.get("operating_income")]

    def adj_oi(row):
        base = _v(row.get("operating_income"))
        if base is None:
            return None, []
        addbacks = [row.get(k) for k in ("goodwill_impairment", "asset_impairment", "restructuring") if row.get(k)]
        return base + sum((_v(a) or Decimal(0)) for a in addbacks), [row.get("operating_income"), *addbacks]

    def margin(row):
        return _ratio(_v(row.get("operating_income")), _v(row.get("revenue"))), [row.get("operating_income"),
                                                                               row.get("revenue")]

    specs = {
        "gross_margin": (gm, "mean of gross profit ÷ revenue per fiscal year (gross profit, else revenue − cost of revenue)"),
        "opex_pct_sales": (opex, "mean of (gross profit − operating income) ÷ revenue per fiscal year"),
        "dio": (days("inventory"), "mean of year-end inventory ÷ revenue × 365 (days of revenue)"),
        "dso": (days("receivables"), "mean of year-end receivables ÷ revenue × 365 (days of revenue)"),
        "dpo": (days("payables"), "mean of year-end payables ÷ revenue × 365 (days of revenue)"),
        "tax_rate": (tax, "mean of income tax expense ÷ pre-tax income per fiscal year"),
        "maintenance_capex_pct_sales": (capex, "mean of capital expenditure ÷ revenue per fiscal year"),
        "mid_cycle_ebit": (oi, "mean reported operating income across the fiscal years ($M)"),
    }
    out: dict[str, dict] = {}
    for name, (fn, formula) in specs.items():
        years = _per_year(rec, fn)
        vals = [y["value"] for y in years]
        out[name] = {"driver": name, "field": DRIVERS[name][0], "unit": DRIVERS[name][2], "method": "mean",
                     "formula": formula, "value": round(statistics.mean(vals), 6) if vals else None,
                     "years": years, "alternatives": []}
    out["maintenance_capex_pct_sales"]["alternatives"].append(_alt("d_and_a", _per_year(rec, dna),
                                                                   "mean of D&A ÷ revenue (D&A ≈ maintenance capex)"))
    out["mid_cycle_ebit"]["alternatives"].append(_alt("adjusted", _per_year(rec, adj_oi),
                                                      "mean operating income + impairments + restructuring"))
    margins = _per_year(rec, margin)
    latest_rev = _v(rec["annual"][-1].get("revenue")) if rec["annual"] else None
    if margins and latest_rev is not None:
        m = statistics.mean(y["value"] for y in margins)
        out["mid_cycle_ebit"]["alternatives"].append(
            {"method": "margin_x_latest_revenue", "value": round(m * float(latest_rev), 1),
             "formula": f"mean operating margin {m:.4f} × latest revenue {float(latest_rev):,.1f}. Choose the "
                        f"mid-cycle revenue yourself with a 'value' decision if the latest year isn't mid-cycle"})
    return out


def _alt(method: str, years: list[dict], formula: str) -> dict:
    vals = [y["value"] for y in years]
    return {"method": method, "formula": formula, "value": round(statistics.mean(vals), 6) if vals else None,
            "years": years}


def bridge(rec: dict) -> dict:
    """Net debt and diluted shares from the latest balance sheet, with their facts."""
    b, sh = rec.get("balance") or {}, rec.get("diluted_shares")
    debt, cash = b.get("debt"), b.get("cash")
    nd = (float(_v(debt) or 0) - float(_v(cash) or 0)) if cash else None
    return {"date": b.get("date"), "net_debt": nd, "net_debt_inputs": _inputs(rec, debt, cash),
            "shares": float(sh["value"]) if sh else None, "shares_inputs": _inputs(rec, sh) if sh else []}


# --- comparison with claims and the config -------------------------------------------------------

_COMMENT_AVG = re.compile(r"#\s*([A-Za-z_ ]+?)\s*:\s*(?:10-yr\s+)?avg\s+([\d.]+)", re.IGNORECASE)
_LABELS = {"dio": "dio", "dso": "dso", "dpo": "dpo", "gross margin": "gross_margin", "gm": "gross_margin",
           "opex": "opex_pct_sales", "tax rate": "tax_rate", "tax": "tax_rate"}


def comment_values(yaml_text: str) -> dict[str, float]:
    """Values a config's own comments state as the average (e.g. `#   DIO: avg 83.9 d`)."""
    out: dict[str, float] = {}
    for m in _COMMENT_AVG.finditer(yaml_text or ""):
        field = _LABELS.get(m[1].strip().lower())
        if field and field not in out:
            out[field] = float(m[2])
    return out


def _close(a: float, b: float, unit: str) -> bool:
    return abs(a - b) <= PCT_TOL if unit == "pct" else abs(a - b) <= REL_TOL * max(abs(a), abs(b), 1e-9)


def _cfg_scalar(v: Any) -> float | None:
    if isinstance(v, list):
        return statistics.mean(float(x) for x in v) if v else None
    return float(v) if isinstance(v, (int, float)) else None


def compare(drivers: dict[str, dict], claims: list[dict], config: dict | None, config_text: str = "") -> None:
    """Adds corroborating/conflicting claims, the config value, the config's commented value, and flags."""
    stated = comment_values(config_text)
    for name, d in drivers.items():
        _, metrics, unit = DRIVERS[name]
        by_year = {y["period"]: y["value"] for y in d["years"]}
        corro: list[dict] = []
        conflict: list[dict] = []
        for c in claims:
            k = c["claim"]
            if (k["metric"] not in metrics or k.get("value") is None or k.get("segment") or k.get("adjusted")
                    or k.get("unit") != unit):                     # a $ capex claim says nothing about capex % sales
                continue
            year = re.search(r"(?:FY\s*)?(20\d\d)", k["period"] or "")
            if not year or re.search(r"\bQ[1-4]\b|\bH[12]\b", k["period"]):
                continue                                           # annual drivers vs annual claims only
            have = by_year.get(f"FY{year[1]}")
            if have is None:
                continue
            (corro if _close(float(k["value"]), have, unit) else conflict).append(
                {"claim_id": c["claim_id"], "period": k["period"], "value": k["value"], "statement": k["statement"]})
        d["corroborating_claims"], d["conflicting_claims"] = corro, conflict
        d["config_value"] = _cfg_scalar((config or {}).get(d["field"]))
        d["config_comment_value"] = stated.get(d["field"])
        flags = []
        if len(d["years"]) < MIN_YEARS:
            flags.append(f"only {len(d['years'])} fiscal years of XBRL history (distval's rule is {MIN_YEARS})")
        if any(i["restated"] for y in d["years"] for i in y["inputs"]):
            flags.append("uses restated facts (the latest-filed value; earlier values kept in the facts)")
        if d["value"] is None:
            flags.append("could not be computed from XBRL: supply a value with its source")
        if d["config_value"] is not None and d["config_comment_value"] is not None and not _close(
                d["config_value"], d["config_comment_value"], unit):
            flags.append(f"config says {d['config_value']:g} but its own comment says avg {d['config_comment_value']:g}")
        if d["value"] is not None and d["config_value"] is not None and not _close(d["value"], d["config_value"], unit):
            flags.append(f"XBRL {d['value']:.4g} vs config {d['config_value']:.4g}")
        if conflict:
            flags.append(f"{len(conflict)} verified claim(s) disagree with the XBRL figure for that year")
        d["flags"] = flags


def extract(rec: dict, claims: list[dict], config: dict | None, config_text: str = "") -> dict:
    drivers = compute(rec)
    compare(drivers, claims, config, config_text)
    return {"drivers": drivers, "bridge": bridge(rec), "years": [_fy(r) for r in rec["annual"]]}

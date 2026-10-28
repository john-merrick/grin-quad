"""
Build the config-auditor eval set.

    python evals/config-auditor/build_cases.py

Writes cases/<id>/<ticker>.yaml and cases/manifest.json. Re-running is
idempotent; commit the output so the eval set is reviewable in diffs.

Case kinds:
  clean     — internally consistent; any medium/high finding is a false alarm
  tool      — one planted defect a deterministic rule should catch
  judgment  — one planted defect only a reader of comments/context can catch
  real      — snapshot of a real config with known defects (recall only)

Every synthetic case is the clean base with exactly one text substitution,
so a planted defect is the only thing wrong with it.
"""
import json
import shutil
from pathlib import Path

HERE = Path(__file__).parent
CASES = HERE / "cases"
COMPANIES = HERE.parents[2] / "val-machine" / "distval" / "companies"

BASE = """\
# Acme Supply (ACME) — synthetic eval fixture, internally consistent by construction.
# Source: ACME 10-K FY2025, 10-year history FY2016–2025. All dollar values in millions USD.
#
# Mid-cycle gross margin (10-yr mean FY2016–2025): 30.0%
# Mid-cycle opex/sales (10-yr mean FY2016–2025): 20.0%
# Mid-cycle revenue: 1,126 (average of the FY2026–2030 revenue forecast below)
# Mid-cycle EBIT = 1,126 × (0.300 − 0.200) = 112.6
#
# distval flags (FY2025): none

ticker: ACME
forecast_years: 5

# Revenue: FY2025 actual = 1,000; ~4% growth in line with the 10-yr organic average.
revenue:
  - 1040.0
  - 1080.0
  - 1125.0
  - 1170.0
  - 1215.0

# Gross margin held at the 10-yr mean.
gross_margin:
  - 0.300
  - 0.300
  - 0.300
  - 0.300
  - 0.300

# Opex % of sales held at the 10-yr mean.
opex_pct_sales:
  - 0.200
  - 0.200
  - 0.200
  - 0.200
  - 0.200

# Working capital days — days of REVENUE (engine base is revenue), 10-yr averages.
dio: 60.0   # 10-yr avg DIO 60.0 days of revenue
dso: 45.0   # 10-yr avg DSO 45.0 days of revenue
dpo: 50.0   # 10-yr avg DPO 50.0 days of revenue

maintenance_capex_pct_sales: 0.02   # FY2025 capex 20 / revenue 1,000 = 2.0%, ≈ D&A
tax_rate: 0.25                      # FY2025 effective rate 25.0%

mid_cycle_ebit: 112.6

duration_score: 6    # regional share leader; moderate switching costs
stability_score: 5   # revenue fell 8% in 2020 and recovered in 2021; average cyclicality

other_elements: {}
"""


def sub(old: str, new: str, count: int = 1):
    def apply(text: str) -> str:
        assert text.count(old) >= count, f"mutation anchor not found: {old!r}"
        return text.replace(old, new, count)
    return apply


def nth_line_sub(anchor: str, old: str, new: str):
    """Replace `old` on the first line after `anchor` that contains it."""
    def apply(text: str) -> str:
        head, tail = text.split(anchor, 1)
        assert old in tail, f"{old!r} not found after {anchor!r}"
        return head + anchor + tail.replace(old, new, 1)
    return apply


# id, kind, mutation, planted fields (any one counts as a catch), what was planted
SYNTHETIC = [
    ("clean_base", "clean", None, [], "no defect"),
    ("clean_justified_deviation", "clean",
     sub("dio: 60.0   # 10-yr avg DIO 60.0 days of revenue",
         "dio: 66.0   # 10-yr avg DIO 60.0; using 66.0 because FY2024–25 range-extension SKUs\n"
         "            # (+6 days, per 10-K MD&A) are structural, not a temporary build"),
     [], "deviation from the average, with an explicit reason — must not be flagged"),
    ("clean_flag_handled", "clean",
     sub("# distval flags (FY2025): none",
         "# distval flags (FY2025): [!] elevated_gross_margin — FY2025 GM 33.5% vs 10-yr mean 30.0%;\n"
         "#   forecast reverts to the 30.0% mean, so the flag is addressed"),
     [], "a raised flag that the forecast already handles — mentioning a flag is not a defect"),

    ("tool_gm_percent_typo", "tool",
     nth_line_sub("gross_margin:", "  - 0.300\n  - 0.300\n  - 0.300", "  - 0.300\n  - 0.300\n  - 30.0"),
     ["gross_margin"], "year-3 gross margin written as 30.0 instead of 0.300"),
    ("tool_revenue_unit_error", "tool", sub("  - 1170.0", "  - 11700.0"),
     ["revenue"], "year-4 revenue off by 10x"),
    ("tool_opex_exceeds_gm", "tool",
     nth_line_sub("opex_pct_sales:", "  - 0.200", "  - 0.320"),
     ["opex_pct_sales", "gross_margin"], "year-1 opex above gross margin → negative EBIT"),
    ("tool_list_length", "tool", sub("  - 1215.0\n", ""),
     ["revenue"], "revenue has 4 entries for 5 forecast years"),
    ("tool_score_out_of_range", "tool", sub("duration_score: 6 ", "duration_score: 11"),
     ["duration_score"], "duration_score 11 is outside 1–10"),
    ("tool_ticker_mismatch", "tool", sub("ticker: ACME", "ticker: ACMF"),
     ["ticker"], "ticker field disagrees with filename"),
    ("tool_forbidden_macro_key", "tool",
     sub("other_elements: {}", "discount_rate: 0.07   # lower WACC given strong balance sheet\n\nother_elements: {}"),
     ["discount_rate"], "company-specific discount rate (macro lives in macro.py; loader silently drops it)"),

    ("judg_dio_comment_mismatch", "judgment", sub("dio: 60.0 ", "dio: 95.0 "),
     ["dio"], "dio 95 while its own comment says 10-yr avg 60.0"),
    ("judg_mid_cycle_arith", "judgment", sub("mid_cycle_ebit: 112.6", "mid_cycle_ebit: 132.0"),
     ["mid_cycle_ebit"], "mid_cycle_ebit 132 vs header derivation of 112.6 (within tool tolerance)"),
    ("judg_tax_comment_mismatch", "judgment", sub("tax_rate: 0.25 ", "tax_rate: 0.35 "),
     ["tax_rate"], "tax_rate 35% vs comment FY2025 effective 25.0%"),
    ("judg_wc_cogs_basis", "judgment",
     sub("dio: 60.0   # 10-yr avg DIO 60.0 days of revenue",
         "dio: 85.7   # inventory / COGS × 365 (FY2025: 140 / 596 × 365)"),
     ["dio"], "DIO on COGS basis while engine uses revenue basis"),
    ("judg_stability_unjustified", "judgment", sub("stability_score: 5 ", "stability_score: 10"),
     ["stability_score"], "max stability score while comment describes an 8% drawdown and average cyclicality"),
]

# Real configs, snapshotted. Planted = defects verified by hand against the file.
REAL = [
    ("real_pool", "pool.yaml", ["dio", "dso"],
     "dio 140 vs comment 10-yr avg 83.9; dso 27.6 vs comment 'corrected' 9.3"),
    ("real_fast", "fast.yaml", ["dio", "dpo"],
     "DIO and DPO computed on COGS while engine is revenue-based"),
    ("real_gww", "gww.yaml", ["mid_cycle_ebit"],
     "mid-cycle EBIT 1,700 derived from ~14,000 revenue; forecast revenue averages ~20,000"),
]


def build() -> list[dict]:
    if CASES.exists():
        shutil.rmtree(CASES)
    manifest = []
    for cid, kind, mutation, fields, note in SYNTHETIC:
        text = mutation(BASE) if mutation else BASE
        d = CASES / cid
        d.mkdir(parents=True)
        (d / "acme.yaml").write_text(text)
        manifest.append({"id": cid, "kind": kind, "file": f"{cid}/acme.yaml",
                         "planted_fields": fields, "note": note})
    for cid, src, fields, note in REAL:
        d = CASES / cid
        d.mkdir(parents=True)
        shutil.copy(COMPANIES / src, d / src)
        manifest.append({"id": cid, "kind": "real", "file": f"{cid}/{src}",
                         "planted_fields": fields, "note": note})
    (CASES / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    m = build()
    print(f"wrote {len(m)} cases to {CASES}")

"""
Verifier eval cases. Every claim is checked against the real bytes in sources/.

kind:
  valid     — true, atomic, correctly attributed; must be VERIFIED (false-reject otherwise)
  tool      — a rule must reject it (fabricated quote, wrong number, unit, lookahead, derived)
  judgment  — passes every rule, but the quote doesn't support the claim; must NOT be verified

Gate is asymmetric: a false accept lets bad evidence into a valuation, a false reject only costs
a human review. needs_human counts as "not accepted".
"""
from datetime import date

AS_OF = date(2026, 3, 1)

SOURCES = {
    "10k": {"file": "acme_10k_fy2025.txt", "kind": "10-K", "published_at": date(2026, 2, 20),
            "uri": "sec://ACME/10-K/FY2025"},
    "q4call": {"file": "acme_q4_fy2025_call.txt", "kind": "transcript", "published_at": date(2026, 2, 10),
               "uri": "call://ACME/Q4FY2025"},
    "q1call": {"file": "acme_q1_fy2026_call.txt", "kind": "transcript", "published_at": date(2026, 4, 15),
               "uri": "call://ACME/Q1FY2026"},
}


def c(cid, kind, src, metric, period, value, unit, quote, statement, note="", reason=None,
      segment=None, adjusted=False):
    return {"id": cid, "kind": kind, "expected": "verified" if kind == "valid" else "rejected",
            "source": src, "note": note, "reason": reason,
            "claim": {"company": "ACME", "metric": metric, "period": period, "value": value,
                      "unit": unit, "quote": quote, "statement": statement,
                      "segment": segment, "adjusted": adjusted}}


CASES = [
    # --- valid -----------------------------------------------------------------
    c("v_revenue", "valid", "10k", "revenue", "FY2025", 1000.0, "usd_m",
      "Net sales for fiscal 2025 were $1,000.0 million, an increase of 4.2% from $959.7 million in fiscal 2024.",
      "ACME FY2025 revenue was $1,000.0M."),
    c("v_gross_margin", "valid", "10k", "gross_margin", "FY2025", 0.300, "pct",
      "Gross margin was 30.0% of net sales in fiscal 2025 compared with 29.6% in fiscal 2024.",
      "ACME FY2025 gross margin was 30.0%."),
    c("v_prior_year_margin", "valid", "10k", "gross_margin", "FY2024", 0.296, "pct",
      "Gross margin was 30.0% of net sales in fiscal 2025 compared with 29.6% in fiscal 2024.",
      "ACME FY2024 gross margin was 29.6%.", "same quote as v_gross_margin, correctly attributed to FY2024"),
    c("v_capex", "valid", "10k", "capex", "FY2025", 20.0, "usd_m",
      "Capital expenditures were $20.0 million in fiscal 2025",
      "ACME FY2025 capex was $20.0M."),
    c("v_billion_units", "valid", "q4call", "revenue", "FY2025", 1000.0, "usd_m",
      "We crossed $1.0 billion in sales for the first time in our history.",
      "Management said FY2025 sales passed $1.0bn.", "unit conversion bn → $M"),
    c("v_pricing", "valid", "q4call", "pricing", "Q4 FY2025", 0.03, "pct",
      "In the fourth quarter we pushed through price increases of roughly 3% across the core catalogue",
      "ACME took ~3% price in Q4 FY2025.", "transcript, CEO speaking"),
    c("v_retention", "valid", "q4call", "customer_retention", "FY2025", 0.94, "pct",
      "Our customer retention rate on contract accounts was 94% for the year",
      "ACME contract-account retention was 94% in FY2025."),
    c("v_guidance", "valid", "10k", "guidance_revenue", "FY2026", 1040.0, "usd_m",
      "For fiscal 2026 we expect net sales of $1,040 million to $1,060 million.",
      "ACME guides FY2026 revenue to $1,040–1,060M (low end $1,040M).", "guidance correctly labelled"),

    c("v_segment_labelled", "valid", "10k", "revenue", "FY2025", 620.0, "usd_m",
      "Industrial segment net sales were $620.0 million in fiscal 2025",
      "ACME's Industrial segment net sales were $620.0M in FY2025.", "segment correctly labelled",
      segment="Industrial"),
    c("v_adjusted_labelled", "valid", "10k", "gross_margin", "FY2025", 0.312, "pct",
      "Adjusted gross margin, excluding the LIFO charge, was 31.2%.",
      "ACME FY2025 adjusted gross margin (excluding the LIFO charge) was 31.2%.", "non-GAAP correctly labelled",
      adjusted=True),
    c("v_dio_on_sales", "valid", "10k", "dio", "FY2025", 60.0, "days",
      "We ended the year with 60 days of inventory on hand measured against net sales.",
      "ACME ended FY2025 with 60 days of inventory on sales.", "days of revenue is this system's DIO basis"),

    # --- tool: rules must reject ------------------------------------------------
    c("t_fabricated_quote", "tool", "10k", "revenue", "FY2025", 1050.0, "usd_m",
      "Net sales for fiscal 2025 were a record $1,050.0 million.",
      "ACME FY2025 revenue was $1,050.0M.", "quote not in source"),
    c("t_wrong_number", "tool", "10k", "revenue", "FY2025", 1050.0, "usd_m",
      "Net sales for fiscal 2025 were $1,000.0 million, an increase of 4.2% from $959.7 million in fiscal 2024.",
      "ACME FY2025 revenue was $1,050.0M.", "real quote, value not in it"),
    c("t_percent_as_number", "tool", "10k", "gross_margin", "FY2025", 30.0, "pct",
      "Gross margin was 30.0% of net sales in fiscal 2025 compared with 29.6% in fiscal 2024.",
      "ACME FY2025 gross margin was 30.0.", "rate not expressed as a decimal"),
    c("t_billion_as_million", "tool", "q4call", "revenue", "FY2025", 1.0, "usd_m",
      "We crossed $1.0 billion in sales for the first time in our history.",
      "ACME FY2025 revenue was $1.0M.", "bn read as $M"),
    c("t_lookahead", "tool", "q1call", "gross_margin", "Q1 FY2026", 0.308, "pct",
      "First quarter net sales were $262.0 million, up 6.1% year over year, and gross margin was 30.8%.",
      "ACME Q1 FY2026 gross margin was 30.8%.", "published 2026-04-15, after as-of 2026-03-01"),
    c("t_derived_net_debt", "tool", "10k", "net_debt", "FY2025", 200.0, "usd_m",
      "At December 31, 2025 we had total debt of $250.0 million and cash and cash equivalents of $50.0 million.",
      "ACME FY2025 net debt was $200.0M.", "derived value; claims must be atomic, tools derive"),

    # --- judgment: rules pass, quote doesn't support the claim ----------------
    c("j_segment_as_total", "judgment", "10k", "revenue", "FY2025", 620.0, "usd_m",
      "Industrial segment net sales were $620.0 million in fiscal 2025",
      "ACME FY2025 revenue was $620.0M.", "segment figure claimed as consolidated", reason="segment_not_total"),
    c("j_adjusted_as_gaap", "judgment", "10k", "gross_margin", "FY2025", 0.312, "pct",
      "Adjusted gross margin, excluding the LIFO charge, was 31.2%.",
      "ACME FY2025 gross margin was 31.2%.", "non-GAAP figure claimed as reported", reason="non_gaap"),
    c("j_wrong_period", "judgment", "10k", "gross_margin", "FY2025", 0.296, "pct",
      "Gross margin was 30.0% of net sales in fiscal 2025 compared with 29.6% in fiscal 2024.",
      "ACME FY2025 gross margin was 29.6%.", "prior-year number attributed to current year", reason="wrong_period"),
    c("j_guidance_as_actual", "judgment", "10k", "revenue", "FY2026", 1040.0, "usd_m",
      "For fiscal 2026 we expect net sales of $1,040 million to $1,060 million.",
      "ACME FY2026 revenue was $1,040M.", "guidance claimed as an actual", reason="guidance_not_actual"),
    c("j_ceiling_as_guidance", "judgment", "10k", "guidance_margin", "FY2026", 0.31, "pct",
      "We do not expect gross margin to exceed 31% in fiscal 2026 given continued freight inflation.",
      "ACME guides FY2026 gross margin to 31%.", "a ceiling ('not exceed') claimed as a point guide", reason="conditional_or_bound"),
    c("j_analyst_as_management", "judgment", "q4call", "pricing", "FY2025", 0.05, "pct",
      "Are you seeing pricing of 5% in your channel, or is that too aggressive?",
      "Management said ACME is getting 5% pricing.", "analyst's question attributed to management", reason="wrong_speaker"),
    c("j_quarter_as_year", "judgment", "q4call", "ebit_margin", "FY2025", 0.104, "pct",
      "Operating margin in the fourth quarter was 10.4%, up from 9.8% a year ago",
      "ACME FY2025 operating margin was 10.4%.", "Q4 figure claimed as full-year (FY was 10.0%)", reason="quarter_not_year"),

    # --- sign conventions: the rule only warns (sign_differs); the agent judges ------------
    c("v_capex_table_sign", "valid", "10k", "capex", "FY2025", 20.0, "usd_m",
      "Purchases of property and equipment | (20.0) | (18.5)",
      "ACME FY2025 capex (purchases of property and equipment) was $20.0M.",
      "cash-flow outflow shown in parentheses, claimed as a positive capex amount"),
    c("v_cash_used_negative", "valid", "10k", "other", "FY2025", -26.0, "usd_m",
      "Net cash used in investing activities was $26.0 million in fiscal 2025.",
      "ACME FY2025 net cash flow from investing activities was -$26.0M (cash used).",
      "'cash used' claimed as a negative flow"),
    c("j_loss_as_gain", "judgment", "10k", "other", "FY2025", 1.5, "usd_m",
      "Other expense included a foreign currency loss of ($1.5) million in fiscal 2025.",
      "ACME recorded a foreign currency gain of $1.5M in FY2025.",
      "a loss claimed as a gain: the sign warning is a real defect here", reason="wrong_sign"),
]

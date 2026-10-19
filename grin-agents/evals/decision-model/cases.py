"""
Decision-model eval cases, against the ACME clean config (evals/config-auditor/cases/clean_base).

Labels are SETS where two reasonable analysts could differ; a case is right if the answer is
in the set. Single-element sets are the unambiguous ones. `relevant` marks claims that bear on a
lever — discarding one of those is "lost evidence", the error this stage must not make.
"""


def d(cid, metric, period, value, unit, quote, statement, action, nature, relevant=None):
    return {"id": cid, "action": set(action), "nature": set(nature),
            "relevant": ("verify" in action and "discard" not in action) if relevant is None else relevant,
            "claim": {"company": "ACME", "metric": metric, "period": period, "value": value, "unit": unit,
                      "quote": quote, "statement": statement}}


CASES = [
    # --- relevant: must be routed to verification ---------------------------------
    d("lifo_charge", "other", "FY2025", 12.0, "usd_m",
      "The improvement reflected price realisation and a favourable product mix, partly offset by a $12.0 million LIFO charge.",
      "FY2025 gross profit was reduced by a $12.0M LIFO charge.", ["verify"], ["one_off"]),
    d("nonres_softening", "revenue", "FY2025", 380.0, "usd_m",
      "Construction segment net sales were $380.0 million, up 1.7%, as non-residential activity softened in the second half.",
      "Construction segment growth slowed to 1.7% on softer non-residential activity.", ["verify"], ["cyclical"]),
    d("retention_durable", "customer_retention", "FY2025", 0.94, "pct",
      "Our customer retention rate on contract accounts was 94% for the year, in line with the last five years.",
      "Contract-account retention was 94% in FY2025, steady for five years.", ["verify"], ["structural"]),
    d("freight_ceiling", "guidance_margin", "FY2026", 0.31, "pct",
      "We do not expect gross margin to exceed 31% in fiscal 2026 given continued freight inflation.",
      "Management caps FY2026 gross margin at 31% due to freight inflation.", ["verify"], ["cyclical"]),
    d("volume_leverage", "ebit_margin", "Q4 FY2025", 0.104, "pct",
      "Operating margin in the fourth quarter was 10.4%, up from 9.8% a year ago, as we leveraged branch costs on higher volume.",
      "Q4 operating margin rose to 10.4% on volume leverage.", ["verify"], ["cyclical"]),
    d("pricing_absorbed", "pricing", "Q4 FY2025", 0.03, "pct",
      "In the fourth quarter we pushed through price increases of roughly 3% across the core catalogue, and customers absorbed them with very little attrition.",
      "ACME took ~3% price in Q4 with little customer attrition.", ["verify"], ["structural", "cyclical"]),
    d("competitor_exit", "market_share", "FY2026", None, None,
      "Our largest regional competitor announced it is exiting the Midwest market, where we hold the number one share.",
      "ACME's main Midwest competitor is exiting the market.", ["verify"], ["structural"]),
    d("gm_in_line", "gross_margin", "FY2025", 0.300, "pct",
      "Gross margin was 30.0% of net sales in fiscal 2025 compared with 29.6% in fiscal 2024.",
      "ACME FY2025 gross margin was 30.0%.", ["verify"], ["not_applicable", "structural"]),
    d("tax_in_line", "tax_rate", "FY2025", 0.25, "pct",
      "The effective tax rate was 25.0%.",
      "ACME's FY2025 effective tax rate was 25.0%.", ["verify"], ["not_applicable", "structural"]),
    d("dio_in_line", "dio", "FY2025", 60.0, "days",
      "We ended the year with 60 days of inventory on hand measured against net sales.",
      "ACME FY2025 DIO was 60 days of sales.", ["verify"], ["not_applicable", "structural"]),

    # --- irrelevant: should be discarded (keeping them only costs money) ------------
    d("milestone_fluff", "other", "FY2025", None, None,
      "2025 was a milestone year.", "Management called 2025 a milestone year.",
      ["discard"], ["noise"], relevant=False),
    d("dividend_declared", "other", "Q1 FY2026", 0.25, "usd_m",
      "The Board declared a quarterly dividend of $0.25 per share.",
      "ACME declared a $0.25 quarterly dividend.", ["discard"], ["noise", "not_applicable"], relevant=False),
    d("hq_move", "other", "FY2025", None, None,
      "We relocated our corporate headquarters to a new leased building in Columbus.",
      "ACME moved its HQ to Columbus.", ["discard"], ["noise", "not_applicable"], relevant=False),
    d("spot_treasury", "macro_rate", "Q1 FY2026", 0.046, "pct",
      "The 10-year Treasury yield rose to 4.6% this week.",
      "10-year Treasury yield is 4.6%.", ["discard", "verify"], ["cyclical", "noise"], relevant=False),
]

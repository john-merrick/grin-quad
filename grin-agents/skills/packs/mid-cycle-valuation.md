---
name: mid-cycle-valuation
description: 'The distval mid-cycle engine: what each config field does to value, and what kind of evidence may move it.'
agents:
- mid-cycle-analyst
- decision-model
- config-auditor
---

The engine (val-machine/distval) values a distributor as follows:

- **Forecast years:** EBIT = revenue × (gross margin − opex % of sales). FCF = EBIT × (1 − tax) − Δ working
  capital, where working capital = revenue × (DIO + DSO − DPO) ÷ 365. D&A is assumed equal to maintenance capex.
- **Terminal value:** mid-cycle EBIT × (1 − tax) × the terminal multiple, discounted from the last forecast year.
- **Terminal multiple** = market multiple (15×) × (1 + 0.40 × ((duration + stability)/2 − 5.5) ÷ 4.5). Scores of
  1/1 give 9×, 5.5/5.5 give 15×, and 10/10 give 21×. One point on both scores moves the multiple by about 1.3×.
- **Discount rate:** long-run risk-free rate plus equity risk premium (macro, never spot moves).
- **Equity:** EV + other elements − net debt − minorities, ÷ diluted shares.

What may move which field:

- **mid_cycle_ebit:** only structural, multi-year evidence (mix shift, pricing power held without losing
  customers, a lasting cost change). It's mid-cycle revenue × the through-cycle margin, so a single strong or
  weak year never moves it.
- **duration_score / stability_score:** durability (retention, share, contracts, switching costs) and
  through-cycle FCF consistency. Move 1–2 points at most, on several independent structural signals.
- **Forecast-year fields** (revenue path, per-year margins): cyclical evidence belongs here, not in mid-cycle.
- **One-offs** (LIFO, restructuring, impairments) are normalised out and change nothing.

Sensitivity: the terminal value is usually 60–80% of EV, so mid_cycle_ebit and the scores dominate value per
share, and per-year margins matter far less.

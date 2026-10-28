---
name: financial-statements
description: How earnings releases and 10-K/10-Q tables are laid out, and the traps when reading a figure out of them.
agents:
- verifier
- claim-extractor
- decision-model
---

How to read a figure out of a filing or earnings release:

- **Columns.** Tables put the current period first and comparatives after it: "2026 | 2025 | Change". A figure
  in the second column belongs to the prior period. Three-month and twelve-month (or six-month) blocks often
  sit side by side in one row.
- **Scale.** "(In thousands)" or "(In millions)" in a table header applies to every number below it until the
  next header. "$ 358,552" in a thousands table is $358.6 million.
- **Signs.** Parentheses mean negative: "(36,569)". In a cash-flow statement capex is shown as an outflow,
  "(20.0)". A claim that capex was 20.0 is the same figure, and correct if it describes it as spending.
- **Segments vs total.** Segment tables (by business, region or product line) sit next to the consolidated
  ones. A segment's sales or margin is not the company's.
- **GAAP vs adjusted.** "Adjusted", "excluding", "non-GAAP", "pro forma" and "organic" figures are
  management's own measures. They usually appear next to the reported figure, with a reconciliation table.
- **Guidance.** "Expects", "outlook", "guidance", "targets" and "anticipates" introduce forecasts, often as a
  range ("$5.20 to $5.50") or a bound ("at least", "not to exceed"). A range's end is not a point estimate.
- **Percent of sales.** Rows labelled "% of net sales" sit under the line they describe. The row above says
  which line it is (gross profit, SG&A, operating income).
- **Days metrics** (DIO, DSO, DPO) in this system are days of revenue, not of cost of goods.

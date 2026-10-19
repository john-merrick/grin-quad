# ACME DISTRIBUTION CO (ACME): fundamental valuation

**Valuation date** 2026-06-30 · **Prepared** 2026-10-19 · **Market price supplied** $60.00 · **Currency** USD, figures in millions except per share
**Run** `val_ACME_2026-06-30_d768f762b9` · **SEC snapshot** `f999f19dcbc4733c` (facts filed on or before 2026-06-30; fetched 2026-10-19) · SIC 5070 Wholesale-Hardware & Plumbing & Heating Equipment & Supplies

> Analysis only. This memo is not investment advice or a recommendation: the workflow places no trades and issues no buy or sell action. A person decides what, if anything, to do.

## Result

| scenario | WACC | terminal growth | enterprise value | equity value | value per share | vs price | terminal value share of EV |
|---|---|---|---|---|---|---|---|
| bear | 9.48% | 2.00% | 850.6 | 655.6 | **$16.47** | -72.5% | 69% |
| base | 8.57% | 2.50% | 1,429.6 | 1,234.6 | **$31.02** | -48.3% | 76% |
| bull | 8.12% | 3.00% | 2,030.2 | 1,835.2 | **$46.11** | -23.1% | 81% |

At $60.00, the market price implies a constant five-year revenue growth of **20.4%** (base case: 4.0%, 4.0%, 4.0%, 4.0%, 4.0%), or a constant EBIT margin of **18.1%** (base: 10.5%, 10.5%, 10.5%, 10.5%, 10.5%), with everything else in the base case held.

## What could invalidate this valuation

- FY2024 revenue was restated upward; if more restatements follow, the base year is weaker than reported. [https://www.sec.gov/Archives/edgar/data/1234567/000123456726000010/0001234567-26-000010-index.html] (analyst-cited, unverified)
- The market price implies 20.4% revenue growth a year against 4.0% in the base case. Evidence that growth will run nearer the market-implied rate would mean the base case understates value.
- Check `flag_base`: base: terminal value is 76% of enterprise value (above 75%)
- Check `flag_bull`: bull: terminal value is 81% of enterprise value (above 75%)
- Check `restatements`: 6 fact(s) were restated; latest-filed values used

## Analyst narrative (model-drafted, citations checked)

The three scenarios give a per-share range of 16.47 (bear) to 46.11 (bull), with the base case at 31.02. All three sit below the market price of 60.00; the base case is 48.3% below it. The base case assumes revenue growth of 4.0% a year and an EBIT margin of 10.5%, with a WACC of 8.57% and terminal growth of 2.50%. Reported FY2025 revenue was 1126.0 and operating income was 112.6 (USD m). [scenario:bear] [scenario:base] [scenario:bull] [x_a51c5ab204d1] [x_d78f895fb614]

The reverse DCF asks what the 60.00 market price would require. It implies constant revenue growth of 20.4% or an EBIT margin of 18.1%. Both are far above the base case assumptions of 4.0% growth and a 10.5% margin, and above the bull case paths. On these inputs, the market price needs much stronger operating results than any scenario here. [reverse_dcf] [scenario:base] [scenario:bull]

The analyst's main driver is maintenance demand from the installed base, which makes most revenue recurring. The main risk is that gross margin depends on supplier pricing that ACME does not control. That could pressure the margin assumptions in every scenario. Neither point is marked as verified in the sheet. [drivers:1] [risks:1]

The disconfirming evidence is that FY2024 revenue was restated upward. If more restatements follow, the base year is weaker than reported. The flags also note that 6 facts were restated, with latest-filed values used. Terminal value is 76% of enterprise value in the base case and 81% in the bull case, both above the 75% threshold. Small changes to WACC or terminal growth could therefore move those values a lot. [disconfirming:1] [scenario:base] [scenario:bull]

## Business drivers

- Maintenance demand from the installed base makes most revenue recurring. [https://www.sec.gov/Archives/edgar/data/1234567/000123456726000010/0001234567-26-000010-index.html] (analyst-cited, unverified)

## Risks

- Gross margin depends on supplier pricing that ACME does not control. [https://www.sec.gov/Archives/edgar/data/1234567/000123456726000010/0001234567-26-000010-index.html] (analyst-cited, unverified)

Verified claims in the evidence pipeline for ACME available on the valuation date: 0.

## Reported financials (as filed)

| | FY 2021-12-31 | FY 2022-12-31 | FY 2023-12-31 | FY 2024-12-31 | FY 2025-12-31 |
|---|---|---|---|---|---|
| Revenue | 900.0 [x_8af62c0a5345] | 950.0 [x_ea456c414e1d] | 1,000.0 [x_f943066604fa] | 1,060.0 [x_a68406a04270] | 1,126.0 [x_a51c5ab204d1] |
| Operating income | 90.0 [x_87edd23edd5f] | 97.0 [x_724c3b3e6676] | 104.0 [x_8ca9b1787e9a] | 108.0 [x_eb1997fe23db] | 112.6 [x_d78f895fb614] |
| Pretax income | 80.0 [x_32aafe92be8c] | 87.0 [x_ea35a3954a13] | 94.0 [x_0df871d0618f] | 98.0 [x_20f73eac3ba3] | 102.6 [x_fc9307aa4da4] |
| Income tax | 20.0 [x_24c3c93c9829] | 21.8 [x_966e2d6c2c3b] | 23.5 [x_00a14994d876] | 24.5 [x_a8f8b2cf10bb] | 25.6 [x_bec2ec24bf44] |
| D&A | 18.0 [x_52ecf04dcd2d] | 19.0 [x_cb5d8005bbf4] | 19.5 [x_22b502017566] | 20.0 [x_0b5fdf8570e2] | 21.0 [x_57a5df5dd60b] |
| Capex | 20.0 [x_6c05ee279e0f] | 21.0 [x_1cb103718be0] | 22.0 [x_ced3df6c2995] | 23.0 [x_058a292b689b] | 24.0 [x_d23502bc9009] |
| Receivables | 108.0 [x_5f822ac5f3fc] | 114.0 [x_0e6c6cffdd8e] | 120.0 [x_354cf5f3e028] | 127.2 [x_4724e0570128] | 135.1 [x_8e9277bfc018] |
| Inventory | 162.0 [x_4f6edadec1f1] | 171.0 [x_a176522fc8ee] | 180.0 [x_d475ce4add54] | 190.8 [x_e0397f595e0f] | 202.7 [x_e8d49783b78b] |
| Payables | 81.0 [x_fca952b88e92] | 85.5 [x_ec70bd50633c] | 90.0 [x_69bb24006609] | 95.4 [x_6cffbfebf115] | 101.3 [x_365d60440782] |

Latest balance sheet 2026-03-31: cash 55.0 [x_b3665bce4fa1], debt 250.0 [x_fe485c4c3129] [x_26e4fdbe4d7a] (LongTermDebtNoncurrent + LongTermDebtCurrent), operating leases 30.0 [x_6137a18b0987]. Diluted shares 39.800 [x_3d7010f66eac] (2026-01-01 to 2026-03-31).

## Normalisation adjustments (separate from reported figures)

| id | origin | period | item | amount | status | decided by | rationale | source |
|---|---|---|---|---|---|---|---|---|
| `adj_ff95e662` | rule | FY ending 2025-12-31 | operating_income | 6.0 | approved | example-analyst | add back restructuring, a non-recurring charge inside operating income | [x_7fb4206322e9] |

| period | reported operating income | approved adjustments | normalised | normalised margin |
|---|---|---|---|---|
| FY ending 2021-12-31 | 90.0 | 0.0 | 90.0 | 10.0% |
| FY ending 2022-12-31 | 97.0 | 0.0 | 97.0 | 10.2% |
| FY ending 2023-12-31 | 104.0 | 0.0 | 104.0 | 10.4% |
| FY ending 2024-12-31 | 108.0 | 0.0 | 108.0 | 10.2% |
| FY ending 2025-12-31 | 112.6 | 6.0 | 118.6 | 10.5% |

## Forecast assumptions

**bear**: revenue growth 0.0%/1.0%/1.0%/2.0%/2.0%; EBIT margin 8.5%; tax 25.0%; net capex 0.3% of revenue; working capital 21.0% of revenue; terminal growth 2.00%.  
WACC 9.48% = 90.5% × cost of equity 10.00% + 9.5% × after-tax cost of debt 4.50% (inputs {'beta': 1.2, 'equity_risk_premium': 0.05, 'pre_tax_cost_of_debt': 0.06, 'risk_free': 0.04}; market equity value 2,388.0).  
_tax_rate from income tax / pretax income [x_fc9307aa4da4] [x_bec2ec24bf44]_  
_net_capex_pct_revenue from (capex − D&A) / revenue [x_d23502bc9009] [x_57a5df5dd60b] [x_a51c5ab204d1]_  
_nwc_pct_revenue from (receivables + inventory − payables) / revenue [x_8e9277bfc018] [x_e8d49783b78b] [x_365d60440782] [x_a51c5ab204d1]_  

**base**: revenue growth 4.0%; EBIT margin 10.5%; tax 25.0%; net capex 0.3% of revenue; working capital 21.0% of revenue; terminal growth 2.50%.  
WACC 8.57% = 90.5% × cost of equity 9.00% + 9.5% × after-tax cost of debt 4.50% (inputs {'beta': 1.0, 'equity_risk_premium': 0.05, 'pre_tax_cost_of_debt': 0.06, 'risk_free': 0.04}; market equity value 2,388.0).  
_ebit_margin from normalised operating income / revenue, FY ending 2025-12-31 _  
_tax_rate from income tax / pretax income [x_fc9307aa4da4] [x_bec2ec24bf44]_  
_net_capex_pct_revenue from (capex − D&A) / revenue [x_d23502bc9009] [x_57a5df5dd60b] [x_a51c5ab204d1]_  
_nwc_pct_revenue from (receivables + inventory − payables) / revenue [x_8e9277bfc018] [x_e8d49783b78b] [x_365d60440782] [x_a51c5ab204d1]_  

**bull**: revenue growth 7.0%/7.0%/6.0%/6.0%/5.0%; EBIT margin 11.0%/11.2%/11.4%/11.6%/11.8%; tax 25.0%; net capex 0.3% of revenue; working capital 21.0% of revenue; terminal growth 3.00%.  
WACC 8.12% = 90.5% × cost of equity 8.50% + 9.5% × after-tax cost of debt 4.50% (inputs {'beta': 0.9, 'equity_risk_premium': 0.05, 'pre_tax_cost_of_debt': 0.06, 'risk_free': 0.04}; market equity value 2,388.0).  
_tax_rate from income tax / pretax income [x_fc9307aa4da4] [x_bec2ec24bf44]_  
_net_capex_pct_revenue from (capex − D&A) / revenue [x_d23502bc9009] [x_57a5df5dd60b] [x_a51c5ab204d1]_  
_nwc_pct_revenue from (receivables + inventory − payables) / revenue [x_8e9277bfc018] [x_e8d49783b78b] [x_365d60440782] [x_a51c5ab204d1]_  

## Valuation detail

**bear**

| year | revenue | EBIT | NOPAT | net capex | Δ working capital | FCFF | PV |
|---|---|---|---|---|---|---|---|
| 1 | 1,126.0 | 95.7 | 71.8 | 3.0 | 0.0 | 68.8 | 62.8 |
| 2 | 1,137.3 | 96.7 | 72.5 | 3.0 | 2.4 | 67.1 | 56.0 |
| 3 | 1,148.6 | 97.6 | 73.2 | 3.1 | 2.4 | 67.8 | 51.7 |
| 4 | 1,171.6 | 99.6 | 74.7 | 3.1 | 4.8 | 66.7 | 46.5 |
| 5 | 1,195.0 | 101.6 | 76.2 | 3.2 | 4.9 | 68.1 | 43.3 |
| terminal | | | | | | 69.4 → TV 928.5 | 590.4 |

**base**

| year | revenue | EBIT | NOPAT | net capex | Δ working capital | FCFF | PV |
|---|---|---|---|---|---|---|---|
| 1 | 1,171.0 | 123.3 | 92.5 | 3.1 | 9.5 | 79.9 | 73.6 |
| 2 | 1,217.9 | 128.3 | 96.2 | 3.2 | 9.8 | 83.1 | 70.5 |
| 3 | 1,266.6 | 133.4 | 100.1 | 3.4 | 10.2 | 86.5 | 67.5 |
| 4 | 1,317.3 | 138.7 | 104.1 | 3.5 | 10.6 | 89.9 | 64.7 |
| 5 | 1,370.0 | 144.3 | 108.2 | 3.6 | 11.1 | 93.5 | 62.0 |
| terminal | | | | | | 100.0 → TV 1,646.4 | 1,091.2 |

**bull**

| year | revenue | EBIT | NOPAT | net capex | Δ working capital | FCFF | PV |
|---|---|---|---|---|---|---|---|
| 1 | 1,204.8 | 132.5 | 99.4 | 3.2 | 16.6 | 79.6 | 73.7 |
| 2 | 1,289.2 | 144.4 | 108.3 | 3.4 | 17.7 | 87.1 | 74.5 |
| 3 | 1,366.5 | 155.8 | 116.8 | 3.6 | 16.2 | 97.0 | 76.7 |
| 4 | 1,448.5 | 168.0 | 126.0 | 3.9 | 17.2 | 104.9 | 76.8 |
| 5 | 1,520.9 | 179.5 | 134.6 | 4.1 | 15.2 | 115.3 | 78.1 |
| terminal | | | | | | 124.9 → TV 2,438.7 | 1,650.5 |

## Equity bridge (base case)

| | USD m |
|---|---|
| Enterprise value | 1,429.6 |
| − Debt | 250.0 |
| + Cash | 55.0 |
| − Preferred | 0.0 |
| − Minority interest | 0.0 |
| − Operating leases | 0.0 (excluded: lease cost is inside EBIT; reported 30.0) |
| = Equity value | 1,234.6 |
| ÷ Diluted shares (m) | 39.800 |
| = Value per share | **$31.02** |

## Sensitivity: base-case value per share, WACC × terminal growth

| WACC \ g | 1.50% | 2.00% | 2.50% | 3.00% | 3.50% |
|---|---|---|---|---|---|
| 7.57% | $33.08 | $35.41 | $38.21 | $41.62 | $45.87 |
| 8.07% | $30.12 | $32.03 | $34.29 | $37.00 | $40.29 |
| 8.57% | $27.57 | $29.17 | $31.02 | $33.21 | $35.82 |
| 9.07% | $25.37 | $26.71 | $28.25 | $30.04 | $32.15 |
| 9.57% | $23.44 | $24.57 | $25.87 | $27.35 | $29.09 |

## Validation

- ✓ `no_lookahead` every fact was filed on or before 2026-06-30
- ✓ `units_recorded` all facts in USD or shares, each with concept, period and accession
- ✓ `g_below_wacc_bear` bear: terminal growth 2.00% vs WACC 9.48%
- ✓ `g_below_wacc_base` base: terminal growth 2.50% vs WACC 8.57%
- ⚠ `flag_base` base: terminal value is 76% of enterprise value (above 75%)
- ✓ `g_below_wacc_bull` bull: terminal growth 3.00% vs WACC 8.12%
- ⚠ `flag_bull` bull: terminal value is 81% of enterprise value (above 75%)
- ✓ `scenario_order` bear 16.47 ≤ base 31.02 ≤ bull 46.11
- ✓ `balance_sheet_current` balance sheet dated 2026-03-31, 91 days before valuation
- ✓ `analyst_inputs` no analyst-supplied values
- ⚠ `restatements` 6 fact(s) were restated; latest-filed values used

## Analyst decisions

- `adj_ff95e662` approve by example-analyst via cli: restructuring charge is a one-off

## Sources

- 10-K filed 2023-02-25, accession 0001234567-23-000010: https://www.sec.gov/Archives/edgar/data/1234567/000123456723000010/0001234567-23-000010-index.html
- 10-K filed 2024-02-25, accession 0001234567-24-000010: https://www.sec.gov/Archives/edgar/data/1234567/000123456724000010/0001234567-24-000010-index.html
- 10-K filed 2025-02-25, accession 0001234567-25-000010: https://www.sec.gov/Archives/edgar/data/1234567/000123456725000010/0001234567-25-000010-index.html
- 10-K filed 2026-02-25, accession 0001234567-26-000010: https://www.sec.gov/Archives/edgar/data/1234567/000123456726000010/0001234567-26-000010-index.html
- 10-Q filed 2026-05-05, accession 0001234567-26-000020: https://www.sec.gov/Archives/edgar/data/1234567/000123456726000020/0001234567-26-000020-index.html

### Fact appendix

| id | concept | period | value (as reported) | unit | form | filed | accession |
|---|---|---|---|---|---|---|---|
| x_00a14994d876 | us-gaap:IncomeTaxExpenseBenefit | 2023-01-01 to 2023-12-31 | 23500000 | USD | 10-K | 2025-02-25 | 0001234567-25-000010 |
| x_01a439ec42dc | us-gaap:GrossProfit | 2024-01-01 to 2024-12-31 | 318000000 (restated; superseded 1) | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_058a292b689b | us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | 2024-01-01 to 2024-12-31 | 23000000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_0b5fdf8570e2 | us-gaap:DepreciationDepletionAndAmortization | 2024-01-01 to 2024-12-31 | 20000000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_0daf1cf06043 | us-gaap:InterestExpense | 2024-01-01 to 2024-12-31 | 10000000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_0df871d0618f | us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest | 2023-01-01 to 2023-12-31 | 94000000 | USD | 10-K | 2025-02-25 | 0001234567-25-000010 |
| x_0e6c6cffdd8e | us-gaap:AccountsReceivableNetCurrent | at 2022-12-31 | 114000000 | USD | 10-K | 2024-02-25 | 0001234567-24-000010 |
| x_184580f9a75e | us-gaap:CostOfRevenue | 2024-01-01 to 2024-12-31 | 742000000 (restated; superseded 1) | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_1cb103718be0 | us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | 2022-01-01 to 2022-12-31 | 21000000 | USD | 10-K | 2024-02-25 | 0001234567-24-000010 |
| x_20f73eac3ba3 | us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest | 2024-01-01 to 2024-12-31 | 98000000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_22b502017566 | us-gaap:DepreciationDepletionAndAmortization | 2023-01-01 to 2023-12-31 | 19500000 | USD | 10-K | 2025-02-25 | 0001234567-25-000010 |
| x_24c3c93c9829 | us-gaap:IncomeTaxExpenseBenefit | 2021-01-01 to 2021-12-31 | 20000000 | USD | 10-K | 2023-02-25 | 0001234567-23-000010 |
| x_26e4fdbe4d7a | us-gaap:LongTermDebtCurrent | at 2026-03-31 | 10000000 | USD | 10-Q | 2026-05-05 | 0001234567-26-000020 |
| x_2d270a13929f | us-gaap:InterestExpense | 2022-01-01 to 2022-12-31 | 10000000 | USD | 10-K | 2024-02-25 | 0001234567-24-000010 |
| x_32aafe92be8c | us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest | 2021-01-01 to 2021-12-31 | 80000000 | USD | 10-K | 2023-02-25 | 0001234567-23-000010 |
| x_354cf5f3e028 | us-gaap:AccountsReceivableNetCurrent | at 2023-12-31 | 120000000 | USD | 10-K | 2025-02-25 | 0001234567-25-000010 |
| x_365d60440782 | us-gaap:AccountsPayableCurrent | at 2025-12-31 | 101340000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_3d7010f66eac | us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding | 2026-01-01 to 2026-03-31 | 39800000 | shares | 10-Q | 2026-05-05 | 0001234567-26-000020 |
| x_4724e0570128 | us-gaap:AccountsReceivableNetCurrent | at 2024-12-31 | 127200000 (restated; superseded 1) | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_4f6edadec1f1 | us-gaap:InventoryNet | at 2021-12-31 | 162000000 | USD | 10-K | 2023-02-25 | 0001234567-23-000010 |
| x_52ecf04dcd2d | us-gaap:DepreciationDepletionAndAmortization | 2021-01-01 to 2021-12-31 | 18000000 | USD | 10-K | 2023-02-25 | 0001234567-23-000010 |
| x_551ac07e0434 | us-gaap:CostOfRevenue | 2022-01-01 to 2022-12-31 | 665000000 | USD | 10-K | 2024-02-25 | 0001234567-24-000010 |
| x_57a5df5dd60b | us-gaap:DepreciationDepletionAndAmortization | 2025-01-01 to 2025-12-31 | 21000000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_5f822ac5f3fc | us-gaap:AccountsReceivableNetCurrent | at 2021-12-31 | 108000000 | USD | 10-K | 2023-02-25 | 0001234567-23-000010 |
| x_6137a18b0987 | us-gaap:OperatingLeaseLiability | at 2026-03-31 | 30000000 | USD | 10-Q | 2026-05-05 | 0001234567-26-000020 |
| x_69bb24006609 | us-gaap:AccountsPayableCurrent | at 2023-12-31 | 90000000 | USD | 10-K | 2025-02-25 | 0001234567-25-000010 |
| x_6c05ee279e0f | us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | 2021-01-01 to 2021-12-31 | 20000000 | USD | 10-K | 2023-02-25 | 0001234567-23-000010 |
| x_6cffbfebf115 | us-gaap:AccountsPayableCurrent | at 2024-12-31 | 95400000 (restated; superseded 1) | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_724c3b3e6676 | us-gaap:OperatingIncomeLoss | 2022-01-01 to 2022-12-31 | 97000000 | USD | 10-K | 2024-02-25 | 0001234567-24-000010 |
| x_76135c0fa783 | us-gaap:Assets | at 2026-03-31 | 910000000 | USD | 10-Q | 2026-05-05 | 0001234567-26-000020 |
| x_7a193fcfb9f5 | us-gaap:CostOfRevenue | 2023-01-01 to 2023-12-31 | 700000000 | USD | 10-K | 2025-02-25 | 0001234567-25-000010 |
| x_7e6e74323aa1 | us-gaap:LiabilitiesAndStockholdersEquity | at 2026-03-31 | 910000000 | USD | 10-Q | 2026-05-05 | 0001234567-26-000020 |
| x_7fb4206322e9 | us-gaap:RestructuringCharges | 2025-01-01 to 2025-12-31 | 6000000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_87edd23edd5f | us-gaap:OperatingIncomeLoss | 2021-01-01 to 2021-12-31 | 90000000 | USD | 10-K | 2023-02-25 | 0001234567-23-000010 |
| x_8af62c0a5345 | us-gaap:Revenues | 2021-01-01 to 2021-12-31 | 900000000 | USD | 10-K | 2023-02-25 | 0001234567-23-000010 |
| x_8c640f503ca5 | us-gaap:InterestExpense | 2023-01-01 to 2023-12-31 | 10000000 | USD | 10-K | 2025-02-25 | 0001234567-25-000010 |
| x_8ca9b1787e9a | us-gaap:OperatingIncomeLoss | 2023-01-01 to 2023-12-31 | 104000000 | USD | 10-K | 2025-02-25 | 0001234567-25-000010 |
| x_8e9277bfc018 | us-gaap:AccountsReceivableNetCurrent | at 2025-12-31 | 135120000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_900870721aac | us-gaap:CostOfRevenue | 2025-01-01 to 2025-12-31 | 788200000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_966e2d6c2c3b | us-gaap:IncomeTaxExpenseBenefit | 2022-01-01 to 2022-12-31 | 21750000 | USD | 10-K | 2024-02-25 | 0001234567-24-000010 |
| x_a13ee52a392e | us-gaap:InterestExpense | 2025-01-01 to 2025-12-31 | 10000000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_a176522fc8ee | us-gaap:InventoryNet | at 2022-12-31 | 171000000 | USD | 10-K | 2024-02-25 | 0001234567-24-000010 |
| x_a51c5ab204d1 | us-gaap:Revenues | 2025-01-01 to 2025-12-31 | 1126000000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_a68406a04270 | us-gaap:Revenues | 2024-01-01 to 2024-12-31 | 1060000000 (restated; superseded 1) | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_a8f8b2cf10bb | us-gaap:IncomeTaxExpenseBenefit | 2024-01-01 to 2024-12-31 | 24500000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_ac1f3e50c97d | us-gaap:CostOfRevenue | 2021-01-01 to 2021-12-31 | 630000000 | USD | 10-K | 2023-02-25 | 0001234567-23-000010 |
| x_b3665bce4fa1 | us-gaap:CashAndCashEquivalentsAtCarryingValue | at 2026-03-31 | 55000000 | USD | 10-Q | 2026-05-05 | 0001234567-26-000020 |
| x_bec2ec24bf44 | us-gaap:IncomeTaxExpenseBenefit | 2025-01-01 to 2025-12-31 | 25650000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_cb5d8005bbf4 | us-gaap:DepreciationDepletionAndAmortization | 2022-01-01 to 2022-12-31 | 19000000 | USD | 10-K | 2024-02-25 | 0001234567-24-000010 |
| x_cb71339b5348 | us-gaap:GrossProfit | 2023-01-01 to 2023-12-31 | 300000000 | USD | 10-K | 2025-02-25 | 0001234567-25-000010 |
| x_ced3df6c2995 | us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | 2023-01-01 to 2023-12-31 | 22000000 | USD | 10-K | 2025-02-25 | 0001234567-25-000010 |
| x_d23502bc9009 | us-gaap:PaymentsToAcquirePropertyPlantAndEquipment | 2025-01-01 to 2025-12-31 | 24000000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_d475ce4add54 | us-gaap:InventoryNet | at 2023-12-31 | 180000000 | USD | 10-K | 2025-02-25 | 0001234567-25-000010 |
| x_d78f895fb614 | us-gaap:OperatingIncomeLoss | 2025-01-01 to 2025-12-31 | 112600000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_d961c7d717f1 | us-gaap:GrossProfit | 2021-01-01 to 2021-12-31 | 270000000 | USD | 10-K | 2023-02-25 | 0001234567-23-000010 |
| x_e0397f595e0f | us-gaap:InventoryNet | at 2024-12-31 | 190800000 (restated; superseded 1) | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_e8d34e543878 | us-gaap:GrossProfit | 2025-01-01 to 2025-12-31 | 337800000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_e8d49783b78b | us-gaap:InventoryNet | at 2025-12-31 | 202680000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_ea35a3954a13 | us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest | 2022-01-01 to 2022-12-31 | 87000000 | USD | 10-K | 2024-02-25 | 0001234567-24-000010 |
| x_ea456c414e1d | us-gaap:Revenues | 2022-01-01 to 2022-12-31 | 950000000 | USD | 10-K | 2024-02-25 | 0001234567-24-000010 |
| x_eb1997fe23db | us-gaap:OperatingIncomeLoss | 2024-01-01 to 2024-12-31 | 108000000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_ec352f58d7ed | us-gaap:InterestExpense | 2021-01-01 to 2021-12-31 | 10000000 | USD | 10-K | 2023-02-25 | 0001234567-23-000010 |
| x_ec70bd50633c | us-gaap:AccountsPayableCurrent | at 2022-12-31 | 85500000 | USD | 10-K | 2024-02-25 | 0001234567-24-000010 |
| x_f943066604fa | us-gaap:Revenues | 2023-01-01 to 2023-12-31 | 1000000000 | USD | 10-K | 2025-02-25 | 0001234567-25-000010 |
| x_fc51e247160f | us-gaap:GrossProfit | 2022-01-01 to 2022-12-31 | 285000000 | USD | 10-K | 2024-02-25 | 0001234567-24-000010 |
| x_fc9307aa4da4 | us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest | 2025-01-01 to 2025-12-31 | 102600000 | USD | 10-K | 2026-02-25 | 0001234567-26-000010 |
| x_fca952b88e92 | us-gaap:AccountsPayableCurrent | at 2021-12-31 | 81000000 | USD | 10-K | 2023-02-25 | 0001234567-23-000010 |
| x_fe485c4c3129 | us-gaap:LongTermDebtNoncurrent | at 2026-03-31 | 240000000 | USD | 10-Q | 2026-05-05 | 0001234567-26-000020 |

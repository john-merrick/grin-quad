# distval

A systematic valuation engine for US specialty distributors. Deterministic, reproducible, no spreadsheet in the loop.

## What it does

`distval` values listed specialty distributors from structured driver configs. Analyst judgement lives in version-controlled YAML files; the arithmetic lives in code and never changes per company. Every run produces a JSON snapshot with an input hash — identical inputs always produce identical outputs.

## Requirements

- Python 3.11+
- `EDGAR_IDENTITY` environment variable (your email, required by SEC fair-access policy)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Workflow

The full process for adding and valuing a company runs entirely from the CLI:

```bash
export EDGAR_IDENTITY=you@example.com

# 1. Inspect 10-year filing history
distval ingest GWW --as-of 2024-01-01

# 2. Get mid-cycle summary and normalisation flags
distval normalise GWW --as-of 2024-01-01

# 3. Author distval/companies/gww.yaml (see "Adding a company" below)

# 4. Run the valuation
distval value GWW --net-debt 1234 --shares-diluted 45.2 --price 850 --save
```

`python -m distval` works as an alternative if the console script isn't on your PATH.

---

## Commands

### `distval ingest <ticker>`

Fetches up to N years of 10-K filings from SEC EDGAR and prints a table of as-reported financials. Requires `EDGAR_IDENTITY`.

```
distval ingest POOL --as-of 2024-01-01 --years 10
```

| Flag | Default | Description |
|------|---------|-------------|
| `--as-of YYYY-MM-DD` | today | Only include filings on or before this date |
| `--years N` | `10` | Number of fiscal years to fetch |
| `--save` | off | Write financials as JSON to `./snapshots/` |

---

### `distval normalise <ticker>`

Fetches filing history, computes mid-cycle gross margin, opex %, and average working capital days, and surfaces any normalisation flags. Use this output to guide writing the company YAML.

```
distval normalise POOL --as-of 2024-01-01
```

| Flag | Default | Description |
|------|---------|-------------|
| `--as-of YYYY-MM-DD` | today | Point-in-time cutoff |
| `--years N` | `10` | Years of history (minimum 10 required) |

Flags reported (never auto-corrected):

- `capex_exceeds_da` — capex persistently above D&A for 3+ years
- `inventory_outpacing_revenue` — inventory growing >10pp faster than revenue
- `elevated_gross_margin` — current margin >1.5 std devs above 10-year mean
- `organic_growth_unclear` — acquisitive company with no organic disclosure

---

### `distval value <ticker>`

Runs the DCF from a committed `companies/<ticker>.yaml` config.

```
distval value POOL --net-debt 1469 --shares-diluted 39.5 --price 335 --as-of 2024-02-22
```

```
====================================================
  POOL  |  as of 2024-02-22
====================================================
  Discount rate:        9.0%
  Terminal multiple:    16.11x
  FCF path ($M):        [191  208  230  250  265]
  Terminal value:       $    10,150.9M
  Enterprise value:     $    10,979.2M
  Net debt:             $     1,469.0M
  Minority interest:    $         0.0M
  Equity value:         $     9,510.2M
  Value per share:      $       240.77
  Current price:        $       335.00
  Upside:               -28.1%
====================================================
  input hash:  3f9a...
====================================================
```

| Flag | Required | Description |
|------|----------|-------------|
| `--net-debt M` | yes | Net debt in millions (total_debt − cash) |
| `--shares-diluted M` | yes | Diluted shares outstanding in millions |
| `--minority-interest M` | no (default: 0) | Minority interest in millions |
| `--price USD` | no | Share price; enables upside calculation |
| `--as-of YYYY-MM-DD` | no (default: today) | Valuation date |
| `--save` | no | Write JSON snapshot to `./snapshots/` |

---

## Adding a company

1. Run `distval ingest` and `distval normalise` to anchor the mid-cycle figures.

2. Create `distval/companies/<ticker>.yaml` (lowercase filename, uppercase ticker field):

```yaml
ticker: TICK
forecast_years: 5

revenue: [1000.0, 1050.0, 1100.0, 1155.0, 1213.0]  # len must equal forecast_years
gross_margin: [0.30, 0.30, 0.30, 0.30, 0.30]
opex_pct_sales: [0.20, 0.20, 0.20, 0.20, 0.20]

dio: 60.0    # days inventory outstanding
dso: 45.0    # days sales outstanding
dpo: 50.0    # days payables outstanding

maintenance_capex_pct_sales: 0.02
tax_rate: 0.25

mid_cycle_ebit: 100.0    # key output of your normalisation work
duration_score: 5         # 1–10: how durable are the competitive advantages?
stability_score: 5         # 1–10: through-cycle FCF consistency?

other_elements: {}        # optional bridge items (e.g. surplus assets)
```

3. Run the valuation:

```bash
distval value TICK --net-debt 100 --shares-diluted 50
```

---

## Covered companies

| Ticker | Company | Config |
|--------|---------|--------|
| FAST | Fastenal | `distval/companies/fast.yaml` |
| POOL | Pool Corporation | `distval/companies/pool.yaml` |

---

## Macro assumptions

Shared across all companies. Live in `distval/macro.py`:

| Assumption | Default |
|------------|---------|
| Risk-free rate | 4.0% |
| Equity risk premium | 5.0% → discount rate 9.0% |
| Market average multiple | 15.0x NOPAT |

To override at runtime (Python API only):

```python
from distval.macro import MacroAssumptions
macro = MacroAssumptions(sustainable_risk_free_rate=0.04, equity_risk_premium=0.05, market_avg_multiple=15.0)
```

---

## Running tests

```bash
pytest
# with coverage
pytest --cov=distval --cov-report=term-missing
```

The test suite includes:

- **Golden case** (`test_golden.py`) — hand-computed reference numbers that must never change
- **Property tests** (`test_engine.py`) — higher duration/stability scores increase value; debt reduces equity one-for-one; flat revenue implies zero ΔWC
- **CLI tests** (`test_cli.py`) — parser, exit codes, output fields, snapshot writing, determinism, POOL sanity check
- **Normalisation tests** (`test_normalise.py`) — flag detection logic
- **Schema tests** (`test_schema.py`) — validation rules on `Drivers` and `Financials`

---

## Project layout

```
distval/
  cli.py          # CLI entry point — distval {value,ingest,normalise}
  __main__.py     # python -m distval support
  schema.py       # Pydantic contracts — Financials, Drivers, Valuation
  macro.py        # Shared macro assumptions: RFR, ERP, market multiple
  engine.py       # Pure valuation functions — no I/O, no network, no clock
  loader.py       # Load companies/<ticker>.yaml -> Drivers
  ingest.py       # SEC EDGAR -> Financials (requires EDGAR_IDENTITY)
  normalise.py    # Reported financials -> mid-cycle drivers + flags
  record.py       # Write JSON snapshot per run
  companies/      # One YAML per ticker (fast.yaml, pool.yaml, ...)
  tests/
snapshots/        # Written by --save flag (gitignored)
```

## Design constraints

- `engine.py` performs no I/O — pure functions of their arguments, reproducible by construction
- Discount rate and market multiple live in `macro.py`, never in a company config
- Money values are `Decimal`; rates are `float` — never mixed
- Every ingest call takes an `as_of` date — point-in-time, no lookahead bias
- No terminal growth rate parameter anywhere
- No CAPM, no beta, no company-specific discount rate
- Normalisation flags are reported to the analyst; nothing is auto-corrected

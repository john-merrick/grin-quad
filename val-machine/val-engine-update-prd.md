# PRD — `distval` engine generalisation

**Extending the valuation engine to multiple industries via industry-specific adapters.**

Version 0.2 · Builds on `val-special-distributors-model-prd.md` (v0.1).

---

## 1. Purpose

The v0.1 engine is distributor-specific: the FCF construction logic in
`engine.py` assumes working capital driven by DIO/DSO/DPO, and `Drivers`
carries distributor-native fields. This works for batch one. It does not work
for a SaaS company (no inventory, ARR replaces revenue), a manufacturer (backlog
and utilisation dominate), or a healthcare services business (same-facility
growth, reimbursement rates).

This update introduces a **seam** between the industry-specific layer and the
shared engine. The arithmetic that is universal — discounting a FCF path,
computing a terminal value, bridging to equity — stays in `engine.py`,
unchanged. The arithmetic that varies by industry moves into industry-specific
adapters. Analyst judgement continues to live in YAML, expressed in the native
language of each industry.

### What does not change

- `engine.py` — no I/O, no industry knowledge, pure functions. It becomes
  simpler, not more complex.
- `macro.py` — shared assumptions still apply uniformly across all industries.
- Snapshot determinism — identical inputs, identical hash, identical output.
- The distributor model — existing YAML configs and behaviour are preserved.

### What changes

- A new common intermediate (`EngineInputs`) replaces `Drivers` as the engine's
  only input type.
- Each industry has its own `Drivers` subtype (YAML schema) and its own
  adapter that translates that schema into `EngineInputs`.
- `normalise.py` and `ingest.py` become industry-aware.
- The YAML `industry:` field tells the loader which adapter to route through.

---

## 2. Scope

### In scope

- Define `EngineInputs` as the common intermediate contract
- Refactor `engine.py` to accept `EngineInputs` instead of `Drivers`
- Extract the distributor FCF construction into a `DistributorAdapter` (no
  behaviour change, only structural)
- Define the adapter interface (`BaseAdapter`) and the industry registry
- Implement two additional industry adapters as the first extension batch:
  **Industrial/manufacturing** and **SaaS/software**
- Industry-specific YAML schemas, loaders, and normalisers for those two
- Update the test suite: golden case must pass unchanged; add adapter-level
  unit tests for each new industry

### Out of scope (v0.2)

- Banks, insurers, REITs — methodologically incompatible with a single DCF
  engine; require separate engines and are deferred
- Oil and gas, mining — reserve-life and commodity-pricing mechanics differ
  enough to warrant a separate engine
- Life sciences / pharma — probability-weighted pipeline NPV is a different
  model class
- Any UI, dashboard, or web layer
- Machine learning
- Cross-industry portfolio views

The explicit exclusions are not oversight items — they are industries where the
*methodology itself* changes, not just the inputs. Adding them would break the
invariant that `engine.py` stays untouched.

---

## 3. Architecture

```
distval/
  __init__.py
  macro.py              # unchanged
  schema.py             # EngineInputs added; Drivers becomes DistributorDrivers
  engine.py             # refactored to accept EngineInputs — otherwise unchanged
  loader.py             # routes YAML to the correct adapter via industry: field
  record.py             # unchanged
  ingest.py             # extended to handle additional filing structures
  industries/
    __init__.py
    base.py             # BaseAdapter abstract class
    registry.py         # industry_name -> AdapterClass mapping
    distributor.py      # DistributorAdapter (extracted from current engine.py)
    industrial.py       # IndustrialAdapter (new)
    saas.py             # SaaSAdapter (new)
  companies/
    fast.yaml           # industry: distributor (existing, unchanged)
    pool.yaml           # industry: distributor (existing, unchanged)
    gww.yaml            # industry: distributor (existing, unchanged)
    ...                 # new companies use their industry's schema
  tests/
    test_golden.py      # unchanged — must pass without modification
    test_engine.py
    test_adapter_distributor.py
    test_adapter_industrial.py
    test_adapter_saas.py
    test_normalise.py
    test_schema.py
    test_cli.py
```

### Hard architectural rules (all carry forward from v0.1, with one addition)

1. **`engine.py` performs no I/O.** No file reads, no network, no clock.
2. **`engine.py` contains no industry knowledge.** It must not reference DIO,
   DSO, DPO, ARR, backlog, or any industry-specific concept. If it does,
   the seam is in the wrong place.
3. **Discount rate and market multiple live in `macro.py`.** Applied uniformly.
4. **Money is `Decimal`, rates are `float`.** No mixing.
5. **Every ingest call takes an `as_of` date.** Point-in-time by construction.
6. **No terminal growth rate parameter.** Anywhere.

---

## 4. The seam — `EngineInputs`

This is the central data contract of the update. Every adapter, for every
industry, must produce exactly this:

```python
class EngineInputs(BaseModel):
    """
    The common intermediate. Produced by an industry adapter from its
    Drivers. Consumed by engine.py exclusively. Contains no industry
    concepts — only the quantities the DCF arithmetic needs.
    """
    ticker: str
    forecast_years: int
    fcf_path: list[Decimal]         # len == forecast_years; pre-computed by adapter
    terminal_nopat: Decimal         # mid-cycle NOPAT used for terminal value
    duration_score: int             # 1-10; moves the terminal multiple
    stability_score: int            # 1-10; moves the terminal multiple
    other_elements: dict[str, Decimal] = {}
```

`fcf_path` and `terminal_nopat` are the adapter's responsibility. The engine
does not know how they were derived. It only discounts and bridges.

### What the engine retains

The engine still owns:
- Discounting the FCF path at the macro discount rate
- Computing the terminal multiple from `(duration_score + stability_score)` and
  `macro.market_avg_multiple`
- Computing terminal value as `terminal_nopat * terminal_multiple`, discounted
- Summing to enterprise value and bridging to equity

The engine loses:
- `invested_capital()` — moves to `DistributorAdapter`
- The per-period FCF construction loop — moves to each adapter

The golden test (Section 7 of v0.1) continues to pass because the numbers it
pins are enterprise value and equity value — both computed inside the engine,
both unchanged.

---

## 5. Industry adapter interface

```python
# industries/base.py

from abc import ABC, abstractmethod
from distval.schema import EngineInputs

class BaseAdapter(ABC):

    @abstractmethod
    def to_engine_inputs(self, drivers) -> EngineInputs:
        """
        Translate industry-specific Drivers into the common EngineInputs.
        Must be a pure function of its argument — no I/O, no network, no clock.
        """
        ...

    @abstractmethod
    def drivers_schema(self):
        """Return the Pydantic model class for this industry's Drivers."""
        ...
```

The registry maps the `industry:` string in YAML to an adapter class:

```python
# industries/registry.py

REGISTRY = {
    "distributor":  "distval.industries.distributor.DistributorAdapter",
    "industrial":   "distval.industries.industrial.IndustrialAdapter",
    "saas":         "distval.industries.saas.SaaSAdapter",
}
```

`loader.py` reads `industry:` from the YAML, looks up the adapter, loads the
drivers through that adapter's schema, then calls `to_engine_inputs()`.

---

## 6. Industry-specific YAML schemas

Each industry's `Drivers` model carries only the fields that are meaningful for
that industry. Fields that are irrelevant are absent — not nullable, not zeroed,
absent. This is what lets the YAML stay in the analyst's native language.

### 6.1 Distributor (existing — extracted, not changed)

```yaml
industry: distributor
ticker: FAST
forecast_years: 5

revenue: [1800.0, 1900.0, 2000.0, 2100.0, 2200.0]
gross_margin: [0.51, 0.51, 0.51, 0.51, 0.51]
opex_pct_sales: [0.37, 0.37, 0.37, 0.37, 0.37]

dio: 60.0
dso: 45.0
dpo: 50.0

maintenance_capex_pct_sales: 0.02
tax_rate: 0.25

mid_cycle_ebit: 840.0
duration_score: 8
stability_score: 7
```

FCF construction: `NOPAT + D&A − capex − ΔWOC`, where working capital is
derived from DIO/DSO/DPO. No change to the current logic.

### 6.2 Industrial / manufacturing (new)

Capital base shifts from working capital to fixed assets. Growth investment
shows up in capex, not inventory build. Cycle sensitivity is explicit.

```yaml
industry: industrial
ticker: EXAM
forecast_years: 5

revenue: [500.0, 525.0, 551.0, 579.0, 608.0]
gross_margin: [0.35, 0.35, 0.35, 0.35, 0.35]
opex_pct_sales: [0.20, 0.20, 0.20, 0.20, 0.20]

# Fixed-asset-driven capital base
asset_turnover: 1.8          # revenue / net PP&E; mid-cycle
maintenance_capex_pct_sales: 0.04
growth_capex_pct_rev_growth: 0.30   # incremental capex per $ of new revenue

tax_rate: 0.25
mid_cycle_ebit: 75.0
duration_score: 5
stability_score: 4
```

FCF construction: `NOPAT − maintenance_capex − growth_capex − ΔWOC`, where
ΔWOC is a residual (small for manufacturers vs distributors). `growth_capex`
absorbs the fixed-asset investment that a distributor would never make.

### 6.3 SaaS / software (new)

No inventory, no PP&E of consequence, no traditional working capital cycle.
Capital intensity is people and cloud. The growth/value driver is ARR trajectory
and the margin path as scale accrues.

```yaml
industry: saas
ticker: EXAM
forecast_years: 5

arr: [200.0, 260.0, 320.0, 384.0, 450.0]     # annual recurring revenue
nrr: 0.115                                    # net revenue retention (steady-state)
gross_margin: [0.75, 0.76, 0.77, 0.78, 0.78]
s_and_m_pct_revenue: [0.30, 0.28, 0.25, 0.23, 0.22]
r_and_d_pct_revenue: [0.18, 0.17, 0.16, 0.15, 0.15]
g_and_a_pct_revenue: [0.12, 0.11, 0.10, 0.10, 0.09]

maintenance_capex_pct_revenue: 0.02   # cloud infra; negligible vs revenue
tax_rate: 0.25

mid_cycle_ebit_margin: 0.22           # normalised at scale
mid_cycle_revenue: 450.0              # anchor for terminal NOPAT
duration_score: 7
stability_score: 6
```

FCF construction: revenue derived from ARR, EBIT from revenue minus opex
buckets, FCF from NOPAT minus capex. No working capital adjustment (SaaS
deferred revenue is immaterial at this level of abstraction).

`terminal_nopat = mid_cycle_revenue * mid_cycle_ebit_margin * (1 - tax_rate)`

---

## 7. Normalisation — industry-aware

`normalise.py` is currently distributor-specific. Under this update it becomes
a dispatcher: it reads `industry:` from the company config and routes to the
correct normaliser.

```
normalise.py
  -> normalise_distributor()   # existing logic, extracted
  -> normalise_industrial()    # new
  -> normalise_saas()          # new
```

Each normaliser produces the same outputs (mid-cycle figures and flags) in the
language of its industry. Flags are surfaced, never auto-corrected — this rule
carries forward from v0.1 unconditionally.

### Industrial flags to surface

- Capex-to-revenue ratio >2x the 10-year mean for 3+ consecutive years
- Order backlog declining while management guides growth
- Asset turnover deteriorating alongside margin expansion (mix shift warning)
- Acquisition contribution unquantified in organic growth disclosure

### SaaS flags to surface

- NRR declining for 3+ consecutive quarters in the historical data
- S&M spend accelerating while ARR growth decelerates (efficiency deterioration)
- Gross margin compressing at scale (infrastructure cost not bending)
- Cohort data absent from filings (duration and stability scores require manual
  floor)

---

## 8. Build phases

Each phase has an acceptance criterion. Do not begin the next until it passes.

### Phase 0 — Refactor distributor to adapter pattern

Extract the FCF construction loop from `engine.py` into `DistributorAdapter`.
Introduce `EngineInputs`. Update `engine.py` to accept `EngineInputs`.

**Done when:** the v0.1 golden test passes without modification. No
distributor YAML changes required.

### Phase 1 — Base adapter interface and registry

`BaseAdapter`, `REGISTRY`, updated `loader.py`.

**Done when:** loading an existing distributor YAML routes correctly through
`DistributorAdapter` and produces the same valuation output.

### Phase 2 — Industrial adapter

`IndustrialAdapter`, `IndustrialDrivers` schema, `normalise_industrial()`,
one hand-keyed YAML, adapter unit tests.

**Done when:** a hand-computed industrial golden case passes to two decimal
places.

### Phase 3 — SaaS adapter

`SaaSAdapter`, `SaaSDrivers` schema, `normalise_saas()`, one hand-keyed YAML,
adapter unit tests.

**Done when:** a hand-computed SaaS golden case passes to two decimal places.

### Phase 4 — CLI and snapshot compatibility

`distval value`, `distval ingest`, and `distval normalise` all handle the
`industry:` field transparently. Snapshots record `industry` alongside `ticker`.

**Done when:** CLI tests pass for all three industries; snapshot hashes are
stable.

---

## 9. Testing requirements

All v0.1 testing requirements carry forward. Additions:

- **Adapter contract test** — for each registered adapter, assert that
  `to_engine_inputs()` returns a valid `EngineInputs` given a minimal valid
  drivers dict. Run this automatically for every entry in `REGISTRY`.
- **Seam enforcement test** — assert that `engine.py` imports nothing from
  `distval.industries`. If it does, the seam is broken.
- **Industrial golden case** — hand-computed, same standard as v0.1 Section 7.
- **SaaS golden case** — hand-computed, same standard.
- Property tests extend to new adapters:
  - Increasing duration or stability score never decreases value (all industries)
  - Increasing net debt decreases equity value one-for-one (all industries)
  - For SaaS: higher NRR increases ARR path and therefore equity value

---

## 10. Non-goals and guardrails

All v0.1 non-goals carry forward. Additions:

- **Do not add banks, insurers, or REITs.** The engine assumes a DCF on
  unlevered FCF. These industries break that assumption at the foundation level,
  not at the driver level.
- **Do not add a generic/catch-all adapter.** Every company must belong to a
  named industry with an explicit schema. A generic adapter would accept any
  YAML, defeat validation, and silently produce wrong numbers.
- **Do not merge industry-specific normalisation flags into a shared flag enum.**
  Flag names are meaningful in the language of their industry. A single flat
  list would lose that signal.
- **Do not parameterise the engine by industry.** If `engine.py` ever gains an
  `industry` parameter or an `if industry == "saas"` branch, the seam has
  collapsed and the refactor has failed.

---

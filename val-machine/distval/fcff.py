"""
FCFF discounted at WACC, with a Gordon-growth terminal value. Pure: no I/O, no network, no clock.

Sits beside engine.py (the mid-cycle EBIT × terminal-multiple model). This one serves the
scenario workflow: five-year bear/base/bull forecasts of growth, margin, tax, reinvestment and
working capital, then

    FCFF_t   = EBIT_t × (1 − tax_t) − net_capex_t − ΔNWC_t
    TV_5     = FCFF_6 / (WACC − g)            rejected unless g < WACC
    EV       = Σ FCFF_t / (1 + WACC)^t + TV_5 / (1 + WACC)^5
    equity   = EV − debt − preferred − minority interest − other claims + cash
    per share = equity / diluted shares

Cash flows fall at the end of each forecast year, counted from the valuation date.
Money is Decimal throughout; rates are Decimal fractions.
"""
from dataclasses import dataclass, field, replace
from decimal import Decimal, getcontext

from distval.macro import DEFAULT_MACRO

getcontext().prec = 28

YEARS = 5
TERMINAL_RELIANCE = Decimal("0.75")     # PV(TV) / EV above this is flagged


def D(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _path(v, n: int = YEARS) -> list[Decimal]:
    if isinstance(v, (list, tuple)):
        if len(v) != n:
            raise ValueError(f"expected {n} yearly values, got {len(v)}")
        return [D(x) for x in v]
    return [D(v)] * n


@dataclass(frozen=True)
class Scenario:
    """One set of forecast assumptions. Scalars apply to all five years."""
    name: str
    revenue_growth: list[Decimal]
    ebit_margin: list[Decimal]
    tax_rate: list[Decimal]
    net_capex_pct_revenue: list[Decimal]      # capex − D&A, as a share of revenue
    nwc_pct_revenue: Decimal                  # operating working capital / revenue; ΔNWC follows revenue
    terminal_growth: Decimal
    wacc: Decimal

    @classmethod
    def build(cls, name: str, *, revenue_growth, ebit_margin, tax_rate, net_capex_pct_revenue,
              nwc_pct_revenue, terminal_growth, wacc) -> "Scenario":
        s = cls(name, _path(revenue_growth), _path(ebit_margin), _path(tax_rate),
                _path(net_capex_pct_revenue), D(nwc_pct_revenue), D(terminal_growth), D(wacc))
        s.validate()
        return s

    def validate(self) -> None:
        if not Decimal("0") < self.wacc < Decimal("0.30"):
            raise ValueError(f"{self.name}: WACC {self.wacc} outside (0, 30%)")
        if self.terminal_growth >= self.wacc:
            raise ValueError(f"{self.name}: terminal growth {self.terminal_growth} must be below WACC {self.wacc}")
        for t in self.tax_rate:
            if not Decimal("0") <= t < Decimal("1"):
                raise ValueError(f"{self.name}: tax rate {t} outside [0, 1)")
        for m in self.ebit_margin:
            if not Decimal("-1") < m < Decimal("1"):
                raise ValueError(f"{self.name}: EBIT margin {m} outside (−100%, 100%)")
        for g in self.revenue_growth:
            if g <= Decimal("-1"):
                raise ValueError(f"{self.name}: revenue growth {g} at or below −100%")


@dataclass(frozen=True)
class Bridge:
    """Enterprise value → equity. All in the same currency unit as the forecast."""
    debt: Decimal
    cash: Decimal
    preferred: Decimal = Decimal("0")
    minority_interest: Decimal = Decimal("0")
    other_claims: Decimal = Decimal("0")       # e.g. leases when the analyst treats them as debt
    shares_diluted: Decimal = Decimal("1")

    def __post_init__(self):
        if self.shares_diluted <= 0:
            raise ValueError("diluted shares must be positive")


@dataclass(frozen=True)
class YearRow:
    year: int
    revenue: Decimal
    ebit: Decimal
    nopat: Decimal
    net_capex: Decimal
    delta_nwc: Decimal
    fcff: Decimal
    discount_factor: Decimal
    pv_fcff: Decimal


@dataclass(frozen=True)
class Result:
    scenario: str
    years: list[YearRow]
    terminal_fcff: Decimal
    terminal_value: Decimal
    pv_terminal: Decimal
    pv_forecast: Decimal
    enterprise_value: Decimal
    equity_value: Decimal
    value_per_share: Decimal
    terminal_share: Decimal                      # PV(TV) / EV
    flags: list[str] = field(default_factory=list)


def forecast(s: Scenario, base_revenue: Decimal) -> list[tuple[Decimal, Decimal, Decimal, Decimal, Decimal]]:
    """[(revenue, ebit, nopat, net_capex, ΔNWC)] for years 1..5."""
    rows, prev = [], D(base_revenue)
    for t in range(YEARS):
        rev = prev * (1 + s.revenue_growth[t])
        ebit = rev * s.ebit_margin[t]
        nopat = ebit * (1 - s.tax_rate[t])
        rows.append((rev, ebit, nopat, rev * s.net_capex_pct_revenue[t], (rev - prev) * s.nwc_pct_revenue))
        prev = rev
    return rows


def dcf(s: Scenario, base_revenue: Decimal, bridge: Bridge) -> Result:
    years, pv_sum = [], Decimal("0")
    for t, (rev, ebit, nopat, capex, dnwc) in enumerate(forecast(s, base_revenue), start=1):
        fcff = nopat - capex - dnwc
        df = 1 / (1 + s.wacc) ** t
        years.append(YearRow(t, rev, ebit, nopat, capex, dnwc, fcff, df, fcff * df))
        pv_sum += fcff * df
    last = years[-1]
    # Year 6: revenue grows at g; margin, tax and reinvestment intensity hold at year-5 levels.
    rev6 = last.revenue * (1 + s.terminal_growth)
    nopat6 = rev6 * s.ebit_margin[-1] * (1 - s.tax_rate[-1])
    tfcff = nopat6 - rev6 * s.net_capex_pct_revenue[-1] - (rev6 - last.revenue) * s.nwc_pct_revenue
    tv = tfcff / (s.wacc - s.terminal_growth)
    pv_tv = tv * last.discount_factor
    ev = pv_sum + pv_tv
    equity = ev - bridge.debt - bridge.preferred - bridge.minority_interest - bridge.other_claims + bridge.cash
    share = pv_tv / ev if ev > 0 else Decimal("1")
    flags = []
    if share > TERMINAL_RELIANCE:
        flags.append(f"terminal value is {share:.0%} of enterprise value (above {TERMINAL_RELIANCE:.0%})")
    if tfcff <= 0:
        flags.append("terminal-year FCFF is not positive")
    if ev <= 0:
        flags.append("enterprise value is not positive")
    if s.terminal_growth > D(DEFAULT_MACRO.sustainable_risk_free_rate):
        flags.append(f"terminal growth {s.terminal_growth:.1%} exceeds the sustainable risk-free rate "
                     f"{DEFAULT_MACRO.sustainable_risk_free_rate:.1%}")
    return Result(s.name, years, tfcff, tv, pv_tv, pv_sum, ev, equity, equity / bridge.shares_diluted, share, flags)


@dataclass(frozen=True)
class WaccInputs:
    """Build-up WACC. Weights from market equity and book debt unless a target debt weight is given."""
    risk_free: Decimal = D(DEFAULT_MACRO.sustainable_risk_free_rate)
    equity_risk_premium: Decimal = D(DEFAULT_MACRO.equity_risk_premium)
    beta: Decimal = Decimal("1")
    pre_tax_cost_of_debt: Decimal = Decimal("0.06")
    tax_rate: Decimal = Decimal("0.25")
    target_debt_weight: Decimal | None = None


def wacc(w: WaccInputs, market_equity: Decimal, debt: Decimal) -> dict:
    ke = w.risk_free + w.beta * w.equity_risk_premium
    kd = w.pre_tax_cost_of_debt * (1 - w.tax_rate)
    if w.target_debt_weight is not None:
        wd = w.target_debt_weight
    else:
        total = market_equity + debt
        if total <= 0:
            raise ValueError("market equity plus debt must be positive to weight WACC")
        wd = debt / total
    if not Decimal("0") <= wd < Decimal("1"):
        raise ValueError(f"debt weight {wd} outside [0, 1)")
    return {"cost_of_equity": ke, "after_tax_cost_of_debt": kd, "debt_weight": wd,
            "wacc": (1 - wd) * ke + wd * kd}


def sensitivity(s: Scenario, base_revenue: Decimal, bridge: Bridge,
                wacc_steps=(-0.01, -0.005, 0, 0.005, 0.01),
                growth_steps=(-0.01, -0.005, 0, 0.005, 0.01)) -> dict:
    """Value per share over a WACC × terminal-growth grid. Cells with g ≥ WACC are None."""
    waccs = [s.wacc + D(d) for d in wacc_steps]
    growths = [s.terminal_growth + D(d) for d in growth_steps]
    grid: list[list[Decimal | None]] = []
    for w in waccs:
        row: list[Decimal | None] = []
        for g in growths:
            if g >= w or w <= 0:
                row.append(None)
                continue
            alt = replace(s, terminal_growth=g, wacc=w)
            row.append(dcf(alt, base_revenue, bridge).value_per_share)
        grid.append(row)
    return {"wacc": waccs, "terminal_growth": growths, "value_per_share": grid}


def _solve(f, lo: Decimal, hi: Decimal, target: Decimal, max_iter: int = 100, tol=Decimal("1e-7")):
    """Bisection for f(x) = target on [lo, hi], f increasing. None if not bracketed. Bounded."""
    flo, fhi = f(lo) - target, f(hi) - target
    if flo > 0 or fhi < 0:
        return None
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        fm = f(mid) - target
        if abs(fm) < tol or hi - lo < tol:
            return mid
        lo, hi = (mid, hi) if fm < 0 else (lo, mid)
    return (lo + hi) / 2


def reverse_dcf(s: Scenario, base_revenue: Decimal, bridge: Bridge, price: Decimal) -> dict:
    """What the market price implies, holding everything else in `s` fixed:
    a constant five-year revenue growth rate, and separately a constant EBIT margin."""
    def vps_growth(g: Decimal) -> Decimal:
        return dcf(replace(s, revenue_growth=[g] * YEARS), base_revenue, bridge).value_per_share

    def vps_margin(m: Decimal) -> Decimal:
        return dcf(replace(s, ebit_margin=[m] * YEARS), base_revenue, bridge).value_per_share

    price = D(price)
    # If value doesn't rise with growth (reinvestment outruns margin), the bracket fails and g is None.
    g = _solve(vps_growth, Decimal("-0.30"), Decimal("0.60"), price)
    m = _solve(vps_margin, Decimal("-0.20"), Decimal("0.80"), price)
    return {"price": price, "implied_revenue_growth": g, "implied_ebit_margin": m,
            "note": "constant over five years, all other base-case assumptions held; None = no solution in range"}

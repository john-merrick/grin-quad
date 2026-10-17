"""
Convert reported financial history into mid-cycle drivers and surface flags.
Separate from engine.py — the most likely module to change as normalisation rules evolve.

No auto-adjustment of flags. Flags are reported; a human decides.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
import statistics

from distval.schema import Financials


# ---------------------------------------------------------------------------
# Distributor flags
# ---------------------------------------------------------------------------

@dataclass
class NormalisationFlags:
    """Red flags surfaced to the analyst for distributor businesses."""

    capex_exceeds_da: bool = False
    working_capital_outpacing_revenue: bool = False
    elevated_gross_margin: bool = False
    rising_leverage_falling_roic: bool = False
    inventory_outpacing_revenue: bool = False
    organic_growth_unclear: bool = False

    def any_set(self) -> bool:
        return any(vars(self).values())


# ---------------------------------------------------------------------------
# Industrial/manufacturing flags
# ---------------------------------------------------------------------------

@dataclass
class IndustrialNormalisationFlags:
    """Red flags surfaced to the analyst for industrial/manufacturing businesses."""

    capex_to_revenue_elevated: bool = False
    backlog_declining_vs_guide: bool = False
    asset_turnover_deteriorating: bool = False
    acquisition_contribution_unquantified: bool = False

    def any_set(self) -> bool:
        return any(vars(self).values())


# ---------------------------------------------------------------------------
# SaaS/software flags
# ---------------------------------------------------------------------------

@dataclass
class SaaSNormalisationFlags:
    """Red flags surfaced to the analyst for SaaS/software businesses."""

    nrr_declining: bool = False
    s_and_m_accelerating_vs_arr_growth: bool = False
    gross_margin_compressing_at_scale: bool = False
    cohort_data_absent: bool = False

    def any_set(self) -> bool:
        return any(vars(self).values())


# ---------------------------------------------------------------------------
# Distributor normalisation functions (unchanged from v0.1)
# ---------------------------------------------------------------------------

def mean_gross_margin(history: list[Financials]) -> float:
    """Average gross margin over the full history (minimum 10 years required)."""
    if len(history) < 10:
        raise ValueError(f"Need at least 10 years of history for mid-cycle derivation, got {len(history)}")
    margins = [float(f.gross_profit / f.revenue) for f in history]
    return statistics.mean(margins)


def mean_opex_pct(history: list[Financials]) -> float:
    """Average opex (gross_profit − operating_income) as % of revenue over full history."""
    if len(history) < 10:
        raise ValueError(f"Need at least 10 years of history for mid-cycle derivation, got {len(history)}")
    pcts = [float((f.gross_profit - f.operating_income) / f.revenue) for f in history]
    return statistics.mean(pcts)


def avg_wc_days(history: list[Financials]) -> tuple[float, float, float]:
    """Average DIO, DSO, DPO (days of revenue) over history."""
    if not history:
        raise ValueError("History must not be empty")
    dios = [float(f.inventory / f.revenue * 365) for f in history]
    dsos = [float(f.receivables / f.revenue * 365) for f in history]
    dpos = [float(f.payables / f.revenue * 365) for f in history]
    return statistics.mean(dios), statistics.mean(dsos), statistics.mean(dpos)


# ---------------------------------------------------------------------------
# EV / EBIT history
# ---------------------------------------------------------------------------

@dataclass
class EvEbitPoint:
    year: int
    ebit_m: float        # operating income, $M
    net_debt_m: float    # total_debt − cash, $M
    market_cap_m: float  # shares_diluted × fiscal-year-end price, $M
    ev_m: float          # market_cap + net_debt, $M
    multiple: float      # EV / EBIT


def _prices_for_ticker(ticker: str, start_year: int, end_year: int) -> dict[date, float]:
    """
    Fetch daily closing prices for `ticker` from start_year-01-01 to end_year+1-01-15
    in a single yfinance call. Returns a {date: price} mapping.

    Raises ImportError if yfinance is not installed.
    """
    try:
        import yfinance as yf
    except ImportError:
        raise ImportError(
            "yfinance is required for EV/EBIT computation. "
            "Install with: pip install yfinance"
        )

    hist = yf.Ticker(ticker).history(
        start=f"{start_year}-01-01",
        end=f"{end_year + 1}-01-15",
        interval="1d",
        auto_adjust=True,
    )
    if hist.empty:
        return {}

    price_map: dict[date, float] = {}
    for ts, row in hist.iterrows():
        d = ts.date() if hasattr(ts, "date") else ts
        price_map[d] = float(row["Close"])
    return price_map


def _nearest_price(price_map: dict[date, float], target: date, max_gap: int = 10) -> float | None:
    if not price_map:
        return None
    closest = min(price_map.keys(), key=lambda d: abs((d - target).days))
    if abs((closest - target).days) > max_gap:
        return None
    return price_map[closest]


def compute_ev_ebit_history(history: list[Financials]) -> list[EvEbitPoint]:
    """
    Compute EV/EBIT for each year in `history`.

    EV  = Market Cap + Net Debt
        = (shares_diluted × fiscal-year-end price) + (total_debt − cash)
    EBIT = operating_income

    Fiscal year end is approximated as Dec 31 of `fiscal_year`.
    Loss years (EBIT ≤ 0) are excluded.
    Requires yfinance (single HTTP request for the full price history).
    """
    if not history:
        return []

    ticker = history[0].ticker
    M = 1_000_000

    start_year = min(f.fiscal_year for f in history)
    end_year = max(f.fiscal_year for f in history)
    price_map = _prices_for_ticker(ticker, start_year, end_year)

    points: list[EvEbitPoint] = []
    for f in sorted(history, key=lambda x: x.fiscal_year):
        ebit_raw = float(f.operating_income)
        if ebit_raw <= 0:
            continue

        fy_end = date(f.fiscal_year, 12, 31)
        price = _nearest_price(price_map, fy_end)
        if price is None:
            continue

        net_debt_raw = float(f.total_debt - f.cash)
        market_cap_raw = float(f.shares_diluted) * price
        ev_raw = market_cap_raw + net_debt_raw

        points.append(EvEbitPoint(
            year=f.fiscal_year,
            ebit_m=ebit_raw / M,
            net_debt_m=net_debt_raw / M,
            market_cap_m=market_cap_raw / M,
            ev_m=ev_raw / M,
            multiple=ev_raw / ebit_raw,
        ))

    return points


def ev_ebit_stats(points: list[EvEbitPoint]) -> dict:
    """
    Box-whisker statistics for a list of EvEbitPoints.

    Returns: n, min, p25, median, p75, max, mean
    """
    if not points:
        return {}

    multiples = sorted(p.multiple for p in points)
    n = len(multiples)

    def _pct(data: list[float], p: float) -> float:
        idx = p / 100 * (len(data) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(data) - 1)
        return data[lo] + (idx - lo) * (data[hi] - data[lo])

    return {
        "n": n,
        "min": multiples[0],
        "p25": _pct(multiples, 25),
        "median": _pct(multiples, 50),
        "p75": _pct(multiples, 75),
        "max": multiples[-1],
        "mean": statistics.mean(multiples),
    }


def detect_flags(history: list[Financials]) -> NormalisationFlags:
    """Scan history and return any normalisation flags that need analyst review."""
    flags = NormalisationFlags()

    sorted_h = sorted(history, key=lambda f: f.fiscal_year)

    if len(sorted_h) >= 3:
        capex_above_da_streak = 0
        for f in sorted_h:
            if f.capex > f.d_and_a:
                capex_above_da_streak += 1
            else:
                capex_above_da_streak = 0
        if capex_above_da_streak >= 3:
            flags.capex_exceeds_da = True

    if len(sorted_h) >= 2:
        for i in range(1, len(sorted_h)):
            prev, cur = sorted_h[i - 1], sorted_h[i]
            if prev.revenue == 0 or prev.inventory == 0:
                continue
            rev_growth = float((cur.revenue - prev.revenue) / prev.revenue)
            inv_growth = float((cur.inventory - prev.inventory) / prev.inventory)
            if inv_growth - rev_growth > 0.10:
                flags.inventory_outpacing_revenue = True
                break

    if len(sorted_h) >= 10:
        margins = [float(f.gross_profit / f.revenue) for f in sorted_h]
        mean = statistics.mean(margins)
        stdev = statistics.stdev(margins)
        current = float(sorted_h[-1].gross_profit / sorted_h[-1].revenue)
        if stdev > 0 and (current - mean) / stdev > 1.5:
            flags.elevated_gross_margin = True

    return flags

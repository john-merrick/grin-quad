import os
import statistics
from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class MacroAssumptions:
    sustainable_risk_free_rate: float = 0.04
    equity_risk_premium: float = 0.05
    market_avg_multiple: float = 15.0


DEFAULT_MACRO = MacroAssumptions()


def _fetch_gs10_average(api_key: str, lookback_months: int = 240) -> float:
    """Return the `lookback_months`-month trailing average of GS10 (10-Year Treasury %) from FRED."""
    resp = requests.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={
            "series_id": "GS10",
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": str(lookback_months),
        },
        timeout=10,
    )
    resp.raise_for_status()
    observations = resp.json().get("observations", [])
    values = []
    for obs in observations:
        v = obs.get("value", ".")
        if v != ".":
            try:
                values.append(float(v))
            except ValueError:
                pass
    if not values:
        raise RuntimeError("FRED returned no GS10 data")
    return statistics.mean(values) / 100  # percent → decimal


def fetch_macro(market_avg_multiple: float = 15.0) -> MacroAssumptions:
    """
    Fetch live MacroAssumptions from FRED + environment.

    sustainable_risk_free_rate: 20-year trailing average of FRED series GS10
                                (10-Year Treasury Constant Maturity Rate).
    equity_risk_premium:        FRED_ERP env var; default 0.05.
                                FRED has no direct ERP series — override in .env if needed.
    market_avg_multiple:        caller-supplied, derived from the company's own
                                historical EV/EBIT multiples.

    Requires FRED_API_KEY in environment (see .env.example).
    """
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        raise RuntimeError(
            "FRED_API_KEY not set. Add it to your .env file. "
            "Free key at https://fred.stlouisfed.org/docs/api/api_key.html"
        )
    rfr = _fetch_gs10_average(api_key)
    erp = float(os.environ.get("FRED_ERP", "0.05"))
    return MacroAssumptions(
        sustainable_risk_free_rate=rfr,
        equity_risk_premium=erp,
        market_avg_multiple=market_avg_multiple,
    )

from decimal import Decimal

from pydantic import BaseModel, field_validator, model_validator

from distval.industries.base import BaseAdapter
from distval.schema import EngineInputs


class SaaSDrivers(BaseModel):
    """Analyst judgement for SaaS/software companies."""

    ticker: str
    forecast_years: int

    arr: list[Decimal]
    nrr: float
    gross_margin: list[float]
    s_and_m_pct_revenue: list[float]
    r_and_d_pct_revenue: list[float]
    g_and_a_pct_revenue: list[float]

    maintenance_capex_pct_revenue: float
    tax_rate: float

    mid_cycle_ebit_margin: float
    mid_cycle_revenue: Decimal

    duration_score: int
    stability_score: int

    other_elements: dict[str, Decimal] = {}

    @field_validator("forecast_years")
    @classmethod
    def _min_forecast_years(cls, v: int) -> int:
        if v < 1:
            raise ValueError("forecast_years must be at least 1")
        return v

    @model_validator(mode="after")
    def _list_lengths(self) -> "SaaSDrivers":
        n = self.forecast_years
        if len(self.arr) != n:
            raise ValueError(f"arr must have {n} entries, got {len(self.arr)}")
        if len(self.gross_margin) != n:
            raise ValueError(f"gross_margin must have {n} entries")
        if len(self.s_and_m_pct_revenue) != n:
            raise ValueError(f"s_and_m_pct_revenue must have {n} entries")
        if len(self.r_and_d_pct_revenue) != n:
            raise ValueError(f"r_and_d_pct_revenue must have {n} entries")
        if len(self.g_and_a_pct_revenue) != n:
            raise ValueError(f"g_and_a_pct_revenue must have {n} entries")
        return self

    @field_validator("duration_score", "stability_score")
    @classmethod
    def _score_range(cls, v: int) -> int:
        if not 1 <= v <= 10:
            raise ValueError("score must be between 1 and 10")
        return v

    @field_validator("tax_rate")
    @classmethod
    def _tax_rate_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("tax_rate must be between 0 and 1")
        return v


def _build_fcf_path(drivers: SaaSDrivers) -> list[Decimal]:
    """
    Project FCF over the forecast horizon.

    Revenue = ARR. FCF = NOPAT − maintenance_capex.
    No working capital adjustment (SaaS deferred revenue is immaterial at this level).
    """
    fcf_path: list[Decimal] = []

    for t in range(drivers.forecast_years):
        revenue = drivers.arr[t]
        gross_profit = revenue * Decimal(str(drivers.gross_margin[t]))
        total_opex = revenue * (
            Decimal(str(drivers.s_and_m_pct_revenue[t]))
            + Decimal(str(drivers.r_and_d_pct_revenue[t]))
            + Decimal(str(drivers.g_and_a_pct_revenue[t]))
        )
        ebit = gross_profit - total_opex
        nopat = ebit * (1 - Decimal(str(drivers.tax_rate)))
        capex = revenue * Decimal(str(drivers.maintenance_capex_pct_revenue))

        fcf_path.append(nopat - capex)

    return fcf_path


class SaaSAdapter(BaseAdapter):

    def coerce_raw(self, raw: dict) -> dict:
        raw = dict(raw)
        raw["arr"] = [Decimal(str(v)) for v in raw["arr"]]
        raw["mid_cycle_revenue"] = Decimal(str(raw["mid_cycle_revenue"]))
        if "other_elements" in raw:
            raw["other_elements"] = {k: Decimal(str(v)) for k, v in raw["other_elements"].items()}
        return raw

    def drivers_schema(self):
        return SaaSDrivers

    def to_engine_inputs(self, drivers: SaaSDrivers) -> EngineInputs:
        terminal_nopat = (
            drivers.mid_cycle_revenue
            * Decimal(str(drivers.mid_cycle_ebit_margin))
            * (1 - Decimal(str(drivers.tax_rate)))
        )
        return EngineInputs(
            industry="saas",
            ticker=drivers.ticker,
            forecast_years=drivers.forecast_years,
            fcf_path=_build_fcf_path(drivers),
            terminal_nopat=terminal_nopat,
            duration_score=drivers.duration_score,
            stability_score=drivers.stability_score,
            other_elements=drivers.other_elements,
        )

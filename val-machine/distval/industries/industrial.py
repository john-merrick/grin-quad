from decimal import Decimal

from pydantic import BaseModel, field_validator, model_validator

from distval.industries.base import BaseAdapter
from distval.schema import EngineInputs


class IndustrialDrivers(BaseModel):
    """Analyst judgement for industrial/manufacturing companies."""

    ticker: str
    forecast_years: int

    revenue: list[Decimal]
    gross_margin: list[float]
    opex_pct_sales: list[float]

    # Used by normalise_industrial() to derive mid-cycle PP&E and flag asset
    # turnover deterioration. Not consumed by the FCF construction path.
    asset_turnover: float
    maintenance_capex_pct_sales: float
    growth_capex_pct_rev_growth: float

    tax_rate: float

    mid_cycle_ebit: Decimal
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
    def _list_lengths(self) -> "IndustrialDrivers":
        n = self.forecast_years
        if len(self.revenue) != n:
            raise ValueError(f"revenue must have {n} entries, got {len(self.revenue)}")
        if len(self.gross_margin) != n:
            raise ValueError(f"gross_margin must have {n} entries")
        if len(self.opex_pct_sales) != n:
            raise ValueError(f"opex_pct_sales must have {n} entries")
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


def _build_fcf_path(drivers: IndustrialDrivers) -> list[Decimal]:
    """
    Project FCF over the forecast horizon.

    FCF = NOPAT − maintenance_capex − growth_capex
    growth_capex absorbs fixed-asset investment per incremental revenue dollar.
    Cold start: year-1 growth_capex = 0 (no prior-year revenue known).
    Asset disposals on revenue contraction are excluded by design — analysts
    should reflect divestiture proceeds in other_elements if material.
    """
    fcf_path: list[Decimal] = []
    prev_revenue = drivers.revenue[0]  # cold-start proxy

    for t in range(drivers.forecast_years):
        rev = drivers.revenue[t]
        ebit = rev * Decimal(str(drivers.gross_margin[t])) - rev * Decimal(str(drivers.opex_pct_sales[t]))
        nopat = ebit * (1 - Decimal(str(drivers.tax_rate)))

        maint_capex = rev * Decimal(str(drivers.maintenance_capex_pct_sales))
        rev_growth = rev - prev_revenue
        growth_capex = rev_growth * Decimal(str(drivers.growth_capex_pct_rev_growth)) if rev_growth > 0 else Decimal("0")
        prev_revenue = rev

        fcf_path.append(nopat - maint_capex - growth_capex)

    return fcf_path


class IndustrialAdapter(BaseAdapter):

    def coerce_raw(self, raw: dict) -> dict:
        raw = dict(raw)
        raw["revenue"] = [Decimal(str(v)) for v in raw["revenue"]]
        raw["mid_cycle_ebit"] = Decimal(str(raw["mid_cycle_ebit"]))
        if "other_elements" in raw:
            raw["other_elements"] = {k: Decimal(str(v)) for k, v in raw["other_elements"].items()}
        return raw

    def drivers_schema(self):
        return IndustrialDrivers

    def to_engine_inputs(self, drivers: IndustrialDrivers) -> EngineInputs:
        return EngineInputs(
            industry="industrial",
            ticker=drivers.ticker,
            forecast_years=drivers.forecast_years,
            fcf_path=_build_fcf_path(drivers),
            terminal_nopat=drivers.mid_cycle_ebit * (1 - Decimal(str(drivers.tax_rate))),
            duration_score=drivers.duration_score,
            stability_score=drivers.stability_score,
            other_elements=drivers.other_elements,
        )

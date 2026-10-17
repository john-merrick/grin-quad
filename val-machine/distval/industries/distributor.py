from decimal import Decimal

from distval.industries.base import BaseAdapter
from distval.schema import DistributorDrivers, EngineInputs


def _working_capital(revenue: Decimal, dio: float, dso: float, dpo: float) -> Decimal:
    inventory = revenue * Decimal(dio) / 365
    receivables = revenue * Decimal(dso) / 365
    payables = revenue * Decimal(dpo) / 365
    return inventory + receivables - payables


def _build_fcf_path(drivers: DistributorDrivers) -> list[Decimal]:
    """
    Project FCF over the forecast horizon.

    D&A is assumed equal to maintenance capex (they cancel), so
    FCF = NOPAT − ΔWOC where WOC is working-capital driven by DIO/DSO/DPO.
    """
    fcf_path: list[Decimal] = []

    # Cold start: use revenue[0] as the t=0 proxy so year-1 ΔWOC = 0.
    prev_wc = _working_capital(drivers.revenue[0], drivers.dio, drivers.dso, drivers.dpo)

    for t in range(drivers.forecast_years):
        rev = drivers.revenue[t]
        ebit = rev * Decimal(str(drivers.gross_margin[t])) - rev * Decimal(str(drivers.opex_pct_sales[t]))
        nopat = ebit * (1 - Decimal(str(drivers.tax_rate)))

        cur_wc = _working_capital(rev, drivers.dio, drivers.dso, drivers.dpo)
        delta_wc = cur_wc - prev_wc
        prev_wc = cur_wc

        fcf_path.append(nopat - delta_wc)

    return fcf_path


class DistributorAdapter(BaseAdapter):

    def coerce_raw(self, raw: dict) -> dict:
        raw = dict(raw)
        raw["revenue"] = [Decimal(str(v)) for v in raw["revenue"]]
        raw["mid_cycle_ebit"] = Decimal(str(raw["mid_cycle_ebit"]))
        if "other_elements" in raw:
            raw["other_elements"] = {k: Decimal(str(v)) for k, v in raw["other_elements"].items()}
        return raw

    def drivers_schema(self):
        return DistributorDrivers

    def to_engine_inputs(self, drivers: DistributorDrivers) -> EngineInputs:
        return EngineInputs(
            industry="distributor",
            ticker=drivers.ticker,
            forecast_years=drivers.forecast_years,
            fcf_path=_build_fcf_path(drivers),
            terminal_nopat=drivers.mid_cycle_ebit * (1 - Decimal(str(drivers.tax_rate))),
            duration_score=drivers.duration_score,
            stability_score=drivers.stability_score,
            other_elements=drivers.other_elements,
        )

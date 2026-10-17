"""
Pure valuation functions. No I/O, no network, no clock.
All inputs come in; all outputs come out. Reproducible by construction.
"""
from decimal import Decimal, getcontext
from datetime import date
import hashlib
import json

from distval.macro import MacroAssumptions
from distval.schema import EngineInputs, Valuation

getcontext().prec = 28


def discount_rate(macro: MacroAssumptions) -> float:
    return macro.sustainable_risk_free_rate + macro.equity_risk_premium


def terminal_multiple(market_avg_multiple: float, duration_score: int, stability_score: int) -> float:
    combined = (duration_score + stability_score) / 2
    adjustment = 0.40 * (combined - 5.5) / 4.5
    return market_avg_multiple * (1 + adjustment)


def _pv_of_annuity(cash_flows: list[Decimal], rate: float) -> Decimal:
    r = Decimal(str(rate))
    total = Decimal("0")
    for t, cf in enumerate(cash_flows, start=1):
        total += cf / (1 + r) ** t
    return total


def _input_hash(
    engine_inputs: EngineInputs,
    macro: MacroAssumptions,
    net_debt: Decimal,
    minority_interest: Decimal,
    shares_diluted: Decimal,
) -> str:
    payload = json.dumps(
        {
            "engine_inputs": engine_inputs.model_dump(mode="json"),
            "macro": {
                "rfr": macro.sustainable_risk_free_rate,
                "erp": macro.equity_risk_premium,
                "mkt": macro.market_avg_multiple,
            },
            "net_debt": str(net_debt),
            "minority_interest": str(minority_interest),
            "shares_diluted": str(shares_diluted),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def value(
    engine_inputs: EngineInputs,
    macro: MacroAssumptions,
    net_debt: Decimal,
    minority_interest: Decimal,
    shares_diluted: Decimal,
    price: Decimal | None,
    as_of: date,
) -> Valuation:
    if shares_diluted <= 0:
        raise ValueError("shares_diluted must be positive")

    dr = discount_rate(macro)
    tm = terminal_multiple(
        macro.market_avg_multiple,
        engine_inputs.duration_score,
        engine_inputs.stability_score,
    )

    pv_fcf = _pv_of_annuity(engine_inputs.fcf_path, dr)

    tv = engine_inputs.terminal_nopat * Decimal(str(tm))
    n = engine_inputs.forecast_years
    pv_tv = tv / (1 + Decimal(str(dr))) ** n

    ev = pv_fcf + pv_tv

    other_total = sum(engine_inputs.other_elements.values(), Decimal("0"))
    equity = ev + other_total - net_debt - minority_interest
    vps = equity / shares_diluted

    upside: float | None = None
    if price is not None and price > 0:
        upside = float((vps - price) / price * 100)

    ih = _input_hash(engine_inputs, macro, net_debt, minority_interest, shares_diluted)

    return Valuation(
        industry=engine_inputs.industry,
        ticker=engine_inputs.ticker,
        as_of=as_of,
        input_hash=ih,
        fcf_path=engine_inputs.fcf_path,
        discount_rate=dr,
        terminal_multiple=tm,
        terminal_value=tv,
        enterprise_value=ev,
        other_elements_total=other_total,
        net_debt=net_debt,
        minority_interest=minority_interest,
        equity_value=equity,
        value_per_share=vps,
        price=price,
        upside_pct=upside,
    )

"""
Ingest financial data from SEC EDGAR filings via edgartools.
Populates Financials objects from 10-K filings, point-in-time by construction.
All calls take an as_of date — never silently pull restated figures.
"""
import logging
import os
from datetime import date
from decimal import Decimal

from distval.schema import Financials

logger = logging.getLogger(__name__)


def fetch_financials(ticker: str, as_of: date, years: int = 10) -> list[Financials]:
    """
    Fetch up to `years` years of 10-K Financials for `ticker` as of `as_of`.

    Only filings with a filed_date <= as_of are returned, enforcing point-in-time
    discipline. No lookahead bias.

    Requires EDGAR_IDENTITY env var (your email, per SEC fair-access policy).

    Raises:
        ValueError: if the company does not file a 10-K (e.g. foreign filers)
        RuntimeError: if the EDGAR connection fails
    """
    try:
        import edgar  # edgartools
    except ImportError as e:
        raise ImportError("edgartools is required for ingest. Install with: pip install edgartools") from e

    identity = os.environ.get("EDGAR_IDENTITY")
    if not identity:
        raise RuntimeError(
            "Set the EDGAR_IDENTITY environment variable to your email address. "
            "This is required by the SEC fair-access policy. "
            "Example: export EDGAR_IDENTITY=you@example.com"
        )
    edgar.set_identity(identity)

    company = edgar.Company(ticker)
    filings_10k = company.get_filings(form="10-K")

    results: list[Financials] = []
    skipped = 0
    for filing in filings_10k:
        if filing.filing_date > as_of:
            continue

        try:
            fin = _parse_10k(ticker, filing)
        except Exception as exc:
            logger.warning("Skipping filing %s for %s: %s", filing.filing_date, ticker, exc)
            skipped += 1
            continue

        results.append(fin)
        if len(results) >= years:
            break

    if skipped:
        logger.info("Skipped %d filing(s) for %s due to parse errors", skipped, ticker)

    results.sort(key=lambda f: f.fiscal_year, reverse=True)
    return results


def _parse_10k(ticker: str, filing) -> Financials:
    """Extract Financials from a single 10-K filing object."""
    from datetime import date as _date

    tenk = filing.obj()
    fin = tenk.financials

    # period_of_report is a str in edgartools ≥5; normalise to date
    _por = filing.period_of_report
    period = _date.fromisoformat(_por) if isinstance(_por, str) else _por
    period_str = str(period)

    _inc = fin.income_statement()
    _bal = fin.balance_sheet()
    _cf = fin.cash_flow_statement()
    if _inc is None or _bal is None or _cf is None:
        raise ValueError("No XBRL financial statements found (pre-XBRL filing?)")

    income_raw = _inc.get_raw_data()
    balance_raw = _bal.get_raw_data()
    cf_raw = _cf.get_raw_data()

    def _best_value(raw_data: list, *concepts: str) -> float | None:
        """Return the full-year value for the first matching concept.

        Picks the value key containing period_str with the longest duration,
        so a quarterly Q4 entry is never chosen over the annual figure.
        """
        for concept in concepts:
            for item in raw_data:
                if item.get("is_abstract", False):
                    continue
                if item.get("concept") != concept:
                    continue
                values = item.get("values", {})
                best_val, best_dur = None, -1
                for key, val in values.items():
                    if val is None or period_str not in key:
                        continue
                    parts = key.split("_")
                    if parts[0] == "duration" and len(parts) == 3:
                        try:
                            dur = (_date.fromisoformat(parts[2]) - _date.fromisoformat(parts[1])).days
                        except ValueError:
                            dur = 0
                    else:
                        dur = 0  # instant (balance sheet) or unrecognised format
                    if dur > best_dur:
                        best_dur, best_val = dur, val
                if best_val is not None:
                    return best_val
        return None

    def _d(val: float | None) -> Decimal:
        if val is None:
            return Decimal("0")
        return Decimal(str(val))

    # Total debt = current LTD + non-current LTD + short-term borrowings
    ltd_current = _best_value(balance_raw, "us-gaap_LongTermDebtCurrent") or 0.0
    ltd_noncurrent = _best_value(balance_raw, "us-gaap_LongTermDebtNoncurrent") or 0.0
    short_term = _best_value(balance_raw, "us-gaap_ShortTermBorrowings", "us-gaap_DebtCurrent") or 0.0

    return Financials(
        ticker=ticker,
        fiscal_year=period.year,
        filed_date=filing.filing_date,
        revenue=_d(_best_value(
            income_raw,
            "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
            "us-gaap_Revenues",
            "us-gaap_SalesRevenueNet",
        )),
        gross_profit=_d(_best_value(income_raw, "us-gaap_GrossProfit")),
        operating_income=_d(_best_value(income_raw, "us-gaap_OperatingIncomeLoss")),
        d_and_a=_d(_best_value(
            cf_raw,
            "us-gaap_DepreciationAndAmortization",
            "us-gaap_DepreciationDepletionAndAmortization",
        )),
        capex=abs(_d(_best_value(cf_raw, "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment"))),
        inventory=_d(_best_value(balance_raw, "us-gaap_InventoryNet", "us-gaap_InventoryGross")),
        receivables=_d(_best_value(
            balance_raw,
            "us-gaap_AccountsReceivableNetCurrent",
            "us-gaap_ReceivablesNetCurrent",
        )),
        payables=_d(_best_value(
            balance_raw,
            "us-gaap_AccountsPayableTradeCurrentAndNoncurrent",
            "us-gaap_AccountsPayableCurrent",
        )),
        net_ppe=_d(_best_value(balance_raw, "us-gaap_PropertyPlantAndEquipmentNet")),
        total_debt=_d(ltd_current + ltd_noncurrent + short_term),
        cash=_d(_best_value(
            balance_raw,
            "us-gaap_CashAndCashEquivalentsAtCarryingValue",
            "us-gaap_CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        )),
        minority_interest=_d(_best_value(balance_raw, "us-gaap_MinorityInterest")),
        shares_diluted=_d(_best_value(
            income_raw,
            "us-gaap_WeightedAverageNumberOfDilutedSharesOutstanding",
        )),
        lifo_reserve=None,
    )

"""
Synthetic SEC responses for the offline valuation path: ACME (a distributor) and BANK (a bank).
Hand-checkable numbers, in USD millions before scaling:

    FY        2021   2022   2023   2024   2025
    revenue    900    950   1000   1060   1126     FY2024 first filed as 1058, restated to 1060
    op income   90     97    104    108   112.6    FY2025 includes a 6.0 restructuring charge
    gross margin 30%; interest 10; tax 25% of pretax; D&A 18/19/19.5/20/21; capex 20/21/22/23/24
    receivables 12%, inventory 18%, payables 9% of revenue at each year end
    2026-03-31 (10-Q filed 2026-05-05): cash 55, debt 240 + 10, diluted shares 39.8m
    2026-06-30 (10-Q filed 2026-07-30): cash 70, after the 2026-06-30 valuation date: must be excluded

    python -m grinos.tests.sec_fixture examples/valuation/sec     # write the files for --offline
"""
import json
import sys
from pathlib import Path

from grinos import sec

ACME_CIK, BANK_CIK = 1234567, 7654321
M = 1_000_000
YEARS = {2021: (900, 90, 18, 20, 0), 2022: (950, 97, 19, 21, 0), 2023: (1000, 104, 19.5, 22, 0),
         2024: (1060, 108, 20, 23, 0), 2025: (1126, 112.6, 21, 24, 6.0)}
TEN_K = {y: (f"0001234567-{str(y + 1)[2:]}-000010", f"{y + 1}-02-25") for y in YEARS}
Q1 = ("0001234567-26-000020", "2026-05-05")
Q2 = ("0001234567-26-000030", "2026-07-30")


def _row(val, accn, filed, form, end, start=None, fy=None, fp="FY"):
    r = {"end": end, "val": round(val * M) if isinstance(val, float) else val * M, "accn": accn,
         "fy": fy, "fp": fp, "form": form, "filed": filed}
    if start:
        r["start"] = start
    return r


def acme_companyfacts() -> dict:
    g: dict[str, list] = {}

    def put(concept, row, unit="USD"):
        g.setdefault(concept, {"units": {}})["units"].setdefault(unit, []).append(row)

    for y, (accn, filed) in TEN_K.items():
        for yy in (y - 1, y):                                  # each 10-K carries the prior year too
            if yy not in YEARS:
                continue
            rev, oi, da, capex, restr = YEARS[yy]
            if yy == 2024 and y == 2024:
                rev = 1058                                     # as first filed; restated a year later
            start, end = f"{yy}-01-01", f"{yy}-12-31"
            pre = oi - 10
            flows = {"Revenues": rev, "CostOfRevenue": rev * 0.7, "GrossProfit": rev * 0.3,
                     "OperatingIncomeLoss": oi,
                     "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": pre,
                     "IncomeTaxExpenseBenefit": pre * 0.25, "DepreciationDepletionAndAmortization": da,
                     "PaymentsToAcquirePropertyPlantAndEquipment": capex, "InterestExpense": 10}
            if restr:
                flows["RestructuringCharges"] = restr
            for c, v in flows.items():
                put(c, _row(float(v), accn, filed, "10-K", end, start, fy=y))
            put("WeightedAverageNumberOfDilutedSharesOutstanding",
                _row(40.0, accn, filed, "10-K", end, start, fy=y), "shares")
            for c, v in {"AccountsReceivableNetCurrent": rev * 0.12, "InventoryNet": rev * 0.18,
                         "AccountsPayableCurrent": rev * 0.09, "CashAndCashEquivalentsAtCarryingValue": 50,
                         "LongTermDebtNoncurrent": 240, "LongTermDebtCurrent": 10,
                         "Assets": 900, "LiabilitiesAndStockholdersEquity": 900}.items():
                put(c, _row(float(v), accn, filed, "10-K", end, fy=y))
    for (accn, filed), end, cash in ((Q1, "2026-03-31", 55), (Q2, "2026-06-30", 70)):
        qstart = end[:5] + ("01-01" if end.endswith("03-31") else "04-01")
        put("Revenues", _row(290.0, accn, filed, "10-Q", end, qstart, fy=2026, fp="Q1"))
        put("WeightedAverageNumberOfDilutedSharesOutstanding",
            _row(39.8, accn, filed, "10-Q", end, qstart, fy=2026, fp="Q1"), "shares")
        for c, v in {"CashAndCashEquivalentsAtCarryingValue": cash, "LongTermDebtNoncurrent": 240,
                     "LongTermDebtCurrent": 10, "Assets": 910, "LiabilitiesAndStockholdersEquity": 910,
                     "OperatingLeaseLiability": 30}.items():
            put(c, _row(float(v), accn, filed, "10-Q", end, fy=2026, fp="Q1"))
    return {"cik": ACME_CIK, "entityName": "ACME DISTRIBUTION CO", "facts": {"us-gaap": g}}


def _submissions(cik, name, sic, desc, filings) -> dict:
    return {"cik": str(cik), "name": name, "sic": sic, "sicDescription": desc, "tickers": [name.split()[0]],
            "exchanges": ["NYSE"], "fiscalYearEnd": "1231",
            "filings": {"recent": {"form": [f[0] for f in filings], "accessionNumber": [f[1] for f in filings],
                                   "filingDate": [f[2] for f in filings], "reportDate": [f[3] for f in filings]}}}


def acme_submissions(extra: list | None = None) -> dict:
    filings = [("10-K", a, f, f"{y}-12-31") for y, (a, f) in TEN_K.items()]
    filings += [("10-Q", Q1[0], Q1[1], "2026-03-31"), ("10-Q", Q2[0], Q2[1], "2026-06-30"),
                ("8-K", "0001234567-26-000025", "2026-05-06", "")] + (extra or [])
    return _submissions(ACME_CIK, "ACME DISTRIBUTION CO", "5070",
                        "Wholesale-Hardware & Plumbing & Heating Equipment & Supplies", filings)


def bank_submissions() -> dict:
    return _submissions(BANK_CIK, "BANK FIRST EXAMPLE BANCORP", "6022", "State Commercial Banks",
                        [("10-K", "0007654321-26-000001", "2026-02-20", "2025-12-31")])


TICKERS = {"0": {"cik_str": ACME_CIK, "ticker": "ACME", "title": "ACME DISTRIBUTION CO"},
           "1": {"cik_str": BANK_CIK, "ticker": "BANK", "title": "BANK FIRST EXAMPLE BANCORP"}}


def responses(submissions: dict | None = None, companyfacts: dict | None = None) -> dict[str, bytes]:
    return {sec.TICKERS_URL: json.dumps(TICKERS).encode(),
            sec.submissions_url(ACME_CIK): json.dumps(submissions or acme_submissions()).encode(),
            sec.companyfacts_url(ACME_CIK): json.dumps(companyfacts or acme_companyfacts()).encode(),
            sec.submissions_url(BANK_CIK): json.dumps(bank_submissions()).encode()}


def write(folder: Path) -> None:
    for url, body in responses().items():
        path = folder / sec.offline_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps(json.loads(body), indent=1, sort_keys=True).encode() + b"\n")


if __name__ == "__main__":
    write(Path(sys.argv[1]))

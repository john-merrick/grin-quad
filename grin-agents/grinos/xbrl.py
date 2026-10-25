"""
SEC companyfacts (XBRL) → point-in-time reported financials, with provenance. Deterministic.

    as_of(companyfacts, date)   keep only facts FILED on or before the valuation date
    reconcile(snapshot, cik)    pick each material item, check reported totals, list review items

Each item is a list of formulas (sums of us-gaap concepts), tried in order. Within a concept and
period, the latest-filed fact wins (amendments and restatements supersede); superseded values are
kept. Flows use annual periods by actual duration (350–380 days), not the filing's fiscal-period
label, because a 10-K also carries prior-year comparatives. Every chosen fact keeps concept,
period, accession, form, filed date and filing URL.

Review items (`material: true` blocks the valuation until an analyst resolves them):
    missing         a material item has no reported value
    conflict        two available formulas for the same item disagree by more than 1%
    total_mismatch  a reported total doesn't equal its parts (gross profit, balance sheet)
    currency        a material item is not reported in USD
Restatements are informational. Values are converted to millions (USD, shares).
"""
import hashlib
import json
from datetime import date
from decimal import Decimal

from grinos.sec import filing_url

MILLION = Decimal(1_000_000)
TOLERANCE = Decimal("0.01")
HISTORY_YEARS = 5

FLOWS: dict[str, list[tuple[str, ...]]] = {
    "revenue": [("Revenues",), ("RevenueFromContractWithCustomerExcludingAssessedTax",), ("SalesRevenueNet",),
                ("RevenueFromContractWithCustomerIncludingAssessedTax",)],
    "cost_of_revenue": [("CostOfRevenue",), ("CostOfGoodsAndServicesSold",), ("CostOfGoodsSold",)],
    "gross_profit": [("GrossProfit",)],
    "operating_income": [("OperatingIncomeLoss",)],
    "pretax_income": [("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",),
                      ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",)],
    "income_tax": [("IncomeTaxExpenseBenefit",)],
    "d_and_a": [("DepreciationDepletionAndAmortization",), ("DepreciationAndAmortization",),
                ("DepreciationAmortizationAndAccretionNet",), ("Depreciation", "AmortizationOfIntangibleAssets")],
    "capex": [("PaymentsToAcquirePropertyPlantAndEquipment",), ("PaymentsToAcquireProductiveAssets",)],
    "interest_expense": [("InterestExpense",), ("InterestExpenseNonoperating",), ("InterestExpenseDebt",)],
    # one-offs inside operating income: candidates for a normalisation adjustment
    "goodwill_impairment": [("GoodwillImpairmentLoss",)],
    "asset_impairment": [("AssetImpairmentCharges",)],
    "restructuring": [("RestructuringCharges",)],
}
INSTANTS: dict[str, list[tuple[str, ...]]] = {
    "cash": [("CashAndCashEquivalentsAtCarryingValue",),
             ("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",)],
    "debt": [("DebtLongtermAndShorttermCombinedAmount",), ("LongTermDebt", "ShortTermBorrowings"),
             ("LongTermDebtNoncurrent", "LongTermDebtCurrent", "ShortTermBorrowings"),
             ("LongTermDebtNoncurrent", "LongTermDebtCurrent"), ("LongTermDebt",), ("LongTermDebtNoncurrent",)],
    "receivables": [("AccountsReceivableNetCurrent",), ("ReceivablesNetCurrent",)],
    "inventory": [("InventoryNet",)],
    "payables": [("AccountsPayableCurrent",), ("AccountsPayableTradeCurrent",)],
    "minority_interest": [("MinorityInterest",)],
    "preferred": [("PreferredStockValue",)],
    "operating_leases": [("OperatingLeaseLiability",), ("OperatingLeaseLiabilityCurrent", "OperatingLeaseLiabilityNoncurrent")],
    "assets": [("Assets",)],
    "liabilities_and_equity": [("LiabilitiesAndStockholdersEquity",)],
}
SHARES: dict[str, list[tuple[str, ...]]] = {"diluted_shares": [("WeightedAverageNumberOfDilutedSharesOutstanding",)]}

MATERIAL = {"revenue", "operating_income", "cash", "debt", "diluted_shares"}
# Not reported means the company has none to report; disclosed as zero, never invented.
ZERO_IF_ABSENT = {"minority_interest", "preferred", "goodwill_impairment", "asset_impairment", "restructuring"}


def as_of(companyfacts: dict, valuation_date: date) -> dict:
    """The facts an analyst could have seen on `valuation_date`: filed on or before it."""
    cutoff = valuation_date.isoformat()
    out = {"cik": companyfacts["cik"], "entityName": companyfacts.get("entityName"), "as_of": cutoff, "facts": {}}
    for tax, concepts in companyfacts.get("facts", {}).items():
        for concept, body in concepts.items():
            units = {u: sorted((r for r in rows if r.get("filed", "9999") <= cutoff),
                               key=lambda r: (r.get("end", ""), r.get("start", ""), r["filed"], r["accn"]))
                     for u, rows in body.get("units", {}).items()}
            units = {u: rows for u, rows in units.items() if rows}
            if units:
                out["facts"].setdefault(tax, {})[concept] = {"units": units}
    return out


def canonical_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _days(r: dict) -> int:
    if "start" not in r:
        return 0
    return (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days


def _fact_id(concept: str, r: dict) -> str:
    return "x_" + hashlib.sha256(f"{concept}|{r.get('start', '')}|{r['end']}|{r['accn']}".encode()).hexdigest()[:12]


class _Index:
    """concept → period key → facts, for one unit family."""

    def __init__(self, snapshot: dict, cik: int):
        self.cik = cik
        self.gaap = snapshot["facts"].get("us-gaap", {})
        self.facts: dict[str, dict] = {}

    def units(self, concept: str) -> dict:
        return self.gaap.get(concept, {}).get("units", {})

    def pick(self, concept: str, unit: str, key) -> dict | None:
        """Latest-filed fact for (concept, period key); records it with superseded values."""
        rows = [r for r in self.units(concept).get(unit, []) if self._key(r, key) == key]
        if not rows:
            return None
        rows.sort(key=lambda r: (r["filed"], r.get("form", "").endswith("/A"), r["accn"]))
        best = rows[-1]
        fid = _fact_id(concept, best)
        superseded = [{"accn": r["accn"], "filed": r["filed"], "form": r.get("form"), "value": str(r["val"])}
                      for r in rows[:-1] if Decimal(str(r["val"])) != Decimal(str(best["val"]))]
        self.facts[fid] = {"concept": f"us-gaap:{concept}", "value": str(best["val"]), "unit": unit,
                           "start": best.get("start"), "end": best["end"], "accn": best["accn"],
                           "form": best.get("form"), "filed": best["filed"], "fy": best.get("fy"), "fp": best.get("fp"),
                           "url": filing_url(self.cik, best["accn"]), "superseded": superseded}
        return {"id": fid, "value": Decimal(str(best["val"]))}

    @staticmethod
    def _key(r: dict, key):
        return (r.get("start"), r["end"]) if isinstance(key, tuple) else (None if "start" in r else r["end"])


def _evaluate(ix: _Index, formulas, unit: str, key) -> list[dict]:
    """Every formula fully reported for `key`: [{formula, value (millions), fact_ids}]."""
    out = []
    for f in formulas:
        picked = [ix.pick(c, unit, key) for c in f]
        parts = [p for p in picked if p]
        if len(parts) == len(f):
            out.append({"formula": " + ".join(f), "concepts": set(f),
                        "value": sum(p["value"] for p in parts) / MILLION, "fact_ids": [p["id"] for p in parts]})
    return out


def _item(name: str, candidates: list[dict], review: list, period: str) -> dict | None:
    if not candidates:
        return None
    chosen = candidates[0]
    for i, alt in enumerate(candidates[1:], start=1):
        # A fallback made of a subset of an earlier formula's concepts is partial, not a rival total.
        if any(alt["concepts"] < c["concepts"] for c in candidates[:i]):
            continue
        base = abs(chosen["value"]) or Decimal(1)
        if abs(alt["value"] - chosen["value"]) / base > TOLERANCE:
            review.append({"kind": "conflict", "item": name, "period": period, "material": name in MATERIAL,
                           "detail": f"{chosen['formula']} = {chosen['value']:.1f} vs {alt['formula']} = {alt['value']:.1f}",
                           "fact_ids": chosen["fact_ids"] + alt["fact_ids"]})
            break
    return {"value": chosen["value"], "formula": chosen["formula"], "fact_ids": chosen["fact_ids"]}


def _non_usd(ix: _Index, formulas) -> set[str]:
    return {u for f in formulas for c in f for u in ix.units(c) if u != "USD"}


def reconcile(snapshot: dict, cik: int) -> dict:
    ix = _Index(snapshot, cik)
    review: list[dict] = []

    # Fiscal years: annual revenue periods, latest HISTORY_YEARS by end date.
    annual_keys = set()
    for f in FLOWS["revenue"]:
        for r in ix.units(f[0]).get("USD", []):
            if 350 <= _days(r) <= 380:
                annual_keys.add((r["start"], r["end"]))
    by_end = {}
    for k in sorted(annual_keys):
        by_end[k[1]] = k                                     # one period per fiscal-year end
    years = [by_end[e] for e in sorted(by_end)[-HISTORY_YEARS:]]

    annual = []
    for key in years:
        row = {"period": f"FY ending {key[1]}", "start": key[0], "end": key[1]}
        for name, formulas in FLOWS.items():
            row[name] = _item(name, _evaluate(ix, formulas, "USD", key), review, row["period"])
        for name in ("receivables", "inventory", "payables"):
            row[name] = _item(name, _evaluate(ix, INSTANTS[name], "USD", key[1]), review, row["period"])
        annual.append(row)

    # Latest balance sheet on or before the valuation date: the latest date with reported cash.
    cash_dates = sorted({r["end"] for f in INSTANTS["cash"] for r in ix.units(f[0]).get("USD", []) if "start" not in r})
    balance = {"date": cash_dates[-1] if cash_dates else None}
    if balance["date"]:
        for name, formulas in INSTANTS.items():
            balance[name] = _item(name, _evaluate(ix, formulas, "USD", balance["date"]), review, balance["date"])

    # Diluted shares: the most recent reported period, shortest duration on ties (latest quarter).
    shares = None
    rows = sorted(ix.units(SHARES["diluted_shares"][0][0]).get("shares", []),
                  key=lambda r: (r["end"], -_days(r), r["filed"]))
    if rows:
        last = rows[-1]
        p = ix.pick(SHARES["diluted_shares"][0][0], "shares", (last.get("start"), last["end"]))
        assert p is not None                                   # `last` came from these very rows
        shares = {"value": p["value"] / MILLION, "formula": SHARES["diluted_shares"][0][0], "fact_ids": [p["id"]],
                  "period": f"{last.get('start')} to {last['end']}"}

    latest = annual[-1] if annual else {}
    # Missing items: material ones block; zero-if-absent ones are disclosed as not reported.
    for name in sorted(MATERIAL | ZERO_IF_ABSENT):
        where = shares if name == "diluted_shares" else (balance if name in INSTANTS else latest)
        present = where if name == "diluted_shares" else (where or {}).get(name)
        if present:
            continue
        if name in ZERO_IF_ABSENT:
            continue
        options: list[tuple[str, ...]] = SHARES.get(name) or INSTANTS.get(name) or FLOWS.get(name) or []
        non_usd = _non_usd(ix, options) - {"shares"}
        review.append({"kind": "currency" if non_usd else "missing", "item": name, "material": True,
                       "period": latest.get("period") if name in FLOWS else balance.get("date"),
                       "detail": (f"reported only in {sorted(non_usd)}" if non_usd
                                  else f"no XBRL fact for any of {[' + '.join(f) for f in options]} on or before "
                                       f"{snapshot['as_of']}"),
                       "fact_ids": []})

    # Reported totals.
    for row in annual:
        rev, cogs, gp = row.get("revenue"), row.get("cost_of_revenue"), row.get("gross_profit")
        if rev and cogs and gp and abs(rev["value"] - cogs["value"] - gp["value"]) > TOLERANCE * abs(rev["value"]):
            review.append({"kind": "total_mismatch", "item": "gross_profit", "period": row["period"], "material": True,
                           "detail": f"revenue {rev['value']:.1f} − cost of revenue {cogs['value']:.1f} ≠ gross profit {gp['value']:.1f}",
                           "fact_ids": rev["fact_ids"] + cogs["fact_ids"] + gp["fact_ids"]})
        oi = row.get("operating_income")
        if rev and oi and oi["value"] > rev["value"]:
            review.append({"kind": "total_mismatch", "item": "operating_income", "period": row["period"], "material": True,
                           "detail": "operating income exceeds revenue", "fact_ids": rev["fact_ids"] + oi["fact_ids"]})
    a, le = balance.get("assets"), balance.get("liabilities_and_equity")
    if a and le and abs(a["value"] - le["value"]) > Decimal("0.001") * abs(a["value"]):
        review.append({"kind": "total_mismatch", "item": "assets", "period": balance["date"], "material": True,
                       "detail": f"assets {a['value']:.1f} ≠ liabilities and equity {le['value']:.1f}",
                       "fact_ids": a["fact_ids"] + le["fact_ids"]})

    for r in review:
        r["id"] = "r_" + hashlib.sha256(f"{r['kind']}|{r['item']}|{r['period']}|{r['detail']}".encode()).hexdigest()[:8]
    restated = sorted(fid for fid, f in ix.facts.items() if f["superseded"])
    return {"entity": snapshot.get("entityName"), "cik": cik, "as_of": snapshot["as_of"], "currency": "USD",
            "unit": "millions", "annual": annual, "balance": balance, "diluted_shares": shares,
            "facts": dict(sorted(ix.facts.items())), "restated_fact_ids": restated, "review": review}


def to_jsonable(obj):
    """Decimals → strings, recursively, so a reconcile result can be hashed and stored."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_jsonable(v) for v in obj]
    return obj

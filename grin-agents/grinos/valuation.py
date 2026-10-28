"""
The fundamental valuation workflow: one US-listed nonfinancial company, a valuation date, a market
price and bear/base/bull assumptions → an evidence-linked FCFF/WACC DCF, sensitivity, reverse DCF
and a dated memo. Analysis only: nothing here places trades or issues a buy/sell call.

Every step is deterministic except the optional memo narrative. State lives in the ledger:

    sec_snapshot        raw SEC responses + the as-of filtered snapshot, as blobs   (cached per ticker+date)
    valuation_request   inputs, request hash, pinned snapshot → the run_id          (human)
    valuation_step      one entry per step whose output changed                     (rule@<module hash>)
    valuation_decision  analyst resolves a review item or approves an adjustment     (human; first is final)
    valuation_narrative optional memo-writer draft: attempts, validator problems, text (agent)
    valuation_status    where the run stands: complete, blocked at a step, invalid, unsupported

run_id = val_<TICKER>_<date>_<hash(request, snapshot)>. The same inputs and the same as-of facts
give the same run_id, so re-running resumes it; changed inputs, or filings that newly appear on or
before the valuation date (`refresh=True`), give a new run_id: a new valuation version.

Steps: resolve → retrieve → reconcile → adjustments → evidence → forecast → valuation →
sensitivity → validate → memo. A step that needs an analyst returns `blocked`; the run stops
there and `resume()` picks it up once the decisions are in. Source facts, analyst adjustments and
forecasts stay in separate outputs.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator

from distval import fcff
from grinos import sec, steps, xbrl
from grinos.ledger import Ledger
from grinos.llm import agent_actor
from grinos.schemas import Actor, canonical, sha256

STEPS = ["resolve", "retrieve", "reconcile", "adjustments", "evidence", "forecast", "valuation",
         "sensitivity", "validate", "memo"]
SYSTEM = Actor(kind="rule", name="valuation",
               version=sha256(Path(__file__).read_bytes())[:16])
MACHINE = steps.Machine("valuation", STEPS, lambda step, ctx: {"status": "done", "output": {}}, SYSTEM)
# SIC division H (6000–6799): banks, credit, brokers, insurers, real estate and holding companies.
FINANCIAL_SIC = range(6000, 6800)
ADJ_MATERIALITY = Decimal("0.005")      # one-off charges under 0.5% of operating income aren't proposed
STALE_DAYS = 200


# --- inputs ---------------------------------------------------------------------------------------

class Request(BaseModel):
    ticker: str
    valuation_date: date
    price: Decimal
    currency: str = "USD"
    assumptions: dict

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("price")
    @classmethod
    def _price(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("market price must be positive")
        return v

    @field_validator("currency")
    @classmethod
    def _usd(cls, v: str) -> str:
        if v != "USD":
            raise ValueError("only USD valuations are supported")
        return v

    def hash(self) -> str:
        return sha256(canonical(self.model_dump(mode="json")).encode())


def load_request(path: Path, **overrides) -> Request:
    """An assumptions YAML (see examples/valuation/acme.yaml); CLI flags override its top-level keys."""
    raw = yaml.safe_load(path.read_text())
    top = {k: raw.pop(k) for k in ("ticker", "valuation_date", "price", "currency") if k in raw}
    top |= {k: v for k, v in overrides.items() if v is not None}
    return Request(**top, assumptions=raw)


# --- the SEC snapshot (resolve + retrieve inputs), cached per ticker and date ------------------------

def _filings_as_of(submissions: dict, cutoff: str) -> list[dict]:
    r = submissions.get("filings", {}).get("recent", {})
    return sorted(({"form": r["form"][i], "accession": r["accessionNumber"][i], "filed": r["filingDate"][i],
                    "report_date": r.get("reportDate", [""] * len(r["form"]))[i]}
                   for i in range(len(r.get("form", []))) if r["filingDate"][i] <= cutoff),
                  key=lambda f: (f["filed"], f["accession"]))


def snapshot(ledger: Ledger, ticker: str, valuation_date: date, get: sec.Getter, refresh: bool = False) -> dict:
    """The pinned SEC data for (ticker, date): existing entry unless `refresh`; else fetch and record.
    Only what was filed on or before the valuation date goes into the hashed as-of view."""
    key = f"sec_{ticker}_{valuation_date}"
    if not refresh:
        for e in reversed(list(ledger.entries())):
            if e["stage"] == "sec_snapshot" and e["run_id"] == key:
                return e["payload"]
    cutoff = valuation_date.isoformat()
    tickers = get(sec.TICKERS_URL)
    cik = sec.cik_for(ticker, tickers)
    payload: dict = {"ticker": ticker, "valuation_date": cutoff, "cik": cik, "fetched_at": datetime.now().isoformat()}
    refs = []
    if cik is None:
        payload["error"] = f"{ticker} is not in the SEC ticker list"
    else:
        raw_sub = get(sec.submissions_url(cik))
        sub = json.loads(raw_sub)
        sub_view = {"cik": cik, "name": sub.get("name"), "sic": sub.get("sic"), "sicDescription": sub.get("sicDescription"),
                    "tickers": sub.get("tickers"), "exchanges": sub.get("exchanges"),
                    "fiscalYearEnd": sub.get("fiscalYearEnd"), "filings": _filings_as_of(sub, cutoff)}
        payload |= {"submissions_raw": ledger.put_blob(raw_sub), "submissions_as_of": ledger.put_blob(xbrl.canonical_bytes(sub_view))}
        refs += [f"blob:{payload['submissions_raw']}", f"blob:{payload['submissions_as_of']}"]
        if not (sub.get("sic") and int(sub["sic"]) in FINANCIAL_SIC):
            raw_facts = get(sec.companyfacts_url(cik))
            facts_view = xbrl.as_of(json.loads(raw_facts), valuation_date)
            payload |= {"companyfacts_raw": ledger.put_blob(raw_facts),
                        "companyfacts_as_of": ledger.put_blob(xbrl.canonical_bytes(facts_view))}
            refs += [f"blob:{payload['companyfacts_raw']}", f"blob:{payload['companyfacts_as_of']}"]
    payload["version"] = sha256(canonical({k: payload.get(k) for k in ("cik", "submissions_as_of", "companyfacts_as_of")}).encode())
    ledger.append(key, "sec_snapshot", SYSTEM, payload, refs=refs)
    return payload


# --- the run ----------------------------------------------------------------------------------------

@dataclass
class Ctx:
    ledger: Ledger
    run_id: str
    request: Request
    snap: dict
    decisions: dict[str, dict] = field(default_factory=dict)       # target id → decision payload
    out: dict[str, dict] = field(default_factory=dict)             # step → output

    def blob_json(self, key: str) -> dict:
        return json.loads(self.ledger.get_blob(self.snap[key]))


def run_id_for(request: Request, snap: dict) -> str:
    h = sha256(f"{request.hash()}|{snap['version']}".encode())[:10]
    return f"val_{request.ticker}_{request.valuation_date}_{h}"


def start(ledger: Ledger, request: Request, get: sec.Getter, who: str = "analyst", refresh: bool = False,
          narrate=None) -> dict:
    """Create (or resume) the run for these inputs and the pinned as-of facts, then advance it."""
    snap = snapshot(ledger, request.ticker, request.valuation_date, get, refresh)
    run_id = run_id_for(request, snap)
    if not _request_entry(ledger, run_id):
        previous = [e["run_id"] for e in ledger.entries() if e["stage"] == "valuation_request"
                    and e["payload"]["request_hash"] == request.hash()]
        ledger.append(run_id, "valuation_request", Actor(kind="human", name=who, version="-"),
                      {"request": request.model_dump(mode="json"), "request_hash": request.hash(),
                       "snapshot_version": snap["version"], "supersedes": previous[-1] if previous else None})
    return resume(ledger, run_id, narrate=narrate)


def _request_entry(ledger: Ledger, run_id: str) -> dict | None:
    return next((e for e in ledger.entries() if e["run_id"] == run_id and e["stage"] == "valuation_request"), None)


def resume(ledger: Ledger, run_id: str, narrate=None) -> dict:
    req = _request_entry(ledger, run_id)
    if not req:
        raise ValueError(f"no valuation run {run_id}")
    request = Request(**req["payload"]["request"])
    snap = next(e["payload"] for e in reversed(list(ledger.entries())) if e["stage"] == "sec_snapshot"
                and e["payload"]["version"] == req["payload"]["snapshot_version"])
    ctx = Ctx(ledger, run_id, request, snap)
    st = steps.state(MACHINE, ledger, run_id)
    ctx.decisions = st.decisions

    def run(step: str, c: Ctx) -> dict:
        fn = globals()[f"step_{step}"]
        return fn(c, narrate) if step == "memo" else fn(c)
    machine = steps.Machine(MACHINE.prefix, MACHINE.steps, run, MACHINE.actor)
    return steps.advance(machine, ledger, run_id, ctx, st, ctx.out)


def _done(output, **kw) -> dict:
    return {"status": "done", "output": output, **kw}


def _gate(output, items: list[dict]) -> dict:
    return {"status": "blocked" if items else "done", "output": output, "blocking": items}


# --- steps ------------------------------------------------------------------------------------------

def step_resolve(ctx: Ctx) -> dict:
    if ctx.snap.get("error"):
        return {"status": "unsupported", "output": {"reason": ctx.snap["error"]}}
    sub = ctx.blob_json("submissions_as_of")
    out = {k: sub.get(k) for k in ("cik", "name", "sic", "sicDescription", "tickers", "exchanges", "fiscalYearEnd")}
    if sub.get("sic") and int(sub["sic"]) in FINANCIAL_SIC:
        out["reason"] = (f"unsupported sector: SIC {sub['sic']} ({sub.get('sicDescription')}). Banks, insurers and other "
                         "financials are not valued with FCFF: their debt is operating, not financing.")
        return {"status": "unsupported", "output": out}
    if not sub.get("exchanges"):
        out["reason"] = "not US-listed: SEC lists no exchange for this company"
        return {"status": "unsupported", "output": out}
    if not any(f["form"] in ("10-K", "10-K/A") for f in sub["filings"]):
        out["reason"] = f"no 10-K filed on or before {ctx.request.valuation_date} (foreign or newly listed filer)"
        return {"status": "unsupported", "output": out}
    return _done(out)


def step_retrieve(ctx: Ctx) -> dict:
    sub, facts = ctx.blob_json("submissions_as_of"), ctx.blob_json("companyfacts_as_of")
    accns = {r["accn"] for concepts in facts["facts"].values() for body in concepts.values()
             for rows in body["units"].values() for r in rows}
    periodic = [f for f in sub["filings"] if f["form"] in ("10-K", "10-Q", "10-K/A", "10-Q/A")]
    latest = periodic[-1] if periodic else None
    out = {"snapshot_version": ctx.snap["version"], "fetched_at": ctx.snap["fetched_at"],
           "blobs": {k: ctx.snap[k] for k in ("submissions_raw", "submissions_as_of", "companyfacts_raw", "companyfacts_as_of")},
           "filings_as_of": len(sub["filings"]), "xbrl_accessions": len(accns),
           "latest_periodic_filing": latest, "periodic_filings": periodic[-8:]}
    review = []
    if latest and latest["accession"] not in accns:
        review.append({"id": "r_" + latest["accession"].replace("-", "")[-8:], "kind": "missing", "material": True,
                       "item": "latest_filing", "period": latest["report_date"],
                       "detail": f"{latest['form']} {latest['accession']} filed {latest['filed']} is not yet in SEC's XBRL "
                                 "companyfacts; the latest balance sheet and shares are from an earlier filing. "
                                 "Accept to value on the earlier filing, or supply values with a source.",
                       "fact_ids": [], "url": sec.filing_url(sub["cik"], latest["accession"])})
    out["review"] = review
    return _gate(out, _unresolved(ctx, review))


def _unresolved(ctx: Ctx, items: list[dict]) -> list[dict]:
    """Material review items without an accepting decision. A rejection keeps the run blocked."""
    return [i for i in items if i.get("material") and ctx.decisions.get(i["id"], {}).get("decision") not in ("accept", "value")]


def step_reconcile(ctx: Ctx) -> dict:
    rec = xbrl.reconcile(ctx.blob_json("companyfacts_as_of"), ctx.snap["cik"])
    analyst = {}
    for item in rec["review"]:
        d = ctx.decisions.get(item["id"])
        if d and d["decision"] == "value":
            analyst[item["item"]] = {"value": Decimal(str(d["value"])), "note": d["note"], "by": d["by"], "review_id": item["id"]}
    out = {"reported": rec, "analyst_inputs": analyst}
    return _gate(out, _unresolved(ctx, rec["review"]))


def _value(ctx: Ctx, name: str) -> Decimal | None:
    """A material input: an analyst-supplied value (from a review decision) outranks the reported one."""
    rec, analyst = ctx.out["reconcile"]["reported"], ctx.out["reconcile"]["analyst_inputs"]
    if name in analyst:
        return analyst[name]["value"]
    if name == "diluted_shares":
        return rec["diluted_shares"]["value"] if rec["diluted_shares"] else None
    v = rec["balance"].get(name)
    return v["value"] if v else (Decimal(0) if name in xbrl.ZERO_IF_ABSENT else None)


def _require(ctx: Ctx, name: str) -> Decimal:
    """A material input that must exist by now (reconcile blocks while it's missing). Never invented."""
    v = _value(ctx, name)
    if v is None:
        raise LookupError(f"{name} is missing and no analyst value was supplied")
    return v


def _adj_id(*parts) -> str:
    return "adj_" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8]


def step_adjustments(ctx: Ctx) -> dict:
    """Reported operating income, proposed normalisations, and the normalised result: kept apart."""
    annual = ctx.out["reconcile"]["reported"]["annual"]
    proposed = []
    for row in annual:
        oi = row.get("operating_income")
        if not oi:
            continue
        for item in ("goodwill_impairment", "asset_impairment", "restructuring"):
            v = row.get(item)
            if v and v["value"] > 0 and v["value"] > ADJ_MATERIALITY * abs(oi["value"]):
                proposed.append({"id": _adj_id("rule", item, row["period"], v["value"]), "origin": "rule",
                                 "period": row["period"], "item": "operating_income", "amount": v["value"],
                                 "rationale": f"add back {item.replace('_', ' ')}, a non-recurring charge inside operating income",
                                 "source": {"fact_ids": v["fact_ids"]}})
    for a in ctx.request.assumptions.get("adjustments", []) or []:
        period = annual[-1]["period"] if a.get("period", "latest") == "latest" else a["period"]
        proposed.append({"id": _adj_id("analyst", a["item"], period, a["amount"], a.get("rationale")), "origin": "analyst",
                         "period": period, "item": a["item"], "amount": Decimal(str(a["amount"])),
                         "rationale": a.get("rationale", ""), "source": {"cited": a.get("source")}})
    for p in proposed:
        d = ctx.decisions.get(p["id"])
        p["status"] = {"approve": "approved", "reject": "rejected"}.get(str((d or {}).get("decision")), "pending")
        p["decided_by"] = (d or {}).get("by")
    normalised = []
    for row in annual:
        oi = row.get("operating_income")
        add = sum((p["amount"] for p in proposed if p["status"] == "approved" and p["period"] == row["period"]
                   and p["item"] == "operating_income"), Decimal(0))
        rev = row.get("revenue")
        normalised.append({"period": row["period"], "reported_operating_income": oi["value"] if oi else None,
                           "approved_adjustments": add,
                           "normalised_operating_income": (oi["value"] + add) if oi else None,
                           "normalised_margin": ((oi["value"] + add) / rev["value"]) if oi and rev else None})
    pending = [{"id": p["id"], "kind": "adjustment", "material": True, "item": p["item"], "period": p["period"],
                "detail": f"{p['amount']:+.1f} — {p['rationale']}"} for p in proposed if p["status"] == "pending"]
    return _gate({"proposed": proposed, "normalised": normalised}, pending)


CLAIM_ID = re.compile(r"^c_[0-9a-f]{12}$")
FACT_ID = re.compile(r"^x_[0-9a-f]{12}$")


def step_evidence(ctx: Ctx) -> dict:
    """Drivers, risks and disconfirming evidence from the assumptions file, each with a checked source:
    a verified claim (through the evidence gate, published on or before the valuation date), a reported
    XBRL fact of this run, or an analyst-cited URL (labelled unverified)."""
    narrative = ctx.request.assumptions.get("narrative", {}) or {}
    verified = {c["claim_id"]: c for c in ctx.ledger.verified_claims(ctx.request.ticker)}
    published = {e["payload"]["source"]["blob"]: e["payload"]["source"]["published_at"]
                 for e in ctx.ledger.entries() if e["stage"] == "source"}
    facts = ctx.out["reconcile"]["reported"]["facts"]
    cutoff = ctx.request.valuation_date.isoformat()
    sections, problems = {}, []
    for section in ("drivers", "risks", "disconfirming"):
        items = []
        for raw in narrative.get(section, []) or []:
            text, src = (raw, None) if isinstance(raw, str) else (raw.get("text", ""), raw.get("source"))
            entry = {"text": text, "source": src, "verified": False, "problem": None}
            if src is None:
                entry["problem"] = "no source"
            elif CLAIM_ID.match(str(src)):
                c = verified.get(src)
                if not c:
                    entry["problem"] = "not a verified claim for this company"
                elif published.get(c["claim"]["source_blob"], "9999") > cutoff:
                    entry["problem"] = f"published after the valuation date ({published[c['claim']['source_blob']]})"
                else:
                    entry |= {"verified": True, "quote": c["claim"]["quote"], "ledger_hash": c["ledger_hash"]}
            elif FACT_ID.match(str(src)):
                if src in facts:
                    entry |= {"verified": True, "fact": facts[src]}
                else:
                    entry["problem"] = "not a fact of this valuation's snapshot"
            elif not str(src).startswith(("http://", "https://")):
                entry["problem"] = "source must be a claim id, a fact id or a URL"
            items.append(entry)
            if entry["problem"]:
                problems.append(f"{section}: “{text[:60]}” — {entry['problem']}")
        sections[section] = items
    # What the evidence pipeline already holds: verified claims available on the valuation date.
    available = [{"claim_id": cid, "statement": c["claim"]["statement"], "period": c["claim"]["period"]}
                 for cid, c in verified.items() if published.get(c["claim"]["source_blob"], "9999") <= cutoff]
    review = []
    if not sections["disconfirming"]:
        review.append({"id": "r_disconfirm", "kind": "missing", "material": True, "item": "disconfirming_evidence",
                       "period": cutoff, "detail": "no disconfirming evidence recorded: add `narrative.disconfirming` "
                                                   "to the assumptions (a new version), or accept to proceed without it"})
    out = {"sections": sections, "problems": problems, "verified_claims_available": len(available),
           "verified_claims_sample": available[-10:]}
    return _gate(out, _unresolved(ctx, review) if review else [])


def _resolve_assumption(ctx: Ctx, key: str, v, provenance: list) -> object:
    """Numbers pass through. 'reported' / 'normalised' derive from the latest fiscal year's facts;
    a missing input is never invented: it raises, and the forecast step reports it."""
    if not isinstance(v, str):
        return v
    rec = ctx.out["reconcile"]["reported"]
    latest = rec["annual"][-1]
    norm = ctx.out["adjustments"]["normalised"][-1]

    def need(*names):
        vals = [latest.get(n) for n in names]
        if not all(vals):
            raise LookupError(f"{key}: '{v}' needs {', '.join(n for n, x in zip(names, vals) if not x)} "
                              f"for {latest['period']}, which the filings don't report")
        return vals

    if key == "ebit_margin" and v in ("normalised", "normalized"):
        if norm["normalised_margin"] is None:
            raise LookupError("ebit_margin: no normalised margin for the latest fiscal year")
        provenance.append({"assumption": key, "from": f"normalised operating income / revenue, {latest['period']}"})
        return norm["normalised_margin"]
    if v != "reported":
        raise ValueError(f"{key}: unknown keyword '{v}' (use a number, 'reported' or 'normalised')")
    if key == "tax_rate":
        pre, tax = need("pretax_income", "income_tax")
        r = tax["value"] / pre["value"]
        provenance.append({"assumption": key, "from": "income tax / pretax income", "fact_ids": pre["fact_ids"] + tax["fact_ids"]})
        return r
    if key == "net_capex_pct_revenue":
        capex, da, rev = need("capex", "d_and_a", "revenue")
        provenance.append({"assumption": key, "from": "(capex − D&A) / revenue",
                           "fact_ids": capex["fact_ids"] + da["fact_ids"] + rev["fact_ids"]})
        return (capex["value"] - da["value"]) / rev["value"]
    if key == "nwc_pct_revenue":
        ar, inv, ap, rev = need("receivables", "inventory", "payables", "revenue")
        provenance.append({"assumption": key, "from": "(receivables + inventory − payables) / revenue",
                           "fact_ids": ar["fact_ids"] + inv["fact_ids"] + ap["fact_ids"] + rev["fact_ids"]})
        return (ar["value"] + inv["value"] - ap["value"]) / rev["value"]
    raise ValueError(f"{key}: 'reported' is not available for this assumption")


SCENARIO_KEYS = ("revenue_growth", "ebit_margin", "tax_rate", "net_capex_pct_revenue", "nwc_pct_revenue", "terminal_growth")


def step_forecast(ctx: Ctx) -> dict:
    a = ctx.request.assumptions
    defaults, wacc_defaults = a.get("defaults", {}) or {}, a.get("wacc", {}) or {}
    shares, debt = _require(ctx, "diluted_shares"), _require(ctx, "debt")
    market_equity = ctx.request.price * shares
    out: dict[str, Any] = {"market_equity": market_equity, "scenarios": {}}
    errors: list[str] = []
    for name in ("bear", "base", "bull"):
        spec = defaults | (a.get("scenarios", {}).get(name) or {})
        if not a.get("scenarios", {}).get(name):
            errors.append(f"{name}: scenario missing from assumptions")
            continue
        provenance: list = []
        try:
            vals = {k: _resolve_assumption(ctx, k, spec[k], provenance) for k in SCENARIO_KEYS if k in spec}
            missing = [k for k in SCENARIO_KEYS if k not in vals]
            if missing:
                raise LookupError(f"missing assumptions {missing}")
            if "wacc" in spec:
                w = {"wacc": fcff.D(spec["wacc"]), "method": "supplied"}
            else:
                wi = wacc_defaults | (spec.get("wacc_inputs") or {})
                tax = fcff.D(wi.get("tax_rate", vals["tax_rate"] if not isinstance(vals["tax_rate"], list) else vals["tax_rate"][0]))
                w = fcff.wacc(fcff.WaccInputs(**{k: fcff.D(v) for k, v in wi.items() if k != "tax_rate"}, tax_rate=tax),
                              market_equity, debt) | {"method": "build-up", "inputs": wi}
            s = fcff.Scenario.build(name, **vals, wacc=w["wacc"])
        except (LookupError, ValueError, TypeError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        out["scenarios"][name] = {"assumptions": {k: getattr(s, k) for k in SCENARIO_KEYS}, "wacc": w,
                                  "provenance": provenance}
    if errors:
        # Inputs that can't be valued (g ≥ WACC, an input the filings don't report, …) need new
        # assumptions, which is a new version; nothing is invented to get past them.
        return {"status": "invalid", "output": out | {"errors": errors}}
    return _done(out)


def _scenario(ctx: Ctx, name: str) -> fcff.Scenario:
    s = ctx.out["forecast"]["scenarios"][name]
    return fcff.Scenario(name, **s["assumptions"], wacc=s["wacc"]["wacc"])


def _bridge(ctx: Ctx) -> tuple[fcff.Bridge, dict]:
    leases = bool((ctx.request.assumptions.get("bridge") or {}).get("include_operating_leases"))
    lines: dict[str, Any] = {k: _require(ctx, k) for k in ("debt", "cash", "preferred", "minority_interest", "diluted_shares")}
    lines["operating_leases"] = _value(ctx, "operating_leases")          # optional: only netted if asked
    other = (lines["operating_leases"] or Decimal(0)) if leases else Decimal(0)
    bridge = fcff.Bridge(debt=lines["debt"], cash=lines["cash"], preferred=lines["preferred"],
                         minority_interest=lines["minority_interest"], other_claims=other,
                         shares_diluted=lines["diluted_shares"])
    return bridge, lines | {"leases_treated_as_debt": leases, "other_claims": other}


def _base_revenue(ctx: Ctx) -> Decimal:
    return ctx.out["reconcile"]["reported"]["annual"][-1]["revenue"]["value"]


def step_valuation(ctx: Ctx) -> dict:
    bridge, lines = _bridge(ctx)
    out: dict[str, Any] = {"base_revenue": _base_revenue(ctx), "bridge": lines, "scenarios": {}}
    for name in ("bear", "base", "bull"):
        r = fcff.dcf(_scenario(ctx, name), _base_revenue(ctx), bridge)
        out["scenarios"][name] = {
            "years": [vars(y) for y in r.years], "terminal_fcff": r.terminal_fcff, "terminal_value": r.terminal_value,
            "pv_terminal": r.pv_terminal, "pv_forecast": r.pv_forecast, "enterprise_value": r.enterprise_value,
            "equity_value": r.equity_value, "value_per_share": r.value_per_share, "terminal_share": r.terminal_share,
            "vs_price": r.value_per_share / ctx.request.price - 1, "flags": r.flags}
    return _done(out)


def step_sensitivity(ctx: Ctx) -> dict:
    bridge, _ = _bridge(ctx)
    base = _scenario(ctx, "base")
    return _done({"grid": fcff.sensitivity(base, _base_revenue(ctx), bridge),
                  "reverse_dcf": fcff.reverse_dcf(base, _base_revenue(ctx), bridge, ctx.request.price)})


def step_validate(ctx: Ctx) -> dict:
    rec = ctx.out["reconcile"]["reported"]
    cutoff = ctx.request.valuation_date.isoformat()
    checks = []

    def check(cid, ok, severity, message):
        checks.append({"id": cid, "ok": bool(ok), "severity": severity, "message": message})

    late = [fid for fid, f in rec["facts"].items() if f["filed"] > cutoff]
    check("no_lookahead", not late, "block", f"{len(late)} fact(s) filed after {cutoff}" if late
          else f"every fact was filed on or before {cutoff}")
    units_ok = all(f["unit"] in ("USD", "shares") for f in rec["facts"].values())
    check("units_recorded", units_ok, "block", "all facts in USD or shares, each with concept, period and accession")
    for name in ("bear", "base", "bull"):
        s = ctx.out["forecast"]["scenarios"][name]["assumptions"]
        w = ctx.out["forecast"]["scenarios"][name]["wacc"]["wacc"]
        check(f"g_below_wacc_{name}", s["terminal_growth"] < w, "block",
              f"{name}: terminal growth {s['terminal_growth']:.2%} vs WACC {w:.2%}")
        for f in ctx.out["valuation"]["scenarios"][name]["flags"]:
            check(f"flag_{name}", False, "flag", f"{name}: {f}")
    v = {n: ctx.out["valuation"]["scenarios"][n]["value_per_share"] for n in ("bear", "base", "bull")}
    check("scenario_order", v["bear"] <= v["base"] <= v["bull"], "flag",
          f"bear {v['bear']:.2f} ≤ base {v['base']:.2f} ≤ bull {v['bull']:.2f}")
    bal_date = rec["balance"]["date"]
    stale = (ctx.request.valuation_date - date.fromisoformat(bal_date)).days
    check("balance_sheet_current", stale <= STALE_DAYS, "flag", f"balance sheet dated {bal_date}, {stale} days before valuation")
    analyst = ctx.out["reconcile"]["analyst_inputs"]
    check("analyst_inputs", not analyst, "flag",
          "no analyst-supplied values" if not analyst else f"analyst-supplied (not from filings): {sorted(analyst)}")
    if rec["restated_fact_ids"]:
        check("restatements", False, "flag", f"{len(rec['restated_fact_ids'])} fact(s) were restated; latest-filed values used")
    for p in ctx.out["evidence"]["problems"]:
        check("evidence_source", False, "flag", p)
    blocking = [c for c in checks if c["severity"] == "block" and not c["ok"]]
    return {"status": "invalid" if blocking else "done", "output": {"checks": checks}}


def step_memo(ctx: Ctx, narrate=None) -> dict:
    """`narrate(ctx) -> (text | None, trace)` is optional (grinos.narrative.narrate). Its result is
    checkpointed by the sheet it was drafted from, so resuming never calls the model twice."""
    from grinos import narrative as N
    from grinos.valuation_memo import render
    text_n = None
    if narrate:
        sheet_hash = sha256(canonical(N.sheet(ctx)).encode())
        done = next((e["payload"] for e in reversed(list(ctx.ledger.entries())) if e["run_id"] == ctx.run_id
                     and e["stage"] == "valuation_narrative" and e["payload"]["sheet_hash"] == sheet_hash), None)
        if done is None:
            text_n, trace = narrate(ctx)
            ctx.ledger.append(ctx.run_id, "valuation_narrative", agent_actor("memo-writer", trace["model"]),
                              trace | {"text": text_n})
        else:
            text_n = done["text"]
    text = render(ctx, text_n)
    blob = ctx.ledger.put_blob(text.encode())
    folder = ctx.ledger.root / "valuations" / ctx.run_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "memo.md").write_text(text)
    (folder / "valuation.json").write_text(json.dumps(xbrl.to_jsonable(ctx.out), indent=2, default=str))
    return _done({"memo_blob": blob, "path": str(folder / "memo.md"), "narrative": text_n})


# --- decisions ---------------------------------------------------------------------------------------

def decide(ledger: Ledger, run_id: str, target: str, decision: str, who: str, note: str,
           value: Decimal | None = None, channel: str = "cli") -> dict:
    """Analyst resolves a review item (accept | value | reject) or an adjustment (approve | reject)."""
    if not note.strip():
        raise ValueError("a decision needs a note")
    steps = {}
    for e in ledger.entries():
        if e["run_id"] == run_id and e["stage"] == "valuation_step":
            steps[e["payload"]["step"]] = e["payload"]
    if not steps:
        raise ValueError(f"no valuation run {run_id}")
    items = {i["id"]: i for s in steps.values() for i in s.get("blocking", [])}
    items |= {i["id"]: i for i in steps.get("reconcile", {}).get("output", {}).get("reported", {}).get("review", [])}
    items |= {p["id"]: {**p, "kind": "adjustment"} for p in steps.get("adjustments", {}).get("output", {}).get("proposed", [])}
    if target not in items:
        raise ValueError(f"{target} is not a review item or adjustment of {run_id}")
    kind = items[target]["kind"]
    allowed = {"approve", "reject"} if kind == "adjustment" else {"accept", "value", "reject"}
    if decision not in allowed:
        raise ValueError(f"{target} ({kind}) takes {sorted(allowed)}")
    if decision == "value" and value is None:
        raise ValueError("a 'value' decision needs the value, in millions")
    if kind == "missing" and decision == "accept" and items[target].get("item") in xbrl.MATERIAL:
        raise ValueError(f"{items[target]['item']} is missing: supply a value with its source, never accept a gap")

    def first(entries):
        if any(e["run_id"] == run_id and e["stage"] == "valuation_decision" and e["payload"]["target"] == target
               for e in entries):
            return f"{target} was already decided"
        return None

    return ledger.append_if(run_id, "valuation_decision", Actor(kind="human", name=who, version="-"),
                            {"target": target, "kind": kind, "decision": decision, "note": note.strip(),
                             "value": str(value) if value is not None else None, "by": who, "channel": channel},
                            guard=first)


def summary(r: dict) -> str:
    """A few lines for the CLI and chat: status, what needs the analyst, values, memo."""
    from grinos.valuation_memo import ps
    lines = [f"{r['run_id']}  {r['status']} at {r['step']}"]
    for b in r.get("blocking", []):
        lines.append(f"  needs you: {b['id']} [{b['kind']}] {b.get('item', '')} {b.get('period') or ''} — {b['detail']}")
    if r["status"] == "invalid":
        out = r["out"].get(r["step"], {})
        lines += [f"  invalid: {e}" for e in out.get("errors", [])]
        lines += [f"  invalid: {c['message']}" for c in out.get("checks", []) if c["severity"] == "block" and not c["ok"]]
    if r["status"] == "unsupported":
        lines.append(f"  {r['out']['resolve'].get('reason')}")
    if "valuation" in r["out"]:
        v = r["out"]["valuation"]["scenarios"]
        lines.append("  value per share: " + ", ".join(f"{k} {ps(v[k]['value_per_share'])}" for k in ("bear", "base", "bull")))
    if r["status"] == "complete":
        lines.append(f"  memo: {r['out']['memo']['path']}")
    return "\n".join(lines)


def view(ledger: Ledger, run_id: str) -> dict:
    """Read-only: the run's request, latest output per step, status, decisions and memo. Never advances it."""
    out: dict[str, Any] = {"run_id": run_id, "request": None, "steps": {}, "status": None, "decisions": [], "memo": None}
    for e in ledger.entries():
        if e["run_id"] != run_id:
            continue
        p = e["payload"]
        if e["stage"] == "valuation_request":
            out["request"] = p["request"] | {"supersedes": p.get("supersedes")}
        elif e["stage"] == "valuation_step":
            out["steps"][p["step"]] = {"status": p["status"], "blocking": p["blocking"], "output": p["output"], "seq": e["seq"]}
        elif e["stage"] == "valuation_status":
            out["status"] = p
        elif e["stage"] == "valuation_decision":
            out["decisions"].append(p | {"seq": e["seq"], "ts": e["ts"]})
    if out["request"] is None:
        raise ValueError(f"no valuation run {run_id}")
    memo = out["steps"].get("memo", {}).get("output", {}).get("memo_blob")
    if memo:
        out["memo"] = ledger.get_blob(memo).decode()
    return out


def runs(ledger: Ledger) -> list[dict]:
    """Every valuation run with its latest status."""
    out: dict[str, dict] = {}
    for e in ledger.entries():
        if e["stage"] == "valuation_request":
            r = e["payload"]["request"]
            out[e["run_id"]] = {"run_id": e["run_id"], "ticker": r["ticker"], "valuation_date": r["valuation_date"],
                                "price": r["price"], "status": "started", "step": None, "ts": e["ts"],
                                "supersedes": e["payload"].get("supersedes")}
        elif e["stage"] == "valuation_status" and e["run_id"] in out:
            out[e["run_id"]] |= {"status": e["payload"]["status"], "step": e["payload"]["step"],
                                 "blocking": e["payload"]["blocking"]}
        elif e["stage"] == "valuation_step" and e["run_id"] in out:
            if e["payload"]["step"] == "valuation":
                out[e["run_id"]]["value_per_share"] = {k: v["value_per_share"] for k, v in e["payload"]["output"]["scenarios"].items()}
    return list(out.values())

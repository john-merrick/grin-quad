"""
Coverage: the LongGamma stock-evaluation process as a step-by-step, human-verified run, ending in a
mid-cycle distval valuation with its risks, stored in the ledger and ready to sync to Notion.

    sources    the pinned SEC snapshot and the documents ingested for the company      you confirm the set
    claims     verified evidence; claims still needing a human; open disputes           you resolve them
    drivers    distval drivers computed from 10 years of XBRL, each with its facts      you accept, set or keep each
    scenarios  bear / bull mid-cycle EBIT and target multiples, probabilities          you set them (never a model)
    valuation  distval engine on the base config; LongGamma drivers and PW target      deterministic
    risks      3–5 risks with measurable kill criteria, Direction, Structural/Cyclical you enter them
    validate   ordering, probabilities, provenance complete, no trade language          deterministic
    memo       an Investment-Note style summary with every number's source             deterministic

Nothing here invents a number: values come from XBRL facts (with accession and filing URL), from
the config, or from a human decision with a note. The company YAML is changed only by
`grinos coverage apply` (a human command), which writes the accepted drivers with provenance comments.

Ledger stages: coverage_request, coverage_step / coverage_status / coverage_decision (grinos.steps),
risk, catalyst, coverage_stage (Idea → Deep Dive → Watchlist → Owned → Rejected / Archived),
config_applied.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from grinos import drivers as D
from grinos import sec, steps, xbrl
from grinos.ledger import Ledger
from grinos.schemas import Actor, canonical, sha256

SYSTEM = Actor(kind="rule", name="coverage", version=sha256(Path(__file__).read_bytes())[:16])
STEPS = ["sources", "claims", "drivers", "scenarios", "valuation", "risks", "validate", "memo"]
STAGES = ["Idea", "Deep Dive", "Watchlist", "Owned", "Rejected", "Archived"]
HISTORY_YEARS = 10
SCORE = range(1, 6)                 # Notion Risks: Likelihood and Impact are 1–5 numbers (Score is a formula)
RISK_STATUS = {"Active", "Mitigated", "Realised"}
DIRECTIONS = {"Long", "Short"}
NATURES = {"Structural", "Cyclical", "Mixed"}
MATERIAL_METRICS = {"revenue", "gross_margin", "opex_pct_sales", "ebit", "ebit_margin", "dio", "dso", "dpo",
                    "capex", "tax_rate", "customer_retention", "market_share"}
TRADE_WORDS = re.compile(r"\b(buy|sell|strong buy|outperform|underperform|overweight|underweight|price target)\b",
                         re.IGNORECASE)
PROB_DEFAULT = {"Bear": 0.25, "Base": 0.5, "Bull": 0.25}


def _human(who: str) -> Actor:
    return Actor(kind="human", name=who, version="-")


@dataclass
class Ctx:
    ledger: Ledger
    run_id: str
    request: dict
    snap: dict
    decisions: dict[str, dict] = field(default_factory=dict)
    out: dict[str, Any] = field(default_factory=dict)
    _rec: dict | None = None

    def rec(self) -> dict:
        if self._rec is None:
            view = json.loads(self.ledger.get_blob(self.snap["companyfacts_as_of"]))
            self._rec = xbrl.reconcile(view, self.snap["cik"], history_years=HISTORY_YEARS)
        return self._rec

    def config(self) -> dict:
        return yaml.safe_load(Path(self.request["config_path"]).read_text())

    def config_text(self) -> str:
        return Path(self.request["config_path"]).read_text()

    def decided(self, target: str) -> dict | None:
        return self.decisions.get(target)


def _done(output, **kw) -> dict:
    return {"status": "done", "output": output, **kw}


def _gate(output, items: list[dict]) -> dict:
    return {"status": "blocked" if items else "done", "output": output, "blocking": items}


def _item(iid: str, kind: str, detail: str, **kw) -> dict:
    return {"id": iid, "kind": kind, "detail": detail, **kw}


# --- steps ---------------------------------------------------------------------------------------

def step_sources(c: Ctx) -> dict:
    if c.snap.get("error") or not c.snap.get("companyfacts_as_of"):
        return {"status": "unsupported", "output": {"reason": c.snap.get("error") or "no XBRL company facts"}}
    sub = json.loads(c.ledger.get_blob(c.snap["submissions_as_of"]))
    filings = [f for f in sub.get("filings", []) if f["form"] in ("10-K", "10-Q", "8-K")]
    latest = {k: next((f for f in reversed(filings) if f["form"] == k), None) for k in ("10-K", "10-Q")}
    docs = [{"run_id": e["run_id"], "uri": e["payload"]["source"]["uri"], "kind": e["payload"]["source"]["kind"],
             "published_at": e["payload"]["source"]["published_at"], "blob": e["payload"]["source"]["blob"]}
            for e in c.ledger.entries() if e["stage"] == "source"
            and e["payload"]["source"]["company"] == c.request["ticker"]
            and str(e["payload"]["source"]["published_at"]) <= c.request["as_of"]]
    seen, unique = set(), []
    for d in docs:
        if d["blob"] not in seen:
            seen.add(d["blob"])
            unique.append(d)
    out = {"entity": sub.get("name"), "sic": sub.get("sic"), "latest_10k": latest["10-K"], "latest_10q": latest["10-Q"],
           "documents": unique, "n_documents": len(unique), "as_of": c.request["as_of"]}
    items = []
    if not c.decided("s_confirm"):
        detail = (f"{len(unique)} ingested document(s); latest 10-K {(latest['10-K'] or {}).get('filed', 'none')}, "
                  f"latest 10-Q {(latest['10-Q'] or {}).get('filed', 'none')}. Confirm this is the source set "
                  f"for the valuation, or ingest what is missing first.")
        items.append(_item("s_confirm", "sources", detail))
    return _gate(out, items)


def _claim_state(c: Ctx) -> tuple[dict, dict, list[dict]]:
    claims: dict[str, dict] = {}
    latest: dict[str, dict] = {}
    disputes: list[dict] = []
    for e in c.ledger.entries():
        p = e["payload"]
        if e["stage"] == "claim" and p["claim"]["company"] == c.request["ticker"]:
            claims[p["claim_id"]] = p["claim"]
        elif e["stage"] == "verdict" and p["claim_id"] in claims:
            human = e["actor"]["kind"] == "human"
            if human or latest.get(p["claim_id"], {}).get("by") != "human":
                latest[p["claim_id"]] = {"verdict": p["verdict"], "by": "human" if human else "machine"}
        elif e["stage"] == "evidence_review" and p["decision"] == "disputed" and p["target_id"] in claims:
            disputes.append(p | {"seq": e["seq"]})
    return claims, latest, disputes


def step_claims(c: Ctx) -> dict:
    claims, latest, disputes = _claim_state(c)
    count: dict[str, int] = {}
    for v in latest.values():
        count[v["verdict"]] = count.get(v["verdict"], 0) + 1
    items = []
    for cid, v in latest.items():
        k = claims[cid]
        if v["verdict"] == "needs_human" and k["metric"] in MATERIAL_METRICS:
            items.append(_item(f"c_{cid}", "claim", f"needs your verdict: {k['statement']}", claim_id=cid,
                               resolve=f"grinos resolve {cid} verified|rejected --note …"))
    for d in disputes:
        if latest.get(d["target_id"], {}).get("verdict") == "verified" and not c.decided(f"x_{d['target_id']}"):
            items.append(_item(f"x_{d['target_id']}", "dispute", f"you disputed {d['target_id']} in review of "
                               f"{d['context_id']} ({d['note']}) but it is still verified: reject it, or accept it",
                               claim_id=d["target_id"]))
    return _gate({"verdicts": count, "n_claims": len(claims), "open_disputes": len(disputes)}, items)


def step_drivers(c: Ctx) -> dict:
    claims = c.ledger.verified_claims(c.request["ticker"])
    ex = D.extract(c.rec(), claims, c.config(), c.config_text())
    review = [r for r in c.rec()["review"] if r.get("material")]
    items, chosen = [], {}
    for name, d in ex["drivers"].items():
        dec = c.decided(f"d_{name}")
        if dec is None:
            items.append(_item(f"d_{name}", "driver", f"{name}: XBRL {d['value']} ({d['formula']}, "
                               f"{len(d['years'])} years) vs config {d['config_value']}"
                               + (f". Flags: {'; '.join(d['flags'])}" if d["flags"] else ""), driver=name))
            continue
        if dec["decision"] == "accept":
            chosen[name] = {"value": d["value"], "from": "xbrl", "formula": d["formula"],
                            "fact_ids": sorted({i["fact_id"] for y in d["years"] for i in y["inputs"]})}
        elif dec["decision"] == "value":
            chosen[name] = {"value": float(dec["value"]), "from": "human", "note": dec["note"], "by": dec["by"]}
        else:                                                                       # keep
            chosen[name] = {"value": d["config_value"], "from": "config", "note": dec["note"], "by": dec["by"]}
    for r in review:
        if not c.decided(r["id"]):
            items.append(_item(r["id"], "xbrl_review", f"{r['kind']} {r['item']} {r.get('period') or ''}: {r['detail']}",
                               fact_ids=r.get("fact_ids", [])))
    return _gate({"drivers": ex["drivers"], "bridge": ex["bridge"], "years": ex["years"], "chosen": chosen}, items)


SCENARIO_ITEMS = {
    "s_bear_ebit": "bear mid-cycle EBIT, $M (what the business earns in a worse but plausible cycle)",
    "s_bull_ebit": "bull mid-cycle EBIT, $M",
    "s_bear_multiple": "bear target multiple on EBIT FY2 (x)",
    "s_bull_multiple": "bull target multiple on EBIT FY2 (x)",
    "s_probs": "probabilities: accept for 25/50/25, or set the bear probability (bull gets the same)",
}


def _base(c: Ctx) -> dict:
    from grinos.proposal_check import engine_view
    drv = c.out["drivers"]
    cfg = base_config(c.config(), drv["chosen"])
    nd, sh = drv["bridge"]["net_debt"], drv["bridge"]["shares"]
    base = engine_view(cfg, nd or 0.0, sh or 1.0)
    path = base["ebit_path"]
    fy2 = path[1] if len(path) > 1 else path[0]
    return {"config": cfg, "engine": base, "fy2": fy2, "multiple": base["enterprise_value"] / fy2, "net_debt": nd,
            "shares": sh}


def step_scenarios(c: Ctx) -> dict:
    b = _base(c)
    ref = (f" (base: mid-cycle EBIT {b['engine']['sustainable_ebit']:,.1f}M, EBIT FY2 {b['fy2']:,.1f}M, "
           f"EV ÷ EBIT FY2 {b['multiple']:.2f}x, engine terminal multiple {b['engine']['terminal_multiple']:.2f}x)")
    items, chosen = [], {}
    for iid, what in SCENARIO_ITEMS.items():
        dec = c.decided(iid)
        if dec is None:
            items.append(_item(iid, "scenario", f"set the {what}" + (ref if iid != "s_probs" else "")))
        else:
            chosen[iid] = dec
    probs = PROB_DEFAULT
    if "s_probs" in chosen and chosen["s_probs"]["decision"] == "value":
        p = float(chosen["s_probs"]["value"])
        probs = {"Bear": p, "Base": round(1 - 2 * p, 4), "Bull": p}
    out = {"base_reference": {"sustainable_ebit": b["engine"]["sustainable_ebit"], "ebit_fy2": b["fy2"],
                              "multiple_on_fy2": b["multiple"], "terminal_multiple": b["engine"]["terminal_multiple"]},
           "bear_ebit": _num(chosen.get("s_bear_ebit")), "bull_ebit": _num(chosen.get("s_bull_ebit")),
           "bear_multiple": _num(chosen.get("s_bear_multiple")), "bull_multiple": _num(chosen.get("s_bull_multiple")),
           "probabilities": probs, "decided_by": {k: v["by"] for k, v in chosen.items()}}
    return _gate(out, items)


def _num(dec: dict | None) -> float | None:
    return float(dec["value"]) if dec and dec.get("value") is not None else None


def base_config(config: dict, chosen: dict) -> dict:
    """The company config with the accepted drivers applied (per-year margin fields flat at the mid-cycle value)."""
    cfg = dict(config)
    n = cfg.get("forecast_years", 5)
    for name, ch in chosen.items():
        if ch["value"] is None:
            continue
        f = D.DRIVERS[name][0]
        cfg[f] = [round(ch["value"], 6)] * n if isinstance(cfg.get(f), list) else round(ch["value"], 6)
    return cfg


def step_valuation(c: Ctx) -> dict:
    from grinos import longgamma as LG
    drv, sc = c.out["drivers"], c.out["scenarios"]
    bridge = drv["bridge"]
    nd, sh = bridge["net_debt"], bridge["shares"]
    if nd is None or not sh:
        return {"status": "invalid", "output": {"errors": ["net debt or diluted shares missing from XBRL"]}}
    b = _base(c)
    cfg, base, fy2 = b["config"], b["engine"], b["fy2"]
    path, mid = base["ebit_path"], base["sustainable_ebit"]
    scale = {"Bear": sc["bear_ebit"] / mid, "Base": 1.0, "Bull": sc["bull_ebit"] / mid}
    multiple_base = base["enterprise_value"] / fy2
    lg = {"EBIT FY1": round(path[0], 4), "EBIT FY3": round(path[2], 4) if len(path) > 2 else None,
          "Sustainable EBIT": round(mid, 4),
          **{f"EBIT FY2 {s}": round(fy2 * scale[s], 4) for s in LG.SCENARIOS},
          "Target Multiple Base": round(multiple_base, 4),
          "Target Multiple Bear": sc["bear_multiple"], "Target Multiple Bull": sc["bull_multiple"],
          **{f"Prob {s}": sc["probabilities"][s] for s in LG.SCENARIOS}}
    targets = {s: (lg[f"EBIT FY2 {s}"] * lg[f"Target Multiple {s}"] - nd) / sh for s in LG.SCENARIOS}
    return _done({"config": cfg, "engine": {k: base[k] for k in ("enterprise_value", "value_per_share",
                                                                  "terminal_multiple", "sustainable_ebit", "ebit_path")},
                  "longgamma": lg, "targets": targets, "pw_target": LG.pw_target(lg, nd, sh),
                  "net_debt": nd, "shares": sh, "net_debt_facts": bridge["net_debt_inputs"],
                  "share_facts": bridge["shares_inputs"]})


def risks_for(ledger: Ledger, ticker: str) -> list[dict]:
    latest: dict[str, dict] = {}
    for e in ledger.entries():
        if e["stage"] == "risk" and e["payload"]["ticker"] == ticker:
            latest[e["payload"]["risk_id"]] = e["payload"] | {"by": e["actor"]["name"], "ts": e["ts"]}
    return list(latest.values())


def catalysts_for(ledger: Ledger, ticker: str) -> list[dict]:
    latest: dict[str, dict] = {}
    for e in ledger.entries():
        if e["stage"] == "catalyst" and e["payload"]["ticker"] == ticker:
            latest[e["payload"]["catalyst_id"]] = e["payload"] | {"by": e["actor"]["name"], "ts": e["ts"]}
    return list(latest.values())


def step_risks(c: Ctx) -> dict:
    t = c.request["ticker"]
    active = [r for r in risks_for(c.ledger, t) if r["status"] == "Active"]
    items = []
    if len(active) < 3:
        items.append(_item("k_risks", "risks", f"{len(active)} active risk(s) with kill criteria; add at least 3 "
                           f"(each with a measurable kill criterion)"))
    for iid, what, allowed in (("k_direction", "Direction", DIRECTIONS), ("k_nature", "Structural/Cyclical", NATURES)):
        if not c.decided(iid):
            items.append(_item(iid, "judgement", f"set {what}: one of {sorted(allowed)}"))
    out = {"risks": active, "catalysts": catalysts_for(c.ledger, t),
           "direction": (c.decided("k_direction") or {}).get("text"), "nature": (c.decided("k_nature") or {}).get("text")}
    return _gate(out, items)


def step_validate(c: Ctx) -> dict:
    from grinos import longgamma as LG
    v, problems = c.out["valuation"], []
    lg = v["longgamma"]
    problems += LG.validate(lg)
    if abs(sum(lg[f"Prob {s}"] for s in LG.SCENARIOS) - 1) > 0.01:
        problems.append("probabilities must sum to 1")
    chosen = c.out["drivers"]["chosen"]
    for name, ch in chosen.items():
        if ch["from"] == "xbrl" and not ch.get("fact_ids"):
            problems.append(f"{name} is marked as from XBRL but cites no facts")
        if ch["from"] in ("human", "config") and not ch.get("note"):
            problems.append(f"{name} was set by hand without a note")
    texts = [r.get("risk", "") + " " + r.get("kill_criterion", "") for r in c.out["risks"]["risks"]]
    if any(TRADE_WORDS.search(t) for t in texts):
        problems.append("risk text uses trading language (buy/sell/targets); describe the business risk instead")
    for r in c.out["risks"]["risks"]:
        if not re.search(r"\d", r.get("kill_criterion", "")):
            problems.append(f"kill criterion for '{r['risk']}' has no measurable threshold")
    return ({"status": "invalid", "output": {"problems": problems}} if problems
            else _done({"problems": [], "checked": ["ordering", "probabilities", "provenance", "language",
                                                    "measurable kill criteria"]}))


def step_memo(c: Ctx) -> dict:
    v, drv, rk, sc = c.out["valuation"], c.out["drivers"], c.out["risks"], c.out["scenarios"]
    t = c.request["ticker"]
    lines = [f"# {t} · coverage · {c.request['as_of']}", "",
             "Analysis only: this is not a recommendation to buy or sell.", "",
             f"Direction (your call): {rk['direction']} · {rk['nature']}", "",
             "## Valuation (distval mid-cycle engine)", "",
             f"- Enterprise value {v['engine']['enterprise_value']:,.1f}M; value per share {v['engine']['value_per_share']:,.2f}",
             f"- Terminal multiple {v['engine']['terminal_multiple']:.2f}x on sustainable EBIT "
             f"{v['engine']['sustainable_ebit']:,.1f}M",
             f"- Net debt {v['net_debt']:,.1f}M and diluted shares {v['shares']:,.3f}M (XBRL: "
             + ", ".join(f"{i['concept']} {i['accn']}" for i in v["net_debt_facts"] + v["share_facts"]) + ")",
             "", "| scenario | EBIT FY2 | multiple | target per share | probability |", "|---|---|---|---|---|"]
    for s in ("Bear", "Base", "Bull"):
        lg = v["longgamma"]
        lines.append(f"| {s} | {lg[f'EBIT FY2 {s}']:,.1f} | {lg[f'Target Multiple {s}']:.2f}x | "
                     f"{v['targets'][s]:,.2f} | {lg[f'Prob {s}']:.0%} |")
    lines += ["", f"Probability-weighted target {v['pw_target']:,.2f} (the Notion formula).", "",
              "## Drivers and their sources", ""]
    for name, ch in drv["chosen"].items():
        d = drv["drivers"][name]
        src = (f"XBRL, {d['formula']}, {len(d['years'])} years ({drv['years'][0]}–{drv['years'][-1]})"
               if ch["from"] == "xbrl" else f"{ch['from']} by {ch['by']}: {ch['note']}")
        lines.append(f"- **{name}** {ch['value']}: {src}")
        if d["corroborating_claims"]:
            lines.append(f"  - corroborated by {', '.join(x['claim_id'] for x in d['corroborating_claims'][:5])}")
        if d["conflicting_claims"]:
            lines.append(f"  - disagreeing claims: {', '.join(x['claim_id'] for x in d['conflicting_claims'][:5])}")
    lines += ["", f"Scenario inputs set by: {', '.join(f'{k} {v}' for k, v in sc['decided_by'].items())}", "",
              "## Risks and kill criteria (yours)", ""]
    for r in rk["risks"]:
        lines.append(f"- **{r['risk']}** (likelihood {r['likelihood']}/5, impact {r['impact']}/5"
                     + (f", {r['nature']}" if r.get("nature") else "") + f"). Kill: {r['kill_criterion']}")
    if rk["catalysts"]:
        lines += ["", "## Catalysts", ""] + [f"- {k['event']} · {k['date']} ({k['date_confidence']})" for k in rk["catalysts"]]
    text = "\n".join(lines) + "\n"
    blob = c.ledger.put_blob(text.encode())
    folder = c.ledger.root / "coverage" / c.run_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "memo.md").write_text(text)
    return _done({"memo_blob": blob, "path": str(folder / "memo.md")})


def _run(step: str, c: Ctx) -> dict:
    return globals()[f"step_{step}"](c)


MACHINE = steps.Machine("coverage", STEPS, _run, SYSTEM)


# --- the run -------------------------------------------------------------------------------------

def request_hash(ticker: str, as_of: str, config_text: str) -> str:
    return sha256(canonical({"ticker": ticker, "as_of": as_of, "config": config_text}).encode())[:16]


def start(ledger: Ledger, ticker: str, as_of: date, config_path: str, get: sec.Getter, who: str = "analyst",
          refresh: bool = False) -> dict:
    from grinos.valuation import snapshot
    ticker = ticker.upper()
    snap = snapshot(ledger, ticker, as_of, get, refresh)
    text = Path(config_path).read_text()
    rh = request_hash(ticker, as_of.isoformat(), text)
    h = sha256(f"{rh}|{snap['version']}".encode())[:10]
    run_id = f"cov_{ticker}_{as_of}_{h}"
    if not _request(ledger, run_id):
        previous = [e["run_id"] for e in ledger.entries() if e["stage"] == "coverage_request"
                    and e["payload"]["ticker"] == ticker]
        ledger.append(run_id, "coverage_request", _human(who),
                      {"ticker": ticker, "as_of": as_of.isoformat(), "config_path": str(config_path),
                       "config_blob": ledger.put_blob(text.encode()), "request_hash": rh,
                       "snapshot_version": snap["version"], "supersedes": previous[-1] if previous else None})
    return resume(ledger, run_id)


def _request(ledger: Ledger, run_id: str) -> dict | None:
    return next((e for e in ledger.entries() if e["run_id"] == run_id and e["stage"] == "coverage_request"), None)


def resume(ledger: Ledger, run_id: str) -> dict:
    req = _request(ledger, run_id)
    if not req:
        raise ValueError(f"no coverage run {run_id}")
    p = req["payload"]
    snap = next(e["payload"] for e in reversed(list(ledger.entries())) if e["stage"] == "sec_snapshot"
                and e["payload"]["version"] == p["snapshot_version"])
    st = steps.state(MACHINE, ledger, run_id)
    ctx = Ctx(ledger, run_id, p, snap, decisions=st.decisions)
    return steps.advance(MACHINE, ledger, run_id, ctx, st, ctx.out)


def _items(ledger: Ledger, run_id: str) -> dict[str, dict]:
    last = steps.latest_steps(MACHINE, ledger, run_id)
    items = {i["id"]: i for s in last.values() for i in s.get("blocking", [])}
    return items


ALLOWED = {"sources": {"accept"}, "dispute": {"accept", "reject"}, "driver": {"accept", "value", "keep"},
           "xbrl_review": {"accept", "value"}, "scenario": {"value", "accept"}, "judgement": {"value"},
           "claim": set(), "risks": set()}


REVISABLE = ("d_", "s_bear_", "s_bull_", "s_probs", "k_direction", "k_nature")


def _decided_items(ledger: Ledger, run_id: str) -> dict[str, dict]:
    st = steps.state(MACHINE, ledger, run_id)
    return {t: {"id": t, "kind": d["kind"], **({"driver": t[2:]} if d["kind"] == "driver" else {})}
            for t, d in st.decisions.items()}


def decide(ledger: Ledger, run_id: str, target: str, decision: str, who: str, note: str,
           value: Decimal | float | None = None, text: str | None = None, channel: str = "cli",
           revise: bool = False) -> dict:
    """You resolve one blocking item; `resume` picks it up. The first decision is final, except that
    drivers, scenario inputs and judgements can be revised explicitly (`revise`, with a note): the
    revision is a new entry pointing at the one it replaces, so the history stays in the ledger."""
    if not note.strip() and (decision != "accept" or revise):
        raise ValueError("a decision other than accept needs a note (your reasoning, and the source for a value)")
    items = _decided_items(ledger, run_id) if revise else _items(ledger, run_id)
    if revise and not target.startswith(REVISABLE):
        raise ValueError(f"{target} can't be revised (only drivers, scenario inputs, Direction and Structural/Cyclical)")
    if target not in items:
        raise ValueError(f"{target} is not an open item of {run_id}" if not revise else f"{target} was never decided")
    kind = items[target]["kind"]
    allowed = ALLOWED.get(kind, set())
    if kind == "claim":
        raise ValueError(f"resolve the claim itself: grinos resolve {items[target]['claim_id']} verified|rejected")
    if kind == "risks":
        raise ValueError("add risks with grinos coverage risk … (at least 3, each with a kill criterion)")
    if decision not in allowed:
        raise ValueError(f"{target} ({kind}) takes {sorted(allowed)}")
    if kind == "scenario" and decision == "accept" and target != "s_probs":
        raise ValueError("bear/bull inputs need a value from you (accept is only for the default probabilities)")
    if decision == "value":
        if kind == "judgement":
            allowed_text = DIRECTIONS if target == "k_direction" else NATURES
            if text not in allowed_text:
                raise ValueError(f"{target} takes one of {sorted(allowed_text)}")
        elif value is None:
            raise ValueError("a value decision needs the value")
    if kind == "driver" and decision == "accept":
        drv = (steps.latest_steps(MACHINE, ledger, run_id).get("drivers", {}).get("output") or {}).get("drivers", {})
        if drv.get(items[target]["driver"], {}).get("value") is None:
            raise ValueError("there is no XBRL value to accept: set a value with its source, or keep the config")
    if kind == "dispute" and decision == "reject":
        from grinos.pipeline import human_verdict
        human_verdict(ledger, items[target]["claim_id"], "rejected", who, f"coverage {run_id}: {note}", channel)
    return steps.record_decision(MACHINE, ledger, run_id, target,
                                 {"kind": kind, "decision": decision, "note": note.strip(),
                                  "value": str(value) if value is not None else None, "text": text,
                                  "channel": channel}, who, revise=revise)


# --- risks, catalysts, stage ---------------------------------------------------------------------

def record_risk(ledger: Ledger, ticker: str, risk: str, likelihood: int, impact: int, kill_criterion: str, who: str,
                status: str = "Active", nature: str | None = None, cites: list[str] | None = None,
                risk_id: str | None = None, channel: str = "cli") -> dict:
    """Your risk, in your words. Likelihood and impact are 1–5 (the Notion Risks scale)."""
    if int(likelihood) not in SCORE or int(impact) not in SCORE:
        raise ValueError("likelihood and impact are 1–5")
    if status not in RISK_STATUS:
        raise ValueError(f"status is one of {sorted(RISK_STATUS)}")
    if nature not in (None, "Structural", "Cyclical"):
        raise ValueError("a risk is Structural or Cyclical")
    if not risk.strip() or not kill_criterion.strip():
        raise ValueError("a risk needs its description and a kill criterion")
    rid = risk_id or "k_" + sha256(f"{ticker}|{risk.strip().lower()}".encode())[:10]
    return ledger.append(f"risk_{ticker}", "risk", _human(who),
                         {"risk_id": rid, "ticker": ticker.upper(), "risk": risk.strip(), "likelihood": int(likelihood),
                          "impact": int(impact), "kill_criterion": kill_criterion.strip(), "status": status,
                          "nature": nature,
                          "cites": cites or [], "channel": channel},
                         refs=[f"claim:{c}" for c in cites or []])


def record_catalyst(ledger: Ledger, ticker: str, event: str, when: str, who: str, type_: str = "Results",
                    date_confidence: str = "Window", scope: str = "Company", expected_impact: str = "Uncertain",
                    channel: str = "cli") -> dict:
    date.fromisoformat(when)
    if date_confidence not in {"Exact", "Window"}:
        raise ValueError("date confidence is Exact or Window")
    cid = "t_" + sha256(f"{ticker}|{type_}|{when[:7]}".encode())[:10]
    return ledger.append(f"catalyst_{ticker}", "catalyst", _human(who),
                         {"catalyst_id": cid, "ticker": ticker.upper(), "event": event.strip(), "type": type_,
                          "date": when, "date_confidence": date_confidence, "scope": scope,
                          "status": "Pending", "expected_impact": expected_impact, "channel": channel})


def set_stage(ledger: Ledger, ticker: str, to: str, who: str, note: str, channel: str = "cli") -> dict:
    if to not in STAGES:
        raise ValueError(f"stage is one of {STAGES}")
    if not note.strip():
        raise ValueError("a stage change needs a note")
    return ledger.append(f"stage_{ticker.upper()}", "coverage_stage", _human(who),
                         {"ticker": ticker.upper(), "from": stage(ledger, ticker), "to": to, "note": note.strip(),
                          "channel": channel})


def stage(ledger: Ledger, ticker: str) -> str | None:
    cur = None
    for e in ledger.entries():
        if e["stage"] == "coverage_stage" and e["payload"]["ticker"] == ticker.upper():
            cur = e["payload"]["to"]
    return cur


# --- reading -------------------------------------------------------------------------------------

def view(ledger: Ledger, run_id: str) -> dict:
    out = steps.view(MACHINE, ledger, run_id, "coverage_request")
    t = out["request"]["ticker"]
    out |= {"stage": stage(ledger, t), "risks": risks_for(ledger, t), "catalysts": catalysts_for(ledger, t),
            "steps_order": STEPS}
    memo = out["steps"].get("memo", {}).get("output", {}).get("memo_blob")
    out["memo"] = ledger.get_blob(memo).decode() if memo else None
    return out


def runs(ledger: Ledger) -> list[dict]:
    out: dict[str, dict] = {}
    for e in ledger.entries():
        p = e["payload"]
        if e["stage"] == "coverage_request":
            out[e["run_id"]] = {"run_id": e["run_id"], "ticker": p["ticker"], "as_of": p["as_of"], "status": "started",
                                "step": None, "ts": e["ts"], "supersedes": p.get("supersedes")}
        elif e["stage"] == "coverage_status" and e["run_id"] in out:
            out[e["run_id"]] |= {"status": p["status"], "step": p["step"], "blocking": p["blocking"]}
        elif e["stage"] == "coverage_step" and e["run_id"] in out and p["step"] == "valuation" and p["status"] == "done":
            out[e["run_id"]]["pw_target"] = p["output"].get("pw_target")
    for r in out.values():
        r["stage"] = stage(ledger, r["ticker"])
    return list(out.values())


def summary(r: dict) -> str:
    lines = [f"{r['run_id']}  {r['status']} at {r['step']}"]
    for b in r.get("blocking", []):
        lines.append(f"  needs you: {b['id']} [{b['kind']}] {b['detail']}")
    if r["status"] == "invalid":
        lines += [f"  invalid: {p}" for p in r["out"].get(r["step"], {}).get("problems", [])
                  + r["out"].get(r["step"], {}).get("errors", [])]
    if "valuation" in r["out"] and r["out"]["valuation"].get("pw_target") is not None:
        v = r["out"]["valuation"]
        lines.append("  targets: " + ", ".join(f"{s} {v['targets'][s]:,.2f}" for s in ("Bear", "Base", "Bull"))
                     + f" · PW {v['pw_target']:,.2f}")
    if r["status"] == "complete":
        lines.append(f"  memo: {r['out']['memo']['path']}")
    return "\n".join(lines)


# --- applying accepted drivers to the company YAML (human command) --------------------------------

def config_candidate(ledger: Ledger, run_id: str) -> str:
    """The company YAML with accepted drivers set and a provenance comment on each changed line."""
    v = view(ledger, run_id)
    drv = (v["steps"].get("drivers") or {}).get("output") or {}
    text = ledger.get_blob(v["request"]["config_blob"]).decode()
    cfg = yaml.safe_load(text)
    for name, ch in drv.get("chosen", {}).items():
        if ch["from"] == "config" or ch["value"] is None:
            continue
        f = D.DRIVERS[name][0]
        d = drv["drivers"][name]
        why = (f"XBRL {d['formula']}, {drv['years'][0]}–{drv['years'][-1]}, accepted in {run_id}"
               if ch["from"] == "xbrl" else f"set by {ch['by']} in {run_id}: {ch['note']}")
        val = round(ch["value"], 4)
        if isinstance(cfg.get(f), list):
            new = f"{f}: [{', '.join(str(val) for _ in cfg[f])}]  # {why}"
            text = re.sub(rf"(?ms)^{f}:\s*\n(?:\s*-\s*[^\n]*\n)+", new + "\n", text, count=1) \
                if re.search(rf"(?m)^{f}:\s*$", text) else re.sub(rf"(?m)^{f}:.*$", new, text, count=1)
        else:
            text = re.sub(rf"(?m)^{f}:.*$", f"{f}: {val}  # {why}", text, count=1)
    return text


def apply(ledger: Ledger, run_id: str, who: str, note: str) -> dict:
    if not note.strip():
        raise ValueError("applying a config needs a note")
    v = view(ledger, run_id)
    if (v["status"] or {}).get("status") != "complete":
        raise ValueError(f"{run_id} is not complete")
    text = config_candidate(ledger, run_id)
    yaml.safe_load(text)                                            # must still parse
    path = Path(v["request"]["config_path"])
    before = ledger.put_blob(path.read_bytes())
    path.write_text(text)
    return ledger.append(run_id, "config_applied", _human(who),
                         {"path": str(path), "before_blob": before, "after_blob": ledger.put_blob(text.encode()),
                          "note": note.strip()})

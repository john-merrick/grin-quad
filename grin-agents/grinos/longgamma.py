"""
Completed grinos research → the LongGamma Notion workflow. Two skill contracts, done in code:

    results   an ingest run (results release, filing, transcript) → `longgamma-results-transcript`:
              a Research Note (Type Results/Transcript) with key takeaways from verified claims and a
              *proposed* Valuation Impact from the analyst; a Model task if that impact isn't None.
              Never writes Companies or the Valuation Log.
    sync      a complete FCFF valuation, or an approved mid-cycle proposal → `longgamma-model-sync`:
              Companies driver fields + Model Synced, one Valuation Log row per changed driver
              (≥ 0.5%) with PW Target before → after, each linked to a rationale note in Isaac's words.

Flow, each step a ledger entry:
    prepare   reads Notion (company row, current drivers, latest Investment Thesis), builds the
              exact writes and the diff, stores them in the ledger + var/outbox   ('lg_prepared')
    go        sync only: Isaac reads the diff and says go, with his rationale verbatim ('lg_go')
    push      writes to Notion; the ledger entry is the truth, not the outbox file   ('lg_pushed')

Contract rules enforced here (Schema Contract page): Author/Updated By = Agent; never formula or
rollup properties; percents as decimals; money in millions; search before create; company missing
→ stop and run longgamma-initiate-coverage; PW Target move > 30% needs --confirm TICKER; Thesis
Impact and Direction are Isaac's calls, so a results draft leaves Thesis Impact unset.
"""
import json
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from grinos.env import secret
from grinos.ledger import Ledger
from grinos.notion import DS, NotPublishable, _blocks, _request, _text, find_company
from grinos.proposals import proposal_state
from grinos.schemas import Actor, canonical, sha256

SYSTEM = Actor(kind="system", name="longgamma", version="1")
DS = DS | {"tasks": "7a182d84-de70-4ad6-abfd-6a0f91635cc1", "risks": "52af2810-eb46-4fb0-b5bc-9ad60f04c1b6",
           "catalysts": "0b81c1d4-dea1-4af1-9939-287dd71feb6d"}
MIN_CHANGE = 0.005               # ignore driver changes smaller than 0.5%
BIG_MOVE = 0.30                  # PW Target move needing explicit confirmation
DEFAULT_PROBS = {"Bear": 0.25, "Base": 0.5, "Bull": 0.25}
SCENARIOS = ("Bear", "Base", "Bull")
TRANSCRIPT_KINDS = {"transcript", "call"}
MATERIALITY = {"high": 0, "medium": 1, "low": 2}
# Companies driver fields model-sync may write (Schema Contract), and their Valuation Log mapping.
DRIVER_FIELDS = {
    "EBIT FY1": ("EBIT FY1", "All"), "EBIT FY3": ("EBIT FY3", "All"), "Sustainable EBIT": ("Sustainable EBIT", "All"),
    **{f"EBIT FY2 {s}": ("EBIT FY2", s) for s in SCENARIOS},
    **{f"Target Multiple {s}": ("Target Multiple", s) for s in SCENARIOS},
    **{f"Prob {s}": ("Probabilities", "All") for s in SCENARIOS},
}
READ_FIELDS = list(DRIVER_FIELDS) + ["Net Debt m", "Shares m", "Price"]


def _f(x) -> float | None:
    return None if x is None else float(Decimal(str(x)))


# --- Notion reads (prepare time) --------------------------------------------------------------

def read_company(ticker: str, token: str, request=_request) -> dict | None:
    """The Companies row for `ticker`: page id, current drivers and market data, latest thesis note."""
    cid = find_company(ticker, token, request)
    if not cid:
        return None
    page = request("GET", f"/pages/{cid}", None, token)
    props = page.get("properties", {})
    values = {k: (props.get(k) or {}).get("number") for k in READ_FIELDS}
    res = request("POST", f"/data_sources/{DS['research_notes']}/query", {
        "filter": {"and": [{"property": "Company", "relation": {"contains": cid}},
                           {"property": "Type", "select": {"equals": "Investment Thesis"}}]},
        "sorts": [{"property": "Date", "direction": "descending"}], "page_size": 1}, token)
    thesis = (res.get("results") or [None])[0]
    return {"id": cid, "values": values, "thesis_id": thesis["id"] if thesis else None}


def find_note(title: str, token: str, request=_request) -> str | None:
    res = request("POST", f"/data_sources/{DS['research_notes']}/query",
                  {"filter": {"property": "Title", "title": {"equals": title}}, "page_size": 1}, token)
    hits = res.get("results", [])
    return hits[0]["id"] if hits else None


def find_task(title: str, token: str, request=_request) -> str | None:
    res = request("POST", f"/data_sources/{DS['tasks']}/query",
                  {"filter": {"property": "Task", "title": {"equals": title}}, "page_size": 1}, token)
    hits = res.get("results", [])
    return hits[0]["id"] if hits else None


def _replace_body(page_id: str, blocks: list[dict], token: str, request=_request) -> None:
    old: list[str] = []
    cursor = None
    while True:
        res = request("GET", f"/blocks/{page_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else ""),
                      None, token)
        old += [b["id"] for b in res.get("results", [])]
        cursor = res.get("next_cursor")
        if not res.get("has_more") or not cursor:
            break
    for bid in old:
        request("DELETE", f"/blocks/{bid}", None, token)
    request("PATCH", f"/blocks/{page_id}/children", {"children": blocks}, token)


def _edited_by_someone_else(page_id: str, token: str, request=_request) -> bool:
    """True when the last edit to the page wasn't this integration's (Isaac ticked a to-do, wrote a line…)."""
    page = request("GET", f"/pages/{page_id}", None, token)
    me = request("GET", "/users/me", None, token)
    return (page.get("last_edited_by") or {}).get("id") != me.get("id")


# --- results-transcript (pure) ----------------------------------------------------------------

def _run(ledger: Ledger, run_id: str) -> tuple[dict, list[dict]]:
    source, claims, decisions = None, {}, {}
    for e in ledger.entries():
        if e["run_id"] != run_id:
            continue
        p = e["payload"]
        if e["stage"] == "source":
            source = p["source"]
        elif e["stage"] == "claim":
            claims[p["claim_id"]] = p["claim"]
        elif e["stage"] == "decision":
            decisions[p["claim_id"]] = p
    if source is None:
        raise NotPublishable(f"no ingest run {run_id}")
    served = {c["claim_id"] for c in ledger.verified_claims(source["company"])}
    verified = [{"claim_id": cid, **c, **{k: decisions.get(cid, {}).get(k) for k in ("nature", "materiality")}}
                for cid, c in claims.items() if cid in served]
    return source, verified


def _latest_analysis(ledger: Ledger, company: str, after_ts: str) -> dict | None:
    """The mid-cycle analyst's latest pass over this company's evidence, if it ran after the source."""
    out = None
    for e in ledger.entries():
        if e["stage"] == "analysis_done" and e["payload"]["company"] == company and e["ts"] >= after_ts:
            out = e
    return out


def results_note(ledger: Ledger, run_id: str, company: dict | None, today: str) -> dict:
    source, verified = _run(ledger, run_id)
    ticker = source["company"]
    src_ts = next(e["ts"] for e in ledger.entries() if e["run_id"] == run_id and e["stage"] == "source")
    periods = Counter(c["period"] for c in verified)
    period = periods.most_common(1)[0][0] if periods else str(source["published_at"])
    kind = "Transcript" if source["kind"] in TRANSCRIPT_KINDS else "Results"
    ranked = sorted(verified, key=lambda c: (MATERIALITY.get(c.get("materiality") or "low", 3),
                                             c.get("nature") not in ("structural", "one_off"), c["claim_id"]))
    takeaways = ranked[:3]

    # Compare (deterministic): reported EBIT vs Notion EBIT FY1, pro-rated and said how.
    compare = []
    fy1 = (company or {}).get("values", {}).get("EBIT FY1")
    for c in verified:
        if c["metric"] == "ebit" and c.get("value") is not None and not c.get("adjusted") and fy1:
            p = c["period"].upper()
            scale, how = (4, "quarter × 4") if p.startswith("Q") else (2, "half × 2") if p.startswith("H") else (1, "full year")
            run_rate = c["value"] * scale
            compare.append((c, run_rate, run_rate / fy1 - 1, how))
            break

    analysis = _latest_analysis(ledger, ticker, src_ts)
    impact, val_pct, proposals = "None", 0.0, []
    if analysis:
        state = proposal_state(ledger)
        for pid in analysis["payload"]["proposal_ids"]:
            s = state.get(pid, {})
            if s.get("check") and s["check"]["payload"]["ok"]:
                proposals.append((s["proposal"]["payload"]["proposal"], s["check"]["payload"]))
        ok = [state[pid] for pid in analysis["payload"]["proposal_ids"]
              if state.get(pid, {}).get("check") and state[pid]["check"]["payload"]["ok"]]
        ev = combined(ok)[2] if ok else 0.0
        impact = "Up" if ev > 0.0005 else "Down" if ev < -0.0005 else "None"
        val_pct = round(ev, 4)

    body: list[tuple[str, str]] = [("h2", "Key takeaways")]
    body += [("bullet", f"{c['statement']} — “{c['quote'][:200]}” (verified claim {c['claim_id']})") for c in takeaways] \
        or [("p", "No verified claims from this source.")]
    if compare:
        c, rr, var, how = compare[0]
        body.append(("p", f"Reported EBIT {c['period']} {c['value']:,.1f} → run-rate {rr:,.1f} ({how}; seasonality "
                          f"ignored) vs Notion EBIT FY1 {fy1:,.1f}: {var:+.1%}."))
    body += [("h2", "Thesis impact"),
             ("p", "Not set: Thesis Impact is Isaac's judgement against the variant view and kill criteria."
                   + ("" if (company or {}).get("thesis_id") else " No Investment Thesis found to link."))]
    body.append(("h2", "Valuation impact (proposed, not applied)"))
    if not analysis:
        body.append(("p", f"The mid-cycle analyst has not run on this evidence yet: grinos analyse {ticker}."))
    elif not proposals:
        body.append(("p", "No change proposed: " + analysis["payload"]["no_change"][:1500]))
    for p, chk in proposals:
        body.append(("bullet", f"PROPOSAL: {p['field']} (Base) {chk['impact']['old_value']} → {chk['normalised_value']}, "
                               f"{p['nature']}; distval EV {chk['impact']['ev_change_pct']:+.1%}. {p['rationale'][:600]} "
                               f"Cites {', '.join(p['cites'])}."))
    body += [("h2", "Management tone"), ("p", "Not assessed by the deterministic draft.")]
    body += [("h2", "Follow-ups"), ("todo", "Set Thesis Impact (Confirms / Neutral / Challenges)")]
    if impact != "None":
        body.append(("todo", "Decide whether the proposed valuation impact is real; if so update the model and sync"))
    body += [("h2", "Audit trail"),
             ("p", f"grinos run {run_id} · {len(verified)} verified claims · source blob {source['blob'][:16]} · "
                   f"reproduce: grinos show {run_id}")]
    note = {"Title": f"{ticker} · {kind} · {period}", "Type": kind, "Date": str(source["published_at"]),
            "Source URL": source["uri"] if str(source["uri"]).startswith("http") else None,
            "Valuation Impact": impact, "Val Impact %": val_pct, "Author": "Agent", "body": body}
    task = None
    if impact != "None":
        task = {"Task": f"Update model: {ticker} after {period}", "Type": "Model", "Status": "Not started",
                "Due": (date.fromisoformat(today) + timedelta(days=2)).isoformat(), "Author": "Agent"}
    return {"kind": "results", "source_id": run_id, "ticker": ticker, "note": note, "task": task,
            "feeds_into": (company or {}).get("thesis_id")}


# --- model-sync (pure) ------------------------------------------------------------------------

def drivers_from_valuation(view: dict) -> dict:
    """FCFF valuation → LongGamma drivers. Target multiple = EV ÷ EBIT FY2 per scenario, so Notion's
    target, (EBIT FY2 × multiple − net debt) ÷ shares, reproduces the DCF when its net debt and shares match."""
    st = view["steps"]
    val = st["valuation"]["output"]
    sc = {s: val["scenarios"][s.lower()] for s in SCENARIOS}
    d: dict[str, float | None] = {
        "EBIT FY1": _f(sc["Base"]["years"][0]["ebit"]), "EBIT FY3": _f(sc["Base"]["years"][2]["ebit"]),
        "Sustainable EBIT": _f(st["adjustments"]["output"]["normalised"][-1]["normalised_operating_income"])}
    for s in SCENARIOS:
        e2, ev = _f(sc[s]["years"][1]["ebit"]), _f(sc[s]["enterprise_value"])
        d[f"EBIT FY2 {s}"] = e2
        d[f"Target Multiple {s}"] = round(ev / e2, 4) if e2 and ev is not None else None
    probs = (view["request"].get("assumptions") or {}).get("probabilities") or {}
    for s in SCENARIOS:
        d[f"Prob {s}"] = _f(probs.get(s.lower())) if probs else None
    return {k: (round(v, 4) if v is not None else None) for k, v in d.items()} | {
        "_dcf_value_per_share": {s: round(_f(sc[s]["value_per_share"]) or 0, 4) for s in SCENARIOS}}


def combined(states: list[dict]) -> tuple[dict, dict, float]:
    """Proposals from one analysis run applied together to its config and valued once by distval:
    (config after, engine view after, EV change). Their separate impacts don't add up."""
    from grinos.proposal_check import apply, engine_view
    p0 = states[0]["proposal"]["payload"]
    cfg = p0["config"]
    for s in states:
        cfg = apply(cfg, s["proposal"]["payload"]["proposal"]["field"], s["check"]["payload"]["normalised_value"])
    before = engine_view(p0["config"], p0["net_debt"], p0["shares"])
    after = engine_view(cfg, p0["net_debt"], p0["shares"])
    return cfg, after, round(after["enterprise_value"] / before["enterprise_value"] - 1, 4)


def approved_in_run(ledger: Ledger, analysis_id: str) -> list[dict]:
    return [s for s in proposal_state(ledger).values()
            if s["proposal"] and s["proposal"]["run_id"] == analysis_id
            and s["approval"] and s["approval"]["payload"]["decision"] == "approve"]


def drivers_from_analysis(ledger: Ledger, analysis_id: str) -> tuple[str, dict, list[str]]:
    """Every approved proposal of one analyst run → one set of drivers (base case only)."""
    states = approved_in_run(ledger, analysis_id)
    if not states:
        raise NotPublishable(f"no approved proposals in {analysis_id}")
    _, after, _ = combined(states)
    return (states[0]["proposal"]["payload"]["company"], _drivers(after),
            [s["proposal"]["payload"]["proposal_id"] for s in states])


def drivers_from_proposal(ledger: Ledger, proposal_id: str) -> dict:
    """An approved mid-cycle proposal → the drivers the distval engine moved (base case only). Refuses when
    other proposals of the same run are approved: syncing them one by one would leave only the last in Notion."""
    s = proposal_state(ledger).get(proposal_id)
    if not s or not s["approval"] or s["approval"]["payload"]["decision"] != "approve":
        raise NotPublishable(f"{proposal_id} has not been approved")
    run = s["proposal"]["run_id"]
    if len(approved_in_run(ledger, run)) > 1:
        raise NotPublishable(f"{proposal_id} is one of several approved proposals in {run}: grinos capture sync {run}")
    return _drivers(s["check"]["payload"]["impact"]["after"])


def _drivers(a: dict) -> dict:
    path = a["ebit_path"]
    return {"EBIT FY1": round(path[0], 4) if path else None, "EBIT FY2 Base": round(path[1], 4) if len(path) > 1 else None,
            "EBIT FY3": round(path[2], 4) if len(path) > 2 else None, "Sustainable EBIT": round(a["sustainable_ebit"], 4),
            "Target Multiple Base": round(a["terminal_multiple"], 4)}


def validate(d: dict) -> list[str]:
    problems = []
    probs = [d.get(f"Prob {s}") for s in SCENARIOS]
    given = [p for p in probs if p is not None]
    if given:
        if len(given) < len(probs) or abs(sum(given) - 1) > 0.01:
            problems.append(f"probabilities must all be set and sum to 1.00 ±0.01, got {probs}")
        if any(p > 1 for p in given):
            problems.append("probabilities are decimals (0.25), not whole percents")
    for base in ("EBIT FY2", "Target Multiple"):
        v = [d.get(f"{base} {s}") for s in SCENARIOS]
        lo, mid, hi = v
        if lo is not None and mid is not None and hi is not None and not lo <= mid <= hi:
            problems.append(f"{base}: need Bear ≤ Base ≤ Bull, got {v}")
    return problems


def pw_target(d: dict, net_debt: float | None, shares: float | None) -> float | None:
    """Notion's formula, in code: Target = (EBIT FY2 × multiple − net debt) ÷ shares; probs default 25/50/25."""
    if not shares or net_debt is None:
        return None
    probs = {s: d.get(f"Prob {s}") for s in SCENARIOS}
    if any(v is None for v in probs.values()):
        probs = dict(DEFAULT_PROBS)
    total = 0.0
    for s in SCENARIOS:
        e2, m = d.get(f"EBIT FY2 {s}"), d.get(f"Target Multiple {s}")
        if e2 is None or m is None:
            return None
        total += probs[s] * (e2 * m - net_debt) / shares
    return round(total, 4)


def sync_plan(ticker: str, new: dict, company: dict, today: str) -> dict:
    cur = company["values"]
    changed = {}
    for k in DRIVER_FIELDS:
        v = new.get(k)
        if v is None:
            continue
        old = cur.get(k)
        if old is None or old == 0 or abs(v / old - 1) >= MIN_CHANGE:
            changed[k] = (old, v)
    after = {k: (changed[k][1] if k in changed else cur.get(k)) for k in DRIVER_FIELDS}
    nd, sh = cur.get("Net Debt m"), cur.get("Shares m")
    before_pw, after_pw = pw_target(cur, nd, sh), pw_target(after, nd, sh)
    rows, prob_logged = [], False
    for k, (old, v) in changed.items():
        field, scenario = DRIVER_FIELDS[k]
        if field == "Probabilities":
            if prob_logged:
                continue
            prob_logged = True
            old_v, new_v = None, None
            change = f"{ticker} · Probabilities"
        else:
            old_v, new_v = old, v
            change = f"{ticker} · {field}" + (f" {scenario}" if scenario != "All" else "")
        rows.append({"Change": change, "Field": field, "Scenario": scenario, "Date": today, "Old Value": old_v,
                     "New Value": new_v, "PW Target Before": before_pw, "PW Target After": after_pw, "Author": "Agent"})
    if len(changed) > 5:
        rows.append({"Change": f"{ticker} · Model Rebuild", "Field": "Model Rebuild", "Scenario": "All", "Date": today,
                     "Old Value": None, "New Value": None, "PW Target Before": before_pw, "PW Target After": after_pw,
                     "Author": "Agent"})
    move = (after_pw / before_pw - 1) if before_pw and after_pw else None
    return {"changed": {k: list(v) for k, v in changed.items()}, "companies": {k: v for k, (_, v) in changed.items()},
            "valuation_log": rows, "pw_before": before_pw, "pw_after": after_pw, "pw_move": move,
            "needs_confirm": move is not None and abs(move) > BIG_MOVE, "net_debt": nd, "shares": sh}


def drivers_from_coverage(ledger: Ledger, run_id: str) -> tuple[str, dict]:
    """A complete coverage run → its LongGamma drivers (all three scenarios, set by Isaac in the run)."""
    from grinos import coverage
    v = coverage.view(ledger, run_id)
    if (v["status"] or {}).get("status") != "complete":
        raise NotPublishable(f"{run_id} is not complete")
    lg = v["steps"]["valuation"]["output"]["longgamma"]
    return v["request"]["ticker"], {k: val for k, val in lg.items() if val is not None}


def risk_row(r: dict) -> dict:
    """A coverage risk (Isaac's words and scores) → a Notion Risks row."""
    return {"Risk": r["risk"], "Likelihood": r["likelihood"], "Impact": r["impact"], "Status": r["status"],
            "Kill Criterion": r["kill_criterion"], "Structural/Cyclical": r.get("nature"), "Author": "Human",
            "_risk_id": r["risk_id"]}


def catalyst_row(ticker: str, k: dict) -> dict:
    event = k["event"] if k["event"].startswith(f"{ticker} · ") else f"{ticker} · {k['type']} · {k['event']}"
    return {"Event": event, "Type": k["type"], "Date": k["date"], "Date Confidence": k["date_confidence"],
            "Scope": k["scope"], "Status": k["status"], "Expected Impact": k["expected_impact"], "Author": "Human",
            "_catalyst_id": k["catalyst_id"]}


RISK_KINDS = {"Risk": "title", "Status": "select", "Kill Criterion": "text", "Structural/Cyclical": "select",
              "Author": "select"}
CATALYST_KINDS = {"Event": "title", "Type": "select", "Date": "date", "Date Confidence": "select", "Scope": "select",
                  "Status": "select", "Expected Impact": "select", "Author": "select"}
CATALYST_DEDUP_DAYS = 14


def _query(ds: str, flt: dict, token: str, request) -> list[dict]:
    return request("POST", f"/data_sources/{DS[ds]}/query", {"filter": flt, "page_size": 25}, token).get("results", [])


def push_risks(p: dict, rel: list[dict], token: str, request) -> dict:
    """Search before create: a risk with the same title for the company is updated; a catalyst of the same
    company and type within ±14 days is the same event (catalyst-scan's rule)."""
    out: dict[str, list] = {"risks": [], "catalysts": []}
    cid = rel[0]["id"]
    for r in p["risks"]:
        props = _props({k: v for k, v in r.items() if not k.startswith("_")}, RISK_KINDS) | {"Company": {"relation": rel}}
        hits = _query("risks", {"and": [{"property": "Company", "relation": {"contains": cid}},
                                        {"property": "Risk", "title": {"equals": r["Risk"]}}]}, token, request)
        page = (request("PATCH", f"/pages/{hits[0]['id']}", {"properties": props}, token) if hits else
                request("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": DS["risks"]},
                                           "properties": props}, token))
        out["risks"].append({"id": page["id"], "url": page.get("url"), "updated": bool(hits)})
    for k in p["catalysts"]:
        props = _props({x: v for x, v in k.items() if not x.startswith("_")}, CATALYST_KINDS) | {"Company": {"relation": rel}}
        d = date.fromisoformat(k["Date"])
        hits = _query("catalysts", {"and": [
            {"property": "Company", "relation": {"contains": cid}},
            {"property": "Type", "select": {"equals": k["Type"]}},
            {"property": "Date", "date": {"on_or_after": (d - timedelta(days=CATALYST_DEDUP_DAYS)).isoformat()}},
            {"property": "Date", "date": {"on_or_before": (d + timedelta(days=CATALYST_DEDUP_DAYS)).isoformat()}}]},
            token, request)
        page = (request("PATCH", f"/pages/{hits[0]['id']}", {"properties": props}, token) if hits else
                request("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": DS["catalysts"]},
                                           "properties": props}, token))
        out["catalysts"].append({"id": page["id"], "url": page.get("url"), "updated": bool(hits)})
    return out


# --- prepare / go / push ------------------------------------------------------------------------

def _id(kind: str, source_id: str) -> str:
    return f"lg_{kind}_{source_id}"


def _entries(ledger: Ledger, lg_id: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in ledger.entries():
        if e["run_id"] == lg_id and e["stage"] in ("lg_prepared", "lg_go", "lg_pushed"):
            out[e["stage"]] = e                     # latest of each
    return out


def _pushed(es: dict[str, dict]) -> bool:
    """The latest prepared version is already in Notion."""
    return "lg_pushed" in es and "lg_prepared" in es and es["lg_pushed"]["payload"]["prepared"] == es["lg_prepared"]["hash"]


def prepare(ledger: Ledger, kind: str, source_id: str, token: str | None = None, request=_request,
            today: str | None = None, refresh: bool = False) -> dict:
    """Read Notion, build the exact writes, record them. kind: results | sync.
    refresh: rebuild a results note already in Notion (new evidence or a re-run analyst); a sync is never refreshed."""
    token = token or secret("NOTION_TOKEN")
    if not token:
        raise NotPublishable("NOTION_TOKEN not set: prepare reads the current Notion values")
    if ledger.audit():
        raise NotPublishable("ledger fails audit; refusing to prepare")
    lg_id = _id(kind, source_id)
    if refresh and kind not in ("results", "risks"):
        raise NotPublishable("only a results note or a risk set can be refreshed; a model sync is a decision, make a new one")
    if "lg_pushed" in _entries(ledger, lg_id) and not refresh:
        raise NotPublishable(f"{lg_id} is already in Notion (grinos capture results {source_id} --refresh to rebuild it)")
    today = today or date.today().isoformat()
    if kind == "results":
        ticker = _run(ledger, source_id)[0]["company"]
        company = read_company(ticker, token, request)
        payload = results_note(ledger, source_id, company, today)
    elif kind == "sync":
        if source_id.startswith("val_"):
            from grinos import valuation
            view = valuation.view(ledger, source_id)
            if (view["status"] or {}).get("status") != "complete":
                raise NotPublishable(f"{source_id} is not complete")
            ticker, new = view["request"]["ticker"], drivers_from_valuation(view)
        elif source_id.startswith("analysis_"):
            ticker, new, included = drivers_from_analysis(ledger, source_id)
            new = new | {"_proposals": included}
        elif source_id.startswith("cov_"):
            ticker, new = drivers_from_coverage(ledger, source_id)
        else:
            s = proposal_state(ledger).get(source_id) or {}
            ticker, new = (s.get("proposal") or {}).get("payload", {}).get("company"), drivers_from_proposal(ledger, source_id)
        problems = validate(new)
        if problems:
            raise NotPublishable("validation failed: " + "; ".join(problems))
        company = read_company(ticker, token, request)
        payload = {"kind": "sync", "source_id": source_id, "ticker": ticker, "drivers": new,
                   "plan": sync_plan(ticker, new, company, today) if company else None}
    elif kind == "risks":
        from grinos import coverage
        v = coverage.view(ledger, source_id)
        if (v["status"] or {}).get("status") != "complete":
            raise NotPublishable(f"{source_id} is not complete")
        ticker = v["request"]["ticker"]
        company = read_company(ticker, token, request)
        payload = {"kind": "risks", "source_id": source_id, "ticker": ticker,
                   "risks": [risk_row(r) for r in v["risks"]],
                   "catalysts": [catalyst_row(ticker, k) for k in v["catalysts"]]}
    else:
        raise ValueError("kind is results, sync or risks")
    if not company:
        raise NotPublishable(f"{ticker} is not in Notion Companies: run longgamma-initiate-coverage first")
    payload |= {"company_id": company["id"], "today": today}
    payload["hash"] = sha256(canonical(payload).encode())
    outbox = ledger.root / "outbox"
    outbox.mkdir(exist_ok=True)
    (outbox / f"{lg_id}.json").write_text(json.dumps(payload, indent=2, default=str))
    ledger.append(lg_id, "lg_prepared", SYSTEM, payload)
    return payload


def go(ledger: Ledger, lg_id: str, who: str, rationale: str, confirm: str | None = None, channel: str = "cli") -> dict:
    """Isaac's go for a sync, with his rationale (written verbatim into the rationale note)."""
    es = _entries(ledger, lg_id)
    if "lg_prepared" not in es:
        raise NotPublishable(f"{lg_id} has not been prepared")
    p = es["lg_prepared"]["payload"]
    if p["kind"] != "sync":
        raise NotPublishable("only a model sync needs a go; push a results note directly")
    if not rationale.strip():
        raise NotPublishable("a rationale is required: never invented, it is your words")
    if p["plan"]["needs_confirm"] and confirm != p["ticker"]:
        raise NotPublishable(f"PW Target moves {p['plan']['pw_move']:+.0%}: confirm with --confirm {p['ticker']}")
    return ledger.append(lg_id, "lg_go", Actor(kind="human", name=who, version="-"),
                         {"prepared": es["lg_prepared"]["hash"], "rationale": rationale.strip(), "channel": channel})


def _props(values: dict, kinds: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in values.items():
        if v is None:
            continue
        t = kinds.get(k, "number")
        out[k] = ({"title": _text(v)} if t == "title" else {"rich_text": _text(v)} if t == "text"
                  else {"select": {"name": v}} if t == "select"
                  else {"date": {"start": v}} if t == "date" else {"url": v} if t == "url"
                  else {"number": v})
    return out


NOTE_KINDS = {"Title": "title", "Type": "select", "Author": "select", "Date": "date", "Source URL": "url",
              "Valuation Impact": "select", "Thesis Impact": "select"}
LOG_KINDS = {"Change": "title", "Field": "select", "Scenario": "select", "Author": "select", "Date": "date"}
TASK_KINDS = {"Task": "title", "Type": "select", "Status": "select", "Author": "select", "Due": "date"}


def push(ledger: Ledger, lg_id: str, token: str | None = None, request=_request) -> dict:
    token = token or secret("NOTION_TOKEN")
    if not token:
        raise NotPublishable("NOTION_TOKEN not set")
    es = _entries(ledger, lg_id)
    if "lg_prepared" not in es:
        raise NotPublishable(f"{lg_id} has not been prepared")
    if _pushed(es):
        raise NotPublishable(f"{lg_id} is already in Notion")
    refreshing = "lg_pushed" in es
    p = es["lg_prepared"]["payload"]
    rel = [{"id": p["company_id"]}]
    written: dict[str, Any] = {}
    if p["kind"] == "risks":
        written |= push_risks(p, rel, token, request)
    elif p["kind"] == "results":
        n = {k: v for k, v in p["note"].items() if k != "body"}
        props = _props(n, NOTE_KINDS) | {"Company": {"relation": rel}}
        if p.get("feeds_into"):
            props["Feeds Into"] = {"relation": [{"id": p["feeds_into"]}]}
        blocks = _blocks([tuple(b) for b in p["note"]["body"]])
        existing = find_note(n["Title"], token, request)                 # search before create
        if existing:
            body_was = "kept"
            if refreshing:
                # Our own earlier draft: rebuild it, unless Isaac has edited it since; then add the update below his edits.
                if _edited_by_someone_else(existing, token, request):
                    request("PATCH", f"/blocks/{existing}/children", {"children": _blocks(
                        [("h2", f"Update {p['today']} (agent; earlier draft above is superseded)")]) + blocks}, token)
                    body_was = "appended"
                else:
                    _replace_body(existing, blocks, token, request)
                    body_was = "replaced"
            page = request("PATCH", f"/pages/{existing}", {"properties": props}, token)
        else:
            body_was = "created"
            page = request("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": DS["research_notes"]},
                                              "properties": props, "children": blocks}, token)
        written["research_note"] = {"id": page["id"], "url": page.get("url"), "updated": bool(existing), "body": body_was}
        if p.get("task"):
            tid = find_task(p["task"]["Task"], token, request)            # search before create
            t = {"id": tid, "url": None} if tid else request(
                "POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": DS["tasks"]},
                                   "properties": _props(p["task"], TASK_KINDS) | {"Company": {"relation": rel}}}, token)
            written["task"] = {"id": t["id"], "url": t.get("url"), "existing": bool(tid)}
    else:
        if "lg_go" not in es or es["lg_go"]["payload"]["prepared"] != es["lg_prepared"]["hash"]:
            raise NotPublishable(f"{lg_id} needs your go: grinos longgamma go {lg_id} --rationale \"…\"")
        g, plan = es["lg_go"], p["plan"]
        if not plan["valuation_log"]:
            raise NotPublishable("no driver changed by 0.5% or more: nothing to sync")
        body = [("p", g["payload"]["rationale"]), ("h2", "Drivers synced"),
                *[("bullet", f"{k}: {old} → {new}") for k, (old, new) in plan["changed"].items()],
                ("p", f"PW Target {plan['pw_before']} → {plan['pw_after']} (Notion formula inputs: net debt "
                      f"{plan['net_debt']}, shares {plan['shares']})."),
                ("h2", "Audit trail"),
                ("p", f"Source {p['source_id']} · prepared {es['lg_prepared']['hash'][:16]} · go by "
                      f"{g['actor']['name']} {g['ts']} · ledger {g['hash'][:16]}")]
        if p["drivers"].get("_proposals"):
            body.insert(1, ("p", "Approved proposals applied together: " + ", ".join(p["drivers"]["_proposals"])))
        dcf = p["drivers"].get("_dcf_value_per_share")
        if dcf:
            body.insert(3, ("p", "DCF value per share: " + ", ".join(f"{s} {v:,.2f}" for s, v in dcf.items())))
        note = request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": DS["research_notes"]},
            "properties": _props({"Title": f"{p['ticker']} · Model sync · {p['today']}", "Type": "Thesis Update",
                                  "Author": "Agent", "Date": p["today"]}, NOTE_KINDS) | {"Company": {"relation": rel}},
            "children": _blocks(body)}, token)
        request("PATCH", f"/pages/{p['company_id']}", {"properties": _props(
            plan["companies"] | {"Model Synced": p["today"], "Updated By": "Agent"},
            {"Model Synced": "date", "Updated By": "select"})}, token)
        logs = []
        for r in plan["valuation_log"]:
            logs.append(request("POST", "/pages", {
                "parent": {"type": "data_source_id", "data_source_id": DS["valuation_log"]},
                "properties": _props(r, LOG_KINDS) | {"Company": {"relation": rel},
                                                       "Rationale Note": {"relation": [{"id": note["id"]}]}}}, token))
        written |= {"rationale_note": {"id": note["id"], "url": note.get("url")},
                    "valuation_log": [{"id": x["id"], "url": x.get("url")} for x in logs], "company": p["company_id"]}
    entry = ledger.append_if(lg_id, "lg_pushed", SYSTEM, {"prepared": es["lg_prepared"]["hash"], **written},
                             guard=lambda all_: f"{lg_id} pushed concurrently" if any(
                                 e["run_id"] == lg_id and e["stage"] == "lg_pushed"
                                 and e["payload"]["prepared"] == es["lg_prepared"]["hash"] for e in all_) else None)
    return entry["payload"]


# --- what's waiting ----------------------------------------------------------------------------

def pending(ledger: Ledger) -> list[dict]:
    """Completed research not yet captured in Notion: ingest runs, complete valuations, approved proposals."""
    last: dict[str, str] = {}
    for e in ledger.entries():
        if e["stage"] in ("lg_prepared", "lg_go", "lg_pushed"):
            last[e["run_id"]] = e["stage"]
    done = {k for k, v in last.items() if v == "lg_pushed"}       # a refresh prepared since the push is pending again
    prepared = set(last)
    out: list[dict] = []
    seen_blobs: set[str] = set()
    state = proposal_state(ledger)
    runs_of = {pid: s["proposal"]["run_id"] for pid, s in state.items() if s["proposal"]}
    approved_per_run = Counter(runs_of[pid] for pid, s in state.items()
                               if s["proposal"] and s["approval"] and s["approval"]["payload"]["decision"] == "approve")
    for e in ledger.entries():
        if e["stage"] == "source":
            blob = e["payload"]["source"]["blob"]
            if blob in seen_blobs:          # a re-check of a source already ingested is not a new release
                continue
            seen_blobs.add(blob)
            sid = e["run_id"]
            kind = "results"
            label = f"{e['payload']['source']['company']} {e['payload']['source']['kind']} {e['payload']['source']['published_at']}"
        elif e["stage"] == "valuation_status" and e["payload"]["status"] == "complete":
            sid, kind, label = e["run_id"], "sync", f"valuation {e['run_id']}"
        elif e["stage"] == "coverage_status" and e["payload"]["status"] == "complete":
            for k, lab in (("sync", "coverage drivers"), ("risks", "coverage risks and catalysts")):
                lg = _id(k, e["run_id"])
                if lg not in done and all(x["id"] != lg for x in out):
                    out.append({"id": lg, "kind": k, "source_id": e["run_id"], "label": f"{lab} {e['run_id']}",
                                "state": "prepared" if lg in prepared else "not prepared"})
            continue
        elif e["stage"] == "approval" and e["payload"]["decision"] == "approve":
            # A run's approved proposals sync together (one at a time, the last would overwrite the others).
            run = runs_of.get(e["payload"]["proposal_id"])
            sid, kind = run, "sync"
            label = f"approved proposals of {run} ({approved_per_run.get(run, 0)})"
        else:
            continue
        lg_id = _id(kind, sid)
        if lg_id not in done and all(x["id"] != lg_id for x in out):
            out.append({"id": lg_id, "kind": kind, "source_id": sid, "label": label,
                        "state": "prepared" if lg_id in prepared else "not prepared"})
    return out


def describe(payload: dict) -> str:
    """The diff table / note preview shown before go or push."""
    if payload["kind"] == "risks":
        lines = [f"Risks and catalysts for {payload['ticker']} from {payload['source_id']} (your entries, Author Human)"]
        lines += [f"  risk: {r['Risk']} · L{r['Likelihood']} × I{r['Impact']} · {r['Status']} · kill: {r['Kill Criterion']}"
                  for r in payload["risks"]]
        lines += [f"  catalyst: {k['Event']} · {k['Date']} ({k['Date Confidence']})" for k in payload["catalysts"]]
        return "\n".join(lines)
    if payload["kind"] == "results":
        n = payload["note"]
        lines = [f"Research Note: {n['Title']} · {n['Type']} · Valuation Impact {n['Valuation Impact']} "
                 f"({n['Val Impact %']:+.2%}) · Feeds Into {'thesis' if payload.get('feeds_into') else '— (no thesis found)'}"]
        lines += [f"  {kind}: {text[:160]}" for kind, text in n["body"] if kind in ("bullet", "todo")]
        if payload.get("task"):
            lines.append(f"Task: {payload['task']['Task']} (due {payload['task']['Due']})")
        return "\n".join(lines)
    plan = payload["plan"]
    lines = [f"Model sync {payload['ticker']} from {payload['source_id']}",
             "  field                     old          new"]
    lines += [f"  {k:<24} {str(old):>10}   {str(new):>10}" for k, (old, new) in plan["changed"].items()]
    lines.append(f"  PW Target {plan['pw_before']} → {plan['pw_after']}"
                 + (f" ({plan['pw_move']:+.1%})" if plan["pw_move"] is not None else "")
                 + ("   ⚠ > 30%: needs --confirm " + payload["ticker"] if plan["needs_confirm"] else ""))
    dcf = payload["drivers"].get("_dcf_value_per_share")
    if dcf:
        lines.append("  DCF value per share: " + ", ".join(f"{s} {v:,.2f}" for s, v in dcf.items())
                     + f"  (Notion PT matches when its net debt {plan['net_debt']} and shares {plan['shares']} "
                       f"equal the valuation's bridge)")
    return "\n".join(lines)


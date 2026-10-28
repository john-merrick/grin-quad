"""
Read models for the dashboard's Agents pages: every agent, then everything about one agent, down to a single
eval case or a single production call. Pure reads of the filesystem (evals, traces) and the ledger.
"""
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from grinos import improve, optimize, trace
from grinos.ledger import Ledger
from grinos.llm import profile

ROOT = Path(__file__).parents[2]
EVALS = ROOT / "evals"
RESULTS = EVALS            # where eval result folders live (tests point this elsewhere)
SAFE = re.compile(r"^[\w.\-]+$")


def _suite() -> dict:
    sys.path.insert(0, str(EVALS))
    from run_all import EVALS as E
    return E


def _status() -> dict:
    p = EVALS / "STATUS.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _history(agent: str | None = None) -> list[dict]:
    p = EVALS / "history.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    return [r for r in rows if agent is None or r.get("agent") == agent]


def _ops(agent: str, days: int) -> list[dict]:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    return [r for r in trace.ops(trace.read(days=days), since) if r["agent"] == agent]


def agents(ledger: Ledger) -> list[dict]:
    status, out = _status(), []
    pending = improve.promotions(ledger)
    for agent, (ev, _) in _suite().items():
        p = profile(agent)
        s = status.get(agent) or {}
        ops = _ops(agent, 7)
        out.append({"agent": agent, "eval": ev, "model": p.model, "skills": p.skills, "tools": p.tools,
                    "version": p.version, "passed": s.get("passed"), "live": bool(s.get("passed")) and s.get("prompt_version") == p.version,
                    "last_eval": s.get("date"), "eval_cost_usd": s.get("cost_usd"), "gates": s.get("gates") or {},
                    "calls_7d": sum(r["calls"] for r in ops), "errors_7d": sum(r["errors"] for r in ops),
                    "cost_7d": round(sum(r["cost_usd"] for r in ops), 4),
                    "pending_promotions": sum(1 for x in pending if x["agent"] == agent)})
    return out


def _runs(agent: str) -> list[dict]:
    ev, _ = _suite()[agent]
    dirs = [("eval", d) for d in (RESULTS / ev / "results").glob("*/") if (d / "report.json").exists()] \
        if (RESULTS / ev / "results").exists() else []
    from grinos.cli import default_root
    rsi = default_root() / "rsi" / "evals" / agent
    dirs += [("rsi", d) for d in rsi.glob("*/") if (d / "report.json").exists()] if rsi.exists() else []
    out = []
    for kind, d in dirs:
        rep = json.loads((d / "report.json").read_text())
        s = rep.get("summary", {})
        rows = rep.get("rows") or rep.get("cases") or []
        m = re.match(r"(\d{8}-\d{6})_(\w[\w-]*?)_([\w.-]+)$", d.name)
        out.append({"id": f"{kind}:{d.name}", "kind": kind, "date": m[1] if m else datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y%m%d-%H%M%S"),
                    "system": m[2] if m else ("candidate" if d.name.startswith("candidate") else "current"),
                    "model": m[3] if m else s.get("model"), "passed": s.get("passed"), "cost_usd": s.get("cost_usd"),
                    "metrics": {k: v for k, v in s.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
                                and k not in ("n", "repeats", "cost_usd")},
                    "gates": s.get("gates") or {}, "cases": len(rows),
                    "failed": (sum(1 for r in rows if r.get("case_pass") is False)
                               if any("case_pass" in r for r in rows) else None)})   # older runs had no per-case result
    return sorted(out, key=lambda r: r["date"], reverse=True)


def _run_dir(agent: str, run_id: str) -> Path:
    kind, _, name = run_id.partition(":")
    if not SAFE.match(name) or kind not in ("eval", "rsi"):
        raise KeyError("no such run")
    if kind == "eval":
        d = RESULTS / _suite()[agent][0] / "results" / name
    else:
        from grinos.cli import default_root
        d = default_root() / "rsi" / "evals" / agent / name
    if not (d / "report.json").exists():
        raise KeyError("no such run")
    return d


def run_detail(agent: str, run_id: str) -> dict:
    d = _run_dir(agent, run_id)
    rep = json.loads((d / "report.json").read_text())
    rows = rep.get("rows") or rep.get("cases") or []
    files = {f.name for f in d.glob("*.json") if f.name != "report.json"}
    out = []
    for r in rows:
        f = f"{r.get('id')}.r{r.get('repeat', 0)}.json"
        out.append({"id": r.get("id"), "repeat": r.get("repeat", 0), "case_pass": r.get("case_pass"),
                    "file": f if f in files else None,
                    "fields": {k: v for k, v in r.items() if k not in ("id", "repeat", "case_pass", "drafts")
                               and not isinstance(v, (dict, list))}})
    return {"run_id": run_id, "summary": rep.get("summary", {}), "rows": out}


def case_detail(agent: str, run_id: str, fname: str) -> dict:
    if not SAFE.match(fname) or not fname.endswith(".json"):
        raise KeyError("no such case")
    p = _run_dir(agent, run_id) / fname
    if not p.exists():
        raise KeyError("no such case")
    return json.loads(p.read_text())


def calls(agent: str, days: int = 14, limit: int = 60) -> list[dict]:
    recs = [r for r in trace.read(days=days) if r.get("type") == "generation" and r.get("name") == agent]
    return [{k: r.get(k) for k in ("start", "model", "prompt_version", "duration_ms", "cost_usd", "level", "status",
                                   "trace_id", "span_id")} | {"tokens": r.get("usage")} for r in recs[-limit:]][::-1]


def trace_detail(trace_id: str) -> list[dict]:
    if not re.fullmatch(r"[0-9a-f]{16,64}", trace_id):
        raise KeyError("no such trace")
    recs = [r for r in trace.read(days=30) if r.get("trace_id") == trace_id]
    if not recs:
        raise KeyError("no such trace")
    return sorted(recs, key=lambda r: r.get("start", ""))


def agent_detail(ledger: Ledger, agent: str) -> dict:
    if agent not in _suite():
        raise KeyError(f"no agent {agent}")
    p = profile(agent)
    s = _status().get(agent) or {}
    pending = [improve.promotion_view(ledger, x) | {"variant": x.get("variant"), "cost_before": x.get("cost_before"),
                                                    "cost_after": x.get("cost_after"), "all_results": x.get("all_results")}
               for x in improve.promotions(ledger) if x["agent"] == agent]
    decided = [e["payload"] | {"ts": e["ts"], "by": e["actor"]["name"]} for e in ledger.entries()
               if e["stage"] in ("prompt_promotion_decision", "prompt_promoted", "no_winner")
               and (e["payload"].get("agent") == agent or e["payload"].get("promotion_id", "").startswith("pr_"))]
    feedback = [e["payload"] | {"ts": e["ts"]} for e in ledger.entries()
                if e["stage"] == "feedback" and e["payload"].get("agent") == agent]
    return {"agent": agent, "model": p.model, "skills": p.skills, "tools": p.tools, "version": p.version,
            "prompt": p.body, "system_chars": len(p.system), "library": optimize.library(),
            "status": s, "live": bool(s.get("passed")) and s.get("prompt_version") == p.version,
            "history": [{k: r.get(k) for k in ("date", "prompt_version", "model", "passed", "cost_usd", "metrics", "profile")}
                        for r in _history(agent)],
            "runs": _runs(agent), "ops_7d": _ops(agent, 7), "ops_30d": _ops(agent, 30), "calls": calls(agent),
            "plans": [{k: x.get(k) for k in ("plan_id", "ts", "total_cost_usd", "failures")} | {"variants": len(x["variants"])}
                      for x in optimize.plans(ledger, agent)][::-1],
            "pending": pending, "decided": decided[-20:][::-1], "feedback": feedback[-20:][::-1],
            "optimizable": {"models": [m for m in optimize.MODELS if m != p.model],
                            "tools": agent in optimize.TOOL_AGENTS},
            "heartbeat": _heartbeat(agent), "added_cases": added_cases(agent),
            "can_add_cases": agent in _added().ADAPTERS}


def _heartbeat(agent: str) -> dict:
    from grinos import evalwatch
    c = evalwatch.compare(agent)
    return c | {"summary": evalwatch.summary(c)}


def plan_detail(ledger: Ledger, agent: str, plan_id: str) -> dict:
    p = next((x for x in optimize.plans(ledger, agent) if x["plan_id"] == plan_id), None)
    if p is None:
        raise KeyError(f"no plan {plan_id}")
    return p


def plan_progress(ledger: Ledger, plan_id: str) -> dict:
    wid = f"optimize:{plan_id}"
    evals = [e["payload"] for e in ledger.entries() if e["run_id"] == wid and e["stage"] == "candidate_eval"]
    sel = next((e["payload"] for e in ledger.entries() if e["run_id"] == wid and e["stage"] in ("prompt_selected", "no_winner")), None)
    started = any(e["stage"] == "workflow_started" and e["payload"]["workflow_id"] == wid for e in ledger.entries())
    return {"workflow_id": wid, "started": started, "evaluated": evals,
            "result": None if sel is None else ({"winner": sel["promotion_id"], "label": sel["variant"]["label"]}
                                                if "promotion_id" in sel else {"winner": None})}


# --- eval cases over time: every case's result in every run, and adding new ones ------------------------

def _added():
    sys.path.insert(0, str(EVALS))
    import added
    return added


def _case_results(d: Path) -> dict[str, list[bool]]:
    rep = json.loads((d / "report.json").read_text())
    out: dict[str, list[bool]] = {}
    for r in rep.get("rows") or rep.get("cases") or []:
        if "case_pass" in r:
            out.setdefault(str(r.get("id")), []).append(bool(r["case_pass"]))
    return out


def case_matrix(agent: str, last: int = 20) -> dict:
    """Each case × the last N agent eval runs: pass rate per run, and what that says about the case."""
    runs = [r for r in _runs(agent) if r["kind"] == "eval" and r["system"] == "agent" and r["failed"] is not None][:last][::-1]
    cols: list[dict] = []
    cells: dict[str, list] = {}
    for i, r in enumerate(runs):
        cols.append({k: r[k] for k in ("id", "date", "model", "passed")})
        for cid, res in _case_results(_run_dir(agent, r["id"])).items():
            cells.setdefault(cid, [None] * len(runs))[i] = sum(res) / len(res)
    holdout = _added().holdout_ids(agent) if agent in _added().EVAL_DIRS else set()
    added = {x["id"] for x in _added().rows(agent)} if agent in _added().EVAL_DIRS else set()
    out = []
    for cid, row in cells.items():
        seen = [v for v in row if v is not None]
        last_v = seen[-1] if seen else None
        if not seen:
            status = "new"
        elif all(v == 1 for v in seen):
            status = "passing"
        elif all(v == 0 for v in seen):
            status = "always failing"
        elif last_v == 1:
            # failures only as one early block, then passing ever since: fixed; failing on and off: flaky
            last_fail = max(i for i, v in enumerate(seen) if v < 1)
            status = "fixed" if all(v < 1 for v in seen[:last_fail + 1]) else "flaky"
        elif all(v == 1 for v in seen[:-1]):
            status = "new failure"
        else:
            status = "flaky"
        out.append({"id": cid, "results": row, "pass_rate": round(sum(seen) / len(seen), 2) if seen else None,
                    "status": status, "added": cid in added, "holdout": cid in holdout})
    order = {"new failure": 0, "always failing": 1, "flaky": 2, "fixed": 3, "new": 4, "passing": 5}
    return {"runs": cols, "cases": sorted(out, key=lambda c: (order[c["status"]], c["id"]))}


def case_history(agent: str, case_id: str) -> dict:
    """One case across every agent eval run: result, model, and the saved detail to open."""
    hist = []
    for r in _runs(agent):
        if r["kind"] != "eval" or r["system"] != "agent":
            continue
        d = _run_dir(agent, r["id"])
        res = _case_results(d).get(case_id)
        if res is None:
            continue
        files = sorted(f.name for f in d.glob(f"{case_id}.r*.json"))
        hist.append({"run_id": r["id"], "date": r["date"], "model": r["model"], "passed": res, "files": files})
    if not hist:
        raise KeyError(f"no runs of {case_id}")
    added = next((x for x in _added().rows(agent) if x["id"] == case_id), None) if agent in _added().EVAL_DIRS else None
    return {"case_id": case_id, "history": hist, "added": added}


def case_template(ledger: Ledger, agent: str, src: str) -> dict:
    """A new eval case for this agent, prefilled from a claim, a production call, a failing case or a proposal."""
    A = _added()
    if agent not in A.ADAPTERS:
        raise KeyError(f"adding cases isn't supported for {agent} yet")
    t = json.loads(json.dumps(A.TEMPLATES[agent]))
    kind, _, ref = src.partition(":")
    if kind == "claim":
        from grinos.review import Index
        ix = Index(ledger)
        c = ix.claims.get(ref)
        if c is None:
            raise KeyError(f"no claim {ref}")
        claim = {k: c.get(k) for k in ("company", "metric", "period", "value", "unit", "quote", "statement", "segment", "adjusted")}
        if agent == "verifier":
            src_meta = ix.sources.get(c["source_blob"]) or {}
            t |= {"id": f"{ref}", "claim": claim, "source_text": ledger.get_blob(c["source_blob"]).decode("utf-8", "replace"),
                  "source_meta": {"kind": src_meta.get("kind", "other"), "uri": src_meta.get("uri", ""),
                                  "published_at": str(src_meta.get("published_at", "2026-01-01"))}}
        elif agent == "decision-model":
            t |= {"id": ref, "claim": claim}
        elif agent == "mid-cycle-analyst":
            t |= {"id": ref, "claims": [{"claim_id": ref, "nature": (ix.decisions.get(ref) or {}).get("nature"),
                                          "lever": ((ix.decisions.get(ref) or {}).get("lever_facts") or {}).get("lever"),
                                          "claim": claim}]}
    elif kind == "proposal" and agent == "mid-cycle-analyst":
        from grinos.proposals import proposal_state
        from grinos.review import Index
        s = proposal_state(ledger).get(ref) or {}
        ix = Index(ledger)
        cites = ((s.get("proposal") or {}).get("payload") or {}).get("proposal", {}).get("cites", [])
        t |= {"id": ref, "claims": [{"claim_id": cid, "nature": (ix.decisions.get(cid) or {}).get("nature"),
                                     "lever": ((ix.decisions.get(cid) or {}).get("lever_facts") or {}).get("lever"),
                                     "claim": {k: ix.claims[cid].get(k) for k in ("company", "metric", "period", "value", "unit",
                                                                                 "quote", "statement", "segment", "adjusted")}}
                                    for cid in cites if cid in ix.claims]}
    elif kind == "trace":
        rec = next((r for r in trace_detail(ref) if r.get("type") == "generation" and r.get("name") == agent), None)
        if rec is None:
            raise KeyError("that trace has no call to this agent")
        text = rec.get("input") or ""
        if agent == "orchestrator":
            m = re.search(r"<message>\n(.*?)\n</message>", text, re.S)
            t |= {"id": ref[:12], "text": (m[1] if m else text).replace("&lt;", "<")}
        elif agent == "claim-extractor":
            m = re.search(r"<source>\n(.*?)\n</source>", text, re.S)
            t |= {"id": ref[:12], "text": (m[1] if m else text).replace("&lt;", "<")}
        else:
            t |= {"id": ref[:12], "note": "from a production call; its input is below", "_input": text[:6000]}
    elif kind == "case":
        run_id, _, fname = ref.rpartition("/")
        detail = case_detail(agent, run_id, fname)
        base = detail.get("case") or {}
        base = {k: (sorted(v) if isinstance(v, set) else v) for k, v in base.items()}
        t = {**t, **{k: v for k, v in base.items() if k in t}, "id": f"{base.get('id', 'case')}_variant"}
    elif kind != "blank":
        raise KeyError(f"unknown source {src}")
    return {"agent": agent, "source": src, "case": t, "holdout_note": "about 1 in 5 added cases is held out: it counts in "
            "every eval but is never shown to the optimiser"}


def added_cases(agent: str) -> list[dict]:
    A = _added()
    return [{k: r.get(k) for k in ("id", "added_by", "source", "ts", "holdout")} for r in A.rows(agent)] \
        if agent in A.EVAL_DIRS else []

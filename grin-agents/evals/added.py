"""
Eval cases Isaac adds or enriches from the dashboard: from a production call, a failing eval case, a claim
in the ledger, or from scratch. They live in evals/<dir>/cases_added.jsonl (versioned in git with the eval),
one row per case: {id, case, added_by, source, ts, holdout}. Each agent's run.py merges them into its cases.

Every case passes its agent's adapter (`normalise`), which checks it against that eval's case shape, so a
malformed case can't break a run. A deterministic fifth of added cases are held out: they count in every
eval, but the improvement loop never shows them to the optimiser, so a winner can't simply learn the cases.
"""
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).parent          # where cases_added.jsonl files live
CODE = Path(__file__).parent          # where the eval code lives
EVAL_DIRS = {"verifier": "verifier", "decision-model": "decision-model", "mid-cycle-analyst": "analyst",
             "orchestrator": "orchestrator", "claim-extractor": "claim-extractor"}
METRICS = None  # filled lazily from grinos.schemas.Metric


def _metrics() -> set[str]:
    import typing

    from grinos.schemas import Metric
    return set(typing.get_args(Metric))


def path(agent: str) -> Path:
    return HERE / EVAL_DIRS[agent] / "cases_added.jsonl"


def holdout(case_id: str) -> bool:
    return int(hashlib.sha256(case_id.encode()).hexdigest(), 16) % 5 == 0


def rows(agent: str) -> list[dict]:
    p = path(agent) if agent in EVAL_DIRS else None
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p and p.exists() else []


def holdout_ids(agent: str) -> set[str]:
    return {r["id"] for r in rows(agent) if r.get("holdout")}


def cases(agent: str) -> list[dict]:
    """Added cases in the shape the agent's runner expects."""
    return [ADAPTERS[agent]["normalise"](r["case"]) for r in rows(agent)] if agent in ADAPTERS else []


def add(agent: str, case: dict, who: str, source: str) -> dict:
    if agent not in ADAPTERS:
        raise ValueError(f"adding cases isn't supported for {agent} yet")
    case = dict(case)
    case["id"] = "added_" + str(case.get("id") or "").removeprefix("added_").strip()
    if case["id"] == "added_":
        raise ValueError("a case needs an id")
    ADAPTERS[agent]["normalise"](case)                  # raises ValueError with what's wrong
    if any(r["id"] == case["id"] for r in rows(agent)):
        raise ValueError(f"{case['id']} already exists: pick another id")
    row = {"id": case["id"], "case": case, "added_by": who, "source": source,
           "ts": datetime.now(UTC).isoformat(timespec="seconds"), "holdout": holdout(case["id"])}
    with path(agent).open("a") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
    return row


# --- adapters: one per agent, matching that eval's case shape ---------------------------------------------

def _need(c: dict, *keys: str) -> None:
    missing = [k for k in keys if c.get(k) in (None, "", [])]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")


def _claim(c: dict) -> dict:
    _need(c, "metric", "period", "quote", "statement")
    if c["metric"] not in _metrics():
        raise ValueError(f"metric must be one of {sorted(_metrics())}")
    if c.get("unit") not in (None, "usd_m", "pct", "days", "x", "count_m"):
        raise ValueError("unit is usd_m, pct, days, x, count_m or null")
    return {"company": c.get("company") or "ACME", "metric": c["metric"], "period": c["period"], "value": c.get("value"),
            "unit": c.get("unit"), "quote": c["quote"], "statement": c["statement"], "segment": c.get("segment"),
            "adjusted": bool(c.get("adjusted"))}


def _verifier(c: dict) -> dict:
    _need(c, "id", "kind", "claim", "source_text")
    if c["kind"] not in ("valid", "tool", "judgment"):
        raise ValueError("kind is valid (must be verified), tool (a rule must reject) or judgment (must not be verified)")
    if c["kind"] == "judgment" and not c.get("reason"):
        raise ValueError("a judgment case needs the reason code the verifier should give (e.g. segment_not_total)")
    meta = c.get("source_meta") or {}
    return {"id": c["id"], "kind": c["kind"], "expected": "verified" if c["kind"] == "valid" else "rejected",
            "source": None, "source_text": c["source_text"],
            "source_meta": {"kind": meta.get("kind") or "other", "uri": meta.get("uri") or f"added://{c['id']}",
                            "published_at": meta.get("published_at") or "2026-01-01"},
            "note": c.get("note", ""), "reason": c.get("reason"), "claim": _claim(c["claim"])}


def _decision(c: dict) -> dict:
    _need(c, "id", "claim", "action", "nature")
    action, nature = set(c["action"]), set(c["nature"])
    if not action <= {"verify", "discard"}:
        raise ValueError("action is a list from: verify, discard")
    if not nature <= {"structural", "cyclical", "one_off", "noise", "not_applicable"}:
        raise ValueError("nature is a list from: structural, cyclical, one_off, noise, not_applicable")
    return {"id": c["id"], "action": action, "nature": nature, "claim": _claim(c["claim"]),
            "relevant": c.get("relevant", "verify" in action and "discard" not in action)}


def _analyst(c: dict) -> dict:
    _need(c, "id", "claims")
    claims = []
    for e in c["claims"]:
        _need(e, "claim_id", "claim")
        claims.append({"claim_id": e["claim_id"], "nature": e.get("nature"), "lever": e.get("lever"), "claim": _claim(e["claim"])})
    expect = {k: tuple(v) for k, v in (c.get("expect") or {}).items()}
    if any(len(v) != 2 or v[0] > v[1] for v in expect.values()):
        raise ValueError("expect maps a field to [low, high]")
    return {"id": c["id"], "expect": expect, "forbid": list(c.get("forbid") or []), "claims": claims,
            "require_any": bool(c.get("require_any")), "perspectives": c.get("perspectives") or None,
            "injection": tuple(c["injection"]) if c.get("injection") else None}


def _orchestrator(c: dict) -> dict:
    sys_path = str(CODE / "orchestrator")
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    import importlib.util
    spec = importlib.util.spec_from_file_location("_orch_cases", CODE / "orchestrator" / "cases.py")
    oc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oc)
    _need(c, "id", "text")
    for e in c.get("expect") or []:
        if e.get("action") not in ("start_analysis", "start_coverage", "start_eval", "start_improvement", "status",
                                   "suggest_command"):
            raise ValueError(f"unknown expected action {e.get('action')}")
    return oc.c(c["id"], c["text"], c.get("expect") or [], bool(c.get("human")), c.get("must") or [], c.get("ok") or [],
                c.get("ledger") or oc.LEDGER, c.get("work") or oc.OPEN_WORK)


def _extractor(c: dict) -> dict:
    _need(c, "id", "text")
    exp = []
    for e in c.get("expected") or []:
        _need(e, "metrics", "year", "value")
        exp.append({"metrics": set(e["metrics"]), "year": str(e["year"]), "value": float(e["value"])})
    return {"id": c["id"], "text": c["text"], "kind": c.get("kind") or "other", "expected": exp,
            "forbidden": list(c.get("forbidden") or [])}


TEMPLATES = {
    "verifier": {"id": "", "kind": "valid | tool | judgment", "reason": "for judgment: segment_not_total, non_gaap, "
                 "wrong_period, quarter_not_year, guidance_not_actual, conditional_or_bound, wrong_speaker, metric_mismatch, wrong_sign",
                 "claim": {"metric": "", "period": "", "value": None, "unit": "usd_m", "quote": "", "statement": "",
                           "segment": None, "adjusted": False},
                 "source_text": "", "source_meta": {"kind": "10-K", "uri": "", "published_at": "2026-01-01"}, "note": ""},
    "decision-model": {"id": "", "claim": {"metric": "", "period": "", "value": None, "unit": "pct", "quote": "", "statement": ""},
                       "action": ["verify"], "nature": ["structural"]},
    "mid-cycle-analyst": {"id": "", "claims": [], "expect": {"mid_cycle_ebit": [0, 0]}, "forbid": [], "perspectives": None},
    "orchestrator": {"id": "", "text": "", "expect": [{"action": "start_coverage", "ticker": "POOL"}], "human": False,
                     "must": [], "ok": []},
    "claim-extractor": {"id": "", "text": "", "kind": "other", "expected": [{"metrics": ["revenue"], "year": "2026", "value": 0}],
                        "forbidden": []},
}

ADAPTERS = {"verifier": {"normalise": _verifier}, "decision-model": {"normalise": _decision},
            "mid-cycle-analyst": {"normalise": _analyst}, "orchestrator": {"normalise": _orchestrator},
            "claim-extractor": {"normalise": _extractor}}

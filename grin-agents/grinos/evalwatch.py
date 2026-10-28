"""
The eval heartbeat: every agent's eval re-run on a schedule (weekly), compared with its previous run, so drift
shows up even when nothing in Grin OS changed (the models behind the agents do change).

A run is flagged when a gate that passed now fails, a case that passed now fails (a new failure), or a gate
metric got worse. For a flagged agent the heartbeat plans an Optimize round (cheap: one optimiser call) and
tells Isaac the variants and their estimated cost. He chooses what runs, and approves any winner.
"""
import json
from pathlib import Path
from typing import Any

EVALS = Path(__file__).parents[1] / "evals"
TOLERANCE = 0.05       # gate-metric moves smaller than this are noise at repeats=2


def _history(agent: str) -> list[dict]:
    p = EVALS / "history.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    return [r for r in rows if r.get("agent") == agent]


def _gates_for(agent: str) -> dict[str, str]:
    """Each gate metric and its direction, read from the eval's GATES ("<=" means lower is better)."""
    import re
    import sys
    sys.path.insert(0, str(EVALS))
    from run_all import EVALS as E
    ev, _ = E[agent]
    return dict(re.findall(r'"(\w+)": \("(<=|>=)"', (EVALS / ev / "run.py").read_text()))


def compare(agent: str) -> dict:
    """The latest heartbeat result for an agent against the one before it."""
    h = _history(agent)
    if not h:
        return {"agent": agent, "status": "no runs"}
    cur = h[-1]
    out: dict[str, Any] = {"agent": agent, "date": cur.get("date"), "passed": cur.get("passed"), "version": cur.get("prompt_version"),
           "model": cur.get("model"), "regressed_gates": [], "worse_metrics": {}, "new_failures": [], "still_failing": []}
    failing_now = sorted(c for c, v in (cur.get("per_case") or {}).items() if v < 1)
    if len(h) < 2:
        out |= {"status": "first run", "still_failing": failing_now}
        return out
    prev = h[-2]
    out["previous"] = {"date": prev.get("date"), "passed": prev.get("passed"), "version": prev.get("prompt_version"),
                       "model": prev.get("model")}
    out["regressed_gates"] = sorted(g for g, ok in (cur.get("gates") or {}).items()
                                    if not ok and (prev.get("gates") or {}).get(g))
    gates = _gates_for(agent)
    lower_better = {k for k, op in gates.items() if op == "<="}
    for k, v in (cur.get("metrics") or {}).items():
        p = (prev.get("metrics") or {}).get(k)
        if p is None or k not in gates:                  # only gate metrics count
            continue
        worse = (v - p) if k in lower_better else (p - v)
        if worse > TOLERANCE + 1e-9:
            out["worse_metrics"][k] = {"before": p, "after": v}
    prev_cases = prev.get("per_case") or {}
    ever_failed = {c for r in h[:-1] for c, v in (r.get("per_case") or {}).items() if v < 1}
    fresh = [c for c in failing_now if prev_cases.get(c, 1.0) == 1.0]
    out["new_failures"] = sorted(c for c in fresh if c not in ever_failed)
    out["flaky"] = sorted(c for c in fresh if c in ever_failed)          # failed before, passed last time
    out["still_failing"] = sorted(c for c in failing_now if c not in fresh)
    flagged = bool(out["regressed_gates"] or out["new_failures"] or out["worse_metrics"] or not cur.get("passed"))
    out["status"] = "regressed" if flagged else "steady"
    return out


def summary(c: dict) -> str:
    if c["status"] in ("no runs", "first run") or (c["status"] == "steady" and not c.get("flaky")):
        return f"{c['agent']}: {c['status']}" + (f" ({len(c['still_failing'])} known failing case(s))" if c.get("still_failing") else "")
    bits = []
    if c["regressed_gates"]:
        bits.append("gates now failing: " + ", ".join(c["regressed_gates"]))
    if c["new_failures"]:
        bits.append(f"{len(c['new_failures'])} new failing case(s): " + ", ".join(c["new_failures"][:5]))
    if c.get("flaky"):
        bits.append(f"{len(c['flaky'])} flaky case(s) failed again: " + ", ".join(c["flaky"][:5]))
    if c["worse_metrics"]:
        bits.append("worse: " + ", ".join(f"{k} {v['before']:.2f}→{v['after']:.2f}" for k, v in c["worse_metrics"].items()))
    return f"{c['agent']}: " + "; ".join(bits)

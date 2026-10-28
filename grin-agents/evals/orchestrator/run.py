"""
Orchestrator eval.

    V=../val-machine/.venv/bin/python
    $V evals/orchestrator/run.py --system naive          # baseline: consults the analyst, starts nothing → FAIL
    $V evals/orchestrator/run.py --system agent --model sonnet --repeats 2

Gates (on the raw plan, before grinos.orchestrator.check_plan):
  action_recall      >= 0.9   requested work is started (right action, ticker, agent, date)
  spurious_rate      <= 0.05  work started that nobody asked for (start_* actions per case)
  human_respected    == 1.0   Isaac's decisions are never taken: nothing started, the command suggested
  arg_validity       == 1.0   every action passes check_plan (known tickers, real agents, dates)
  consult_recall     >= 0.85  the right specialist is asked for an expert view
  consult_precision  >= 0.8   consults are relevant
  invented_numbers   == 0     no number in a reply that the orchestrator wasn't given
"""
import argparse
import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parent))
from cases import CASES, TICKERS  # noqa: E402
from common import results_dir  # noqa: E402

from grinos.llm import call_agent  # noqa: E402
from grinos.orchestrator import PLAN_SCHEMA, check_plan, invented_numbers, plan_message  # noqa: E402

GATES = {"action_recall": (">=", 0.9), "spurious_rate": ("<=", 0.05), "human_respected": (">=", 1.0),
         "arg_validity": (">=", 1.0), "consult_recall": (">=", 0.85), "consult_precision": (">=", 0.8),
         "invented_numbers": ("<=", 0)}
START = {"start_analysis", "start_coverage", "start_eval", "start_improvement"}


def naive(case: dict) -> dict:
    return {"reply": "On it.", "actions": [], "consults": [{"agent": "mid-cycle-analyst", "question": case["text"]}]}


def _matches(a: dict, e: dict) -> bool:
    if a.get("action") != e["action"]:
        return False
    for k in ("ticker", "agent", "as_of"):
        if k in e and str(a.get(k) or "").upper() != str(e[k]).upper():
            return False
    if "verb" in e:
        cmd = (a.get("command") or "").strip().lower()
        if not cmd.startswith("/" + e["verb"]):
            return False
    return True


def grade(case: dict, out: dict | None, msg: str) -> dict:
    out = out or {"reply": "", "actions": [], "consults": []}
    acts = out.get("actions") or []
    matched = [e for e in case["expect"] if any(_matches(a, e) for a in acts)]
    spurious = [a for a in acts if a.get("action") in START and not any(_matches(a, e) for e in case["expect"])]
    _, _, problems = check_plan(out, TICKERS, {"cov_POOL_2026-09-21_0c58535064"})
    consulted = {c.get("agent") for c in out.get("consults") or []}
    human_ok = (not any(a.get("action") in START for a in acts) and len(matched) == len(case["expect"])) \
        if case["human"] else None
    invented = sorted(invented_numbers(out.get("reply", ""), msg))
    row = {"id": case["id"], "recall": len(matched) / len(case["expect"]) if case["expect"] else None,
           "spurious": len(spurious), "human_ok": human_ok, "arg_valid": not problems, "problems": problems,
           "must_ok": case["must"] <= consulted, "precision": (len(consulted & case["ok"]) / len(consulted)) if consulted else None,
           "invented": invented, "actions": acts, "consults": sorted(consulted), "reply": out.get("reply", "")}
    row["case_pass"] = (row["recall"] in (None, 1.0) and not spurious and human_ok is not False and not problems
                        and row["must_ok"] and row["precision"] in (None, 1.0) and not invented)
    return row


def summarise(rows: list[dict]) -> dict:
    def mean(xs):
        xs = [x for x in xs if x is not None]
        return statistics.mean(xs) if xs else 1.0
    s = {"n": len(rows), "action_recall": mean(r["recall"] for r in rows),
         "spurious_rate": sum(r["spurious"] for r in rows) / len(rows),
         "human_respected": mean(r["human_ok"] for r in rows),
         "arg_validity": mean(float(r["arg_valid"]) for r in rows),
         "consult_recall": mean(float(r["must_ok"]) for r in rows if r["id"] in MUST_IDS),
         "consult_precision": mean(r["precision"] for r in rows),
         "invented_numbers": sum(len(r["invented"]) for r in rows)}
    s["gates"] = {k: (s[k] <= t if op == "<=" else s[k] >= t) for k, (op, t) in GATES.items()}
    s["passed"] = all(s["gates"].values())
    return s


MUST_IDS = {c["id"] for c in CASES if c["must"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", choices=["agent", "naive"], default="agent")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    model = args.model if args.system == "agent" else "none"
    out_dir = results_dir(HERE, args.system, model)

    def one(job):
        case, rep = job
        msg = plan_message(case["text"], TICKERS, case["work"], case["ledger"], "(none)")
        if args.system == "naive":
            out, meta = naive(case), {}
        else:
            out, meta = call_agent("orchestrator", msg, PLAN_SCHEMA, model=args.model)
        g = grade(case, out, msg) | {"repeat": rep, "cost_usd": meta.get("cost_usd") or 0}
        (out_dir / f"{case['id']}.r{rep}.json").write_text(json.dumps({"case": {**case, "must": sorted(case["must"]),
                                                                                 "ok": sorted(case["ok"])},
                                                                        "output": out, "grade": g}, indent=2, default=str))
        return g

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(one, [(c, r) for r in range(args.repeats) for c in CASES if args.only in c["id"]]))
    s = summarise(rows)
    s.update(system=args.system, model=model, repeats=args.repeats, cost_usd=round(sum(r["cost_usd"] for r in rows), 4))
    (out_dir / "report.json").write_text(json.dumps({"summary": s, "rows": rows}, indent=2, default=str))
    print(f"# orchestrator eval — {args.system} / {model} (repeats={args.repeats}, cost=${s['cost_usd']})\n")
    for k in GATES:
        print(f"  {k:<18} {s[k]:.2f}  {'PASS' if s['gates'][k] else 'FAIL'}")
    print(f"\n  {'PASSED' if s['passed'] else 'FAILED'}\n")
    for r in sorted(rows, key=lambda r: (r["id"], r["repeat"])):
        if not r["case_pass"]:
            print(f"  BAD {r['id']:<24} actions={[(a.get('action'), a.get('ticker') or a.get('agent') or a.get('command')) for a in r['actions']]} "
                  f"consults={r['consults']} invented={r['invented']} problems={r['problems']}")
    print(f"\nresults: {out_dir}")
    return 0 if s["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

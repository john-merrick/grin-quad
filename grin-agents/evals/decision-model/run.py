"""
Decision-model eval.

    V=../val-machine/.venv/bin/python
    $V evals/decision-model/run.py --system rules-only      # baseline: routes everything, no nature → must FAIL
    $V evals/decision-model/run.py --system agent --model sonnet --repeats 3

Gates:
  lost_evidence_rate == 0     a relevant claim discarded
  action_accuracy    >= 0.9
  nature_accuracy    >= 0.8   only on relevant claims (nature of a discard barely matters)
"""
import argparse
import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[1]))

from cases import CASES  # noqa: E402
from grinos.decide import decide_claim  # noqa: E402

CONFIG = yaml.safe_load((HERE.parent / "config-auditor" / "cases" / "clean_base" / "acme.yaml").read_text())
GATES = {"lost_evidence_rate": ("<=", 0.0), "action_accuracy": (">=", 0.9), "nature_accuracy": (">=", 0.8)}


def grade(case: dict, res: dict) -> dict:
    return {"id": case["id"], "relevant": case["relevant"], "action": res["action"], "nature": res["nature"],
            "action_ok": res["action"] in case["action"], "nature_ok": res["nature"] in case["nature"],
            "lost": case["relevant"] and res["action"] == "discard"}


def summarise(rows: list[dict]) -> dict:
    rel = [r for r in rows if r["relevant"]]
    s = {"n": len(rows),
         "lost_evidence_rate": statistics.mean(r["lost"] for r in rel),
         "action_accuracy": statistics.mean(r["action_ok"] for r in rows),
         "nature_accuracy": statistics.mean(r["nature_ok"] for r in rel)}
    s["gates"] = {k: (s[k] <= t if op == "<=" else s[k] >= t) for k, (op, t) in GATES.items()}
    s["passed"] = all(s["gates"].values())
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", choices=["agent", "rules-only"], default="agent")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=6)
    args = ap.parse_args()
    model = args.model if args.system == "agent" else "none"
    out_dir = HERE / "results" / f"{datetime.now():%Y%m%d-%H%M%S}_{args.system}_{model}"
    out_dir.mkdir(parents=True)

    def one(job):
        case, rep = job
        res = decide_claim(case["claim"], CONFIG, model=args.model, use_agent=args.system == "agent")
        g = grade(case, res) | {"repeat": rep, "cost_usd": (res["agent_meta"] or {}).get("cost_usd") or 0}
        (out_dir / f"{case['id']}.r{rep}.json").write_text(json.dumps(
            {"case": case | {"action": sorted(case["action"]), "nature": sorted(case["nature"])},
             "result": res, "grade": g}, indent=2, default=str))
        return g

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        rows = list(pool.map(one, [(c, r) for r in range(args.repeats) for c in CASES]))
    s = summarise(rows) | {"system": args.system, "model": model,
                           "cost_usd": round(sum(r["cost_usd"] for r in rows), 4)}
    (out_dir / "report.json").write_text(json.dumps({"summary": s, "cases": rows}, indent=2))

    print(f"# decision-model eval — {args.system} / {model} (repeats={args.repeats}, cost=${s['cost_usd']})\n")
    for k in GATES:
        print(f"  {k:<20} {s[k]:>5.0%}  {'PASS' if s['gates'][k] else 'FAIL'}")
    print(f"\n  {'PASSED' if s['passed'] else 'FAILED'}\n")
    for r in sorted(rows, key=lambda r: (r["id"], r["repeat"])):
        if not (r["action_ok"] and (r["nature_ok"] or not r["relevant"])):
            print(f"  BAD {r['id']:<20} action={r['action']:<8} nature={r['nature']}")
    print(f"\nresults: {out_dir}")
    return 0 if s["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

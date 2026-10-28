"""
Desk-router eval.

    V=../val-machine/.venv/bin/python
    $V evals/desk-router/run.py --system default    # baseline: always the analyst → must FAIL
    $V evals/desk-router/run.py --system agent --model haiku --repeats 3

Gates: must_recall >= 0.9 (required agents picked), precision >= 0.85 (picks within ok set).
"""
import argparse
import json
import statistics
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parent))
from cases import CASES  # noqa: E402
from common import results_dir  # noqa: E402

from grinos.desk import Desk  # noqa: E402
from grinos.ledger import Ledger  # noqa: E402

GATES = {"must_recall": 0.9, "precision": 0.85}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", choices=["agent", "default"], default="agent")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--repeats", type=int, default=1)
    args = ap.parse_args()
    desk = Desk(Ledger(Path(tempfile.mkdtemp())), router_model=args.model)

    def one(job):
        case, rep = job
        picked = ["mid-cycle-analyst"] if args.system == "default" else desk.route(case["text"], "Everyone" in case["text"])
        return {"id": case["id"], "picked": picked,
                "must": len(case["must"] & set(picked)) / len(case["must"]),
                "precision": len(set(picked) & case["ok"]) / len(picked),
                "case_pass": case["must"] <= set(picked) and set(picked) <= case["ok"]}

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(one, [(c, r) for r in range(args.repeats) for c in CASES]))
    s = {"must_recall": statistics.mean(r["must"] for r in rows), "precision": statistics.mean(r["precision"] for r in rows)}
    passed = all(s[k] >= v for k, v in GATES.items())
    model = args.model if args.system == "agent" else "none"
    out_dir = results_dir(HERE, args.system, model)
    (out_dir / "report.json").write_text(json.dumps({"summary": s | {"passed": passed}, "rows": rows}, indent=2))
    print(f"# desk-router eval — {args.system} / {args.model if args.system == 'agent' else 'none'} (repeats={args.repeats})\n")
    for k, v in GATES.items():
        print(f"  {k:<12} {s[k]:.2f}  {'PASS' if s[k] >= v else 'FAIL'}")
    print(f"\n  {'PASSED' if passed else 'FAILED'}\n")
    for r in rows:
        if r["must"] < 1 or r["precision"] < 1:
            print(f"  BAD {r['id']:<16} picked={r['picked']}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

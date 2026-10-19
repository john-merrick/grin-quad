"""
Verifier eval.

    V=../val-machine/.venv/bin/python
    $V evals/verifier/run.py --system rules-only        # free baseline: must FAIL (accepts judgment cases)
    $V evals/verifier/run.py --system agent --model sonnet --repeats 3

Gates (asymmetric on purpose — see cases.py):
  false_accept_rate   == 0     any non-valid claim verified
  tool_reject_rate    == 1     rules catch every tool case without an agent
  valid_accept_rate   >= 0.85  valid claims verified (not rejected / needs_human)
  reason_match_rate   >= 0.9   judgment rejections cite the planted reason code
"""
import argparse
import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[1]))

from cases import AS_OF, CASES, SOURCES  # noqa: E402
from grinos.schemas import Claim, Source, sha256  # noqa: E402
from grinos.verify import verify_claim  # noqa: E402

GATES = {"false_accept_rate": ("<=", 0.0), "tool_reject_rate": (">=", 1.0), "valid_accept_rate": (">=", 0.85),
         "reason_match_rate": (">=", 0.9)}


def load(case: dict) -> tuple[Claim, Source, str]:
    meta = SOURCES[case["source"]]
    text = (HERE / "sources" / meta["file"]).read_text()
    source = Source(blob=sha256(text.encode()), uri=meta["uri"], kind=meta["kind"],
                    company="ACME", published_at=meta["published_at"])
    return Claim(source_blob=source.blob, **case["claim"]), source, text


def grade(case: dict, result: dict) -> dict:
    v = result["verdict"]
    codes = {r["code"] for r in (result["agent"] or {}).get("reasons", [])}
    return {"id": case["id"], "kind": case["kind"], "verdict": v,
            "reason_ok": case["reason"] in codes if case["kind"] == "judgment" else None,
            "ok": (v == "verified") if case["kind"] == "valid" else (v != "verified"),
            "false_accept": case["kind"] != "valid" and v == "verified",
            "by_rules": result["agent"] is None and v == "rejected"}


def summarise(rows: list[dict]) -> dict:
    def rate(rs, pred):
        return statistics.mean(pred(r) for r in rs) if rs else None
    bad = [r for r in rows if r["kind"] != "valid"]
    s = {
        "n": len(rows),
        "false_accept_rate": rate(bad, lambda r: r["false_accept"]),
        "tool_reject_rate": rate([r for r in rows if r["kind"] == "tool"], lambda r: r["by_rules"]),
        "valid_accept_rate": rate([r for r in rows if r["kind"] == "valid"], lambda r: r["verdict"] == "verified"),
        "judgment_reject_rate": rate([r for r in rows if r["kind"] == "judgment"], lambda r: r["ok"]),
        "reason_match_rate": rate([r for r in rows if r["kind"] == "judgment"], lambda r: r["reason_ok"]),
    }
    s["gates"] = {k: (s[k] <= t if op == "<=" else s[k] >= t) for k, (op, t) in GATES.items()}
    s["passed"] = all(s["gates"].values())
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", choices=["agent", "rules-only"], default="agent")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--only", default="")
    ap.add_argument("--jobs", type=int, default=6)
    args = ap.parse_args()

    model = args.model if args.system == "agent" else "none"
    out_dir = HERE / "results" / f"{datetime.now():%Y%m%d-%H%M%S}_{args.system}_{model}"
    out_dir.mkdir(parents=True)
    jobs = [(c, r) for r in range(args.repeats) for c in CASES if args.only in c["id"]]

    def one(job):
        case, rep = job
        claim, source, text = load(case)
        res = verify_claim(claim, source, text, AS_OF, model=args.model, use_agent=args.system == "agent")
        g = grade(case, res) | {"repeat": rep, "cost_usd": (res["agent_meta"] or {}).get("cost_usd") or 0}
        (out_dir / f"{case['id']}.r{rep}.json").write_text(json.dumps({"case": case, "result": res, "grade": g},
                                                                        indent=2, default=str))
        return g | {"reason": ((res["agent"] or {}).get("reasons") or [{}])[0].get("code", "rule")}

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        rows = list(pool.map(one, jobs))
    s = summarise(rows)
    s.update(system=args.system, model=model, repeats=args.repeats, cost_usd=round(sum(r["cost_usd"] for r in rows), 4))
    (out_dir / "report.json").write_text(json.dumps({"summary": s, "cases": rows}, indent=2))

    pct = lambda v: "n/a" if v is None else f"{v:.0%}"  # noqa: E731
    print(f"# verifier eval — {args.system} / {model} (repeats={args.repeats}, cost=${s['cost_usd']})\n")
    for k in ("false_accept_rate", "tool_reject_rate", "valid_accept_rate", "judgment_reject_rate", "reason_match_rate"):
        gate = "" if k not in GATES else ("PASS" if s["gates"][k] else "FAIL")
        print(f"  {k:<22} {pct(s[k]):>5}  {gate}")
    print(f"\n  {'PASSED' if s['passed'] else 'FAILED'}\n")
    for r in sorted(rows, key=lambda r: (r["kind"], r["id"], r["repeat"])):
        if not r["ok"] or args.repeats == 1:
            print(f"  {'ok ' if r['ok'] else 'BAD'} {r['kind']:<9} {r['id']:<26} {r['verdict']:<12} {r['reason']}")
    print(f"\nresults: {out_dir}")
    return 0 if s["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

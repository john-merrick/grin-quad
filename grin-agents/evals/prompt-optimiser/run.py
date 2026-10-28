"""
Prompt-optimiser eval.

    V=../val-machine/.venv/bin/python
    $V evals/prompt-optimiser/run.py --system naive     # baseline: pastes the failures into the prompt → FAIL
    $V evals/prompt-optimiser/run.py --system agent --model sonnet --repeats 2

Gates (every candidate goes through grinos.improve's deterministic checks):
  apply_rate        >= 0.9   candidates whose edits apply exactly (each `find` once)
  rules_kept        >= 0.9   applied candidates that keep every existing rule line (grinos.improve refuses the rest
                             deterministically, so this gate measures wasted candidates, not a safety gap)
  leak_rate         == 0     candidates that copy case ids or quotes from the failures
  size_ok           == 1.0   applied candidates within 1.5× the prompt length
  restores          >= 0.8   cases where a clean candidate puts the cut rule's meaning back
"""
import argparse
import json
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parent))
from cases import CASES, PACK_CASES  # noqa: E402
from common import results_dir  # noqa: E402

from grinos import improve  # noqa: E402

GATES = {"apply_rate": (">=", 0.9), "rules_kept": (">=", 0.9), "leak_rate": ("<=", 0.0), "size_ok": (">=", 1.0),
         "restores": (">=", 0.8), "pack_ok": (">=", 0.8), "pack_leaks": ("<=", 0)}


def naive_call(name, message, schema, model="sonnet", **k):
    """Pastes the failures at the end of the prompt: overfits and leaks the eval."""
    prompt = message.split("<current_prompt>\n", 1)[1].split("\n</current_prompt>", 1)[0]
    failures = message.split("<failures>\n", 1)[1].split("\n</failures>", 1)[0]
    last = [ln for ln in prompt.splitlines() if ln.strip()][-1]
    return {"candidates": [{"rationale": "remember the failures", "edits": [
        {"find": last, "replace": last + "\n\nKnown failures, never repeat them:\n" + failures}]}]}, {"cost_usd": 0}


def grade(case: dict, cands: list[dict]) -> dict:
    applied = [c for c in cands if c["body"] is not None]
    clean = [c for c in applied if not c["problems"]]
    restored = [c for c in clean if re.search(case["restores"], c["body"], re.I | re.S)]
    row = {"id": case["id"], "agent": case["agent"], "n": len(cands), "applied": len(applied),
           "rules_lost": sum(any("rule line" in p for p in c["problems"]) for c in applied),
           "leaks": sum(any("copies eval material" in p for p in c["problems"]) for c in cands),
           "too_big": sum(any("grows the prompt" in p for p in c["problems"]) for c in applied),
           "restored": bool(restored), "problems": [c["problems"] for c in cands],
           "rationales": [c["rationale"][:200] for c in cands]}
    row["case_pass"] = row["restored"] and not row["leaks"] and not row["rules_lost"] and not row["too_big"]
    return row


def naive_pack_call(name, message, schema, model=None, **k):
    """Writes the failures into the pack."""
    failures = message.split("<failures>\n", 1)[1].split("\n</failures>", 1)[0]
    return {"draft": {"name": "lessons", "description": "past failures", "content": failures}}, {"cost_usd": 0}


def grade_pack(case: dict, out: dict | None) -> dict:
    d = (out or {}).get("draft")
    leaks = [t for t in improve.leak_tokens(case["failures"]) if d and t in d["content"]]
    ok = (d is None) if not case["expect_pack"] else bool(
        d and re.search(case["covers"], d["content"], re.I | re.S) and len(d["content"]) <= 6000)
    return {"id": case["id"], "pack": True, "drafted": d is not None, "pack_leaks": len(leaks), "ok": bool(ok) and not leaks,
            "case_pass": bool(ok) and not leaks, "content": (d or {}).get("content", "")[:400]}


def summarise(rows: list[dict]) -> dict:
    packs = [r for r in rows if r.get("pack")]
    rows = [r for r in rows if not r.get("pack")]
    n = sum(r["n"] for r in rows) or 1
    applied = sum(r["applied"] for r in rows) or 1
    s = {"n_cases": len(rows), "n_candidates": n,
         "apply_rate": sum(r["applied"] for r in rows) / n,
         "rules_kept": 1 - sum(r["rules_lost"] for r in rows) / applied,
         "leak_rate": sum(r["leaks"] for r in rows) / n,
         "size_ok": 1 - sum(r["too_big"] for r in rows) / applied,
         "restores": statistics.mean(float(r["restored"]) for r in rows),
         "pack_ok": statistics.mean(float(r["ok"]) for r in packs) if packs else 1.0,
         "pack_leaks": sum(r["pack_leaks"] for r in packs)}
    s["gates"] = {k: (s[k] <= t if op == "<=" else s[k] >= t) for k, (op, t) in GATES.items()}
    s["passed"] = all(s["gates"].values())
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", choices=["agent", "naive"], default="agent")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--repeats", type=int, default=1)
    args = ap.parse_args()
    model = args.model if args.system == "agent" else "none"
    out_dir = results_dir(HERE, args.system, model)

    def one(job):
        case, rep = job
        call = naive_call if args.system == "naive" else improve.call_agent
        cands, meta = improve.propose(case["agent"], case["prompt"], case["failures"], model=args.model, call=call)
        g = grade(case, cands) | {"repeat": rep, "cost_usd": meta.get("cost_usd") or 0}
        (out_dir / f"{case['id']}.r{rep}.json").write_text(json.dumps({"candidates": cands, "grade": g}, indent=2))
        return g

    def pack(job):
        case, rep = job
        from grinos import optimize
        from grinos.llm import call_agent
        from grinos.schemas import fence
        msg = (f"<agent>\n{case['agent']}\n</agent>\n\n<failures>\n{fence(json.dumps(case['failures'], indent=2))}\n</failures>\n\n"
               "Draft one reference pack (general domain knowledge the agent lacked) that would have prevented these "
               "failures, or return null if knowledge isn't the problem.")
        call = naive_pack_call if args.system == "naive" else call_agent
        out, meta = call("prompt-optimiser", msg, optimize.SKILL_SCHEMA, model=None if args.system == "naive" else args.model)
        g = grade_pack(case, out) | {"repeat": rep, "cost_usd": meta.get("cost_usd") or 0}
        (out_dir / f"{case['id']}.r{rep}.json").write_text(json.dumps({"output": out, "grade": g}, indent=2))
        return g

    with ThreadPoolExecutor(max_workers=5) as pool:
        rows = list(pool.map(one, [(c, r) for r in range(args.repeats) for c in CASES]))
        rows += list(pool.map(pack, [(c, r) for r in range(args.repeats) for c in PACK_CASES]))
    s = summarise(rows)
    s.update(system=args.system, model=model, repeats=args.repeats, cost_usd=round(sum(r["cost_usd"] for r in rows), 4))
    (out_dir / "report.json").write_text(json.dumps({"summary": s, "rows": rows}, indent=2, default=str))
    print(f"# prompt-optimiser eval — {args.system} / {model} (repeats={args.repeats}, cost=${s['cost_usd']})\n")
    for k in GATES:
        print(f"  {k:<12} {s[k]:.2f}  {'PASS' if s['gates'][k] else 'FAIL'}")
    print(f"\n  {'PASSED' if s['passed'] else 'FAILED'}\n")
    for r in rows:
        if not r["case_pass"]:
            print(f"  BAD {r['id']:<20} " + (f"drafted={r['drafted']} leaks={r['pack_leaks']} {r['content'][:160]!r}" if r.get("pack") else
                  f"restored={r['restored']} leaks={r['leaks']} rules_lost={r['rules_lost']} problems={r['problems']}"))
    print(f"\nresults: {out_dir}")
    return 0 if s["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

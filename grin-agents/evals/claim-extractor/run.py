"""
Claim-extractor eval.

    V=../val-machine/.venv/bin/python
    $V evals/claim-extractor/run.py --system empty      # baseline: extracts nothing → must FAIL
    $V evals/claim-extractor/run.py --system agent --model sonnet --repeats 3

Gates:
  rule_pass_rate  >= 0.95  extracted claims whose quote is verbatim and value is in the quote
  recall          >= 0.80  expected facts found (metric, year, value)
  forbidden       == 0     analyst-question text extracted as a fact
"""
import argparse
import json
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE.parent))
from added import cases as added_cases  # noqa: E402
from cases import EXPECTED, FORBIDDEN, SOURCES  # noqa: E402
from common import results_dir  # noqa: E402

from grinos.evidence_check import check_claim  # noqa: E402
from grinos.extract import extract_claims  # noqa: E402
from grinos.schemas import Claim, Source, sha256  # noqa: E402

SRC_DIR = ROOT / "evals" / "verifier" / "sources"
for _c in added_cases("claim-extractor"):                # sources Isaac added, with the facts they must yield
    SOURCES[_c["id"]] = {"file": None, "text": _c["text"], "kind": _c["kind"], "published_at": "2026-01-01",
                         "uri": f"added://{_c['id']}"}
    EXPECTED[_c["id"]] = _c["expected"]
    FORBIDDEN[_c["id"]] = _c["forbidden"]
GATES = {"rule_pass_rate": (">=", 0.95), "recall": (">=", 0.80), "forbidden": ("<=", 0)}


def grade(src_key: str, text: str, claims: list[dict]) -> dict:
    meta = SOURCES[src_key]
    source = Source(blob=sha256(text.encode()), uri=meta["uri"], kind=meta["kind"], company="ACME",
                    published_at=date.fromisoformat(meta["published_at"]))
    passed, bad = 0, []
    for c in claims:
        try:
            claim = Claim(source_blob=source.blob, company="ACME", **c)
        except Exception as exc:  # malformed claim counts as a failed claim
            bad.append({"claim": c, "why": f"invalid: {exc}"})
            continue
        r = check_claim(claim, source, text, date(2100, 1, 1))
        if r["rules_verdict"] == "pass":
            passed += 1
        else:
            bad.append({"claim": c, "why": [k["id"] for k in r["checks"] if not k["ok"]]})

    def found(exp):
        return any(c["metric"] in exp["metrics"] and exp["year"] in c["period"]
                   and c["value"] is not None and abs(c["value"] - exp["value"]) <= abs(exp["value"]) * 0.005
                   for c in claims)
    missed = [e for e in EXPECTED[src_key] if not found(e)]
    forbidden = sum(any(f.lower() in c["quote"].lower() for f in FORBIDDEN.get(src_key, [])) for c in claims)
    return {"n_claims": len(claims), "rule_pass": passed / len(claims) if claims else 0.0,
            "recall": 1 - len(missed) / len(EXPECTED[src_key]), "forbidden": forbidden, "id": src_key,
            "case_pass": not missed and not forbidden and (passed / len(claims) if claims else 0.0) >= GATES["rule_pass_rate"][1],
            "missed": [f"{sorted(e['metrics'])[0]} {e['year']} {e['value']}" for e in missed], "bad": bad}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", choices=["agent", "empty"], default="agent")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--repeats", type=int, default=1)
    args = ap.parse_args()
    model = args.model if args.system == "agent" else "none"
    out_dir = results_dir(HERE, args.system, model)

    def one(job):
        key, rep = job
        text = SOURCES[key].get("text") or (SRC_DIR / SOURCES[key]["file"]).read_text()
        if args.system == "empty":
            claims, metas = [], []
        else:
            claims, metas = extract_claims(text, "ACME", SOURCES[key]["kind"], model=args.model)
        g = grade(key, text, claims) | {"source": key, "repeat": rep,
                                        "cost_usd": sum((m or {}).get("cost_usd") or 0 for m in metas)}
        (out_dir / f"{key}.r{rep}.json").write_text(json.dumps({"claims": claims, "grade": g}, indent=2, default=str))
        return g

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(one, [(k, r) for r in range(args.repeats) for k in SOURCES]))
    s = {"rule_pass_rate": statistics.mean(r["rule_pass"] for r in rows),
         "recall": statistics.mean(r["recall"] for r in rows),
         "forbidden": sum(r["forbidden"] for r in rows),
         "cost_usd": round(sum(r["cost_usd"] for r in rows), 4)}
    s["gates"] = {k: (s[k] <= t if op == "<=" else s[k] >= t) for k, (op, t) in GATES.items()}
    s["passed"] = all(s["gates"].values())
    (out_dir / "report.json").write_text(json.dumps({"summary": s, "rows": rows}, indent=2, default=str))

    print(f"# claim-extractor eval — {args.system} / {model} (repeats={args.repeats}, cost=${s['cost_usd']})\n")
    for k in GATES:
        print(f"  {k:<15} {s[k]:>6.2f}  {'PASS' if s['gates'][k] else 'FAIL'}")
    print(f"\n  {'PASSED' if s['passed'] else 'FAILED'}\n")
    for r in rows:
        print(f"  {r['source']}.r{r['repeat']}: {r['n_claims']} claims, missed={r['missed']}")
        for b in r["bad"][:5]:
            quote = re.sub(r"\s+", " ", b["claim"].get("quote", ""))
            print(f"     bad: {b['why']} — {quote[:90]}")
    print(f"\nresults: {out_dir}")
    return 0 if s["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

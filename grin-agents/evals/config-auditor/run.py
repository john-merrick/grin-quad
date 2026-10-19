"""
Run the config-auditor eval.

    VENV=../val-machine/.venv/bin/python
    $VENV evals/config-auditor/run.py --system tools-only          # free, offline baseline
    $VENV evals/config-auditor/run.py --system agent --model sonnet
    $VENV evals/config-auditor/run.py --system agent --repeats 3 --only judg_

Writes results/<stamp>_<system>_<model>/{<case>.json, report.json, report.md}.
Exit code 0 if every gate in grade.GATES passes, 1 otherwise.

The agent is called headless and hermetic: no tools, no MCP, no skills, no
user settings, empty cwd. It gets the same two inputs it would gather itself
interactively (raw YAML + config_facts JSON), so this tests its judgement
in isolation from tool-calling.
"""
import argparse
import json
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from config_facts import config_facts  # noqa: E402
from grade import grade_case, summarise  # noqa: E402

AGENT_FILE = ROOT / "agents" / "config-auditor.md"
SCHEMA = (HERE / "schema.json").read_text()


def agent_prompt() -> str:
    text = AGENT_FILE.read_text()
    return text.split("---", 2)[2].strip() if text.startswith("---") else text


def user_message(path: Path, facts: dict) -> str:
    return (
        f"Audit `{path.name}`.\n\n"
        f"<config filename=\"{path.name}\">\n{path.read_text()}</config>\n\n"
        f"<facts>\n{json.dumps(facts, indent=2)}\n</facts>"
    )


def run_tools_only(path: Path, facts: dict) -> tuple[dict, dict]:
    findings = [
        {"field": c["field"], "category": "other", "severity": c["severity"],
         "evidence": c["message"], "suggested_fix": ""}
        for c in facts["checks"]
    ]
    verdict = "fail" if any(f["severity"] in {"high", "medium"} for f in findings) else "pass"
    return {"ticker": facts.get("ticker") or "", "verdict": verdict, "findings": findings}, {"cost_usd": 0.0}


def run_agent(path: Path, facts: dict, model: str, system_prompt: str) -> tuple[dict | None, dict]:
    cmd = [
        "claude", "-p", user_message(path, facts),
        "--model", model,
        "--system-prompt", system_prompt,
        "--tools", "",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "project",
        "--disable-slash-commands",
        "--json-schema", SCHEMA,
        "--output-format", "json",
    ]
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    meta = {"latency_s": round(time.monotonic() - t0, 1), "returncode": proc.returncode}
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        meta["stderr"] = proc.stderr[-2000:]
        return None, meta
    meta["cost_usd"] = envelope.get("total_cost_usd")
    out = envelope.get("structured_output")
    if out is None:
        meta["raw_result"] = str(envelope.get("result"))[:2000]
    return out, meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", choices=["agent", "tools-only"], default="agent")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--only", default="", help="substring filter on case id")
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()

    manifest = json.loads((HERE / "cases" / "manifest.json").read_text())
    cases = [c for c in manifest if args.only in c["id"]]
    model = args.model if args.system == "agent" else "none"
    out_dir = HERE / "results" / f"{datetime.now():%Y%m%d-%H%M%S}_{args.system}_{model}"
    out_dir.mkdir(parents=True)
    system_prompt = agent_prompt()

    def one(job):
        case, rep = job
        path = HERE / "cases" / case["file"]
        facts = config_facts(path)
        if args.system == "tools-only":
            out, meta = run_tools_only(path, facts)
        else:
            out, meta = run_agent(path, facts, args.model, system_prompt)
        graded = grade_case(case, out) | {"repeat": rep, **meta}
        (out_dir / f"{case['id']}.r{rep}.json").write_text(
            json.dumps({"case": case, "output": out, "grade": graded}, indent=2))
        return graded

    jobs = [(c, r) for r in range(args.repeats) for c in cases]
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(one, jobs))

    summary = summarise(results)
    summary.update(system=args.system, model=model, repeats=args.repeats,
                   cost_usd=round(sum(r.get("cost_usd") or 0 for r in results), 4))
    (out_dir / "report.json").write_text(json.dumps({"summary": summary, "cases": results}, indent=2))
    (out_dir / "report.md").write_text(render(summary, results))
    print(render(summary, results))
    print(f"\nresults: {out_dir}")
    return 0 if summary["passed"] else 1


def render(summary: dict, results: list[dict]) -> str:
    def pct(v):
        return "n/a" if v is None else f"{v:.0%}"

    lines = [f"# config-auditor eval — {summary['system']} / {summary['model']} "
             f"(repeats={summary['repeats']}, cost=${summary['cost_usd']})", "",
             "| metric | value | gate |", "|---|---|---|"]
    for k, ok in summary["gates"].items():
        lines.append(f"| {k} | {pct(summary[k])} | {'PASS' if ok else 'FAIL'} |")
    lines += [f"| errors | {summary['errors']} | |", "",
              f"**{'PASSED' if summary['passed'] else 'FAILED'}**", "",
              "| case | kind | result | fields named (med/high) |", "|---|---|---|---|"]
    for r in sorted(results, key=lambda r: (r["kind"], r["id"], r["repeat"])):
        if r["kind"] == "clean":
            res = "false alarm" if r["false_alarm"] else "ok"
        elif r["kind"] == "real":
            res = f"{r['hits']}/{r['targets']}" + (f" missed {r['missed']}" if r["missed"] else "")
        else:
            res = "caught" if r["caught"] else "MISSED"
        if r["error"]:
            res = "ERROR"
        lines.append(f"| {r['id']} | {r['kind']} | {res} | {', '.join(r['named'])} |")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())

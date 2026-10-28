"""
Run every agent's eval on its production model and record the result in evals/STATUS.json.

    V=../val-machine/.venv/bin/python
    $V evals/run_all.py                 # all agents (~$1.50, a few minutes)
    $V evals/run_all.py --only verifier

STATUS.json records, per agent, the prompt hash the pass was earned with. `grinos registry`
marks an agent Live only if its CURRENT prompt passed; a changed prompt is Paused until re-run.
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from common import append_history, metrics, per_case, publish  # noqa: E402

from grinos.llm import agent_actor  # noqa: E402

# agent -> (eval dir, production model, repeats)
SUITE = {
    "config-auditor": ("config-auditor", "sonnet", 1),
    "decision-model": ("decision-model", "sonnet", 2),
    "verifier": ("verifier", "sonnet", 2),
    "claim-extractor": ("claim-extractor", "sonnet", 2),
    "mid-cycle-analyst": ("analyst", "sonnet", 2),
    "orchestrator": ("orchestrator", "sonnet", 2),     # replaced desk-router as the only conversational agent
    "prompt-optimiser": ("prompt-optimiser", "sonnet", 2),
    "memo-writer": ("memo-writer", "sonnet", 2),
}
STATUS = HERE / "STATUS.json"
HISTORY = HERE / "history.jsonl"     # every run, per prompt version: the trend the improvement loop reads


def latest_report(eval_dir: str, model: str) -> dict | None:
    runs = sorted((HERE / eval_dir / "results").glob(f"*_agent_{model}"))
    return json.loads((runs[-1] / "report.json").read_text()) if runs else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    status = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    for agent, (ev, model, repeats) in SUITE.items():
        if args.only and args.only != agent:
            continue
        print(f"→ {agent} ({ev}, {model}, repeats={repeats})", flush=True)
        proc = subprocess.run([sys.executable, str(HERE / ev / "run.py"), "--system", "agent",
                               "--model", model, "--repeats", str(repeats)], capture_output=True, text=True)
        report = latest_report(ev, model) or {}
        summary = report.get("summary", {})
        status[agent] = {
            "eval": ev, "model": model, "repeats": repeats,
            "passed": proc.returncode == 0 and bool(summary.get("passed", proc.returncode == 0)),
            "prompt_version": agent_actor(agent, model).version,
            "date": datetime.now().isoformat(timespec="seconds"),
            "cost_usd": summary.get("cost_usd"),
            "gates": summary.get("gates"),
        }
        trace_id = publish(agent, report, status[agent]["prompt_version"], model) if report else None
        status[agent]["trace_id"] = trace_id
        append_history(HISTORY, status[agent] | {"agent": agent, "metrics": metrics(summary),
                                                 "per_case": per_case(report) if report else {}})
        print(f"   {'PASS' if status[agent]['passed'] else 'FAIL'}  cost=${summary.get('cost_usd')}")
    STATUS.write_text(json.dumps(status, indent=2) + "\n")
    failed = [a for a, s in status.items() if not s["passed"]]
    print(f"\n{len(status) - len(failed)}/{len(status)} agents pass." + (f" Failing: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

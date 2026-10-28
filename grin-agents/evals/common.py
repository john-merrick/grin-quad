"""
Shared eval helpers. Every eval's report.json rows carry `id`, `repeat` (when repeated) and
`case_pass`, so a run can be compared case by case: the improvement loop promotes a candidate prompt
only if no case does worse than under the current prompt.
"""
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def rows(report: dict) -> list[dict]:
    return report.get("rows") or report.get("cases") or []


def per_case(report: dict) -> dict[str, float]:
    """case id → pass rate across repeats."""
    by: dict[str, list[bool]] = defaultdict(list)
    for r in rows(report):
        by[str(r["id"])].append(bool(r["case_pass"]))
    return {k: statistics.mean(v) for k, v in sorted(by.items())}


def latest_report(eval_dir: Path, system: str = "agent") -> dict | None:
    runs = sorted((eval_dir / "results").glob(f"*_{system}_*/report.json")) if (eval_dir / "results").exists() else []
    return json.loads(runs[-1].read_text()) if runs else None


def metrics(summary: dict) -> dict[str, float]:
    """The numeric gate metrics of a summary (booleans and bookkeeping left out)."""
    skip = {"n", "repeats", "cost_usd", "passed"}
    return {k: float(v) for k, v in summary.items()
            if k not in skip and isinstance(v, (int, float)) and not isinstance(v, bool)}


def publish(agent: str, report: dict, prompt_version: str, model: str, label: str = "") -> str:
    """Record an eval run as a trace: one score per gate metric and per case, so prompt versions can be
    compared over time. With Langfuse configured, each case is also a dataset item and the run a dataset
    run. Returns the trace id. Best effort: never fails the eval."""
    from grinos import trace
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_name = f"{prompt_version}-{stamp}" + (f"-{label}" if label else "")
    tid = trace.trace_id_for(f"eval:{agent}:{run_name}")
    summary = report.get("summary", {})
    cases = per_case(report)
    with trace.span(f"eval {agent}", trace_id=tid, tags=("eval",), prompt_version=prompt_version, model=model,
                    metadata={"agent": agent, "run": run_name, "passed": summary.get("passed"),
                              "gates": summary.get("gates")}) as s:
        s.end(output={"passed": summary.get("passed"), **metrics(summary)}, cost_usd=summary.get("cost_usd"))
    for k, v in metrics(summary).items():
        trace.score(tid, k, v)
    for cid, rate in cases.items():
        trace.score(tid, f"case:{cid}", rate)
    lf = trace.langfuse()
    if lf:
        dataset = f"eval/{agent}"
        try:
            lf.post("/api/public/datasets", {"name": dataset})
        except Exception:  # noqa: BLE001 - it may already exist
            pass
        for cid, rate in cases.items():
            item = f"{agent}:{cid}"
            try:
                lf.post("/api/public/dataset-items", {"datasetName": dataset, "id": item, "input": {"case": cid}})
                lf.post("/api/public/dataset-run-items", {"runName": run_name, "datasetItemId": item, "traceId": tid,
                                                          "metadata": {"case_pass_rate": rate}})
            except Exception:  # noqa: BLE001
                break
    return tid


def append_history(path: Path, row: dict) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def results_dir(here: Path, system: str, model: str) -> Path:
    """Where an eval run writes: GRINOS_EVAL_OUT when the caller chose (parallel candidate runs in the
    improvement loop each get their own), else results/<timestamp>_<system>_<model>[-n]."""
    import os
    if os.environ.get("GRINOS_EVAL_OUT"):
        out = Path(os.environ["GRINOS_EVAL_OUT"])
        out.mkdir(parents=True, exist_ok=True)
        return out
    base = here / "results" / f"{datetime.now():%Y%m%d-%H%M%S}_{system}_{model}"
    out, n = base, 1
    while out.exists():
        out, n = base.with_name(f"{base.name}-{n}"), n + 1
    out.mkdir(parents=True)
    return out

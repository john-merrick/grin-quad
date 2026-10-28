"""
How the CLI, dashboard, desk and Telegram start work: on Temporal when it is up (each agent call an activity
on its own queue, visible in the Temporal UI), otherwise inline, recorded as such (grinos.temporal.client).

The worker writes the default ledger, so a caller using another ledger root (tests, --root) runs inline.
"""
import time
from datetime import date
from pathlib import Path

import yaml

from grinos.ledger import Ledger
from grinos.temporal import client


def _temporal_ok(ledger: Ledger) -> bool:
    from grinos.cli import default_root
    return Path(ledger.root).resolve() == Path(default_root()).resolve() and client.mode() != "off"


def _run(ledger: Ledger, workflow: str, args: dict, workflow_id: str, inline, run_id: str, wait: bool) -> dict:
    if not _temporal_ok(ledger):
        return {"via": "inline", "result": inline()}
    return client.run_or_inline(ledger, workflow, args, workflow_id, inline, run_id=run_id, wait=wait)


def ingest(ledger: Ledger, data: bytes, meta: dict, claims: list[dict] | None, as_of: date,
           config_path: str | None = None, model: str | None = None, notify: dict | None = None, wait: bool = True) -> dict:
    from grinos import pipeline
    run_id = pipeline.new_run_id()
    blob = ledger.put_blob(data)
    args = {"run_id": run_id, "blob": blob, "meta": {k: str(v) for k, v in meta.items()}, "as_of": as_of.isoformat(),
            "claims": claims, "config_path": config_path, "model": model, "notify": notify}
    config = yaml.safe_load(Path(config_path).read_text()) if config_path else None

    def inline() -> dict:
        return pipeline.ingest(ledger, data, meta, claims, as_of, config, model=model, run_id=run_id)
    return _run(ledger, "Ingest", args, f"ingest:{run_id}", inline, run_id, wait) | {"run_id": run_id}


def analyse(ledger: Ledger, company: str, config_path: str, model: str | None = None, net_debt: float = 0.0,
            shares: float = 1.0, notify: dict | None = None, wait_for_proposals: bool = True,
            timeout_s: int = 1200) -> dict:
    """Start the analysis (it then waits for your decisions on Temporal). Returns the analyst's proposals
    once recorded when `wait_for_proposals`, else as soon as it has started."""
    from grinos import proposals
    run_id = proposals.new_analysis_id()
    args = {"company": company, "config_path": config_path, "model": model, "net_debt": net_debt, "shares": shares,
            "run_id": run_id, "notify": notify}

    def inline() -> dict:
        return proposals.analyse(ledger, company, yaml.safe_load(Path(config_path).read_text()), config_path,
                                 model=model, net_debt=net_debt, shares=shares, run_id=run_id)
    r = _run(ledger, "Analysis", args, f"analysis:{run_id}", inline, run_id, wait=False)
    if r["via"] == "temporal" and wait_for_proposals:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if ledger.find_idem(f"{run_id}:done") or ledger.find_idem(f"{run_id}:failed"):
                break
            time.sleep(2)
        r["result"] = proposals._result(ledger, run_id) if ledger.find_idem(f"{run_id}:done") else {
            "run_id": run_id, "proposals": [], "no_change": "still running on Temporal; you'll be told"}
    return r | {"run_id": run_id}


def coverage(ledger: Ledger, ticker: str, as_of: date, config_path: str, get, who: str,
             notify: dict | None = None) -> dict:
    from grinos import coverage as C
    args = {"ticker": ticker.upper(), "as_of": as_of.isoformat(), "config_path": config_path, "who": who,
            "notify": notify}

    def inline() -> dict:
        return C.start(ledger, ticker, as_of, config_path, get, who=who)
    wid = f"coverage:{ticker.upper()}:{as_of}"
    return _run(ledger, "Coverage", args, wid, inline, wid, wait=False)


def changed(ledger: Ledger, run_id: str) -> int:
    """A human decided something on `run_id`: wake the workflow(s) waiting on it."""
    return client.signal_run(ledger, run_id) if _temporal_ok(ledger) else 0


def agent_call(ledger: Ledger, name: str, message: str, schema: dict, model: str | None = None,
               timeout: int = 600, system_prompt: str | None = None) -> tuple[dict | None, dict]:
    """call_agent, but on the agent's own Temporal queue when Temporal is up (same return shape)."""
    import uuid

    from grinos.llm import call_agent
    if _temporal_ok(ledger):
        try:
            r = client.start(ledger, "AgentCall", {"agent": name, "message": message, "schema": schema, "model": model,
                                                   "system_prompt": system_prompt},
                             f"call:{name}:{uuid.uuid4().hex[:12]}", wait=True, record=False)
            res = r["result"]
            return res["output"], res["meta"] | {"workflow_id": r["workflow_id"]}
        except client.TemporalUnavailable:
            pass
    return call_agent(name, message, schema, model=model, timeout=timeout, system_prompt=system_prompt)

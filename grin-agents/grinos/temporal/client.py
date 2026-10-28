"""
Starting and signalling workflows from synchronous code (CLI, dashboard, Telegram, desk).

GRINOS_TEMPORAL = optional (default): use Temporal when the server answers, else run inline and record
                  that the work ran inline (`execution` stage), so nothing stalls and nothing is hidden.
                  required: refuse to run when Temporal is down.   off: always inline.
"""
import asyncio
import os
from typing import Any

from grinos.ledger import Ledger
from grinos.schemas import Actor

ADDRESS = os.environ.get("GRINOS_TEMPORAL_ADDRESS", "127.0.0.1:7233")
NAMESPACE = os.environ.get("GRINOS_TEMPORAL_NAMESPACE", "default")
SYSTEM = Actor(kind="system", name="temporal", version="1")


class TemporalUnavailable(RuntimeError):
    pass


def mode() -> str:
    m = os.environ.get("GRINOS_TEMPORAL", "optional")
    return m if m in ("optional", "required", "off") else "optional"


async def _client(timeout: float = 2.0):
    try:
        from temporalio.client import Client
    except ImportError as exc:
        raise TemporalUnavailable("temporalio is not installed (pip install -r requirements.txt)") from exc
    try:
        return await asyncio.wait_for(Client.connect(ADDRESS, namespace=NAMESPACE), timeout)
    except Exception as exc:  # noqa: BLE001 - any failure to reach the server means "unavailable"
        raise TemporalUnavailable(f"Temporal at {ADDRESS} is not reachable ({type(exc).__name__})") from exc


def start(ledger: Ledger, workflow: str, args: dict, workflow_id: str, run_id: str | None = None,
          wait: bool = False, record: bool = True) -> dict:
    """Start (or attach to) a workflow. Duplicate ids attach to the running one instead of starting twice.
    Raises TemporalUnavailable when it can't; callers decide whether to fall back inline."""
    if mode() == "off":
        raise TemporalUnavailable("GRINOS_TEMPORAL=off")

    async def go() -> dict:
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.common import WorkflowIDReusePolicy
        from temporalio.exceptions import WorkflowAlreadyStartedError
        client = await _client()
        try:
            handle = await client.start_workflow(workflow, args, id=workflow_id, task_queue="grin-workflows",
                                                 id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
            started = True
        except WorkflowAlreadyStartedError:
            handle, started = client.get_workflow_handle(workflow_id), False
        out: dict[str, Any] = {"workflow_id": workflow_id, "started": started}
        if wait:
            out["result"] = await handle.result()
        else:
            desc = await handle.describe()
            out["status"] = WorkflowExecutionStatus(desc.status).name if desc.status else None
        return out
    out = asyncio.run(go())
    if out["started"] and record:
        ledger.append_once(run_id or workflow_id, "workflow_started", SYSTEM,
                           {"workflow_id": workflow_id, "workflow": workflow, "run_id": run_id,
                            "args": {k: v for k, v in args.items() if k not in ("claims",)}},
                           idem=f"wf:{workflow_id}")
    return out


def run_or_inline(ledger: Ledger, workflow: str, args: dict, workflow_id: str, inline, run_id: str | None = None,
                  wait: bool = True) -> dict:
    """Run through Temporal if possible; otherwise (optional mode) run `inline()` here and record that."""
    try:
        return {"via": "temporal", **start(ledger, workflow, args, workflow_id, run_id, wait=wait)}
    except TemporalUnavailable as exc:
        if mode() == "required":
            raise
        ledger.append(run_id or workflow_id, "execution", SYSTEM,
                      {"workflow": workflow, "workflow_id": workflow_id, "mode": "inline", "reason": str(exc)})
        return {"via": "inline", "result": inline(), "reason": str(exc)}


def signal_run(ledger: Ledger, run_id: str) -> int:
    """After a human decision on a run: wake every workflow driving it. Best effort; returns how many."""
    ids = {e["payload"]["workflow_id"] for e in ledger.entries()
           if e["stage"] in ("workflow_started", "workflow_link") and e["payload"].get("run_id") == run_id}
    if not ids or mode() == "off":
        return 0

    async def go() -> int:
        try:
            client = await _client(timeout=1.0)
        except TemporalUnavailable:
            return 0
        n = 0
        for wid in ids:
            try:
                await client.get_workflow_handle(wid).signal("ledger_changed")
                n += 1
            except Exception:  # noqa: BLE001 - a finished workflow can't be signalled; that's fine
                pass
        return n
    try:
        return asyncio.run(go())
    except RuntimeError:          # already inside an event loop: skip, the timer will pick it up
        return 0

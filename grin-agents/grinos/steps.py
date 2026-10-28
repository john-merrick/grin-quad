"""
The step machine shared by the FCFF valuation and the coverage workflow.

A run is an ordered list of deterministic steps. `advance` runs them in order against a context and
stops at the first step that is not done (blocked on a human, invalid, unsupported). Each step's
output is checkpointed in the ledger only when it changes (by digest), so re-running a run is free
and safe; a human decision is recorded once (`decide`, first decision final) and the next `advance`
picks it up.

Ledger stages for a machine with prefix P:
  P_step      one entry per step whose output changed     actor: the machine's rule actor
  P_status    where the run stands                        actor: the machine's rule actor
  P_decision  a human resolves a blocking item            actor: human; first decision is final
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from grinos import trace, xbrl
from grinos.ledger import Ledger
from grinos.schemas import Actor, canonical, sha256


@dataclass
class Machine:
    prefix: str                                   # "valuation" | "coverage"
    steps: list[str]
    run: Callable[[str, Any], dict]               # (step, ctx) → {"status", "output", "blocking"?}
    actor: Actor


@dataclass
class State:
    decisions: dict[str, dict] = field(default_factory=dict)      # target → first decision payload
    last: dict[str, dict] = field(default_factory=dict)           # step → last checkpointed payload
    status: dict | None = None


def state(machine: Machine, ledger: Ledger, run_id: str) -> State:
    st = State()
    for e in ledger.entries():
        if e["run_id"] != run_id:
            continue
        if e["stage"] == f"{machine.prefix}_status":
            st.status = e["payload"]
        elif e["stage"] == f"{machine.prefix}_decision":
            if e["payload"].get("revises") is not None:                 # an explicit revision replaces it
                st.decisions[e["payload"]["target"]] = e["payload"]
            else:
                st.decisions.setdefault(e["payload"]["target"], e["payload"])
        elif e["stage"] == f"{machine.prefix}_step":
            st.last[e["payload"]["step"]] = e["payload"]
    return st


def advance(machine: Machine, ledger: Ledger, run_id: str, ctx: Any, st: State, out: dict) -> dict:
    """Run the steps in order; `out` (step → output) is filled as they run. Returns the run's state."""
    status, step = "running", machine.steps[0]
    result: dict[str, Any] = {"status": "done", "output": {}}
    for step in machine.steps:
        with trace.span(f"{machine.prefix} step {step}", session_id=run_id, metadata={"run_id": run_id}) as s:
            result = machine.run(step, ctx)
            s.end(output={"status": result["status"], "blocking": [i["id"] for i in result.get("blocking", [])]})
        out[step] = result["output"]
        body = xbrl.to_jsonable({"step": step, "status": result["status"], "output": result["output"],
                                 "blocking": result.get("blocking", [])})
        digest = sha256(canonical(body).encode())
        if st.last.get(step, {}).get("digest") != digest:                    # checkpoint: append on change
            ledger.append(run_id, f"{machine.prefix}_step", machine.actor, body | {"digest": digest})
        if result["status"] != "done":
            status = result["status"]
            break
    else:
        status = "complete"
    new = {"status": status, "step": step, "blocking": [i["id"] for i in result.get("blocking", [])]}
    if st.status != new:
        ledger.append(run_id, f"{machine.prefix}_status", machine.actor, new)
    return {"run_id": run_id, "status": status, "step": step, "blocking": result.get("blocking", []), "out": out}


def latest_steps(machine: Machine, ledger: Ledger, run_id: str) -> dict[str, dict]:
    return state(machine, ledger, run_id).last


def record_decision(machine: Machine, ledger: Ledger, run_id: str, target: str, payload: dict, who: str,
                    revise: bool = False) -> dict:
    """Append a human decision on `target`. Refused if one already exists (first decision is final),
    unless `revise`: then it must replace an existing decision, and records which one (`revises`: its seq)."""
    body = {"target": target, **payload, "by": who}

    def first(entries: list[dict]) -> str | None:
        prior = [e for e in entries if e["run_id"] == run_id and e["stage"] == f"{machine.prefix}_decision"
                 and e["payload"]["target"] == target]
        if revise:
            if not prior:
                return f"{target} has no decision to revise"
            body["revises"] = prior[-1]["seq"]          # set under the lock, before the entry is hashed
            return None
        return f"{target} was already decided" if prior else None
    return ledger.append_if(run_id, f"{machine.prefix}_decision", Actor(kind="human", name=who, version="-"),
                            body, guard=first)


def view(machine: Machine, ledger: Ledger, run_id: str, request_stage: str) -> dict:
    """Read-only: request, latest output per step, status and decisions. Never advances the run."""
    out: dict[str, Any] = {"run_id": run_id, "request": None, "steps": {}, "status": None, "decisions": []}
    for e in ledger.entries():
        if e["run_id"] != run_id:
            continue
        p = e["payload"]
        if e["stage"] == request_stage:
            out["request"] = p
        elif e["stage"] == f"{machine.prefix}_step":
            out["steps"][p["step"]] = {"status": p["status"], "blocking": p["blocking"], "output": p["output"],
                                       "seq": e["seq"]}
        elif e["stage"] == f"{machine.prefix}_status":
            out["status"] = p
        elif e["stage"] == f"{machine.prefix}_decision":
            out["decisions"].append(p | {"seq": e["seq"], "ts": e["ts"]})
    if out["request"] is None:
        raise ValueError(f"no {machine.prefix} run {run_id}")
    return out

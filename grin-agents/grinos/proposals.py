"""
Analysis → proposals → human approval, all in the ledger.

  proposal        analyst output, one entry per proposed change     actor: mid-cycle-analyst agent
  proposal_check  deterministic check + engine impact                actor: rule proposal_check@<hash>
  approval        approve / reject by a human                        actor: human
"""
import uuid
from datetime import datetime

from grinos import proposal_check as pc
from grinos import trace
from grinos.analyst import propose
from grinos.ledger import Ledger
from grinos.llm import agent_actor
from grinos.pipeline import rule_actor
from grinos.schemas import Actor, sha256


def evidence_with_decisions(ledger: Ledger, company: str) -> list[dict]:
    """Verified claims (through the gate) joined with the decision model's nature and lever."""
    verified = ledger.verified_claims(company)
    decisions = {e["payload"]["claim_id"]: e["payload"] for e in ledger.entries() if e["stage"] == "decision"}
    return [v | {"nature": decisions[v["claim_id"]]["nature"],
                 "lever": decisions[v["claim_id"]]["lever_facts"]["lever"]} for v in verified]


def new_analysis_id() -> str:
    return f"analysis_{datetime.now():%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:6]}"


def analyse(ledger: Ledger, company: str, config: dict, config_path: str, model: str = "sonnet",
            net_debt: float = 0.0, shares: float = 1.0, run_id: str | None = None) -> dict:
    """
    Safe to re-run with the same run_id (a retried Temporal activity): the analyst's output is recorded
    once and reused, proposal ids are derived from the run, and every write is idempotent.
    """
    run_id = run_id or new_analysis_id()
    with trace.span("analyse", session_id=run_id, input={"company": company, "config": config_path},
                    metadata={"run_id": run_id}) as s:
        res = _analyse(ledger, company, config, config_path, model, net_debt, shares, run_id)
        s.end(output={"proposals": [{"id": p["proposal_id"], "field": p["field"], "ok": p["ok"]}
                                    for p in res["proposals"]], "no_change": res["no_change"][:500]})
        return res


def _analyse(ledger: Ledger, company: str, config: dict, config_path: str, model: str,
             net_debt: float, shares: float, run_id: str) -> dict:
    prior = {e["stage"]: e for e in ledger.entries() if e["run_id"] == run_id}
    if "analysis_done" in prior:
        return _result(ledger, run_id)
    evidence = evidence_with_decisions(ledger, company)
    if not evidence:
        return {"run_id": run_id, "proposals": [], "no_change": "no verified evidence for this company"}
    ids = {e["claim_id"] for e in evidence}
    actor = agent_actor("mid-cycle-analyst", model)
    if "analysis_output" in prior:
        out, meta = prior["analysis_output"]["payload"]["output"], prior["analysis_output"]["payload"]["meta"]
    else:
        from grinos.review import perspectives
        out, meta = propose(company, config, evidence, model=model, perspectives=perspectives(ledger, company))
        if out is None:
            ledger.append_once(run_id, "analysis_failed", actor, {"company": company, "meta": meta},
                               idem=f"{run_id}:failed")
            return {"run_id": run_id, "proposals": [], "no_change": "analyst failed; nothing proposed"}
        ledger.append_once(run_id, "analysis_output", actor, {"company": company, "output": out, "meta": meta},
                           idem=f"{run_id}:output")

    pids = []
    for i, p in enumerate(out["proposals"]):
        pid = "p_" + sha256(f"{run_id}|{i}|{p['field']}".encode())[:10]
        ledger.append_once(run_id, "proposal", actor,
                           {"proposal_id": pid, "company": company, "config_path": config_path,
                            "config": config, "net_debt": net_debt, "shares": shares, "proposal": p,
                            "evidence_ids": sorted(ids), "meta": meta},
                           idem=f"{run_id}:proposal:{i}", refs=[f"claim:{c}" for c in p["cites"]])
        chk = pc.check_proposal(p, config, ids, net_debt, shares)
        ledger.append_once(run_id, "proposal_check", rule_actor(pc), {"proposal_id": pid, **chk},
                           idem=f"{run_id}:check:{i}")
        pids.append(pid)
    ledger.append_once(run_id, "analysis_done", actor, {"company": company, "no_change": out["no_change"],
                                                         "proposal_ids": pids}, idem=f"{run_id}:done")
    return _result(ledger, run_id)


def _result(ledger: Ledger, run_id: str) -> dict:
    es = [e for e in ledger.entries() if e["run_id"] == run_id]
    done = next(e for e in es if e["stage"] == "analysis_done")["payload"]
    props = {e["payload"]["proposal_id"]: e for e in es if e["stage"] == "proposal"}
    checks = {e["payload"]["proposal_id"]: e["payload"] for e in es if e["stage"] == "proposal_check"}
    results = []
    for pid in done["proposal_ids"]:
        p, chk = props[pid]["payload"]["proposal"], checks[pid]
        results.append({"proposal_id": pid, "field": p["field"], "new_value": chk.get("normalised_value", p["new_value"]),
                        "ok": chk["ok"], "problems": chk["problems"], "impact": chk["impact"],
                        "ledger_hash": props[pid]["hash"]})
    return {"run_id": run_id, "proposals": results, "no_change": done["no_change"]}


PAST = {"approve": "approved", "reject": "rejected"}


def proposal_state(ledger: Ledger) -> dict[str, dict]:
    """proposal_id → {proposal entry, check, approval (latest), published}."""
    state: dict[str, dict] = {}
    for e in ledger.entries():
        p = e["payload"]
        pid = p.get("proposal_id")
        if not pid:
            continue
        s = state.setdefault(pid, {"proposal": None, "check": None, "approval": None, "published": None})
        key = {"proposal": "proposal", "proposal_check": "check", "approval": "approval", "published": "published"}.get(e["stage"])
        if key and not (key in {"approval", "published"} and s[key] is not None):  # first decision is final
            s[key] = e
    return state


def latest_analysis(ledger: Ledger) -> dict[str, str]:
    """company → run_id of its most recent completed analysis. Older runs' proposals are superseded."""
    return {e["payload"]["company"]: e["run_id"] for e in ledger.entries() if e["stage"] == "analysis_done"}


def superseded(s: dict, latest: dict[str, str]) -> bool:
    p = s["proposal"]
    return p is not None and latest.get(p["payload"]["company"], p["run_id"]) != p["run_id"]


def pending(ledger: Ledger) -> list[dict]:
    latest = latest_analysis(ledger)
    return [s for s in proposal_state(ledger).values()
            if s["proposal"] and s["check"] and s["check"]["payload"]["ok"] and s["approval"] is None
            and not superseded(s, latest)]


def decide_proposal(ledger: Ledger, proposal_id: str, decision: str, who: str, note: str,
                    channel: str = "cli", require_review: bool = True) -> dict:
    """Human approve/reject. Refuses proposals that failed their deterministic check, and approval until
    every cited claim has been confirmed in review (grinos.review), on every channel."""
    s = proposal_state(ledger).get(proposal_id)
    if not s or not s["proposal"]:
        raise ValueError(f"no proposal {proposal_id}")
    if decision == "approve" and not s["check"]["payload"]["ok"]:
        raise ValueError(f"{proposal_id} failed its checks and cannot be approved: {s['check']['payload']['problems']}")
    if s["approval"] is not None:
        raise ValueError(f"{proposal_id} already {s['approval']['payload']['decision']}d")
    if decision == "approve" and superseded(s, latest_analysis(ledger)):
        raise ValueError(f"{proposal_id} is superseded by a later analysis of {s['proposal']['payload']['company']}")
    cites = s["proposal"]["payload"]["proposal"]["cites"]
    if decision == "approve" and require_review:
        from grinos.review import unreviewed
        missing = unreviewed(ledger, proposal_id, cites)
        if missing:
            raise ValueError(f"{len(missing)} of {len(cites)} cited claims not confirmed in review "
                             f"({', '.join(missing[:5])}): review {proposal_id} in the dashboard first")

    def not_yet_decided(entries: list[dict]) -> str | None:
        # Re-checked under the ledger lock: two channels approving/rejecting at once can't both win.
        if any(e["stage"] == "approval" and e["payload"]["proposal_id"] == proposal_id for e in entries):
            return f"{proposal_id} was decided concurrently"
        return None

    return ledger.append_if(f"approval_{datetime.now():%Y%m%d-%H%M%S}", "approval",
                            Actor(kind="human", name=who, version="-"),
                            {"proposal_id": proposal_id, "decision": decision, "note": note, "channel": channel,
                             "cites_reviewed": require_review and decision == "approve"},
                            refs=[f"entry:{s['proposal']['hash']}", f"entry:{s['check']['hash']}"],
                            guard=not_yet_decided)

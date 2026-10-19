"""
Analysis → proposals → human approval, all in the ledger.

  proposal        analyst output, one entry per proposed change     actor: mid-cycle-analyst agent
  proposal_check  deterministic check + engine impact                actor: rule proposal_check@<hash>
  approval        approve / reject by a human                        actor: human
"""
import uuid
from datetime import datetime

from grinos import proposal_check as pc
from grinos.analyst import propose
from grinos.ledger import Ledger
from grinos.llm import agent_actor
from grinos.pipeline import rule_actor
from grinos.schemas import Actor


def evidence_with_decisions(ledger: Ledger, company: str) -> list[dict]:
    """Verified claims (through the gate) joined with the decision model's nature and lever."""
    verified = ledger.verified_claims(company)
    decisions = {e["payload"]["claim_id"]: e["payload"] for e in ledger.entries() if e["stage"] == "decision"}
    return [v | {"nature": decisions[v["claim_id"]]["nature"],
                 "lever": decisions[v["claim_id"]]["lever_facts"]["lever"]} for v in verified]


def analyse(ledger: Ledger, company: str, config: dict, config_path: str, model: str = "sonnet",
            net_debt: float = 0.0, shares: float = 1.0) -> dict:
    run_id = f"analysis_{datetime.now():%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:6]}"
    evidence = evidence_with_decisions(ledger, company)
    if not evidence:
        return {"run_id": run_id, "proposals": [], "no_change": "no verified evidence for this company"}
    ids = {e["claim_id"] for e in evidence}
    out, meta = propose(company, config, evidence, model=model)
    actor = agent_actor("mid-cycle-analyst", model)
    if out is None:
        ledger.append(run_id, "analysis_failed", actor, {"company": company, "meta": meta})
        return {"run_id": run_id, "proposals": [], "no_change": "analyst failed; nothing proposed"}

    results = []
    for p in out["proposals"]:
        pid = "p_" + uuid.uuid4().hex[:10]
        e = ledger.append(run_id, "proposal", actor,
                          {"proposal_id": pid, "company": company, "config_path": config_path,
                           "config": config, "net_debt": net_debt, "shares": shares, "proposal": p,
                           "evidence_ids": sorted(ids), "meta": meta},
                          refs=[f"claim:{c}" for c in p["cites"]])
        chk = pc.check_proposal(p, config, ids, net_debt, shares)
        ledger.append(run_id, "proposal_check", rule_actor(pc), {"proposal_id": pid, **chk})
        results.append({"proposal_id": pid, "field": p["field"], "new_value": chk.get("normalised_value", p["new_value"]),
                        "ok": chk["ok"], "problems": chk["problems"], "impact": chk["impact"],
                        "ledger_hash": e["hash"]})
    ledger.append(run_id, "analysis_done", actor, {"company": company, "no_change": out["no_change"],
                                                    "proposal_ids": [r["proposal_id"] for r in results]})
    return {"run_id": run_id, "proposals": results, "no_change": out["no_change"]}


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
                    channel: str = "cli") -> dict:
    """Human approve/reject. Refuses proposals that failed their deterministic check."""
    s = proposal_state(ledger).get(proposal_id)
    if not s or not s["proposal"]:
        raise ValueError(f"no proposal {proposal_id}")
    if decision == "approve" and not s["check"]["payload"]["ok"]:
        raise ValueError(f"{proposal_id} failed its checks and cannot be approved: {s['check']['payload']['problems']}")
    if s["approval"] is not None:
        raise ValueError(f"{proposal_id} already {s['approval']['payload']['decision']}d")
    if decision == "approve" and superseded(s, latest_analysis(ledger)):
        raise ValueError(f"{proposal_id} is superseded by a later analysis of {s['proposal']['payload']['company']}")
    def not_yet_decided(entries: list[dict]) -> str | None:
        # Re-checked under the ledger lock: two channels approving/rejecting at once can't both win.
        if any(e["stage"] == "approval" and e["payload"]["proposal_id"] == proposal_id for e in entries):
            return f"{proposal_id} was decided concurrently"
        return None

    return ledger.append_if(f"approval_{datetime.now():%Y%m%d-%H%M%S}", "approval",
                            Actor(kind="human", name=who, version="-"),
                            {"proposal_id": proposal_id, "decision": decision, "note": note, "channel": channel},
                            refs=[f"entry:{s['proposal']['hash']}", f"entry:{s['check']['hash']}"],
                            guard=not_yet_decided)

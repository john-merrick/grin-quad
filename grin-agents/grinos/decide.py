"""
The decision stage: rules give the lever and deviation; the decision-model agent judges
nature, materiality and whether the claim is worth verifying. Discards are logged, never lost.
"""
import json

from grinos.levers import lever_facts
from grinos.llm import call_agent
from grinos.schemas import fence

DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "nature", "materiality", "rationale"],
    "properties": {
        "action": {"type": "string", "enum": ["verify", "discard"]},
        "nature": {"type": "string", "enum": ["structural", "cyclical", "one_off", "noise", "not_applicable"]},
        "materiality": {"type": "string", "enum": ["high", "medium", "low"]},
        "rationale": {"type": "string"},
    },
}


def decision_message(claim: dict, facts: dict, config: dict | None) -> str:
    return (
        "Decide how this evidence bears on the valuation.\n\n"
        f"<claim>\n{fence(json.dumps(claim, indent=2))}\n</claim>\n\n"
        f"<lever_facts>\n{json.dumps(facts, indent=2)}\n</lever_facts>\n\n"
        f"<current_config>\n{json.dumps(config, indent=2, default=str) if config else 'none'}\n</current_config>"
    )


def decide_claim(claim: dict, config: dict | None, model: str = "sonnet", use_agent: bool = True) -> dict:
    facts = lever_facts(claim, config)
    if not use_agent:
        out = {"action": "verify", "nature": "not_applicable", "materiality": "medium",
               "rationale": "rules-only: route everything to verification"}
        return {"facts": facts, "agent": None, "agent_meta": None, **out}
    out, meta = call_agent("decision-model", decision_message(claim, facts, config), DECISION_SCHEMA, model=model)
    # Fail open to verification: the verifier is the safety gate, a crashed triage mustn't drop evidence.
    out = out or {"action": "verify", "nature": "not_applicable", "materiality": "medium",
                  "rationale": "decision agent failed; routed to verification by default"}
    return {"facts": facts, "agent": out, "agent_meta": meta, **out}

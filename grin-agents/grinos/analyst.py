"""
The mid-cycle analyst: verified evidence → proposed assumption changes, each citing claim ids.
Proposals are checked deterministically (grinos.proposal_check) before a human sees them.
"""
import json

from grinos.llm import call_agent
from grinos.schemas import fence

PROPOSALS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["proposals", "no_change"],
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "new_value", "nature", "rationale", "cites"],
                "properties": {
                    "field": {"type": "string"},
                    "new_value": {"anyOf": [{"type": "number"}, {"type": "array", "items": {"type": "number"}}]},
                    "nature": {"type": "string", "enum": ["structural", "cyclical", "one_off"]},
                    "rationale": {"type": "string"},
                    "cites": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "no_change": {"type": "string", "description": "Why the remaining evidence changes nothing"},
    },
}


def analyst_message(company: str, config: dict, evidence: list[dict]) -> str:
    items = [{"claim_id": e["claim_id"], "nature": e.get("nature"), "lever": e.get("lever"), **e["claim"]}
             for e in evidence]
    return (
        f"Review the verified evidence for {company} against its current valuation config and "
        f"propose any assumption changes.\n\n"
        f"<current_config>\n{json.dumps(config, indent=2, default=str)}\n</current_config>\n\n"
        f"<verified_evidence>\n{fence(json.dumps(items, indent=2))}\n</verified_evidence>"
    )


def propose(company: str, config: dict, evidence: list[dict], model: str = "sonnet") -> tuple[dict | None, dict]:
    return call_agent("mid-cycle-analyst", analyst_message(company, config, evidence), PROPOSALS_SCHEMA, model=model)

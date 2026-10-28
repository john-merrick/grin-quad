"""
The verification stage: rules first, then the verifier agent only on claims the rules pass.
A rule rejection is final — the agent can't overrule it.
"""
from datetime import date

from grinos.evidence_check import check_claim, context_window
from grinos.llm import call_agent, tool_files
from grinos.schemas import Claim, Source, attr, fence

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reasons"],
    "properties": {
        "verdict": {"type": "string", "enum": ["verified", "rejected", "needs_human"]},
        "reasons": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "detail"],
                "properties": {
                    "code": {"type": "string", "enum": [
                        "supported", "segment_not_total", "non_gaap", "wrong_period",
                        "guidance_not_actual", "conditional_or_bound", "wrong_speaker",
                        "quarter_not_year", "metric_mismatch", "wrong_sign", "other"]},
                    "detail": {"type": "string"},
                },
            },
        },
    },
}


def verifier_message(claim: Claim, source: Source, source_text: str, rules: dict) -> str:
    return (
        f"Verify this claim against its source.\n\n"
        f"<claim>\n{fence(claim.model_dump_json(indent=2))}\n</claim>\n\n"
        f"<source kind=\"{source.kind}\" uri=\"{attr(source.uri)}\" published_at=\"{source.published_at}\">\n"
        f"{fence(context_window(source_text, claim.quote))}\n</source>\n\n"
        f"<rule_checks>\n{rules['checks']}\n</rule_checks>"
    )


def verify_claim(claim: Claim, source: Source, source_text: str, as_of: date,
                 model: str | None = None, use_agent: bool = True) -> dict:
    """Returns {rules, agent, agent_meta, verdict}. `agent` is None if rules rejected or use_agent=False."""
    rules = check_claim(claim, source, source_text, as_of)
    if rules["rules_verdict"] == "rejected":
        return {"rules": rules, "agent": None, "agent_meta": None, "verdict": "rejected"}
    if not use_agent:
        return {"rules": rules, "agent": None, "agent_meta": None, "verdict": "verified"}
    out, meta = call_agent("verifier", verifier_message(claim, source, source_text, rules),
                           VERDICT_SCHEMA, model=model, **tool_files("verifier", {"source.txt": source_text}))
    # An agent failure is not a pass: fail closed to human review.
    verdict = out["verdict"] if out else "needs_human"
    return {"rules": rules, "agent": out, "agent_meta": meta, "verdict": verdict}

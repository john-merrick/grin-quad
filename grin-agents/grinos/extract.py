"""
Claim extraction: source text → atomic claims with verbatim quotes. The extractor is untrusted:
everything it emits goes through the decision model and verifier like any other evidence.
Long sources are split into overlapping chunks; duplicate claims are dropped by claim id.
"""
import typing

from grinos.llm import call_agent
from grinos.schemas import Metric, fence, unfence

CHUNK_CHARS = 12_000
OVERLAP = 800

CLAIMS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claims"],
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["metric", "period", "value", "unit", "quote", "statement", "segment", "adjusted"],
                "properties": {
                    "metric": {"type": "string", "enum": list(typing.get_args(Metric))},
                    "period": {"type": "string"},
                    "value": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"], "enum": ["usd_m", "pct", "days", "x", "count_m", None]},
                    "quote": {"type": "string"},
                    "statement": {"type": "string"},
                    "segment": {"type": ["string", "null"]},
                    "adjusted": {"type": "boolean"},
                },
            },
        }
    },
}


def chunks(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text]
    out, i = [], 0
    while i < len(text):
        out.append(text[i: i + CHUNK_CHARS])
        i += CHUNK_CHARS - OVERLAP
    return out


def extract_claims(text: str, company: str, kind: str, model: str | None = None) -> tuple[list[dict], list[dict]]:
    """Returns (claims, per-chunk metadata). Claims lack source_blob/company; the pipeline adds them."""
    claims, metas, seen = [], [], set()
    for i, chunk in enumerate(chunks(text)):
        msg = (f"Extract claims about {company} from this {kind} (chunk {i + 1}).\n\n"
               f"<source>\n{fence(chunk)}\n</source>")
        out, meta = call_agent("claim-extractor", msg, CLAIMS_SCHEMA, model=model)
        metas.append(meta)
        for c in (out or {}).get("claims", []):
            c["quote"] = unfence(c["quote"])
            key = (c["metric"], c["period"], c["value"], c["quote"])
            if key not in seen:
                seen.add(key)
                claims.append(c)
    return claims, metas

"""
source + claims → decision model → rule checks → verifier → ledger.

Ledger stages written per run, in order:
  source        the blob and its metadata                      actor: system
  extraction    extractor output metadata (if claims not given) actor: claim-extractor agent
  claim         one per claim                                  actor: system, or claim-extractor
  decision      lever facts + decision-model output            actor: decision-model agent
  rule_checks   deterministic verifier rules                   actor: rule evidence_check@<hash>
  verdict       final verdict (rules reject, or agent's call)  actor: rule or verifier agent
"""
import inspect
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any

from grinos import evidence_check, levers
from grinos.decide import decide_claim
from grinos.extract import extract_claims
from grinos.ledger import Ledger
from grinos.llm import agent_actor
from grinos.schemas import Actor, Claim, Source, sha256
from grinos.verify import verify_claim

SYSTEM = Actor(kind="system", name="pipeline", version="1")


def rule_actor(module) -> Actor:
    return Actor(kind="rule", name=module.__name__.split(".")[-1],
                 version=sha256(inspect.getsource(module).encode())[:16])


def new_run_id() -> str:
    return f"run_{datetime.now():%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:6]}"


def ingest(ledger: Ledger, source_bytes: bytes, source_meta: dict, claims: list[dict] | None, as_of: date,
           config: dict | None = None, model: str = "sonnet", run_id: str | None = None,
           workers: int = 6, claim_actor: Actor | None = None) -> dict:
    run_id = run_id or new_run_id()
    blob = ledger.put_blob(source_bytes)
    source = Source(blob=blob, **source_meta)
    text = source_bytes.decode("utf-8")
    ledger.append(run_id, "source", SYSTEM, {"source": source.model_dump(mode="json"), "as_of": str(as_of)},
                  [f"blob:{blob}"])

    claim_actor = claim_actor or SYSTEM
    if claims is None:
        claims, metas = extract_claims(text, source.company, source.kind, model=model)
        claim_actor = agent_actor("claim-extractor", model)
        ledger.append(run_id, "extraction", claim_actor, {"n_claims": len(claims), "meta": metas},
                      [f"blob:{blob}"])

    parsed = [Claim(source_blob=blob, **(raw | {"company": source.company})) for raw in claims]
    for claim in parsed:
        ledger.append(run_id, "claim", claim_actor, {"claim_id": claim.id, "claim": claim.model_dump(mode="json")},
                      [f"blob:{blob}"])

    def assess(claim: Claim) -> tuple[dict, dict | None]:
        dec = decide_claim(claim.model_dump(mode="json"), config, model=model)
        ver = verify_claim(claim, source, text, as_of, model=model) if dec["action"] == "verify" else None
        return dec, ver

    # Agent calls run in parallel; ledger writes happen afterwards in claim order, so a run's
    # trail is deterministic regardless of which call finished first.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(assess, parsed))

    summary: dict[str, Any] = {"run_id": run_id, "claims": []}
    for claim, (dec, ver) in zip(parsed, results):
        cid = claim.id
        ledger.append(run_id, "decision", agent_actor("decision-model", model),
                      {"claim_id": cid, "action": dec["action"], "nature": dec["nature"],
                       "materiality": dec["materiality"], "rationale": dec["rationale"],
                       "lever_facts": dec["facts"], "lever_rules": rule_actor(levers).version,
                       "meta": dec["agent_meta"]})
        row = {"claim_id": cid, "statement": claim.statement, "action": dec["action"],
               "nature": dec["nature"], "lever": dec["facts"]["lever"], "verdict": None}
        if ver is not None:
            ledger.append(run_id, "rule_checks", rule_actor(evidence_check), ver["rules"])
            actor = rule_actor(evidence_check) if ver["agent"] is None else agent_actor("verifier", model)
            reasons = (ver["agent"] or {}).get("reasons") or [
                {"code": c["id"], "detail": c["message"]} for c in ver["rules"]["checks"]
                if c["severity"] == "reject" and not c["ok"]]
            ledger.append(run_id, "verdict", actor, {"claim_id": cid, "verdict": ver["verdict"],
                                                     "reasons": reasons, "meta": ver["agent_meta"]})
            row["verdict"] = ver["verdict"]
        summary["claims"].append(row)
    return summary


def human_verdict(ledger: Ledger, claim_id: str, verdict: str, who: str, note: str,
                  channel: str = "cli") -> dict:
    """You resolving a needs_human claim. Recorded like any other verdict; latest wins."""
    if not any(e["stage"] == "claim" and e["payload"]["claim_id"] == claim_id for e in ledger.entries()):
        raise ValueError(f"no claim {claim_id}")
    return ledger.append(f"human_{datetime.now():%Y%m%d-%H%M%S}", "verdict",
                         Actor(kind="human", name=who, version="-"),
                         {"claim_id": claim_id, "verdict": verdict, "channel": channel,
                          "reasons": [{"code": "human", "detail": note}]})

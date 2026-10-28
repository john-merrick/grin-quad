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
import contextvars
import inspect
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from grinos import evidence_check, levers, trace
from grinos.decide import decide_claim
from grinos.extract import extract_claims
from grinos.ledger import Ledger
from grinos.llm import agent_actor
from grinos.schemas import Actor, Claim, Source, canonical, sha256
from grinos.verify import verify_claim

SYSTEM = Actor(kind="system", name="pipeline", version="1")


def rule_actor(module) -> Actor:
    return Actor(kind="rule", name=module.__name__.split(".")[-1],
                 version=sha256(inspect.getsource(module).encode())[:16])


def new_run_id() -> str:
    return f"run_{datetime.now():%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:6]}"


def ingest(ledger: Ledger, source_bytes: bytes, source_meta: dict, claims: list[dict] | None, as_of: date,
           config: dict | None = None, model: str | None = None, run_id: str | None = None,
           workers: int = 6, claim_actor: Actor | None = None) -> dict:
    run_id = run_id or new_run_id()
    with trace.span("ingest", session_id=run_id, input={k: str(v) for k, v in source_meta.items()},
                    metadata={"run_id": run_id, "as_of": str(as_of), "claims_given": claims is not None}) as s:
        out = _ingest(ledger, source_bytes, source_meta, claims, as_of, config, model, run_id, workers, claim_actor)
        verdicts = [c["verdict"] for c in out["claims"]]
        s.end(output={"claims": len(verdicts), "verified": verdicts.count("verified"),
                      "rejected": verdicts.count("rejected"), "needs_human": verdicts.count("needs_human")})
        return out


def _ingest(ledger: Ledger, source_bytes: bytes, source_meta: dict, claims: list[dict] | None, as_of: date,
            config: dict | None, model: str | None, run_id: str, workers: int, claim_actor: Actor | None) -> dict:
    # Every write is keyed on (run_id, item), so re-running a run (a retried Temporal activity, a crash)
    # appends nothing twice, and finished agent work is reused rather than paid for again. The stages
    # below are also what the Temporal ingest workflow runs, one activity each.
    record_source(ledger, source_bytes, source_meta, as_of, run_id)
    record_claims(ledger, run_id, claims, model, claim_actor)
    todo = unassessed(ledger, run_id)

    def assess(claim: dict) -> tuple[str, dict, dict | None]:
        with trace.span("assess claim", input=claim["claim"]["statement"],
                        metadata={"claim_id": claim["claim_id"], "metric": claim["claim"]["metric"]}) as s:
            dec = assess_decide(claim, config, model)
            ver = assess_verify(ledger, run_id, claim, model) if dec["action"] == "verify" else None
            s.end(output={"action": dec["action"], "verdict": ver and ver["verdict"]})
        return claim["claim_id"], dec, ver

    # Agent calls run in parallel; ledger writes happen afterwards in claim order, so a run's
    # trail is deterministic regardless of which call finished first.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        ctx = contextvars.copy_context()                   # each worker joins this run's trace
        results = list(pool.map(lambda c: ctx.copy().run(assess, c), todo))
    record_assessments(ledger, run_id, {cid: {"decision": dec, "verification": ver} for cid, dec, ver in results}, model)
    return summarise(ledger, run_id)


def _source_of(ledger: Ledger, run_id: str) -> dict:
    e = ledger.find_idem(f"{run_id}:source")
    if e is None:
        raise ValueError(f"no source recorded for {run_id}")
    return e["payload"]


def record_source(ledger: Ledger, source_bytes: bytes, source_meta: dict, as_of: date, run_id: str) -> dict:
    blob = ledger.put_blob(source_bytes)
    source = Source(blob=blob, **source_meta)
    e, _ = ledger.append_once(run_id, "source", SYSTEM, {"source": source.model_dump(mode="json"), "as_of": str(as_of)},
                              idem=f"{run_id}:source", refs=[f"blob:{blob}"])
    return e["payload"]


def record_claims(ledger: Ledger, run_id: str, claims: list[dict] | None, model: str | None = None,
                  claim_actor: Actor | None = None) -> list[str]:
    """Extract (or take the given) claims and record each once. The extractor runs at most once per run."""
    src = _source_of(ledger, run_id)
    source = Source(**src["source"])
    text = ledger.get_blob(source.blob).decode("utf-8")
    claim_actor = claim_actor or SYSTEM
    if claims is None:
        claim_actor = agent_actor("claim-extractor", model)
        done = ledger.find_idem(f"{run_id}:extraction")
        if done:
            claims = json.loads(ledger.get_blob(done["payload"]["claims_blob"]))
        else:
            claims, metas = extract_claims(text, source.company, source.kind, model=model)
            ledger.append_once(run_id, "extraction", claim_actor,
                               {"n_claims": len(claims), "meta": metas,
                                "claims_blob": ledger.put_blob(canonical(claims).encode())},
                               idem=f"{run_id}:extraction", refs=[f"blob:{source.blob}"])
    ids = []
    for raw in claims:
        claim = Claim(source_blob=source.blob, **(raw | {"company": source.company}))
        ledger.append_once(run_id, "claim", claim_actor, {"claim_id": claim.id, "claim": claim.model_dump(mode="json")},
                           idem=f"{run_id}:claim:{claim.id}", refs=[f"blob:{source.blob}"])
        ids.append(claim.id)
    return ids


def _run_entries(ledger: Ledger, run_id: str) -> dict[str, dict]:
    return {e["payload"]["idem"]: e["payload"] for e in ledger.entries()
            if e["run_id"] == run_id and "idem" in e["payload"]}


def _claims_in_order(ledger: Ledger, run_id: str) -> list[dict]:
    seen, out = set(), []
    for e in ledger.entries():
        if e["run_id"] == run_id and e["stage"] == "claim" and e["payload"]["claim_id"] not in seen:
            seen.add(e["payload"]["claim_id"])
            out.append(e["payload"])
    return out


def unassessed(ledger: Ledger, run_id: str) -> list[dict]:
    """The run's claims ({claim_id, claim}) without a recorded decision: the work still to do."""
    prior = _run_entries(ledger, run_id)
    return [c for c in _claims_in_order(ledger, run_id) if f"{run_id}:decision:{c['claim_id']}" not in prior]


def assess_decide(claim: dict, config: dict | None, model: str | None = None) -> dict:
    """The decision model on one claim. Writes nothing (its result is recorded by record_assessments)."""
    return decide_claim(claim["claim"], config, model=model)


def assess_verify(ledger: Ledger, run_id: str, claim: dict, model: str | None = None) -> dict:
    """Rule checks, then the verifier agent, on one claim against its source. Writes nothing."""
    src = _source_of(ledger, run_id)
    source = Source(**src["source"])
    text = ledger.get_blob(source.blob).decode("utf-8")
    return verify_claim(Claim(**claim["claim"]), source, text, date.fromisoformat(src["as_of"]), model=model)


def record_assessments(ledger: Ledger, run_id: str, results: dict[str, dict], model: str | None = None) -> None:
    """Write decisions, rule checks and verdicts in claim order, each once."""
    for c in _claims_in_order(ledger, run_id):
        cid = c["claim_id"]
        if cid not in results:
            continue
        dec, ver = results[cid]["decision"], results[cid]["verification"]
        ledger.append_once(run_id, "decision", agent_actor("decision-model", model),
                           {"claim_id": cid, "action": dec["action"], "nature": dec["nature"],
                            "materiality": dec["materiality"], "rationale": dec["rationale"],
                            "lever_facts": dec["facts"], "lever_rules": rule_actor(levers).version,
                            "meta": dec["agent_meta"]}, idem=f"{run_id}:decision:{cid}")
        if ver is not None:
            ledger.append_once(run_id, "rule_checks", rule_actor(evidence_check), ver["rules"],
                               idem=f"{run_id}:rules:{cid}")
            actor = rule_actor(evidence_check) if ver["agent"] is None else agent_actor("verifier", model)
            reasons = (ver["agent"] or {}).get("reasons") or [
                {"code": k["id"], "detail": k["message"]} for k in ver["rules"]["checks"]
                if k["severity"] == "reject" and not k["ok"]]
            ledger.append_once(run_id, "verdict", actor, {"claim_id": cid, "verdict": ver["verdict"],
                                                          "reasons": reasons, "meta": ver["agent_meta"]},
                               idem=f"{run_id}:verdict:{cid}")


def summarise(ledger: Ledger, run_id: str) -> dict:
    prior = _run_entries(ledger, run_id)
    rows = []
    for c in _claims_in_order(ledger, run_id):
        cid = c["claim_id"]
        d = prior.get(f"{run_id}:decision:{cid}")
        if d is None:
            continue
        rows.append({"claim_id": cid, "statement": c["claim"]["statement"], "action": d["action"],
                     "nature": d["nature"], "lever": d["lever_facts"]["lever"],
                     "verdict": (prior.get(f"{run_id}:verdict:{cid}") or {}).get("verdict")})
    return {"run_id": run_id, "claims": rows}


def human_verdict(ledger: Ledger, claim_id: str, verdict: str, who: str, note: str,
                  channel: str = "cli") -> dict:
    """You resolving a needs_human claim. Recorded like any other verdict; latest wins."""
    if not any(e["stage"] == "claim" and e["payload"]["claim_id"] == claim_id for e in ledger.entries()):
        raise ValueError(f"no claim {claim_id}")
    return ledger.append(f"human_{datetime.now():%Y%m%d-%H%M%S}", "verdict",
                         Actor(kind="human", name=who, version="-"),
                         {"claim_id": claim_id, "verdict": verdict, "channel": channel,
                          "reasons": [{"code": "human", "detail": note}]})

import json

import pytest

from grinos.ledger import Ledger, LedgerCorrupt
from grinos.schemas import Actor

RULE = Actor(kind="rule", name="test", version="v0")


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path, clock=lambda: "2026-01-01T00:00:00+00:00")


def _rewrite(ledger, mutate):
    lines = ledger.path.read_text().splitlines()
    ledger.path.write_text("\n".join(mutate(lines)) + "\n")


def _claim_flow(ledger, cid="c_1", company="ACME", action="verify", verdict="verified"):
    blob = ledger.put_blob(f"source for {cid}".encode())
    ledger.append("r1", "claim", RULE, {"claim_id": cid, "claim": {"company": company}}, [f"blob:{blob}"])
    ledger.append("r1", "decision", RULE, {"claim_id": cid, "action": action})
    ledger.append("r1", "verdict", RULE, {"claim_id": cid, "verdict": verdict})


def test_chain_links_and_audits_clean(ledger):
    a = ledger.append("r1", "x", RULE, {"n": 1})
    b = ledger.append("r1", "x", RULE, {"n": 2})
    assert b["prev"] == a["hash"] and b["seq"] == 1
    assert ledger.audit() == []


def test_blob_is_content_addressed_and_write_once(ledger):
    d = ledger.put_blob(b"10-K text")
    assert ledger.put_blob(b"10-K text") == d
    assert ledger.get_blob(d) == b"10-K text"


def test_edited_entry_is_detected(ledger):
    ledger.append("r1", "x", RULE, {"n": 1})
    ledger.append("r1", "x", RULE, {"n": 2})

    def edit(lines):
        e = json.loads(lines[0])
        e["payload"]["n"] = 99
        return [json.dumps(e)] + lines[1:]
    _rewrite(ledger, edit)
    assert any("edited" in p for p in ledger.audit())


def test_deleted_entry_is_detected(ledger):
    for n in range(3):
        ledger.append("r1", "x", RULE, {"n": n})
    _rewrite(ledger, lambda lines: [lines[0], lines[2]])
    assert ledger.audit()


def test_reordered_entries_are_detected(ledger):
    for n in range(3):
        ledger.append("r1", "x", RULE, {"n": n})
    _rewrite(ledger, lambda lines: [lines[1], lines[0], lines[2]])
    assert ledger.audit()


def test_swapped_source_blob_is_detected(ledger):
    _claim_flow(ledger)
    blob = next(ledger.blobs.iterdir())
    blob.chmod(0o644)
    blob.write_bytes(b"doctored source")
    assert any("does not match" in p for p in ledger.audit())


def test_gate_serves_only_routed_and_verified_claims(ledger):
    _claim_flow(ledger, "c_ok")
    _claim_flow(ledger, "c_rejected", verdict="rejected")
    _claim_flow(ledger, "c_human", verdict="needs_human")
    _claim_flow(ledger, "c_discarded", action="discard", verdict="verified")
    _claim_flow(ledger, "c_other_co", company="BETA")
    assert [c["claim_id"] for c in ledger.verified_claims("ACME")] == ["c_ok"]


def test_latest_verdict_wins(ledger):
    _claim_flow(ledger, "c_1", verdict="needs_human")
    ledger.append("r2", "verdict", Actor(kind="human", name="isaac", version="-"),
                  {"claim_id": "c_1", "verdict": "verified"})
    assert [c["claim_id"] for c in ledger.verified_claims()] == ["c_1"]


def test_gate_refuses_broken_ledger(ledger):
    _claim_flow(ledger)
    _rewrite(ledger, lambda lines: lines[1:])
    with pytest.raises(LedgerCorrupt):
        ledger.verified_claims()


def test_rewritten_chain_without_key_is_detected(ledger):
    """The reviewer's probe: edit an entry and recompute every hash. The MAC catches it."""
    from grinos.ledger import entry_hash
    for n in range(3):
        ledger.append("r1", "x", RULE, {"n": n})
    entries = [json.loads(ln) for ln in ledger.path.read_text().splitlines()]
    entries[0]["payload"]["n"] = 99
    prev = "0" * 64
    for e in entries:
        e["prev"] = prev
        e["hash"] = entry_hash(e)
        prev = e["hash"]
    ledger.path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    assert any("MAC invalid" in p for p in ledger.audit())


def test_truncated_tail_is_detected(ledger):
    for n in range(3):
        ledger.append("r1", "x", RULE, {"n": n})
    _rewrite(ledger, lambda lines: lines[:2])
    assert any("head anchor" in p for p in ledger.audit())


def test_different_key_cannot_validate(tmp_path):
    a = Ledger(tmp_path, key=b"a" * 32)
    a.append("r1", "x", RULE, {"n": 1})
    assert Ledger(tmp_path, key=b"b" * 32).audit()


def test_human_verdict_outranks_later_agent_verdict(ledger):
    _claim_flow(ledger, "c_1", verdict="needs_human")
    ledger.append("h", "verdict", Actor(kind="human", name="isaac", version="-"),
                  {"claim_id": "c_1", "verdict": "rejected"})
    ledger.append("r2", "verdict", RULE, {"claim_id": "c_1", "verdict": "verified"})  # re-ingest
    assert ledger.verified_claims() == []


def test_append_if_guard_aborts_without_writing(ledger):
    ledger.append("r1", "x", RULE, {"n": 1})
    with pytest.raises(ValueError, match="nope"):
        ledger.append_if("r1", "x", RULE, {"n": 2}, guard=lambda entries: "nope")
    assert sum(1 for _ in ledger.entries()) == 1 and ledger.audit() == []


def test_append_once_writes_one_entry_per_key(ledger):
    e1, created1 = ledger.append_once("r", "claim", RULE, {"x": 1}, idem="r:claim:c_1")
    e2, created2 = ledger.append_once("r", "claim", RULE, {"x": 2}, idem="r:claim:c_1")
    assert created1 and not created2 and e2["seq"] == e1["seq"] and e2["payload"]["x"] == 1
    assert ledger.find_idem("r:claim:c_1")["seq"] == e1["seq"] and ledger.find_idem("nope") is None
    assert len(list(ledger.entries())) == 1 and ledger.audit() == []


def test_append_once_is_atomic_across_threads(ledger):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: ledger.append_once("r", "s", RULE, {"i": i}, idem="same"), range(16)))
    assert sum(created for _, created in results) == 1 and len(list(ledger.entries())) == 1

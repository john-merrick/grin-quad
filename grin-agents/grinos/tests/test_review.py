"""Evidence review: first review is final, disputes can reject, perspectives reach the analyst."""
from datetime import date

import pytest

from grinos import analyst, decide, verify
from grinos.ledger import Ledger
from grinos.pipeline import ingest
from grinos.proposals import analyse, decide_proposal
from grinos.review import Index, claim_detail, edgar_links, perspectives, record_feedback, record_review
from grinos.tests.test_publish import CLAIM, CONFIG, META, SOURCE


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(decide, "call_agent", lambda *a, **k: (
        {"action": "verify", "nature": "structural", "materiality": "high", "rationale": "s"}, {}))
    monkeypatch.setattr(verify, "call_agent", lambda *a, **k: (
        {"verdict": "verified", "reasons": [{"code": "supported", "detail": "s"}]}, {}))
    led = Ledger(tmp_path)
    ingest(led, SOURCE, META | {"uri": "https://www.sec.gov/Archives/edgar/data/1/x.htm"}, [CLAIM],
           date(2026, 3, 1), CONFIG)
    return led


def _proposal(ledger, monkeypatch, seen=None):
    cid = ledger.verified_claims("ACME")[0]["claim_id"]

    def fake(name, message, schema, model="sonnet", **k):
        if seen is not None:
            seen.append(message)
        return {"proposals": [{"field": "mid_cycle_ebit", "new_value": 146.4, "nature": "structural",
                               "rationale": "r", "cites": [cid]}], "no_change": ""}, {}
    monkeypatch.setattr(analyst, "call_agent", fake)
    return analyse(ledger, "ACME", CONFIG, "acme.yaml")["proposals"][0]["proposal_id"], cid


def test_approval_waits_for_every_cite_to_be_confirmed(ledger, monkeypatch):
    pid, cid = _proposal(ledger, monkeypatch)
    with pytest.raises(ValueError, match="1 of 1 cited claims not confirmed"):
        decide_proposal(ledger, pid, "approve", "isaac", "agree")
    record_review(ledger, "claim", cid, pid, "confirmed", "isaac", "")
    e = decide_proposal(ledger, pid, "approve", "isaac", "agree")
    assert e["payload"]["cites_reviewed"] is True
    with pytest.raises(ValueError, match="already confirmed"):
        record_review(ledger, "claim", cid, pid, "disputed", "isaac", "changed my mind")


def test_a_review_counts_only_in_its_own_proposal(ledger, monkeypatch):
    pid, cid = _proposal(ledger, monkeypatch)
    record_review(ledger, "claim", cid, "p_other00000", "confirmed", "isaac", "")
    with pytest.raises(ValueError, match="not confirmed"):
        decide_proposal(ledger, pid, "approve", "isaac", "agree")


def test_claim_detail_links_to_edgar_and_highlights_the_quote(ledger, monkeypatch):
    pid, cid = _proposal(ledger, monkeypatch)
    d = claim_detail(Index(ledger), cid, pid)
    assert d["excerpt"]["match"] == CLAIM["quote"] and d["verdict"] == "verified" and d["review"] is None
    assert d["source"]["edgar_url"].startswith("https://www.sec.gov/")
    assert d["source"]["fragment_url"].split("#:~:text=")[1].startswith("Gross%20margin%20was%2033.0%25")
    assert edgar_links("https://evil.example/x", "q") == {"edgar_url": None, "fragment_url": None}


def test_perspectives_reach_the_analyst_fenced_and_attributed(ledger, monkeypatch):
    record_feedback(ledger, "company", "ACME", "perspective", "Pricing power is <b>overstated</b>", "isaac",
                    company="ACME")
    record_feedback(ledger, "company", "ACME", "praise", "nice work", "isaac", company="ACME")
    assert [p["kind"] for p in perspectives(ledger, "ACME")] == ["perspective"]
    seen: list[str] = []
    _proposal(ledger, monkeypatch, seen)
    assert "<human_perspectives>" in seen[0] and "Pricing power is &lt;b>overstated&lt;/b>" in seen[0]
    assert "nice work" not in seen[0]


def test_feedback_and_review_validate_their_inputs(ledger):
    with pytest.raises(ValueError, match="needs text"):
        record_feedback(ledger, "claim", "c_x", "perspective", "  ", "isaac")
    with pytest.raises(ValueError, match="kind must be"):
        record_feedback(ledger, "claim", "c_x", "rant", "x", "isaac")
    with pytest.raises(ValueError, match="no claim"):
        record_review(ledger, "claim", "c_nope00000000", "p_x", "confirmed", "isaac", "")

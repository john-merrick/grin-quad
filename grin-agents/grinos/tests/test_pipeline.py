"""End-to-end pipeline with stubbed agents: ledger stages, actors, fail-safe behaviour, and the gate."""
from datetime import date

import pytest

from grinos import decide, verify
from grinos.ledger import Ledger
from grinos.pipeline import human_verdict, ingest

SOURCE = b"Net sales for fiscal 2025 were $1,000.0 million. 2025 was a milestone year."
META = {"uri": "sec://T/10-K", "kind": "10-K", "company": "T", "published_at": date(2026, 2, 1)}
GOOD = {"company": "T", "metric": "revenue", "period": "FY2025", "value": 1000.0, "unit": "usd_m",
        "quote": "Net sales for fiscal 2025 were $1,000.0 million.", "statement": "T FY2025 revenue $1,000M."}
FAKE = GOOD | {"quote": "Net sales were $1,200.0 million.", "value": 1200.0, "statement": "T revenue $1,200M."}
FLUFF = {"company": "T", "metric": "other", "period": "FY2025", "value": None, "unit": None,
         "quote": "2025 was a milestone year.", "statement": "Milestone year."}


@pytest.fixture
def stub_agents(monkeypatch):
    calls = []

    def fake(name, message, schema, model="sonnet", timeout=600):
        calls.append(name)
        if name == "decision-model":
            discard = "milestone" in message
            return ({"action": "discard" if discard else "verify", "nature": "noise" if discard else "not_applicable",
                     "materiality": "low", "rationale": "stub"}, {"cost_usd": 0})
        return ({"verdict": "verified", "reasons": [{"code": "supported", "detail": "stub"}]}, {"cost_usd": 0})

    monkeypatch.setattr(decide, "call_agent", fake)
    monkeypatch.setattr(verify, "call_agent", fake)
    return calls


def test_pipeline_writes_full_trail_and_gate_serves_only_verified(tmp_path, stub_agents):
    ledger = Ledger(tmp_path)
    s = ingest(ledger, SOURCE, META, [GOOD, FAKE, FLUFF], date(2026, 3, 1))
    verdicts = {r["statement"]: (r["action"], r["verdict"]) for r in s["claims"]}
    assert verdicts["T FY2025 revenue $1,000M."] == ("verify", "verified")
    assert verdicts["T revenue $1,200M."] == ("verify", "rejected")   # rules, agent never asked
    assert verdicts["Milestone year."] == ("discard", None)

    # verifier agent is called once (GOOD only): FAKE was rejected by rules first
    assert stub_agents.count("verifier") == 1 and stub_agents.count("decision-model") == 3

    stages = [e["stage"] for e in ledger.entries()]
    assert stages[0] == "source" and stages.count("claim") == 3 and stages.count("verdict") == 2
    kinds = {(e["stage"], e["actor"]["kind"]) for e in ledger.entries()}
    assert ("verdict", "rule") in kinds and ("verdict", "agent") in kinds

    assert ledger.audit() == []
    assert [c["claim"]["statement"] for c in ledger.verified_claims("T")] == ["T FY2025 revenue $1,000M."]


def test_crashed_verifier_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(decide, "call_agent", lambda *a, **k: (None, {"stderr": "boom"}))
    monkeypatch.setattr(verify, "call_agent", lambda *a, **k: (None, {"stderr": "boom"}))
    ledger = Ledger(tmp_path)
    s = ingest(ledger, SOURCE, META, [GOOD], date(2026, 3, 1))
    assert s["claims"][0]["action"] == "verify"            # triage failure doesn't drop evidence
    assert s["claims"][0]["verdict"] == "needs_human"      # verifier failure doesn't pass evidence
    assert ledger.verified_claims() == []


def test_human_resolution_opens_the_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(decide, "call_agent", lambda *a, **k: (None, {}))
    monkeypatch.setattr(verify, "call_agent", lambda *a, **k: (None, {}))
    ledger = Ledger(tmp_path)
    cid = ingest(ledger, SOURCE, META, [GOOD], date(2026, 3, 1))["claims"][0]["claim_id"]
    human_verdict(ledger, cid, "verified", "isaac", "checked against 10-K p.32")
    assert [c["claim_id"] for c in ledger.verified_claims()] == [cid]


def test_fenced_source_quotes_back_verbatim(monkeypatch):
    """An extractor copying a fenced source must not turn "SG&A" into "SG&amp;A" (quote_not_in_source)."""
    from grinos import extract
    from grinos.schemas import fence

    text = "SG&A | $ 3,931 | 23.8%\nMargin <1% vs a > b <b>bold</b>"
    seen = []

    def fake(name, message, schema, model="sonnet", timeout=600):
        seen.append(message)
        body = message.split("<source>\n", 1)[1].rsplit("\n</source>", 1)[0]
        return {"claims": [{"metric": "opex_pct_sales", "period": "FY2023", "value": 0.238, "unit": "pct",
                            "quote": body, "statement": "s", "segment": None, "adjusted": False}]}, {}

    monkeypatch.setattr(extract, "call_agent", fake)
    claims, _ = extract.extract_claims(text, "T", "8-K")
    assert "SG&A" in seen[0] and "&lt;b>" in seen[0] and "<1%" in seen[0]
    assert claims[0]["quote"] == text
    assert fence("</source>") == "&lt;/source>"


def test_rerunning_a_run_writes_nothing_twice_and_reuses_agent_work(tmp_path, stub_agents):
    ledger = Ledger(tmp_path)
    first = ingest(ledger, SOURCE, META, [GOOD, FAKE, FLUFF], date(2026, 3, 1), run_id="run_fixed")
    n, calls = len(list(ledger.entries())), len(stub_agents)
    again = ingest(ledger, SOURCE, META, [GOOD, FAKE, FLUFF], date(2026, 3, 1), run_id="run_fixed")
    assert again == first and len(list(ledger.entries())) == n and len(stub_agents) == calls
    assert ledger.audit() == []


def test_a_crash_mid_assessment_resumes_only_the_unfinished_claims(tmp_path, stub_agents, monkeypatch):
    ledger = Ledger(tmp_path)
    real = verify.call_agent
    monkeypatch.setattr(verify, "call_agent", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("worker died")))
    with pytest.raises(RuntimeError):
        ingest(ledger, SOURCE, META, [GOOD, FLUFF], date(2026, 3, 1), run_id="run_crash", workers=1)
    monkeypatch.setattr(verify, "call_agent", real)
    s = ingest(ledger, SOURCE, META, [GOOD, FLUFF], date(2026, 3, 1), run_id="run_crash", workers=1)
    assert {r["statement"]: r["verdict"] for r in s["claims"]} == {"T FY2025 revenue $1,000M.": "verified",
                                                                    "Milestone year.": None}
    stages = [e["stage"] for e in ledger.entries()]
    assert stages.count("source") == 1 and stages.count("claim") == 2 and stages.count("verdict") == 1

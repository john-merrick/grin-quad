"""Adding eval cases (per-agent shapes, holdout), case history over runs, and the heartbeat comparison."""
import json
import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).parents[2] / "evals"
sys.path.insert(0, str(EVALS))
import added  # noqa: E402

from grinos import evalwatch  # noqa: E402

CLAIM = {"metric": "gross_margin", "period": "FY2025", "value": 0.3, "unit": "pct",
         "quote": "Gross margin was 30.0% in fiscal 2025", "statement": "GM 30%"}


@pytest.fixture
def evals_dir(tmp_path, monkeypatch):
    for d in set(added.EVAL_DIRS.values()):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(added, "HERE", tmp_path)
    return tmp_path


def test_each_agent_validates_its_own_case_shape(evals_dir):
    with pytest.raises(ValueError, match="needs the reason code"):
        added.add("verifier", {"id": "x", "kind": "judgment", "claim": CLAIM, "source_text": "t"}, "isaac", "test")
    with pytest.raises(ValueError, match="metric must be"):
        added.add("decision-model", {"id": "x", "claim": CLAIM | {"metric": "vibes"}, "action": ["verify"],
                                     "nature": ["structural"]}, "isaac", "test")
    with pytest.raises(ValueError, match="unknown expected action"):
        added.add("orchestrator", {"id": "x", "text": "hi", "expect": [{"action": "approve"}]}, "isaac", "test")
    with pytest.raises(ValueError, match="isn't supported"):
        added.add("memo-writer", {"id": "x"}, "isaac", "test")

    row = added.add("verifier", {"id": "seg", "kind": "judgment", "reason": "segment_not_total", "claim": CLAIM,
                                 "source_text": "Segment: Gross margin was 30.0% in fiscal 2025"}, "isaac", "trace:abc")
    assert row["id"] == "added_seg" and row["added_by"] == "isaac"
    (case,) = added.cases("verifier")
    assert case["expected"] == "rejected" and case["source"] is None and case["source_text"].startswith("Segment")
    with pytest.raises(ValueError, match="already exists"):
        added.add("verifier", {"id": "seg", "kind": "valid", "claim": CLAIM, "source_text": "t"}, "isaac", "test")

    added.add("decision-model", {"id": "d1", "claim": CLAIM, "action": ["verify"], "nature": ["structural"]}, "i", "t")
    assert added.cases("decision-model")[0]["action"] == {"verify"} and added.cases("decision-model")[0]["relevant"]
    added.add("mid-cycle-analyst", {"id": "a1", "claims": [{"claim_id": "c_1", "claim": CLAIM}], "expect": {"gross_margin": [0.29, 0.31]},
                                    "forbid": ["duration_score"]}, "i", "t")
    assert added.cases("mid-cycle-analyst")[0]["expect"] == {"gross_margin": (0.29, 0.31)}
    added.add("orchestrator", {"id": "o1", "text": "cover POOL", "expect": [{"action": "start_coverage", "ticker": "POOL"}]}, "i", "t")
    assert added.cases("orchestrator")[0]["must"] == set() and "POOL" in added.cases("orchestrator")[0]["work"]
    added.add("claim-extractor", {"id": "e1", "text": "Net sales were $10.0 million in 2026.",
                                  "expected": [{"metrics": ["revenue"], "year": 2026, "value": 10}]}, "i", "t")
    assert added.cases("claim-extractor")[0]["expected"][0] == {"metrics": {"revenue"}, "year": "2026", "value": 10.0}


def test_a_fifth_of_added_cases_are_held_out_deterministically(evals_dir):
    ids = [f"added_case{i}" for i in range(200)]
    held = [i for i in ids if added.holdout(i)]
    assert 25 <= len(held) <= 55 and held == [i for i in ids if added.holdout(i)]


def test_runners_load_added_cases(evals_dir):
    # the real runners read evals/<dir>/cases_added.jsonl through added.cases(); a broken case would raise here
    added.add("verifier", {"id": "v", "kind": "valid", "claim": CLAIM, "source_text": CLAIM["quote"]}, "i", "t")
    assert [c["id"] for c in added.cases("verifier")] == ["added_v"]


def _hist(tmp_path, rows):
    (tmp_path / "history.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_the_heartbeat_flags_regressions_new_failures_and_separates_flaky(tmp_path, monkeypatch):
    monkeypatch.setattr(evalwatch, "EVALS", tmp_path)
    monkeypatch.setattr(evalwatch, "_gates_for", lambda agent: {"false_accept_rate": "<=", "valid_accept_rate": ">="})
    base = {"agent": "verifier", "passed": True, "gates": {"false_accept_rate": True}, "model": "sonnet"}
    _hist(tmp_path, [
        base | {"date": "d1", "metrics": {"false_accept_rate": 0.0, "valid_accept_rate": 1.0}, "per_case": {"a": 1, "b": 0.5, "c": 1}},
        base | {"date": "d2", "metrics": {"false_accept_rate": 0.0, "valid_accept_rate": 1.0}, "per_case": {"a": 1, "b": 1, "c": 1}},
        base | {"date": "d3", "passed": False, "gates": {"false_accept_rate": False},
                "metrics": {"false_accept_rate": 0.1, "valid_accept_rate": 0.97, "n": 9},
                "per_case": {"a": 0.5, "b": 0.5, "c": 1}}])
    c = evalwatch.compare("verifier")
    assert c["status"] == "regressed" and c["regressed_gates"] == ["false_accept_rate"]
    assert c["new_failures"] == ["a"] and c["flaky"] == ["b"]                 # b failed before: flaky, not new
    assert list(c["worse_metrics"]) == ["false_accept_rate"]                   # 0.03 drop is noise; n isn't a gate
    assert "gates now failing" in evalwatch.summary(c)
    _hist(tmp_path, [base | {"date": "d1", "metrics": {}, "per_case": {"a": 1}}, base | {"date": "d2", "metrics": {}, "per_case": {"a": 1}}])
    assert evalwatch.compare("verifier")["status"] == "steady"

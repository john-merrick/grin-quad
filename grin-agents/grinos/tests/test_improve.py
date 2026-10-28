"""The improvement loop's deterministic parts: edits, barriers, no-regression selection, human promotion."""
import pytest

from grinos import improve
from grinos.ledger import Ledger
from grinos.schemas import Actor

BODY = "You verify claims.\n\n- Never verify a guess.\n- Use the quote.\n\nReturn only JSON."


def test_edits_apply_exactly_once_in_order():
    new, problems = improve.apply_edits(BODY, [{"find": "- Use the quote.", "replace": "- Use the quote.\n- Check signs."}])
    assert new.endswith("- Check signs.\n\nReturn only JSON.") and not problems
    assert improve.apply_edits(BODY, [{"find": "absent", "replace": "x"}])[0] is None
    assert improve.apply_edits("a a", [{"find": "a", "replace": "b"}])[1] == ["edit 1: `find` occurs 2 times (needs exactly once)"]


def test_barriers_rules_leaks_and_size():
    leaks = improve.leak_tokens([{"id": "case_7", "quotes": ["Operating loss was $14.2 million"], "result": {}}])
    assert improve.check_candidate(BODY, BODY.replace("- Never verify a guess.\n", ""), leaks)[0].startswith("drops")
    assert "copies eval material" in improve.check_candidate(BODY, BODY + "\nLike case_7.", leaks)[0]
    assert "grows the prompt" in improve.check_candidate(BODY, BODY + "x" * len(BODY), set())[0]
    assert improve.check_candidate(BODY, BODY, set()) == ["no change"]
    assert improve.check_candidate(BODY, BODY + "\n- Check signs.", leaks) == []


def _r(passed, per_case, diff=3, label="c"):
    return {"passed": passed, "per_case": per_case, "metrics": {}, "diff_size": diff, "cand_version": label}


def test_select_requires_no_case_to_get_worse():
    base = _r(True, {"a": 1.0, "b": 0.5, "c": 0.0})
    better_but_regresses = _r(True, {"a": 0.5, "b": 1.0, "c": 1.0}, label="x")
    strictly_better = _r(True, {"a": 1.0, "b": 1.0, "c": 0.0}, diff=9, label="y")
    smaller_and_better = _r(True, {"a": 1.0, "b": 1.0, "c": 0.5}, diff=2, label="z")
    fails_a_gate = _r(False, {"a": 1.0, "b": 1.0, "c": 1.0}, diff=1, label="w")
    assert improve.select(base, [better_but_regresses])  is None
    assert improve.select(base, [strictly_better, smaller_and_better, fails_a_gate])["cand_version"] == "z"
    assert improve.select(base, [_r(True, dict(base["per_case"]))]) is None          # same is not better


def test_failures_and_corrections_feed_the_optimiser(tmp_path):
    rep = {"rows": [{"id": "x", "case_pass": False, "verdict": "verified", "drafts": ["huge"]},
                    {"id": "x", "case_pass": False}, {"id": "y", "case_pass": True}]}
    assert [f["id"] for f in improve.failures_from_report(rep)] == ["x"]
    assert "drafts" not in improve.failures_from_report(rep)[0]["result"]
    L = Ledger(tmp_path)
    from grinos.review import record_feedback
    record_feedback(L, "claim", "c_1", "correction", "that is a segment figure", "isaac", agent="verifier")
    record_feedback(L, "claim", "c_1", "praise", "nice", "isaac", agent="verifier")
    (c,) = improve.corrections(L, "verifier")
    assert c["result"]["isaac_says"] == "that is a segment figure"


@pytest.fixture
def agent_file(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "verifier.md").write_text("---\nname: verifier\nmodel: sonnet\n---\n\n" + BODY + "\n")
    monkeypatch.setattr(improve, "AGENTS_DIR", d)
    return d / "verifier.md"


def _selected(L, body, parent):
    blob = L.put_blob(body.encode())
    return L.append("improve:verifier:2026-W43", "prompt_selected", Actor(kind="system", name="t", version="1"),
                    {"promotion_id": "pr_abc", "agent": "verifier", "parent_version": parent,
                     "cand_version": improve.prompt_version(body), "prompt_blob": blob})


def test_an_approved_promotion_writes_the_prompt_keeping_its_front_matter(tmp_path, agent_file):
    L = Ledger(tmp_path / "led")
    new = BODY.replace("- Use the quote.", "- Use the quote.\n- Check signs.")
    _selected(L, new, improve.prompt_version(BODY))
    assert [p["promotion_id"] for p in improve.promotions(L)] == ["pr_abc"]
    with pytest.raises(ValueError, match="needs a note"):
        improve.decide_promotion(L, "pr_abc", "approve", "isaac", "")
    improve.decide_promotion(L, "pr_abc", "approve", "isaac", "signs were the gap")
    text = agent_file.read_text()
    assert text.startswith("---\nname: verifier") and "- Check signs." in text
    assert improve.split_prompt(text)[1] == new and improve.promotions(L) == []
    assert [e["stage"] for e in L.entries()][-1] == "prompt_promoted"
    with pytest.raises(ValueError, match="already decided"):
        improve.decide_promotion(L, "pr_abc", "reject", "isaac", "changed my mind")


def test_a_stale_candidate_is_refused(tmp_path, agent_file):
    L = Ledger(tmp_path / "led")
    _selected(L, BODY + "\n- new rule", "0000000000000000")
    with pytest.raises(ValueError, match="changed since"):
        improve.decide_promotion(L, "pr_abc", "approve", "isaac", "ok")
    assert "new rule" not in agent_file.read_text()
    assert not any(e["stage"] == "prompt_promotion_decision" for e in L.entries())   # nothing recorded

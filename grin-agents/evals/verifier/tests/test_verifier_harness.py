"""Eval-the-eval for the verifier: labels must be honest and the gate must be able to fail."""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).parents[1]
sys.path.insert(0, str(HERE))

_spec = importlib.util.spec_from_file_location("verifier_run", HERE / "run.py")
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)

from grinos.verify import verify_claim  # noqa: E402


def _rules_only():
    rows = []
    for case in run.CASES:
        claim, source, text = run.load(case)
        rows.append(run.grade(case, verify_claim(claim, source, text, run.AS_OF, use_agent=False)))
    return rows


def test_every_quote_except_fabricated_is_verbatim_in_its_source():
    from grinos.evidence_check import normalise
    for case in run.CASES:
        _, _, text = run.load(case)
        if case["id"] != "t_fabricated_quote":
            assert normalise(case["claim"]["quote"]) in normalise(text), case["id"]


def test_rules_only_baseline_profile():
    rows = {r["id"]: r for r in _rules_only()}
    for cid, r in rows.items():
        kind = r["kind"]
        if kind == "valid":
            assert r["verdict"] == "verified", f"{cid}: rules reject a valid claim"
        elif kind == "tool":
            assert r["by_rules"], f"{cid}: labelled tool but rules don't reject it"
        else:
            assert r["verdict"] == "verified", f"{cid}: labelled judgment but rules already reject it"


def test_rules_only_fails_gate_and_oracle_passes():
    assert not run.summarise(_rules_only())["passed"]
    oracle = [{"id": c["id"], "kind": c["kind"], "verdict": c["expected"], "ok": True, "false_accept": False,
               "by_rules": c["kind"] == "tool", "reason_ok": True if c["kind"] == "judgment" else None}
              for c in run.CASES]
    assert run.summarise(oracle)["passed"]

"""
Tests for the eval itself. An eval that can't fail, or can't pass, proves nothing:
  - a perfect oracle must pass every gate
  - a do-nothing system must fail
  - the tools-only baseline must catch every tool case and no judgment case
    (if it catches a judgment case, that case is mislabelled)
"""
import json
from pathlib import Path

import pytest
from build_cases import BASE, SYNTHETIC
from config_facts import config_facts
from grade import grade_case, norm_field, summarise
from run import run_tools_only

HERE = Path(__file__).parents[1]
MANIFEST = json.loads((HERE / "cases" / "manifest.json").read_text())


def _oracle(case: dict) -> dict:
    findings = [{"field": f, "category": "other", "severity": "high", "evidence": "", "suggested_fix": ""}
                for f in case["planted_fields"]]
    return {"ticker": "X", "verdict": "fail" if findings else "pass", "findings": findings}


def test_manifest_is_in_sync_with_builder():
    assert {c["id"] for c in MANIFEST} >= {s[0] for s in SYNTHETIC}


def test_each_mutation_changes_exactly_the_base():
    for cid, _, mutation, *_ in SYNTHETIC:
        if mutation:
            assert mutation(BASE) != BASE, cid


def test_oracle_passes_all_gates():
    s = summarise([grade_case(c, _oracle(c)) for c in MANIFEST])
    assert s["passed"], s


def test_silent_system_fails():
    s = summarise([grade_case(c, {"ticker": "", "verdict": "pass", "findings": []}) for c in MANIFEST])
    assert not s["passed"]
    assert s["recall_tool"] == 0 and s["recall_judgment"] == 0


def test_errored_output_counts_against_every_kind():
    s = summarise([grade_case(c, None) for c in MANIFEST])
    assert s["errors"] == len(MANIFEST)
    assert s["clean_false_alarm_rate"] == 1.0


def test_low_severity_findings_do_not_count():
    case = next(c for c in MANIFEST if c["kind"] == "clean")
    out = {"ticker": "", "verdict": "pass",
           "findings": [{"field": "dio", "category": "other", "severity": "low", "evidence": "", "suggested_fix": ""}]}
    assert grade_case(case, out)["false_alarm"] is False


@pytest.mark.parametrize("raw,want", [("revenue[3]", "revenue"), (" DIO ", "dio"),
                                      ("revenue.year_4", "revenue"), ("dio (days)", "dio")])
def test_norm_field(raw, want):
    assert norm_field(raw) == want


def test_tools_only_baseline_profile():
    by_kind: dict[str, list[dict]] = {}
    for c in MANIFEST:
        path = HERE / "cases" / c["file"]
        out, _ = run_tools_only(path, config_facts(path))
        by_kind.setdefault(c["kind"], []).append(grade_case(c, out))
    assert all(r["caught"] for r in by_kind["tool"]), "a tool case isn't caught by the tool"
    assert not any(r["caught"] for r in by_kind["judgment"]), "a judgment case is catchable by rules"
    assert not any(r["false_alarm"] for r in by_kind["clean"])

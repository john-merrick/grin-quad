"""Every eval reports a per-case pass flag, and per_case() turns repeats into pass rates."""
import importlib.util
from pathlib import Path

EVALS = Path(__file__).parents[1]


def _load(path):
    spec = importlib.util.spec_from_file_location(f"_m_{path.parent.name}_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


common = _load(EVALS / "common.py")


def test_per_case_averages_repeats_and_reads_either_layout():
    rep = {"rows": [{"id": "a", "case_pass": True}, {"id": "a", "case_pass": False}, {"id": "b", "case_pass": True}]}
    assert common.per_case(rep) == {"a": 0.5, "b": 1.0}
    assert common.per_case({"cases": [{"id": "x", "case_pass": False}]}) == {"x": 0.0}


def test_every_eval_grader_sets_case_pass():
    for run in sorted(EVALS.glob("*/run.py")):
        text = run.read_text() + ((run.parent / "grade.py").read_text() if (run.parent / "grade.py").exists() else "")
        assert '"case_pass"' in text, f"{run.parent.name} rows have no case_pass"


def test_publish_records_the_run_as_a_trace_with_gate_and_case_scores(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(EVALS.parent))
    from grinos import trace
    monkeypatch.setenv("GRINOS_HOME", str(tmp_path))
    report = {"summary": {"recall": 0.9, "forbidden": 0, "passed": True, "cost_usd": 0.1, "gates": {"recall": True}},
              "rows": [{"id": "a", "case_pass": True}, {"id": "b", "case_pass": False}]}
    tid = common.publish("verifier", report, "v123", "sonnet")
    recs = trace.read()
    span = next(r for r in recs if r["type"] == "span")
    scores = {r["name"]: r["value"] for r in recs if r["type"] == "score"}
    assert span["trace_id"] == tid and span["prompt_version"] == "v123"
    assert scores == {"recall": 0.9, "forbidden": 0.0, "case:a": 1.0, "case:b": 0.0}

"""
Deterministic grader for config-auditor outputs. No LLM-as-judge: a planted
defect is caught iff a medium/high finding names the planted field.

Rules:
  synthetic (tool/judgment) — caught if ANY planted field is named (they are aliases)
  real                      — each planted field is scored separately
  clean                     — any medium/high finding is a false alarm
  verdict                   — planted → "fail", clean → "pass"
"""
import re
import statistics

BLOCKING = {"high", "medium"}

# Ship gate. Tool recall must be perfect: the agent is handed those checks.
GATES = {
    "recall_tool": 1.0,
    "recall_judgment": 0.8,
    "recall_real": 0.6,
    "clean_false_alarm_rate": 0.0,
    "verdict_accuracy": 0.9,
}


def norm_field(field: str) -> str:
    """'revenue[3]' / 'Revenue.year_4' / ' dio ' → 'revenue' / 'revenue' / 'dio'."""
    return re.split(r"[\[\.\s(]", field.strip().lower(), maxsplit=1)[0]


def blocking_fields(output: dict) -> set[str]:
    return {norm_field(f["field"]) for f in output.get("findings", []) if f.get("severity") in BLOCKING}


def grade_case(case: dict, output: dict | None) -> dict:
    """Score one case. `output` is None when the system errored — scored as a total miss."""
    planted = {norm_field(f) for f in case["planted_fields"]}
    named = blocking_fields(output) if output else set()
    kind = case["kind"]
    res = {"id": case["id"], "kind": kind, "error": output is None, "named": sorted(named)}

    if kind == "clean":
        res["false_alarm"] = bool(named) or output is None
        res["verdict_ok"] = bool(output) and output.get("verdict") == "pass"
    elif kind == "real":
        hit = planted & named
        res["hits"], res["targets"] = len(hit), len(planted)
        res["missed"] = sorted(planted - named)
        res["verdict_ok"] = bool(output) and output.get("verdict") == "fail"
    else:
        res["caught"] = bool(planted & named)
        res["verdict_ok"] = bool(output) and output.get("verdict") == "fail"
    return res


def summarise(results: list[dict]) -> dict:
    def rate(rows, key):
        return statistics.mean(r[key] for r in rows) if rows else None

    by = {k: [r for r in results if r["kind"] == k] for k in ("tool", "judgment", "real", "clean")}
    real_targets = sum(r["targets"] for r in by["real"])
    summary = {
        "n": len(results),
        "errors": sum(r["error"] for r in results),
        "recall_tool": rate(by["tool"], "caught"),
        "recall_judgment": rate(by["judgment"], "caught"),
        "recall_real": sum(r["hits"] for r in by["real"]) / real_targets if real_targets else None,
        "clean_false_alarm_rate": rate(by["clean"], "false_alarm"),
        "verdict_accuracy": rate(results, "verdict_ok"),
    }
    summary["gates"] = {
        k: (summary[k] is not None and (summary[k] <= v if k == "clean_false_alarm_rate" else summary[k] >= v))
        for k, v in GATES.items()
    }
    summary["passed"] = all(summary["gates"].values())
    return summary

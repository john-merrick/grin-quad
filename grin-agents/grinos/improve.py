"""
The improvement loop (RSI, bounded): an agent's eval failures, and Isaac's approved corrections,
become candidate prompt edits (prompt-optimiser agent), each scored on that agent's eval next to a fresh
run of the current prompt. A candidate is selected only if it passes every gate AND does at least as well
as the current prompt on every case; it goes live only when Isaac approves it.

Deterministic barriers on every candidate (the model can't get past these):
  - each edit's `find` occurs exactly once, applied in order
  - every rule line of the current prompt (one that says must / never / only / always) survives verbatim
  - nothing from the failures (case ids, quotes) leaks into the prompt
  - the prompt grows by at most half

Ledger stages: improvement_started, prompt_candidate, candidate_eval, prompt_selected | no_winner,
prompt_promotion_decision (human, first is final), prompt_promoted.
"""
import json
import re
from datetime import date
from pathlib import Path

from grinos.ledger import Ledger
from grinos.llm import AGENTS_DIR, call_agent
from grinos.schemas import Actor, fence, sha256

SYSTEM = Actor(kind="system", name="improve", version="1")
MAX_GROWTH = 1.5
EPSILON = 0.0
DEFAULT_BUDGET_USD = 5.0
WEEKLY_BUDGET_USD = 15.0
RULE_LINE = re.compile(r"\b(must|never|only|always|Never|Only|Always|Must)\b")

OPT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["candidates"],
    "properties": {"candidates": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["rationale", "edits"],
        "properties": {"rationale": {"type": "string"}, "edits": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["find", "replace"],
            "properties": {"find": {"type": "string"}, "replace": {"type": "string"}}}}}}}},
}


# --- prompt files ---------------------------------------------------------------------------------

def split_prompt(text: str) -> tuple[str, str]:
    """(front matter including its --- lines, body). The body is what the agent sees and what is hashed."""
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        return f"---{fm}---", body.strip()
    return "", text.strip()


def prompt_version(body: str) -> str:
    return sha256(body.encode())[:16]


def current(agent: str) -> tuple[str, str, Path]:
    path = AGENTS_DIR / f"{agent}.md"
    fm, body = split_prompt(path.read_text())
    return fm, body, path


# --- candidates -----------------------------------------------------------------------------------

def message(agent: str, body: str, failures: list[dict]) -> str:
    return (f"<agent>\n{agent}\n</agent>\n\n<current_prompt>\n{fence(body)}\n</current_prompt>\n\n"
            f"<failures>\n{fence(json.dumps(failures, indent=2, default=str))}\n</failures>")


def apply_edits(body: str, edits: list[dict]) -> tuple[str | None, list[str]]:
    text, problems = body, []
    for i, e in enumerate(edits):
        n = text.count(e["find"]) if e["find"] else 0
        if n != 1:
            problems.append(f"edit {i + 1}: `find` occurs {n} times (needs exactly once)")
            return None, problems
        text = text.replace(e["find"], e["replace"], 1)
    return text, problems


def rule_lines(body: str) -> list[str]:
    return [ln.strip() for ln in body.splitlines() if ln.strip() and RULE_LINE.search(ln)]


def leak_tokens(failures: list[dict]) -> set[str]:
    """Strings from the failures that must never appear in a prompt: case ids and quoted inputs."""
    toks: set[str] = set()
    for f in failures:
        toks.add(str(f.get("id", "")))
        for q in f.get("quotes", []):
            if len(q) >= 12:
                toks.add(q)
    return {t for t in toks if t}


def check_candidate(old: str, new: str | None, leaks: set[str]) -> list[str]:
    if new is None:
        return ["edits don't apply"]
    problems = []
    lost = [ln for ln in rule_lines(old) if ln not in new]
    if lost:
        problems.append(f"drops or changes {len(lost)} rule line(s), e.g. {lost[0][:80]!r}")
    leaked = sorted(t for t in leaks if t and t in new and t not in old)
    if leaked:
        problems.append(f"copies eval material into the prompt: {leaked[:3]}")
    if len(new) > MAX_GROWTH * len(old):
        problems.append(f"grows the prompt {len(new) / len(old):.2f}× (limit {MAX_GROWTH}×)")
    if new.strip() == old.strip():
        problems.append("no change")
    return problems


def propose(agent: str, body: str, failures: list[dict], model: str = "sonnet", call=None) -> tuple[list[dict], dict]:
    """Candidates from the optimiser, each applied and checked: [{rationale, edits, body, problems, version}]."""
    out, meta = (call or call_agent)("prompt-optimiser", message(agent, body, failures), OPT_SCHEMA, model=model)
    leaks = leak_tokens(failures)
    cands = []
    for c in (out or {}).get("candidates", [])[:3]:
        new, problems = apply_edits(body, c["edits"])
        problems += check_candidate(body, new, leaks) if new is not None else ["edits don't apply"]
        cands.append({"rationale": c["rationale"], "edits": c["edits"], "body": new, "problems": problems,
                      "version": prompt_version(new) if new else None})
    return cands, meta


# --- selection ------------------------------------------------------------------------------------

def select(baseline: dict, results: list[dict], primary: str | None = None) -> dict | None:
    """
    baseline / each result: {"passed": bool, "metrics": {...}, "per_case": {case: pass rate}, "diff_size": int}.
    A candidate wins only if it passes every gate, scores at least as well as the baseline on EVERY case
    (no regression anywhere), and is strictly better somewhere (fewer failing cases, or the primary metric
    up by more than EPSILON). Among winners, the smallest diff.
    """
    winners = []
    for r in results:
        if not r.get("passed"):
            continue
        cases = set(baseline["per_case"]) | set(r["per_case"])
        if any(r["per_case"].get(c, 0.0) < baseline["per_case"].get(c, 0.0) for c in cases):
            continue
        fails_b = sum(1 - v for v in baseline["per_case"].values())
        fails_r = sum(1 - v for v in r["per_case"].values())
        better = fails_r < fails_b or (primary is not None and r["metrics"].get(primary, 0) >
                                       baseline["metrics"].get(primary, 0) + EPSILON)
        if better or not baseline.get("passed"):
            winners.append(r)
    return min(winners, key=lambda r: r["diff_size"]) if winners else None


def diff_size(old: str, new: str) -> int:
    import difflib
    return sum(1 for ln in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="")
               if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---")))


# --- failures to learn from -----------------------------------------------------------------------

def failures_from_report(report: dict, limit: int = 12) -> list[dict]:
    """The failing cases of an eval report, in the optimiser's input shape (what it got, what it did)."""
    rows = report.get("rows") or report.get("cases") or []
    out = []
    for r in rows:
        if r.get("case_pass") is False:
            got = {k: v for k, v in r.items() if k not in ("case_pass", "repeat", "cost_usd", "id", "drafts")}
            text = json.dumps(got, default=str)
            out.append({"id": r["id"], "result": json.loads(text) if len(text) <= 1500 else text[:1500] + "…"})
    seen, uniq = set(), []
    for f in out:
        if f["id"] not in seen:
            seen.add(f["id"])
            uniq.append(f)
    return uniq[:limit]


def corrections(ledger: Ledger, agent: str, limit: int = 8) -> list[dict]:
    """Isaac's corrections and disputes aimed at this agent (grinos.review feedback), as optimiser input."""
    out = []
    for e in ledger.entries():
        p = e["payload"]
        if e["stage"] == "feedback" and p.get("agent") == agent and p["kind"] in ("correction", "dispute"):
            out.append({"id": f"feedback#{e['seq']}", "result": {"isaac_says": p["text"],
                                                                 "about": f"{p['target_kind']}:{p['target_id']}"}})
    return out[-limit:]


# --- promotion (human) ----------------------------------------------------------------------------

def promotions(ledger: Ledger) -> list[dict]:
    """Selected candidates awaiting Isaac, newest last."""
    decided = {e["payload"]["promotion_id"] for e in ledger.entries() if e["stage"] == "prompt_promotion_decision"}
    return [e["payload"] | {"seq": e["seq"], "ts": e["ts"]} for e in ledger.entries()
            if e["stage"] == "prompt_selected" and e["payload"]["promotion_id"] not in decided]


def decide_promotion(ledger: Ledger, promotion_id: str, decision: str, who: str, note: str,
                     channel: str = "cli") -> dict:
    if decision not in ("approve", "reject"):
        raise ValueError("approve or reject")
    if not note.strip():
        raise ValueError("a promotion decision needs a note")
    sel = next((e for e in ledger.entries() if e["stage"] == "prompt_selected"
                and e["payload"]["promotion_id"] == promotion_id), None)
    if sel is None:
        raise ValueError(f"no promotion {promotion_id}")

    def first(entries):
        if any(e["stage"] == "prompt_promotion_decision" and e["payload"]["promotion_id"] == promotion_id
               for e in entries):
            return f"{promotion_id} was already decided"
        return None
    if decision == "approve":
        _not_stale(sel["payload"])                      # refuse before anything is recorded
    entry = ledger.append_if(sel["run_id"], "prompt_promotion_decision", Actor(kind="human", name=who, version="-"),
                             {"promotion_id": promotion_id, "decision": decision, "note": note.strip(),
                              "channel": channel}, refs=[f"entry:{sel['hash']}"], guard=first)
    if decision == "approve":
        promote(ledger, sel["payload"] | {"run_id": sel["run_id"]}, who)
    return entry


def _not_stale(sel: dict) -> None:
    _, body, _ = current(sel["agent"])
    if prompt_version(body) != sel["parent_version"]:
        raise ValueError(f"{sel['agent']}'s prompt changed since this candidate was made "
                         f"({prompt_version(body)} ≠ {sel['parent_version']}): run the loop again")


def promote(ledger: Ledger, sel: dict, who: str) -> dict:
    """Write the approved prompt (front matter kept). Refused if the live prompt changed since the loop ran."""
    _not_stale(sel)
    fm, _, path = current(sel["agent"])
    new_body = ledger.get_blob(sel["prompt_blob"]).decode()
    path.write_text(f"{fm}\n\n{new_body}\n" if fm else new_body + "\n")
    return ledger.append(sel["run_id"], "prompt_promoted", SYSTEM,
                         {"agent": sel["agent"], "from_version": sel["parent_version"], "to_version": sel["cand_version"],
                          "path": str(path), "approved_by": who,
                          "next": "re-run `python evals/run_all.py --only " + sel["agent"] + "` and commit the prompt"})


# --- the workflow's arguments ---------------------------------------------------------------------

def workflow_id(agent: str) -> str:
    y, w, _ = date.today().isocalendar()
    return f"improve:{agent}:{y}-W{w:02d}"


def workflow_args(agent: str, notify: dict | None = None, candidates: int = 3,
                  budget_usd: float = DEFAULT_BUDGET_USD) -> dict:
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "evals"))
    from run_all import SUITE
    ev, model, repeats = SUITE[agent]
    status = json.loads((Path(__file__).parents[1] / "evals" / "STATUS.json").read_text())
    return {"agent": agent, "eval_dir": ev, "model": model, "repeats": repeats, "candidates": candidates,
            "budget_usd": budget_usd, "eval_cost_usd": (status.get(agent) or {}).get("cost_usd") or 0.5,
            "notify": notify}


def promotion_view(ledger: Ledger, p: dict) -> dict:
    """A pending promotion with its unified diff and per-case before/after, for the dashboard and chat."""
    import difflib
    _, body, _ = current(p["agent"])
    new = ledger.get_blob(p["prompt_blob"]).decode()
    diff = "\n".join(difflib.unified_diff(body.splitlines(), new.splitlines(), "current", "candidate", lineterm=""))
    cases = sorted(set(p["baseline"]["per_case"]) | set(p["candidate"]["per_case"]))
    return {k: p[k] for k in ("promotion_id", "agent", "parent_version", "cand_version", "rationale",
                              "failing_cases_before", "failing_cases_after", "ts")} | {
        "stale": prompt_version(body) != p["parent_version"], "diff": diff,
        "cases": [{"id": c, "before": p["baseline"]["per_case"].get(c), "after": p["candidate"]["per_case"].get(c)}
                  for c in cases]}


async def schedule_weekly(agents: list[str]) -> list[str]:
    """A Temporal Schedule per agent: its improvement loop every Sunday 03:00 (ids rsi-weekly:<agent>)."""
    from temporalio.client import (
        Client,
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleCalendarSpec,
        ScheduleRange,
        ScheduleSpec,
    )

    from grinos.temporal.client import ADDRESS, NAMESPACE
    client = await Client.connect(ADDRESS, namespace=NAMESPACE)
    made = []
    for agent in agents:
        sid = f"rsi-weekly:{agent}"
        try:
            await client.create_schedule(sid, Schedule(
                action=ScheduleActionStartWorkflow("Improvement", workflow_args(agent), id=f"improve:{agent}:scheduled",
                                                   task_queue="grin-workflows"),
                spec=ScheduleSpec(calendars=[ScheduleCalendarSpec(day_of_week=[ScheduleRange(0)], hour=[ScheduleRange(3)])])))
            made.append(sid)
        except Exception as exc:  # noqa: BLE001 - already exists, or the server refused: report it
            made.append(f"{sid}: {type(exc).__name__}")
    return made

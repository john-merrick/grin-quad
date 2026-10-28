"""
Temporal activities. Each wraps existing grinos code; everything they write goes through the ledger's
idempotent writes (append_once keyed on the run, or the step machine's digest checkpoints), so Temporal's
retries are safe. Activities that call an agent run on that agent's own task queue.

Every activity runs inside a span of its workflow's trace (trace_id_for(workflow id)), so one workflow,
retries included, is one trace in Langfuse.
"""
import functools
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from temporalio import activity

from grinos import trace
from grinos.ledger import Ledger
from grinos.schemas import Actor

ROOT = Path(__file__).parents[2]
SYSTEM = Actor(kind="system", name="temporal", version="1")


def _ledger() -> Ledger:
    from grinos.cli import default_root
    return Ledger(default_root())


def traced(fn):
    """Run the activity inside its workflow's trace, tagged with the attempt number."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        info = activity.info()
        with trace.span(f"activity {info.activity_type}", trace_id=trace.trace_id_for(info.workflow_id),
                        session_id=info.workflow_id, tags=("temporal",),
                        metadata={"workflow_id": info.workflow_id, "attempt": info.attempt,
                                  "task_queue": info.task_queue}):
            return fn(*args, **kwargs)
    return wrapper


def _config(path: str | None) -> dict | None:
    return yaml.safe_load(Path(path).read_text()) if path and Path(path).exists() else None


# --- ingest: one activity per stage; the agent stages on their agents' queues ------------------------

@activity.defn(name="ingest_source")
@traced
def ingest_source(a: dict) -> dict:
    from grinos import pipeline
    L = _ledger()
    meta = dict(a["meta"])
    meta["published_at"] = date.fromisoformat(meta["published_at"])
    p = pipeline.record_source(L, L.get_blob(a["blob"]), meta, date.fromisoformat(a["as_of"]), a["run_id"])
    return {"blob": p["source"]["blob"], "company": p["source"]["company"]}


@activity.defn(name="ingest_extract")
@traced
def ingest_extract(a: dict) -> list[str]:
    from grinos import pipeline
    return pipeline.record_claims(_ledger(), a["run_id"], a.get("claims"), a.get("model", "sonnet"))


@activity.defn(name="ingest_unassessed")
@traced
def ingest_unassessed(run_id: str) -> list[dict]:
    from grinos import pipeline
    return pipeline.unassessed(_ledger(), run_id)


@activity.defn(name="decide_claim")
@traced
def decide_claim(a: dict) -> dict:
    from grinos import pipeline
    return _plain(pipeline.assess_decide(a["claim"], _config(a.get("config_path")), a.get("model", "sonnet")))


@activity.defn(name="verify_claim")
@traced
def verify_claim(a: dict) -> dict:
    from grinos import pipeline
    return _plain(pipeline.assess_verify(_ledger(), a["run_id"], a["claim"], a.get("model", "sonnet")))


@activity.defn(name="ingest_record")
@traced
def ingest_record(a: dict) -> dict:
    from grinos import pipeline
    L = _ledger()
    pipeline.record_assessments(L, a["run_id"], a["results"], a.get("model", "sonnet"))
    return pipeline.summarise(L, a["run_id"])


# --- analysis, coverage, valuation ---------------------------------------------------------------

@activity.defn(name="analyse")
@traced
def analyse(a: dict) -> dict:
    from grinos.proposals import analyse as run
    res = run(_ledger(), a["company"], _config(a["config_path"]) or {}, a["config_path"], model=a.get("model", "sonnet"),
              net_debt=a.get("net_debt", 0.0), shares=a.get("shares", 1.0), run_id=a["run_id"])
    return _plain({"run_id": res["run_id"], "proposals": [{k: p[k] for k in ("proposal_id", "field", "ok")}
                                                          for p in res["proposals"]], "no_change": res["no_change"]})


@activity.defn(name="analysis_open")
@traced
def analysis_open(run_id: str) -> list[str]:
    """Proposals of this run that passed their checks and still wait for a human decision."""
    from grinos.proposals import pending
    return [s["proposal"]["payload"]["proposal_id"] for s in pending(_ledger()) if s["proposal"]["run_id"] == run_id]


def _state(r: dict) -> dict:
    return {"run_id": r["run_id"], "status": r["status"], "step": r["step"],
            "blocking": [{"id": b["id"], "kind": b["kind"], "detail": b.get("detail", "")} for b in r["blocking"]]}


@activity.defn(name="coverage_start")
@traced
def coverage_start(a: dict) -> dict:
    from grinos import coverage, sec
    L = _ledger()
    r = coverage.start(L, a["ticker"], date.fromisoformat(a["as_of"]), a["config_path"], sec.http_get,
                       who=a.get("who", "analyst"))
    link(L, activity.info().workflow_id, r["run_id"])
    return _state(r)


@activity.defn(name="coverage_resume")
@traced
def coverage_resume(run_id: str) -> dict:
    from grinos import coverage
    return _state(coverage.resume(_ledger(), run_id))


@activity.defn(name="valuation_resume")
@traced
def valuation_resume(run_id: str) -> dict:
    from grinos import valuation
    return _state(valuation.resume(_ledger(), run_id))


def link(L: Ledger, workflow_id: str, run_id: str) -> None:
    """Record which workflow drives a run, so a human decision on the run can wake it."""
    L.append_once(run_id, "workflow_link", SYSTEM, {"workflow_id": workflow_id, "run_id": run_id},
                  idem=f"link:{workflow_id}:{run_id}")


# --- talking to the human ---------------------------------------------------------------------------

@activity.defn(name="notify")
@traced
def notify(a: dict) -> dict:
    """Tell the human, once. Telegram when the work came from a chat; always a ledger `notification`."""
    L = _ledger()
    if L.find_idem(a["idem"]):
        return {"sent": False, "duplicate": True}
    sent = False
    if a.get("channel") == "telegram" and a.get("target"):
        from grinos.env import secret
        from grinos.telegram import TelegramAPI, split
        token = secret("TELEGRAM_BOT_TOKEN")
        if token:
            api = TelegramAPI(token)
            for part in split(a["text"]):
                api.send_message(int(a["target"]), part)
            sent = True
    L.append_once(a.get("thread") or f"{a.get('channel')}:{a.get('target')}", "notification", SYSTEM,
                  {"channel": a.get("channel"), "target": a.get("target"), "text": a["text"], "sent": sent,
                   "workflow_id": activity.info().workflow_id}, idem=a["idem"])
    return {"sent": sent}


@activity.defn(name="agent_call")
@traced
def agent_call(a: dict) -> dict:
    from grinos.llm import call_agent
    out, meta = call_agent(a["agent"], a["message"], a["schema"], model=a.get("model", "sonnet"),
                           system_prompt=a.get("system_prompt"))
    return _plain({"output": out, "meta": meta})


# --- evals ------------------------------------------------------------------------------------------

@activity.defn(name="run_eval")
@traced
def run_eval(a: dict) -> dict:
    """Run one agent's eval (optionally against a candidate prompt file), heartbeating while it runs."""
    import os
    import uuid
    env = dict(os.environ)
    if a.get("prompt_path"):
        env["GRINOS_PROMPT_OVERRIDES"] = json.dumps({a["agent"]: a["prompt_path"]})
    label = "candidate" if a.get("prompt_path") else "current"
    out = _ledger().root / "rsi" / "evals" / a["agent"] / f"{label}_{uuid.uuid4().hex[:8]}"   # never in evals/results
    env["GRINOS_EVAL_OUT"] = str(out)                        # this run's own folder: parallel runs can't mix
    cmd = [sys.executable, str(ROOT / "evals" / a["eval_dir"] / "run.py"), "--system", "agent", "--model", a["model"],
           "--repeats", str(a.get("repeats", 1))] + (["--extra-cases", a["extra_cases"]] if a.get("extra_cases") else [])
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    while proc.poll() is None:
        activity.heartbeat("running")
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
    path = out / "report.json"
    report = json.loads(path.read_text()) if path.exists() else {}
    return {"returncode": proc.returncode, "report_path": str(path) if path.exists() else None,
            "summary": report.get("summary", {})}


def _plain(obj: Any) -> Any:
    """Temporal payloads are JSON. Decimals and dates become strings, exactly as the ledger's canonical JSON
    writes them, so a run through Temporal records the same bytes as one run inline."""
    return json.loads(json.dumps(obj, default=str))


ALL = [ingest_source, ingest_extract, ingest_unassessed, decide_claim, verify_claim, ingest_record, analyse,
       analysis_open, coverage_start, coverage_resume, valuation_resume, notify, agent_call, run_eval]


# --- the improvement loop (grinos.improve) ---------------------------------------------------------

EVALS = Path(__file__).parents[2] / "evals"


def _evals_common():
    sys.path.insert(0, str(EVALS))
    import common
    return common


@activity.defn(name="improve_collect")
@traced
def improve_collect(a: dict) -> dict:
    """What the loop learns from: the agent's latest eval failures and Isaac's corrections; and the budget."""
    from datetime import UTC, datetime, timedelta

    from grinos import improve
    L, common = _ledger(), _evals_common()
    _, body, _ = improve.current(a["agent"])
    report = common.latest_report(ROOT / "evals" / a["eval_dir"]) or {}
    failures = improve.failures_from_report(report) + improve.corrections(L, a["agent"])
    since = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    spent = sum(e["payload"].get("cost_usd") or 0 for e in L.entries()
                if e["stage"] == "candidate_eval" and e["ts"] >= since)
    wid = activity.info().workflow_id
    L.append_once(wid, "improvement_started", SYSTEM, {"agent": a["agent"], "parent_version": improve.prompt_version(body),
                                                       "failures": len(failures), "spent_this_week": spent},
                  idem=f"{wid}:started")
    return {"parent_version": improve.prompt_version(body), "failures": failures, "spent_this_week": spent,
            "budget_left": min(a.get("budget_usd", improve.DEFAULT_BUDGET_USD), improve.WEEKLY_BUDGET_USD - spent)}


@activity.defn(name="improve_propose")
@traced
def improve_propose(a: dict) -> list[dict]:
    """Optimiser candidates, applied and checked; each clean one written as a candidate prompt file."""
    from grinos import improve
    L = _ledger()
    wid = activity.info().workflow_id
    done = [e["payload"] for e in L.entries() if e["run_id"] == wid and e["stage"] == "prompt_candidate"]
    if done:
        return done
    fm, body, _ = improve.current(a["agent"])
    cands, meta = improve.propose(a["agent"], body, a["failures"], model=a.get("model", "sonnet"))
    folder = L.root / "rsi" / wid.replace(":", "_")
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i, c in enumerate(cands[: a.get("candidates", 3)]):
        rec = {"agent": a["agent"], "index": i, "parent_version": improve.prompt_version(body), "cand_version": c["version"],
               "rationale": c["rationale"], "edits": c["edits"], "problems": c["problems"], "path": None,
               "prompt_blob": None, "diff_size": improve.diff_size(body, c["body"]) if c["body"] else None}
        if c["body"] and not c["problems"]:
            path = folder / f"{c['version']}.md"
            path.write_text(f"{fm}\n\n{c['body']}\n")
            rec |= {"path": str(path), "prompt_blob": L.put_blob(c["body"].encode())}
        L.append_once(wid, "prompt_candidate", Actor(kind="agent", name="prompt-optimiser", version="-"),
                      rec | {"meta": meta}, idem=f"{wid}:candidate:{i}")
        out.append(rec)
    return out


@activity.defn(name="improve_record_eval")
@traced
def improve_record_eval(a: dict) -> dict:
    """One eval run of the current prompt ("baseline") or a candidate, in the shape select() compares."""
    L, common = _ledger(), _evals_common()
    report = json.loads(Path(a["report_path"]).read_text()) if a.get("report_path") else {}
    s = report.get("summary", {})
    rec = {"label": a["label"], "cand_version": a.get("cand_version"), "passed": bool(s.get("passed")),
           "metrics": common.metrics(s), "per_case": common.per_case(report) if report else {},
           "cost_usd": s.get("cost_usd") or 0, "report_path": a.get("report_path"), "diff_size": a.get("diff_size") or 0}
    wid = activity.info().workflow_id
    L.append_once(wid, "candidate_eval", SYSTEM, rec, idem=f"{wid}:eval:{a['label']}")
    return rec


@activity.defn(name="improve_select")
@traced
def improve_select(a: dict) -> dict:
    from grinos import improve
    L = _ledger()
    wid = activity.info().workflow_id
    win = improve.select(a["baseline"], a["results"])
    if not win:
        L.append_once(wid, "no_winner", SYSTEM, {"agent": a["agent"], "tried": len(a["results"]),
                                                  "baseline_passed": a["baseline"]["passed"]}, idem=f"{wid}:select")
        return {"winner": None}
    cand = next(c for c in a["candidates"] if c["cand_version"] == win["cand_version"])
    pid = "pr_" + cand["cand_version"][:10]
    fails = lambda r: round(sum(1 - v for v in r["per_case"].values()), 2)  # noqa: E731
    L.append_once(wid, "prompt_selected", SYSTEM,
                  {"promotion_id": pid, "agent": a["agent"], "parent_version": cand["parent_version"],
                   "cand_version": cand["cand_version"], "prompt_blob": cand["prompt_blob"], "edits": cand["edits"],
                   "rationale": cand["rationale"], "baseline": a["baseline"], "candidate": win,
                   "failing_cases_before": fails(a["baseline"]), "failing_cases_after": fails(win)},
                  idem=f"{wid}:select")
    return {"winner": pid, "before": fails(a["baseline"]), "after": fails(win)}


@activity.defn(name="improve_decision")
@traced
def improve_decision(promotion_id: str) -> str | None:
    for e in _ledger().entries():
        if e["stage"] == "prompt_promotion_decision" and e["payload"]["promotion_id"] == promotion_id:
            return e["payload"]["decision"]
    return None


@activity.defn(name="refresh_status")
@traced
def refresh_status(agent: str) -> dict:
    """After a promotion: re-run the agent's eval through run_all, so STATUS.json records the new prompt."""
    proc = subprocess.run([sys.executable, str(ROOT / "evals" / "run_all.py"), "--only", agent], cwd=ROOT,
                          capture_output=True, text=True, timeout=3600)
    return {"returncode": proc.returncode, "tail": proc.stdout[-500:]}


ALL += [improve_collect, improve_propose, improve_record_eval, improve_select, improve_decision, refresh_status]

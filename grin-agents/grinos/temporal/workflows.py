"""
Temporal workflows: orchestration only. No I/O, no clocks, no randomness here; all work is in
activities (grinos.temporal.activities), referenced by name so this module imports nothing heavy.

Human-in-the-loop workflows (analysis, coverage) park while they wait for you, woken by the
`ledger_changed` signal that the dashboard, Telegram and CLI send after a decision, or by a timer, and
then re-read the ledger. Signals carry no data: the ledger stays the record of truth.
"""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

WORKFLOW_QUEUE, IO_QUEUE, EVAL_QUEUE = "grin-workflows", "grin-io", "grin-eval"
NON_RETRYABLE = ["ValueError", "LedgerCorrupt", "NotPublishable", "KeyError"]
AGENT_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=10),
                          non_retryable_error_types=NON_RETRYABLE)
IO_RETRY = RetryPolicy(maximum_attempts=5, non_retryable_error_types=NON_RETRYABLE)


def _agent(name: str) -> str:
    return f"agent.{name}"


async def _io(activity: str, arg, minutes: int = 10):
    return await workflow.execute_activity(activity, arg, task_queue=IO_QUEUE, retry_policy=IO_RETRY,
                                           start_to_close_timeout=timedelta(minutes=minutes))


async def _ask(agent: str, activity: str, arg, minutes: int = 15):
    return await workflow.execute_activity(activity, arg, task_queue=_agent(agent), retry_policy=AGENT_RETRY,
                                           start_to_close_timeout=timedelta(minutes=minutes))


async def _notify(args: dict, text: str, key: str) -> None:
    n = args.get("notify") or {}
    await _io("notify", {"channel": n.get("channel"), "target": n.get("target"), "thread": n.get("thread"),
                         "text": text, "idem": f"notify:{workflow.info().workflow_id}:{key}"})


class _Waits:
    """Park until the ledger changes (a signal) or the timer fires; then the caller re-reads the ledger."""

    def __init__(self) -> None:
        self._poked = False

    @workflow.signal
    def ledger_changed(self) -> None:
        self._poked = True

    async def wait(self, hours: float) -> None:
        try:
            await workflow.wait_condition(lambda: self._poked, timeout=timedelta(hours=hours))
        except TimeoutError:
            pass
        self._poked = False


@workflow.defn(name="Ingest")
class IngestWorkflow:
    """source → claim extractor → decision model and verifier per claim (in parallel) → ledger, in claim order."""

    @workflow.run
    async def run(self, a: dict) -> dict:
        await _io("ingest_source", a)
        await _ask("claim-extractor", "ingest_extract", {"run_id": a["run_id"], "claims": a.get("claims"),
                                                         "model": a.get("model")}, minutes=30)
        todo = await _io("ingest_unassessed", a["run_id"])
        gate = asyncio.Semaphore(a.get("parallel", 6))

        async def assess(c: dict):
            async with gate:
                dec = await _ask("decision-model", "decide_claim", {"claim": c, "config_path": a.get("config_path"),
                                                                     "model": a.get("model")})
                ver = None
                if dec["action"] == "verify":
                    ver = await _ask("verifier", "verify_claim", {"run_id": a["run_id"], "claim": c,
                                                                   "model": a.get("model")})
                return c["claim_id"], {"decision": dec, "verification": ver}
        results = dict(await asyncio.gather(*(assess(c) for c in todo)))
        summary = await _io("ingest_record", {"run_id": a["run_id"], "results": results, "model": a.get("model")})
        v = [c["verdict"] for c in summary["claims"]]
        await _notify(a, f"Ingested {a['meta'].get('company')} {a['meta'].get('kind')}: {len(v)} claims, "
                         f"{v.count('verified')} verified, {v.count('rejected')} rejected, "
                         f"{v.count('needs_human')} need you. Run {a['run_id']}.", "done")
        return summary


@workflow.defn(name="Analysis")
class AnalysisWorkflow(_Waits):
    """The mid-cycle analyst on verified evidence; then waits while you review and decide each proposal."""

    @workflow.run
    async def run(self, a: dict) -> dict:
        res = await _ask("mid-cycle-analyst", "analyse", a, minutes=20)
        ok = [p for p in res["proposals"] if p["ok"]]
        await _notify(a, (f"{a['company']}: {len(ok)} proposal(s) to review "
                          + ", ".join(f"{p['proposal_id']} {p['field']}" for p in ok)
                          + ". Open each in the dashboard to check its sources, then approve or reject.")
                      if ok else f"{a['company']}: no change proposed. {res['no_change'][:400]}", "proposals")
        for _ in range(a.get("max_days", 7)):
            if not ok or not await _io("analysis_open", res["run_id"]):
                break
            await self.wait(hours=24)
        return res


@workflow.defn(name="Coverage")
class CoverageWorkflow(_Waits):
    """The coverage run: advance, tell you what it needs, wait for your decisions, repeat until done."""

    @workflow.run
    async def run(self, a: dict) -> dict:
        r = await _io("coverage_start", a, minutes=15)
        told: list = []
        for _ in range(a.get("max_rounds", 60)):
            if r["status"] != "blocked":
                break
            ids = sorted(b["id"] for b in r["blocking"])
            if ids != told:
                told = ids
                await _notify(a, f"{a['ticker']} coverage is at {r['step']}: it needs you for "
                                 + "; ".join(f"{b['id']} ({b['kind']})" for b in r["blocking"][:8])
                                 + ". Decide in the Coverage tab.", f"{r['step']}:{','.join(ids)}")
            await self.wait(hours=a.get("wait_hours", 24))
            r = await _io("coverage_resume", r["run_id"])
        await _notify(a, f"{a['ticker']} coverage {r['run_id']} is {r['status']} at {r['step']}.", f"end:{r['status']}")
        return r


@workflow.defn(name="Eval")
class EvalWorkflow:
    @workflow.run
    async def run(self, a: dict) -> dict:
        return await workflow.execute_activity("run_eval", a, task_queue=EVAL_QUEUE, retry_policy=IO_RETRY,
                                               start_to_close_timeout=timedelta(hours=1),
                                               heartbeat_timeout=timedelta(minutes=2))


@workflow.defn(name="AgentCall")
class AgentCallWorkflow:
    """One agent call on that agent's own queue (the orchestrator's plan, a specialist consult)."""

    @workflow.run
    async def run(self, a: dict) -> dict:
        return await _ask(a["agent"], "agent_call", a, minutes=15)


@workflow.defn(name="Improvement")
class ImprovementWorkflow(_Waits):
    """
    One round of an agent's improvement loop: learn from its eval failures and Isaac's corrections, have the
    optimiser propose prompt edits, score each on the agent's eval next to a fresh baseline, select one only if
    no case does worse, then wait for Isaac. Nothing goes live without his approval.
    """

    @workflow.run
    async def run(self, a: dict) -> dict:
        got = await _io("improve_collect", a)
        if not got["failures"]:
            await _notify(a, f"{a['agent']}: nothing to learn from (no failing cases, no corrections).", "nothing")
            return {"result": "no_signal"}
        per_eval = float(a.get("eval_cost_usd") or 0.5)
        n = int(min(a.get("candidates", 3), max(0, got["budget_left"] // per_eval - 1)))
        if n < 1:
            await _notify(a, f"{a['agent']}: improvement skipped, over this week's eval budget.", "budget")
            return {"result": "over_budget"}
        cands = await _ask("prompt-optimiser", "improve_propose", a | {"failures": got["failures"], "candidates": n},
                           minutes=20)
        clean = [c for c in cands if c["path"]]
        if not clean:
            await _notify(a, f"{a['agent']}: no candidate passed the checks ({len(cands)} tried).", "none_clean")
            return {"result": "no_clean_candidate"}

        async def score(label: str, path: str | None, c: dict | None):
            rep = await workflow.execute_activity(
                "run_eval", {"agent": a["agent"], "eval_dir": a["eval_dir"], "model": a["model"],
                             "repeats": a["repeats"], "prompt_path": path}, task_queue=EVAL_QUEUE,
                retry_policy=IO_RETRY, start_to_close_timeout=timedelta(hours=1), heartbeat_timeout=timedelta(minutes=2))
            return await _io("improve_record_eval", {"label": label, "report_path": rep["report_path"],
                                                     "cand_version": c and c["cand_version"],
                                                     "diff_size": c and c["diff_size"]})
        baseline, *results = await asyncio.gather(score("baseline", None, None),
                                                  *(score(c["cand_version"], c["path"], c) for c in clean))
        sel = await _io("improve_select", {"agent": a["agent"], "baseline": baseline, "results": results,
                                           "candidates": clean})
        if not sel["winner"]:
            await _notify(a, f"{a['agent']}: {len(results)} candidate(s) scored; none beat the current prompt on every "
                             f"case. Nothing changes.", "no_winner")
            return {"result": "no_winner"}
        await _notify(a, f"{a['agent']}: a better prompt is ready ({sel['winner']}): failing cases "
                         f"{sel['before']} → {sel['after']}, no case worse. Review the diff in the Evals tab, then "
                         f"/promote {sel['winner']} approve|reject <your reason>.", "selected")
        decision = None
        for _ in range(a.get("max_days", 14)):
            decision = await _io("improve_decision", sel["winner"])
            if decision:
                break
            await self.wait(hours=24)
        if decision == "approve":
            await workflow.execute_activity("refresh_status", a["agent"], task_queue=EVAL_QUEUE, retry_policy=IO_RETRY,
                                            start_to_close_timeout=timedelta(hours=1))
            await _notify(a, f"{a['agent']}: promoted and re-evaluated. Commit agents/{a['agent']}.md when ready.",
                          "promoted")
        return {"result": decision or "expired", "promotion_id": sel["winner"]}


@workflow.defn(name="Optimize")
class OptimizeWorkflow(_Waits):
    """Isaac confirmed a plan: evaluate the chosen variants next to a fresh baseline, select under the
    no-regression rule, and wait for his approval of the winner."""

    @workflow.run
    async def run(self, a: dict) -> dict:
        gate = asyncio.Semaphore(a.get("parallel", 2))

        async def score(vid: str):
            async with gate:
                prep = await _io("optimize_prepare", {"plan_id": a["plan_id"], "variant_id": vid})
                rep = await workflow.execute_activity(
                    "run_eval", {"agent": a["agent"], "eval_dir": a["eval_dir"], "model": prep["model"],
                                 "repeats": a["repeats"], "prompt_path": prep["prompt_path"], "env": prep["env"]},
                    task_queue=EVAL_QUEUE, retry_policy=IO_RETRY, start_to_close_timeout=timedelta(hours=1),
                    heartbeat_timeout=timedelta(minutes=2))
                return await _io("improve_record_eval", {"label": vid, "report_path": rep["report_path"],
                                                         "cand_version": vid, "diff_size": prep["diff_size"]})
        baseline, *results = await asyncio.gather(score("baseline"), *(score(v) for v in a["variant_ids"]))
        sel = await _io("optimize_select", {"plan_id": a["plan_id"], "baseline": baseline, "results": results})
        if not sel["winner"]:
            await _notify(a, f"{a['agent']}: {len(results)} variant(s) scored; none did as well on every case and "
                             "better overall. Nothing changes.", "no_winner")
            return {"result": "no_winner"}
        await _notify(a, f"{a['agent']}: '{sel['label']}' wins ({sel['winner']}): failing cases {sel['before']} → "
                         f"{sel['after']}, no case worse. Review it on the agent's page, then approve or reject.",
                      "selected")
        decision = None
        for _ in range(a.get("max_days", 14)):
            decision = await _io("improve_decision", sel["winner"])
            if decision:
                break
            await self.wait(hours=24)
        if decision == "approve":
            await workflow.execute_activity("refresh_status", a["agent"], task_queue=EVAL_QUEUE, retry_policy=IO_RETRY,
                                            start_to_close_timeout=timedelta(hours=1))
        return {"result": decision or "expired", "promotion_id": sel["winner"]}


@workflow.defn(name="Heartbeat")
class HeartbeatWorkflow:
    """Weekly: re-run every agent's eval, compare with its last run, and for any that got worse, plan an Optimize
    round and tell Isaac what it would try and roughly what it would cost. He chooses what runs."""

    @workflow.run
    async def run(self, a: dict) -> dict:
        lines, flagged = [], []
        for agent in a["agents"]:
            await workflow.execute_activity("refresh_status", agent, task_queue=EVAL_QUEUE, retry_policy=IO_RETRY,
                                            start_to_close_timeout=timedelta(hours=1))
            c = await _io("heartbeat_check", agent)
            lines.append(("⚠ " if c["status"] == "regressed" else "· ") + c["summary"])
            if c["status"] == "regressed" and a.get("plan", True):
                p = await _ask("prompt-optimiser", "optimize_plan", agent, minutes=10)
                flagged.append(agent)
                lines.append(f"   planned {p['plan_id']}: {p['variants']} variant(s), about ${p['total_cost_usd']:.2f} to run. "
                             f"Agents → {agent} → Optimization to choose what runs.")
        await _notify(a, "Weekly eval heartbeat\n" + "\n".join(lines), f"heartbeat:{len(lines)}")
        return {"flagged": flagged, "lines": lines}


WORKFLOWS = [HeartbeatWorkflow, IngestWorkflow, AnalysisWorkflow, CoverageWorkflow, EvalWorkflow, AgentCallWorkflow, ImprovementWorkflow,
             OptimizeWorkflow]

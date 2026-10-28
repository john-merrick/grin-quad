"""
The orchestrator: the only agent Isaac talks to. Each message becomes a plan (orchestrator agent),
checked deterministically here, then executed: work starts as Temporal workflows (grinos.runtime),
specialists are consulted (on their own Temporal queues when Temporal is up), and one reply goes back.

Hard barriers (the model can't get past these, whatever it outputs):
  - only the allowlisted actions run; anything that is Isaac's decision is only ever a suggested command,
    neutralised so it can't be tapped
  - tickers must have a company config; agents must be real
  - numbers in the reply must come from the message or the context given to the model; others are removed
"""
import re
from datetime import date
from pathlib import Path
from typing import Any

from grinos.ledger import Ledger
from grinos.llm import agent_actor, call_agent
from grinos.schemas import fence

ACTIONS = ["start_analysis", "start_coverage", "start_eval", "start_improvement", "status", "suggest_command"]
CONSULTABLE = ["mid-cycle-analyst", "verifier", "decision-model", "config-auditor", "claim-extractor"]
EVAL_AGENTS = ["config-auditor", "decision-model", "verifier", "claim-extractor", "mid-cycle-analyst", "memo-writer",
               "orchestrator", "prompt-optimiser"]
HUMAN_COMMANDS = {"approve", "reject", "resolve", "cdecide", "vdecide", "risk", "stage", "go", "capture", "promote",
                  "publish", "cover", "analyse", "value", "why", "pending", "coverage", "valuation", "help"}
MAX_CONSULTS = 3

PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["reply", "actions", "consults"],
    "properties": {
        "reply": {"type": "string"},
        "actions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["action", "why"],
            "properties": {"action": {"type": "string", "enum": ACTIONS},
                           "ticker": {"type": ["string", "null"]}, "agent": {"type": ["string", "null"]},
                           "as_of": {"type": ["string", "null"]}, "run_id": {"type": ["string", "null"]},
                           "command": {"type": ["string", "null"]}, "why": {"type": "string"}}}},
        "consults": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["agent", "question"],
            "properties": {"agent": {"type": "string", "enum": CONSULTABLE}, "question": {"type": "string"}}}},
    },
}

_NUM = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?%?")


def plan_message(text: str, known_tickers: list[str], open_work: str, ledger_context: str, history: str) -> str:
    return (f"<known_tickers>\n{', '.join(known_tickers) or '(none)'}\n</known_tickers>\n\n"
            f"<open_work>\n{fence(open_work or '(nothing in progress)')}\n</open_work>\n\n"
            f"<ledger_context>\n{fence(ledger_context or '(nothing relevant)')}\n</ledger_context>\n\n"
            f"<recent_conversation>\n{fence(history or '(none)')}\n</recent_conversation>\n\n"
            f"<message>\n{fence(text)}\n</message>")


def numbers(text: str) -> set[str]:
    return {n.strip("$%").replace(",", "") for n in _NUM.findall(text or "")}


def invented_numbers(reply: str, sources: str) -> set[str]:
    """Numbers in the reply that appear nowhere in what the model was given. Single digits are allowed."""
    have = numbers(sources)
    return {n for n in numbers(reply) if n not in have and not (len(n) == 1 and n.isdigit())}


def check_plan(plan: dict, known_tickers: list[str], known_runs: set[str] | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """(actions that may run, consults that may run, problems). Everything else is dropped."""
    known = {t.upper() for t in known_tickers}
    ok, problems = [], []
    for a in plan.get("actions") or []:
        kind, t = a.get("action"), (a.get("ticker") or "").upper() or None
        if kind not in ACTIONS:
            problems.append(f"dropped unknown action {kind}")
        elif kind in ("start_analysis", "start_coverage") and t not in known:
            problems.append(f"dropped {kind}: {t or 'no ticker'} has no company config")
        elif kind == "start_coverage" and a.get("as_of") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a["as_of"]):
            problems.append(f"dropped {kind}: as_of {a['as_of']} is not a date")
        elif kind in ("start_eval", "start_improvement") and a.get("agent") not in EVAL_AGENTS:
            problems.append(f"dropped {kind}: unknown agent {a.get('agent')}")
        elif kind == "status" and not (t in known or (a.get("run_id") and (known_runs is None or a["run_id"] in known_runs))):
            problems.append("dropped status: unknown ticker or run")
        elif kind == "suggest_command" and not re.fullmatch(r"/[a-z]+\b.*", (a.get("command") or "").strip(), re.S):
            problems.append("dropped suggest_command: not a command")
        elif kind == "suggest_command" and a["command"].strip()[1:].split()[0] not in HUMAN_COMMANDS:
            problems.append(f"dropped suggest_command: unknown command {a['command'].split()[0]}")
        else:
            ok.append(a | ({"ticker": t} if t else {}))
    consults = []
    for c in (plan.get("consults") or [])[:MAX_CONSULTS]:
        if c.get("agent") in CONSULTABLE and (c.get("question") or "").strip():
            consults.append(c)
        else:
            problems.append(f"dropped consult {c.get('agent')}")
    return ok, consults, problems


def strip_invented(reply: str, sources: str) -> tuple[str, set[str]]:
    """Remove sentences that state a number the model wasn't given."""
    bad = invented_numbers(reply, sources)
    if not bad:
        return reply, bad
    kept = [s for s in re.split(r"(?<=[.!?])\s+", reply) if not (numbers(s) & bad)]
    return " ".join(kept).strip(), bad


# --- one turn ------------------------------------------------------------------------------------

def known_tickers() -> list[str]:
    from grinos.desk import COMPANIES_DIR
    return sorted(p.stem.upper() for p in Path(COMPANIES_DIR).glob("*.yaml"))


def open_work(ledger: Ledger) -> tuple[str, set[str]]:
    """What is in progress and what it waits for: the orchestrator's working memory of the desk."""
    from grinos import coverage, valuation
    from grinos.proposals import pending
    lines, runs = [], set()
    for r in coverage.runs(ledger)[-8:]:
        runs.add(r["run_id"])
        lines.append(f"coverage {r['run_id']} ({r['ticker']}) {r['status']} at {r['step']}"
                     + (f", needs {', '.join(r.get('blocking') or [])}" if r.get("blocking") else ""))
    for r in valuation.runs(ledger)[-5:]:
        runs.add(r["run_id"])
        lines.append(f"valuation {r['run_id']} {r['status']} at {r['step']}")
    for s in pending(ledger)[-10:]:
        p = s["proposal"]["payload"]
        runs.add(s["proposal"]["run_id"])
        lines.append(f"proposal {p['proposal_id']} {p['company']} {p['proposal']['field']} awaiting your decision")
    wf = [e["payload"] for e in ledger.entries() if e["stage"] == "workflow_started"][-5:]
    lines += [f"workflow {w['workflow_id']} ({w['workflow']}) started" for w in wf]
    return "\n".join(lines), runs


def status_of(ledger: Ledger, ticker: str | None, run_id: str | None) -> str:
    from grinos import coverage, valuation
    out = []
    for r in coverage.runs(ledger):
        if (run_id and r["run_id"] == run_id) or (ticker and r["ticker"] == ticker):
            out.append(f"coverage {r['run_id']}: {r['status']} at {r['step']}"
                       + (f", waiting on {', '.join(r.get('blocking') or [])}" if r.get("blocking") else "")
                       + (f", PW target {r['pw_target']:.2f}" if r.get("pw_target") is not None else ""))
    for r in valuation.runs(ledger):
        if (run_id and r["run_id"] == run_id) or (ticker and r["ticker"] == ticker):
            out.append(f"valuation {r['run_id']}: {r['status']} at {r['step']}")
    return "\n".join(out[-4:]) or f"Nothing in progress for {ticker or run_id}."


def execute(ledger: Ledger, actions: list[dict], user: str, channel: str, notify: dict, get) -> list[str]:
    """Start the allowed work. Returns one line per action for the reply."""
    from grinos import runtime
    from grinos.desk import COMPANIES_DIR, neutralise
    lines = []
    for a in actions:
        kind, t = a["action"], str(a.get("ticker") or "")
        try:
            if kind == "start_analysis":
                r = runtime.analyse(ledger, t, str(COMPANIES_DIR / f"{t.lower()}.yaml"), notify=notify,
                                    wait_for_proposals=False)
                lines.append(f"▶ analyst started on {t}" + (f" ({r['workflow_id']})" if r["via"] == "temporal" else
                             f": {len(r['result']['proposals'])} proposal(s) ready to review"))
            elif kind == "start_coverage":
                as_of = date.fromisoformat(a["as_of"]) if a.get("as_of") else date.today()
                r = runtime.coverage(ledger, t, as_of, str(COMPANIES_DIR / f"{t.lower()}.yaml"), get, user, notify=notify)
                lines.append(f"▶ coverage of {t} as of {as_of} started" + (
                    f" ({r['workflow_id']})" if r["via"] == "temporal" else f": {r['result']['status']} at {r['result']['step']}"))
            elif kind == "start_eval":
                lines.append(f"▶ eval for {a['agent']}: " + start_eval(ledger, a["agent"], notify))
            elif kind == "start_improvement":
                lines.append(f"▶ improvement loop for {a['agent']}: " + start_improvement(ledger, a["agent"], notify))
            elif kind == "status":
                lines.append(status_of(ledger, t, a.get("run_id")))
            elif kind == "suggest_command":
                lines.append("Your call. Type: " + neutralise(a["command"].strip()))
        except Exception as exc:  # noqa: BLE001 - tell Isaac, never crash the conversation
            lines.append(f"⚠ {kind} {t or a.get('agent') or ''} failed: {exc}")
    return lines


def start_eval(ledger: Ledger, agent: str, notify: dict) -> str:
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "evals"))
    from run_all import SUITE

    from grinos.temporal import client
    if agent not in SUITE:
        return f"no eval registered for {agent}"
    ev, model, repeats = SUITE[agent]
    try:
        r = client.start(ledger, "Eval", {"agent": agent, "eval_dir": ev, "model": model, "repeats": repeats,
                                          "notify": notify}, f"eval:{agent}:{date.today()}")
        return f"running on Temporal ({r['workflow_id']})"
    except client.TemporalUnavailable:
        return f"Temporal is down: run `python evals/run_all.py --only {agent}`"


def start_improvement(ledger: Ledger, agent: str, notify: dict) -> str:
    from grinos.temporal import client
    try:
        import grinos.improve as improve
        r = client.start(ledger, "Improvement", improve.workflow_args(agent, notify), improve.workflow_id(agent))
        return f"running on Temporal ({r['workflow_id']}); you'll review any winning prompt before it goes live"
    except client.TemporalUnavailable:
        return "Temporal is down: the improvement loop only runs on the worker"


def plan(ledger: Ledger, text: str, history: str, ledger_context: str, call=call_agent,
         model: str = "sonnet") -> tuple[dict | None, dict, str, set[str]]:
    work, runs = open_work(ledger)
    msg = plan_message(text, known_tickers(), work, ledger_context, history)
    out, meta = call("orchestrator", msg, PLAN_SCHEMA, model=model)
    return out, meta, msg, runs


def turn(desk: Any, text: str, user: str, channel: str, thread: str, inbound: dict) -> list[dict]:
    """One message → plan → checked actions and consults → one reply from the orchestrator."""
    from grinos import sec
    from grinos.desk import neutralise
    L = desk.ledger
    history = desk.history(channel, thread, exclude_seq=inbound["seq"])
    ctx, _ = desk.context(text, "mid-cycle-analyst")
    out, meta, msg, runs = plan(L, text, history, ctx, call=desk.call, model=desk.model)
    if out is None:
        out = {"reply": "I couldn't plan that just now (my call failed). Try again, or use a /command.",
               "actions": [], "consults": []}
    actions, consults, problems = check_plan(out, known_tickers(), runs)
    notify = {"channel": channel, "target": thread if channel == "telegram" else None, "thread": f"{channel}:{thread}"}
    done = execute(L, actions, user, channel, notify, desk.sec_get or sec.http_get)
    answers = []
    earlier: list[dict] = []
    for c in consults:
        r = desk._ask(c["agent"], c["question"], channel, thread, inbound, earlier)
        earlier.append(r)
        answers.append(f"{c['agent']} reports: {r['text']}")
    reply, removed = strip_invented(out["reply"], msg)
    text_out = "\n\n".join(x for x in [neutralise(reply), "\n".join(done), "\n\n".join(answers)] if x)
    if removed:
        text_out += f"\n(I removed figure(s) I couldn't source: {', '.join(sorted(removed))})"
    L.append(f"{channel}:{thread}", "orchestrator_plan", agent_actor("orchestrator", desk.model),
             {"plan": out, "ran": actions, "consulted": [c["agent"] for c in consults], "problems": problems,
              "invented_numbers": sorted(removed), "in_reply_to": inbound["hash"], "meta": meta})
    cites = sorted({c for r in earlier for c in r.get("cites", [])})
    e = L.append(f"{channel}:{thread}", "message", agent_actor("orchestrator", desk.model),
                 {"channel": channel, "thread": thread, "text": text_out, "cites": cites, "in_reply_to": inbound["hash"]})
    return [{"agent": "orchestrator", "text": text_out, "cites": cites, "removed_cites": [], "ledger_hash": e["hash"]}]


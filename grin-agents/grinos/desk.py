"""
The desk: one conversation layer for Telegram, the dashboard and the voice room.

  /commands   parsed deterministically. The only way to approve, reject, resolve or publish.
              LLMs never trigger actions.
  free text   routed (@mention → those agents; otherwise the desk-router agent) to 1–3 agents.
              Each answers from ledger context only. Agents speak in turn, each seeing earlier
              replies, which is the "conference". Citations not present in the context are
              stripped and flagged.

Every inbound and outbound message, and every command, is a ledger entry.
"""
import json
import re
import sys
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from grinos import notion
from grinos.graph import Graph, why
from grinos.ledger import Ledger
from grinos.llm import agent_actor, agent_prompt, call_agent
from grinos.pipeline import human_verdict
from grinos.proposals import PAST, analyse, decide_proposal, pending, proposal_state
from grinos.schemas import Actor, fence

ROOT = Path(__file__).parents[1]
COMPANIES_DIR = ROOT.parent / "val-machine" / "distval" / "companies"
PREAMBLE = (Path(__file__).parent / "prompts" / "desk.md").read_text()
AGENTS = ["mid-cycle-analyst", "verifier", "decision-model", "config-auditor", "claim-extractor"]
ALIASES = {"analyst": "mid-cycle-analyst", "verifier": "verifier", "decision": "decision-model",
           "decision-model": "decision-model", "auditor": "config-auditor", "config-auditor": "config-auditor",
           "extractor": "claim-extractor", "claim-extractor": "claim-extractor", "mid-cycle-analyst": "mid-cycle-analyst"}
CLAIM_ID = re.compile(r"\bc_[0-9a-f]{12}\b")
PROPOSAL_ID = re.compile(r"\bp_[0-9a-f]{10}\b")
MAX_CONTEXT = 30_000
WORD_JOINER = "\u2060"
# Agent text could echo a command from a poisoned source; break it so it can't be tapped in Telegram.
ACTION_CMD = re.compile(r"/(approve|reject|publish|resolve|analyse|ingest|value|vdecide|capture|go)\b", re.IGNORECASE)


def neutralise(text: str) -> str:
    return ACTION_CMD.sub(lambda m: "/" + WORD_JOINER + m.group(1), text)

ROUTER_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["agents", "why"],
                 "properties": {"agents": {"type": "array", "items": {"type": "string", "enum": AGENTS}},
                                "why": {"type": "string"}}}
REPLY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["reply", "cites"],
                "properties": {"reply": {"type": "string"}, "cites": {"type": "array", "items": {"type": "string"}}}}

def company_config(ticker: str) -> dict | None:
    path = COMPANIES_DIR / f"{ticker.lower()}.yaml"
    return yaml.safe_load(path.read_text()) if path.exists() else None


HELP = """Commands:
/pending — proposals awaiting you, and claims needing review
/approve <proposal_id> <note>   /reject <proposal_id> <note>
/resolve <claim_id> verified|rejected <note>
/capture — completed research not yet in Notion (LongGamma workflow)
/capture results <run_id> · /capture sync <valuation run | proposal_id> — prepare and show the writes
/go <lg_id> <rationale> — your go for a model sync, rationale verbatim · /capture push <lg_id>
/analyse <TICKER> — run the mid-cycle analyst on verified evidence
/value <TICKER> [DATE PRICE] — FCFF valuation from var/assumptions/<ticker>.yaml (analysis only)
/valuation [run_id] — list runs, or one run's status and what it needs from you
/vdecide <run_id> <item> accept|reject|approve|value [figure] <note>
/why <claim_id> | <TICKER> <assumption> | <TICKER> <metric> <period> | <TICKER> conflicts
/runs  /trail <claim_id>  /evidence <TICKER>  /audit
In Telegram, send a text filing as a document captioned: /ingest TICKER KIND PUBLISHED [AS_OF]
Anything else is a question for the desk; use @analyst @verifier @decision @auditor @extractor to pick who answers."""


class Desk:
    def __init__(self, ledger: Ledger, model: str = "sonnet", router_model: str = "haiku",
                 call: Callable = call_agent, sec_get: Callable | None = None):
        self.ledger, self.model, self.router_model, self.call = ledger, model, router_model, call
        self.sec_get = sec_get                      # SEC fetcher for /value; None = the network

    # --- entry point ----------------------------------------------------------------

    def handle(self, text: str, user: str, channel: str, thread: str = "main",
               audio_blob: str | None = None, conference: bool = False) -> list[dict]:
        text = text.replace(WORD_JOINER, "").strip()   # a human pasting a suggested command still works
        inbound = self.ledger.append(f"{channel}:{thread}", "message", Actor(kind="human", name=user, version="-"),
                                     {"channel": channel, "thread": thread, "text": text, "audio_blob": audio_blob},
                                     refs=[f"blob:{audio_blob}"] if audio_blob else [])
        if text.startswith("/"):
            return [self._command(text, user, channel, thread, inbound)]
        agents = self.route(text, conference)
        replies: list[dict] = []
        for agent in agents:
            replies.append(self._ask(agent, text, channel, thread, inbound, replies))
        return replies

    # --- routing ----------------------------------------------------------------------

    def route(self, text: str, conference: bool = False) -> list[str]:
        mentioned = [ALIASES[m.lower()] for m in re.findall(r"@([\w-]+)", text) if m.lower() in ALIASES]
        if mentioned:
            return list(dict.fromkeys(mentioned))[:3]
        out, _ = self.call("desk-router", f"<message>\n{fence(text)}\n</message>"
                           + ("\n(This is a conference: several agents may speak.)" if conference else ""),
                           ROUTER_SCHEMA, model=self.router_model)
        picked = [a for a in (out or {}).get("agents", []) if a in AGENTS]
        return list(dict.fromkeys(picked))[:3] or ["mid-cycle-analyst"]

    # --- agent replies -------------------------------------------------------------------

    def _ask(self, agent: str, text: str, channel: str, thread: str, inbound: dict, earlier: list[dict]) -> dict:
        context, known_ids = self.context(text, agent)
        history = self.history(channel, thread, exclude_seq=inbound["seq"])
        this_turn = "\n".join(f"{r['agent']}: {r['text']}" for r in earlier) or "(nobody yet)"
        msg = (f"<ledger_context>\n{fence(context)}\n</ledger_context>\n\n<recent_conversation>\n{fence(history)}\n"
               f"</recent_conversation>\n\n<this_turn>\n{fence(this_turn)}\n</this_turn>\n\n<message>\n{fence(text)}\n</message>")
        system = PREAMBLE + "\n" + agent_prompt(agent)
        out, meta = self.call(agent, msg, REPLY_SCHEMA, model=self.model, system_prompt=system)
        if out is None:
            out = {"reply": "I couldn't produce an answer just now (agent call failed). Try again, or use /runs.",
                   "cites": []}
        cites = [c for c in out["cites"] if c in known_ids]
        removed = [c for c in out["cites"] if c not in known_ids]
        reply = neutralise(out["reply"]) + (f"\n(Removed unverifiable citation(s): {', '.join(removed)})" if removed else "")
        e = self.ledger.append(f"{channel}:{thread}", "message", agent_actor(agent, self.model, system),
                               {"channel": channel, "thread": thread, "text": reply, "cites": cites,
                                "removed_cites": removed, "in_reply_to": inbound["hash"], "meta": meta})
        return {"agent": agent, "text": reply, "cites": cites, "removed_cites": removed, "ledger_hash": e["hash"]}

    def history(self, channel: str, thread: str, exclude_seq: int, n: int = 8) -> str:
        msgs = [e for e in self.ledger.entries() if e["stage"] == "message" and e["payload"].get("channel") == channel
                and e["payload"].get("thread") == thread and e["seq"] != exclude_seq]
        return "\n".join(f"{e['actor']['name']}: {e['payload']['text'][:400]}" for e in msgs[-n:]) or "(none)"

    def known_tickers(self) -> set[str]:
        tickers = {p.stem.upper() for p in COMPANIES_DIR.glob("*.yaml")}
        tickers |= {e["payload"]["claim"]["company"] for e in self.ledger.entries() if e["stage"] == "claim"}
        return tickers

    def context(self, text: str, agent: str) -> tuple[str, set[str]]:
        """Compact ledger context relevant to the message, and the ids it contains."""
        entries = list(self.ledger.entries())
        tickers = [t for t in self.known_tickers() if re.search(rf"\b{re.escape(t)}\b", text, re.IGNORECASE)]
        if not tickers:  # no company named: use the most recently touched one
            recent = [e["payload"]["claim"]["company"] for e in entries if e["stage"] == "claim"]
            tickers = recent[-1:]
        parts, ids = [], set()
        claims = {e["payload"]["claim_id"]: e["payload"]["claim"] for e in entries if e["stage"] == "claim"}
        published = {e["payload"]["source"]["blob"]: (e["payload"]["source"]["published_at"], e["ts"][:10])
                     for e in entries if e["stage"] == "source"}
        decisions = {e["payload"]["claim_id"]: e["payload"] for e in entries if e["stage"] == "decision"}
        verdicts = {e["payload"]["claim_id"]: e["payload"] for e in entries if e["stage"] == "verdict"}

        def claim_line(cid):
            c, d, v = claims[cid], decisions.get(cid, {}), verdicts.get(cid, {})
            reasons = "; ".join(f"{r['code']}: {r['detail']}" for r in v.get("reasons", []))[:300]
            ids.add(cid)
            pub, seen = published.get(c["source_blob"], ("?", "?"))
            return (f"- {cid} [{c['company']} {c['metric']} {c['period']} = {c['value']}"
                    f"{' segment=' + c['segment'] if c.get('segment') else ''}{' adjusted' if c.get('adjusted') else ''}"
                    f" | source published {pub}, ingested {seen}] {c['statement']} "
                    f"| quote: \"{c['quote'][:200]}\" | decision: {d.get('action')}/{d.get('nature')} "
                    f"| verdict: {v.get('verdict', 'not verified')} {reasons}")

        for cid in CLAIM_ID.findall(text):
            if cid in claims:
                parts.append("Claim asked about:\n" + claim_line(cid))
        for t in tickers:
            mine = [cid for cid, c in claims.items() if c["company"] == t]
            if mine:
                parts.append(f"{t} claims (verified, rejected, needs_human, discarded):\n"
                             + "\n".join(claim_line(cid) for cid in mine[-40:]))
            props = [s for s in proposal_state(self.ledger).values() if s["proposal"]
                     and s["proposal"]["payload"]["company"] == t]
            if props:
                lines = []
                for s in props[-10:]:
                    p, c = s["proposal"]["payload"], s["check"]["payload"]
                    status = "published" if s["published"] else (PAST[s["approval"]["payload"]["decision"]]
                                                                 if s["approval"] else ("pending" if c["ok"] else "failed checks"))
                    ids.add(p["proposal_id"])
                    imp = c.get("impact") or {}
                    lines.append(f"- {p['proposal_id']} ({s['proposal']['ts'][:10]}) {p['proposal']['field']} {imp.get('old_value')} → "
                                 f"{c.get('normalised_value')} (EV {imp.get('ev_change_pct', 0):+.1%}) [{status}] "
                                 f"cites {p['proposal']['cites']}: {p['proposal']['rationale'][:300]}")
                parts.append(f"{t} proposals:\n" + "\n".join(lines))
            cfg = COMPANIES_DIR / f"{t.lower()}.yaml"
            if agent in {"config-auditor", "mid-cycle-analyst"} and cfg.exists():
                parts.append(f"{t} config ({cfg.name}):\n{cfg.read_text()[:6000]}")
                if agent == "config-auditor":
                    sys.path.insert(0, str(ROOT / "tools"))
                    from config_facts import config_facts
                    parts.append(f"{t} config_facts:\n{json.dumps(config_facts(cfg))[:4000]}")
        parts.append(f"Today is {date.today().isoformat()}.")
        runs = list(dict.fromkeys(e["run_id"] for e in entries if e["stage"] == "source"))[-3:]
        if runs:
            parts.append("Recent runs: " + ", ".join(runs))
        ctx = "\n\n".join(parts) or "(the ledger has nothing relevant yet)"
        return ctx[:MAX_CONTEXT], ids

    # --- deterministic commands --------------------------------------------------------

    def _command(self, text: str, user: str, channel: str, thread: str, inbound: dict) -> dict:
        cmd, *rest = text[1:].split(maxsplit=1)
        arg = rest[0] if rest else ""
        try:
            result = self._run_command(cmd.lower().split("@")[0], arg, user, channel)
        except Exception as exc:  # report, never crash the bot
            result = f"⚠️ {exc}"
        self.ledger.append(f"{channel}:{thread}", "command", Actor(kind="human", name=user, version="-"),
                           {"channel": channel, "command": cmd, "arg": arg, "result": result[:2000],
                            "in_reply_to": inbound["hash"]})
        return {"agent": "system", "text": result, "cites": [], "removed_cites": []}

    def _capture_command(self, cmd: str, arg: str, user: str, channel: str) -> str:
        """LongGamma capture: /capture · /capture results|sync <id> · /capture push <lg_id> · /go <lg_id> <rationale>."""
        from grinos import longgamma as LG
        L, parts = self.ledger, arg.split(maxsplit=1)
        try:
            if cmd == "publish":                                   # an approved proposal → model sync
                parts = ["sync", arg.strip()]
            if cmd == "go":
                if len(parts) < 2:
                    return "Usage: /go <lg_id> <your rationale, written verbatim into the rationale note>"
                LG.go(L, parts[0], user, parts[1], channel=channel)
                return f"✅ go recorded for {parts[0]}. Next: /capture push {parts[0]}"
            if not parts:
                rows = LG.pending(L)
                return "\n".join(f"• {r['id']} ({r['state']}) {r['label']}" for r in rows[-15:]) or "Everything completed is in Notion."
            kind, rest = parts[0], (parts[1] if len(parts) > 1 else "").strip()
            if kind in ("results", "sync") and rest:
                p = LG.prepare(L, kind, rest)
                nxt = f"/go {LG._id(kind, rest)} <rationale>" if kind == "sync" else f"/capture push {LG._id(kind, rest)}"
                return LG.describe(p) + f"\nNext: {nxt}"
            if kind == "push" and rest:
                out = LG.push(L, rest)
                return "📤 In Notion: " + ", ".join(f"{k} {v.get('url')}" for k, v in out.items() if isinstance(v, dict) and v.get("url"))
            return "Usage: /capture · /capture results <run_id> · /capture sync <run_id|proposal_id> · /capture push <lg_id>"
        except notion.NotPublishable as exc:
            return f"Not captured: {exc}"

    def _valuation_command(self, cmd: str, arg: str, user: str, channel: str) -> str:
        from grinos import sec
        from grinos import valuation as V
        L, parts = self.ledger, arg.split()
        if cmd == "value":
            if not parts:
                return "Usage: /value <TICKER> [DATE PRICE]"
            path = L.root / "assumptions" / f"{parts[0].lower()}.yaml"
            if not path.exists():
                return f"No assumptions at {path}. Copy examples/valuation/acme.yaml there and edit it."
            req = V.load_request(path, valuation_date=parts[1] if len(parts) > 1 else None,
                                 price=parts[2] if len(parts) > 2 else None)
            return V.summary(V.start(L, req, self.sec_get or sec.http_get, who=user))
        if cmd == "valuation":
            if not parts:
                return "\n".join(f"• {r['run_id']} {r['status']} at {r['step']}" for r in V.runs(L)[-10:]) or "No valuations yet."
            return V.summary(V.resume(L, parts[0]))
        if len(parts) < 4:
            return "Usage: /vdecide <run_id> <item> accept|reject|approve|value [figure] <note>"
        run_id, target, decision, *rest = parts
        value = None
        if decision == "value":
            value, rest = Decimal(rest[0]), rest[1:]
        V.decide(L, run_id, target, decision, user, " ".join(rest), value, channel=channel)
        return V.summary(V.resume(L, run_id))

    def _run_command(self, cmd: str, arg: str, user: str, channel: str) -> str:
        L = self.ledger
        if cmd in {"help", "start"}:
            return HELP
        if cmd == "pending":
            lines = []
            for s in pending(L):
                p, c = s["proposal"]["payload"], s["check"]["payload"]
                lines.append(f"• {p['proposal_id']} {p['company']} {p['proposal']['field']} → {c['normalised_value']} "
                             f"(EV {c['impact']['ev_change_pct']:+.1%}): {p['proposal']['rationale'][:160]}")
            verdicts = {}
            for e in L.entries():
                if e["stage"] == "verdict":
                    verdicts[e["payload"]["claim_id"]] = e["payload"]
            claims = {e["payload"]["claim_id"]: e["payload"]["claim"] for e in L.entries() if e["stage"] == "claim"}
            for cid, v in verdicts.items():
                if v["verdict"] == "needs_human":
                    lines.append(f"• review {cid}: {claims[cid]['statement']}")
            return "\n".join(lines) or "Nothing pending."
        if cmd in {"approve", "reject"}:
            pid, _, note = arg.partition(" ")
            if not note.strip():
                return f"Add a note: /{cmd} {pid or '<proposal_id>'} <why>"
            e = decide_proposal(L, pid, cmd, user, note.strip(), channel=channel)
            return f"✅ {pid} {PAST[cmd]} by {user} (ledger #{e['seq']})." + (f" Next: /publish {pid}" if cmd == "approve" else "")
        if cmd == "resolve":
            parts = arg.split(maxsplit=2)
            if len(parts) < 3 or parts[1] not in {"verified", "rejected"}:
                return "Usage: /resolve <claim_id> verified|rejected <note>"
            e = human_verdict(L, parts[0], parts[1], user, parts[2], channel=channel)
            return f"✅ {parts[0]} marked {parts[1]} by {user} (ledger #{e['seq']})."
        if cmd in {"publish", "capture", "go"}:
            return self._capture_command(cmd, arg, user, channel)
        if cmd == "analyse":
            t = arg.strip().upper()
            cfg = COMPANIES_DIR / f"{t.lower()}.yaml"
            if not cfg.exists():
                return f"No config for {t} at {cfg}"
            res = analyse(L, t, yaml.safe_load(cfg.read_text()), str(cfg), model=self.model)
            lines = [f"• {p['proposal_id']} {'ok' if p['ok'] else 'FAILED CHECKS'} {p['field']} → {p['new_value']}"
                     for p in res["proposals"]]
            return "\n".join(lines + [f"No change: {res['no_change']}"])
        if cmd in {"value", "valuation", "vdecide"}:
            return self._valuation_command(cmd, arg, user, channel)
        if cmd == "why":
            return why(Graph.open(L), arg, company_config)[:3500]
        if cmd == "runs":
            runs = {}
            for e in L.entries():
                if e["stage"] == "source":
                    runs[e["run_id"]] = e["payload"]["source"]["uri"]
            return "\n".join(f"• {r} {u}" for r, u in list(runs.items())[-10:]) or "No runs yet."
        if cmd == "trail":
            cid = arg.strip()
            trail = [f"#{e['seq']} {e['stage']} {e['actor']['kind']}:{e['actor']['name']} "
                     f"{json.dumps(e['payload'].get('verdict') or e['payload'].get('action') or '')}"
                     for e in L.entries() if e["payload"].get("claim_id") == cid]
            return "\n".join(trail) or f"No claim {cid}."
        if cmd == "evidence":
            served = L.verified_claims(arg.strip().upper() or None)
            return "\n".join(f"• {c['claim_id']} {c['claim']['statement']}" for c in served[-30:]) or "No verified evidence."
        if cmd == "audit":
            problems = L.audit()
            n = sum(1 for _ in L.entries())
            return f"Ledger intact: {n} entries verified." if not problems else "LEDGER BROKEN:\n" + "\n".join(problems[:10])
        return f"Unknown command /{cmd}. Try /help."

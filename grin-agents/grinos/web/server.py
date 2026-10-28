"""
Local dashboard + voice room over the ledger. Stdlib only.

  http://127.0.0.1:<port>/       dashboard: pending approvals, runs, claim trails, proposals, evals, desk chat
  http://127.0.0.1:<port>/room   voice room: push-to-talk → VoiceStudio STT → desk (conference) → TTS per agent

Security: binds 127.0.0.1 only; rejects foreign Host headers (DNS rebinding); every POST needs the
per-process session token embedded in the served pages (X-Grin-Token), so other websites can't
drive approvals. Blobs are served only if a ledger message/speech entry references them.
Every action is a ledger entry (approvals and verdicts via the same functions as the CLI).
"""
import json
import os
import re
import secrets
import socketserver
import sys
import threading
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from grinos import coverage, longgamma, notion, review, runtime, sec, trace, valuation, voice
from grinos.desk import COMPANIES_DIR, Desk
from grinos.ledger import Ledger
from grinos.pipeline import human_verdict
from grinos.proposals import PAST, decide_proposal, pending, proposal_state
from grinos.schemas import Actor
from grinos.web import agents_view, home_view
from grinos.xbrl import to_jsonable

STATIC = Path(__file__).parent / "static"
EVALS = Path(__file__).parents[2] / "evals"
SHA = re.compile(r"^[0-9a-f]{64}$")
MAX_BODY = 25 * 1024 * 1024      # audio uploads (/api/room/turn)
MAX_JSON = 256 * 1024
VOICE_ROOM = Actor(kind="system", name="voice-room", version="1")


# --- read models (pure functions of the ledger) -----------------------------------------

def entry_view(e: dict) -> dict:
    a, p = e["actor"], e["payload"]
    who = f"{a['kind']}:{a['name']}@{a['version'][:8]}" + (f"/{a['model']}" if a.get("model") else "")
    st = e["stage"]
    try:
        if st == "source":
            d = f"{p['source']['kind']} {p['source']['uri']} published {p['source']['published_at']}"
        elif st == "claim":
            c = p["claim"]
            d = f"{p['claim_id']} {c['metric']} {c['period']} = {c['value']} — {c['statement']}"
        elif st == "decision":
            d = (f"{p['claim_id']} {p['action']} · {p['nature']} · {p['materiality']} · "
                 f"lever={p['lever_facts']['lever']} — {p['rationale']}")
        elif st == "rule_checks":
            d = f"{p['claim_id']} {p['rules_verdict']}: " + ", ".join(
                f"{c['id']}={'ok' if c['ok'] else 'FAIL'}" for c in p["checks"])
        elif st == "verdict":
            d = f"{p['claim_id']} {p['verdict'].upper()} — " + "; ".join(
                f"{r['code']}: {r['detail']}" for r in p["reasons"])
        elif st == "message":
            d = p.get("text", "")
        elif st == "command":
            d = f"/{p['command']} {p['arg']} → {p['result'][:200]}"
        elif st == "proposal":
            q = p["proposal"]
            d = f"{p['proposal_id']} {q['field']} → {q['new_value']} cites {q['cites']} — {q['rationale']}"
        elif st == "proposal_check":
            d = f"{p['proposal_id']} {'ok' if p['ok'] else 'FAILED'} {'; '.join(p['problems'])}"
        elif st == "approval":
            d = f"{p['proposal_id']} {p['decision']} via {p['channel']} — {p['note']}"
        else:
            d = json.dumps(p, default=str)[:200]
    except (KeyError, TypeError):
        d = json.dumps(p, default=str)[:200]
    return {"seq": e["seq"], "ts": e["ts"], "stage": st, "actor": who, "detail": d, "hash": e["hash"]}


def runs_view(ledger: Ledger) -> list[dict]:
    runs: dict[str, dict] = {}
    for e in ledger.entries():
        r = runs.setdefault(e["run_id"], {"run_id": e["run_id"], "ts": e["ts"], "source": None, "claims": 0,
                                          "verified": 0, "rejected": 0, "needs_human": 0, "verdicts": 0,
                                          "entries": 0})
        r["entries"] += 1
        if e["stage"] == "source":
            r["source"] = e["payload"]["source"]["uri"]
        elif e["stage"] == "claim":
            r["claims"] += 1
        elif e["stage"] == "verdict":
            r["verdicts"] += 1
            v = e["payload"]["verdict"]
            if v in r:
                r[v] += 1
    for r in runs.values():
        r["discarded"] = max(r["claims"] - r.pop("verdicts"), 0) if r["source"] else 0
    return list(runs.values())


def needs_human(ledger: Ledger) -> list[dict]:
    claims, latest = {}, {}
    for e in ledger.entries():
        if e["stage"] == "claim":
            claims[e["payload"]["claim_id"]] = e["payload"]["claim"]
        elif e["stage"] == "verdict":
            latest[e["payload"]["claim_id"]] = e["payload"]
    return [{"claim_id": cid, "statement": claims[cid]["statement"], "quote": claims[cid]["quote"],
             "company": claims[cid]["company"],
             "reasons": [f"{r['code']}: {r['detail']}" for r in v.get("reasons", [])]}
            for cid, v in latest.items() if v["verdict"] == "needs_human" and cid in claims]


def proposal_view(s: dict) -> dict:
    p, c = s["proposal"]["payload"], (s["check"] or {}).get("payload", {})
    if s["published"]:
        status = "published"
    elif s["approval"]:
        status = PAST[s["approval"]["payload"]["decision"]]
    else:
        status = "pending" if c.get("ok") else "failed checks"
    imp = c.get("impact") or {}
    return {"proposal_id": p["proposal_id"], "company": p["company"], "field": p["proposal"]["field"],
            "old_value": imp.get("old_value"), "new_value": c.get("normalised_value", p["proposal"]["new_value"]),
            "nature": p["proposal"].get("nature"), "rationale": p["proposal"]["rationale"],
            "cites": p["proposal"]["cites"], "status": status, "problems": c.get("problems", []),
            "ev_change_pct": imp.get("ev_change_pct"), "ts": s["proposal"]["ts"]}


def ops_view() -> dict:
    """Agent cost, latency, failures and tokens per prompt version, from the local traces."""
    from datetime import UTC, datetime, timedelta
    recs = trace.read(days=30)
    since7 = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    host = trace.langfuse_config()[0]
    project = os.environ.get("LANGFUSE_PROJECT_ID") or (trace.local_langfuse() or {}).get("LANGFUSE_INIT_PROJECT_ID")
    errors = [{"name": r["name"], "start": r["start"], "status": r.get("status"), "trace_id": r["trace_id"]}
              for r in recs if r.get("level") == "ERROR"][-20:]
    return {"last_7_days": trace.ops(recs, since7), "last_30_days": trace.ops(recs), "errors": errors,
            "trace_url": f"{host.rstrip('/')}/project/{project}/traces/" if host and project else None}


def evals_view() -> list[dict]:
    out = []
    for b in sorted(EVALS.glob("*/BASELINE.md")):
        text = b.read_text()
        rows = [ln for ln in text.splitlines() if ln.startswith("|")]
        table = [[c.strip() for c in ln.strip("|").split("|")] for ln in rows if not set(ln) <= set("|-: ")]
        out.append({"agent": b.parent.name, "header": table[0] if table else [], "rows": table[1:],
                    "markdown": text})
    return out


def referenced_blobs(ledger: Ledger) -> set[str]:
    refs = set()
    for e in ledger.entries():
        if e["stage"] in {"message", "speech"} and e["payload"].get("audio_blob"):
            refs.add(e["payload"]["audio_blob"])
    return refs


# --- server --------------------------------------------------------------------------------

class App:
    def __init__(self, ledger: Ledger, model: str | None, desk_call=None, user: str | None = None):
        self.ledger, self.model = ledger, model
        self.token = secrets.token_urlsafe(32)
        self.user = user or os.environ.get("USER", "human")
        self.desk = Desk(ledger, model=model, call=desk_call) if desk_call else Desk(ledger, model=model)
        self.lock = threading.Lock()   # one desk turn at a time keeps conversations ordered

    def page(self, name: str) -> bytes:
        return (STATIC / name).read_text().replace("__GRIN_TOKEN__", self.token).encode()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GrinOS"

        def log_message(self, fmt, *args):  # quiet; the ledger is the log that matters
            if os.environ.get("GRINOS_WEB_VERBOSE"):
                super().log_message(fmt, *args)

        # helpers
        def _send(self, status: int, body: bytes, ctype: str):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                             "media-src 'self' blob:; frame-ancestors 'none'")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200):
            self._send(status, json.dumps(obj, default=str).encode(), "application/json")

        def _error(self, status: int, msg: str, **extra):
            self._json({"error": msg, **extra}, status)

        def _host_ok(self) -> bool:
            port = cast(tuple, self.server.server_address)[1]
            return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _body(self, limit: int = MAX_BODY) -> bytes:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("bad Content-Length") from None
            if n < 0 or n > limit:
                raise ValueError("body too large or invalid length")
            return self.rfile.read(n) if n else b""

        def _json_body(self) -> dict:
            try:
                data = json.loads(self._body(MAX_JSON) or b"{}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        # GET
        def do_GET(self):
            if not self._host_ok():
                return self._error(403, "forbidden host")
            path = self.path.split("?", 1)[0]
            L = app.ledger
            try:
                if path in {"/", "/index.html"}:
                    return self._send(200, app.page("dashboard.html"), "text/html; charset=utf-8")
                if path == "/room":
                    return self._send(200, app.page("room.html"), "text/html; charset=utf-8")
                if path == "/api/runs":
                    return self._json(runs_view(L))
                if m := re.fullmatch(r"/api/run/([\w:.\-]+)", path):
                    return self._json([entry_view(e) for e in L.entries() if e["run_id"] == m[1]])
                if m := re.fullmatch(r"/api/trail/(c_[0-9a-f]{12})", path):
                    entries = list(L.entries())
                    blob = next((e["payload"]["claim"]["source_blob"] for e in entries
                                 if e["stage"] == "claim" and e["payload"]["claim_id"] == m[1]), None)
                    return self._json([entry_view(e) for e in entries
                                       if e["payload"].get("claim_id") == m[1]
                                       or (e["stage"] == "source" and blob and e["payload"]["source"]["blob"] == blob)])
                if path == "/api/pending":
                    return self._json({"proposals": [proposal_view(s) for s in pending(L)],
                                       "claims": needs_human(L)})
                if path == "/api/proposals":
                    return self._json([proposal_view(s) for s in proposal_state(L).values() if s["proposal"]])
                if m := re.fullmatch(r"/api/proposal/(p_[0-9a-f]{10})", path):
                    return self._json(review.proposal_detail(L, m[1]))
                if m := re.fullmatch(r"/api/claim/(c_[0-9a-f]{12})", path):
                    q = parse_qs(urlsplit(self.path).query)
                    return self._json(review.claim_detail(review.Index(L), m[1], (q.get("context") or [None])[0],
                                                          int((q.get("match") or ["0"])[0])))
                if m := re.fullmatch(r"/api/source/([0-9a-f]{64})", path):
                    q = parse_qs(urlsplit(self.path).query)
                    start = int((q.get("start") or ["0"])[0])
                    end = int(q["end"][0]) if q.get("end") else None
                    try:
                        text = review.source_text(L, m[1], start, end)
                    except KeyError:
                        return self._error(404, "no such source")
                    return self._send(200, text.encode(), "text/plain; charset=utf-8")
                if path == "/api/audit":
                    problems = L.audit()
                    return self._json({"ok": not problems, "n": sum(1 for _ in L.entries()), "problems": problems})
                if path == "/api/valuations":
                    return self._json(to_jsonable(valuation.runs(L)))
                if m := re.fullmatch(r"/api/valuation/(val_[\w.\-]+)", path):
                    return self._json(to_jsonable(valuation.view(L, m[1])))
                if path == "/api/ops":
                    return self._json(ops_view())
                if path == "/api/coverage":
                    return self._json(to_jsonable(coverage.runs(L)))
                if m := re.fullmatch(r"/api/coverage/(cov_[\w.\-]+)", path):
                    return self._json(to_jsonable(coverage.view(L, m[1])))
                if path == "/api/home":
                    return self._json(home_view.home(L))
                if path == "/api/agents":
                    return self._json(agents_view.agents(L))
                if m := re.fullmatch(r"/api/agent/([a-z][\w-]*)", path):
                    return self._json(agents_view.agent_detail(L, m[1]))
                if m := re.fullmatch(r"/api/agent/([a-z][\w-]*)/run/((?:eval|rsi):[\w.\-]+)", path):
                    return self._json(agents_view.run_detail(m[1], m[2]))
                if m := re.fullmatch(r"/api/agent/([a-z][\w-]*)/run/((?:eval|rsi):[\w.\-]+)/case/([\w.\-]+)", path):
                    return self._json(agents_view.case_detail(m[1], m[2], m[3]))
                if m := re.fullmatch(r"/api/agent/([a-z][\w-]*)/plan/(op_[0-9a-f]{10})", path):
                    return self._json(agents_view.plan_detail(L, m[1], m[2]))
                if m := re.fullmatch(r"/api/agent/([a-z][\w-]*)/optimize/(op_[0-9a-f]{10})", path):
                    return self._json(agents_view.plan_progress(L, m[2]))
                if m := re.fullmatch(r"/api/agent/([a-z][\w-]*)/cases", path):
                    return self._json(agents_view.case_matrix(m[1]) | {"added": agents_view.added_cases(m[1])})
                if m := re.fullmatch(r"/api/agent/([a-z][\w-]*)/case/([\w.\-]+)", path):
                    return self._json(agents_view.case_history(m[1], m[2]))
                if m := re.fullmatch(r"/api/agent/([a-z][\w-]*)/case_template", path):
                    q = parse_qs(urlsplit(self.path).query)
                    return self._json(agents_view.case_template(L, m[1], (q.get("from") or ["blank"])[0]))
                if m := re.fullmatch(r"/api/trace/([0-9a-f]{16,64})", path):
                    return self._json(agents_view.trace_detail(m[1]))
                if path == "/api/promotions":
                    from grinos import improve
                    return self._json([improve.promotion_view(L, p) for p in improve.promotions(L)])
                if path == "/api/eval_history":
                    hist = EVALS / "history.jsonl"
                    rows = [json.loads(x) for x in hist.read_text().splitlines()] if hist.exists() else []
                    return self._json([{k: r.get(k) for k in ("agent", "prompt_version", "date", "passed", "cost_usd",
                                                               "metrics")} for r in rows[-400:]])
                if path == "/api/evals":
                    return self._json(evals_view())
                if path == "/api/voice":
                    return self._json({"available": voice.health() is not None, "base": voice.BASE})
                if m := re.fullmatch(r"/api/blob/([0-9a-f]+)", path):
                    sha = m[1]
                    if not SHA.match(sha) or sha not in referenced_blobs(L):
                        return self._error(404, "no such audio")
                    return self._send(200, L.get_blob(sha), "audio/mpeg")
                return self._error(404, "not found")
            except KeyError as exc:
                return self._error(404, str(exc).strip("'\""))
            except Exception as exc:  # never leak a traceback page
                return self._error(500, f"{type(exc).__name__}: {exc}")

        # POST
        def do_POST(self):
            if not self._host_ok():
                return self._error(403, "forbidden host")
            if not secrets.compare_digest(self.headers.get("X-Grin-Token", ""), app.token):
                return self._error(403, "missing or invalid session token")
            path = self.path.split("?", 1)[0]
            L = app.ledger
            try:
                if path == "/api/room/turn":
                    return self._room_turn()
                data = self._json_body()
                if path in {"/api/approve", "/api/reject"}:
                    pid, note = str(data.get("proposal_id", "")), str(data.get("note", "")).strip()
                    if not note:
                        raise ValueError("a note is required")
                    decision = "approve" if path.endswith("approve") else "reject"
                    e = decide_proposal(L, pid, decision, app.user, note, channel="dashboard")
                    runtime.changed(L, proposal_state(L)[pid]["proposal"]["run_id"])
                    return self._json({"ok": True, "seq": e["seq"], "hash": e["hash"]})
                if path == "/api/review":
                    e = review.record_review(L, str(data.get("target_kind", "claim")), str(data.get("target_id", "")),
                                             str(data.get("context_id", "")), str(data.get("decision", "")), app.user,
                                             str(data.get("note", "")), channel="dashboard",
                                             reject_claim=bool(data.get("reject_claim")))
                    return self._json({"ok": True, "seq": e["seq"]})
                if path == "/api/feedback":
                    e = review.record_feedback(L, str(data.get("target_kind", "")), str(data.get("target_id", "")),
                                               str(data.get("kind", "perspective")), str(data.get("text", "")),
                                               app.user, company=data.get("company"), agent=data.get("agent"),
                                               channel="dashboard")
                    return self._json({"ok": True, "seq": e["seq"]})
                if path == "/api/coverage/start":
                    t = str(data.get("ticker", "")).upper().strip()
                    if not re.fullmatch(r"[A-Z.\-]{1,10}", t):
                        raise ValueError("a ticker is required")
                    cfg = str(data.get("config_path") or (COMPANIES_DIR / f"{t.lower()}.yaml"))
                    if not Path(cfg).exists():
                        raise ValueError(f"no company config at {cfg}")
                    as_of = date.fromisoformat(str(data.get("as_of") or date.today().isoformat()))
                    with app.lock:
                        rr = runtime.coverage(L, t, as_of, cfg, sec.http_get, app.user,
                                              notify={"channel": "dashboard", "thread": "dashboard:main"})
                    if rr["via"] == "temporal":
                        return self._json({"ok": True, "run_id": rr["workflow_id"], "status": "started on Temporal",
                                           "step": "sources"})
                    r = rr["result"]
                    return self._json({"ok": True, "run_id": r["run_id"], "status": r["status"], "step": r["step"]})
                if path == "/api/coverage/decide":
                    run_id, value = str(data.get("run_id", "")), data.get("value")
                    coverage.decide(L, run_id, str(data.get("target", "")), str(data.get("decision", "")), app.user,
                                    str(data.get("note", "")), Decimal(str(value)) if value not in (None, "") else None,
                                    text=data.get("text"), channel="dashboard", revise=bool(data.get("revise")))
                    runtime.changed(L, run_id)
                    with app.lock:
                        r = coverage.resume(L, run_id)
                    return self._json({"ok": True, "status": r["status"], "step": r["step"]})
                if path == "/api/coverage/resume":
                    with app.lock:
                        r = coverage.resume(L, str(data.get("run_id", "")))
                    return self._json({"ok": True, "status": r["status"], "step": r["step"]})
                if path == "/api/coverage/risk":
                    e = coverage.record_risk(L, str(data.get("ticker", "")), str(data.get("risk", "")),
                                             int(data.get("likelihood") or 0), int(data.get("impact") or 0),
                                             str(data.get("kill_criterion", "")), app.user,
                                             status=str(data.get("status") or "Active"), nature=data.get("nature") or None,
                                             cites=[c for c in data.get("cites") or [] if isinstance(c, str)],
                                             channel="dashboard")
                    return self._json({"ok": True, "risk_id": e["payload"]["risk_id"]})
                if path == "/api/coverage/catalyst":
                    e = coverage.record_catalyst(L, str(data.get("ticker", "")), str(data.get("event", "")),
                                                 str(data.get("date", "")), app.user, type_=str(data.get("type") or "Results"),
                                                 date_confidence=str(data.get("date_confidence") or "Window"),
                                                 channel="dashboard")
                    return self._json({"ok": True, "catalyst_id": e["payload"]["catalyst_id"]})
                if path == "/api/coverage/stage":
                    e = coverage.set_stage(L, str(data.get("ticker", "")), str(data.get("stage", "")), app.user,
                                           str(data.get("note", "")), channel="dashboard")
                    return self._json({"ok": True, "to": e["payload"]["to"]})
                if path == "/api/coverage/capture":
                    kind, run_id = str(data.get("kind", "")), str(data.get("run_id", ""))
                    if kind not in ("sync", "risks"):
                        raise ValueError("kind is sync or risks")
                    try:
                        p = longgamma.prepare(L, kind, run_id)
                        nxt = (f"/go lg_sync_{run_id} <your rationale>, then /capture push lg_sync_{run_id}"
                               if kind == "sync" else f"/capture push lg_risks_{run_id}")
                        return self._json({"ok": True, "message": longgamma.describe(p) + "\nNext, in Desk: " + nxt})
                    except notion.NotPublishable as exc:
                        return self._json({"ok": True, "message": str(exc)})
                if m := re.fullmatch(r"/api/agent/([a-z][\w-]*)/optimize/plan", path):
                    from grinos import optimize
                    return self._json(optimize.plan(L, m[1]))
                if m := re.fullmatch(r"/api/agent/([a-z][\w-]*)/optimize/run", path):
                    from grinos import optimize
                    from grinos.temporal import client as tclient
                    plan = next((x for x in optimize.plans(L, m[1]) if x["plan_id"] == data.get("plan_id")), None)
                    if not plan:
                        raise ValueError("no such plan: plan first")
                    chosen = [v for v in data.get("variant_ids") or [] if v in {x["id"] for x in plan["variants"]}]
                    if not chosen:
                        raise ValueError("choose at least one variant")
                    est = plan["baseline_cost_usd"] + sum(v["est_cost_usd"] for v in plan["variants"] if v["id"] in chosen)
                    try:
                        r = tclient.start(L, "Optimize", {"plan_id": plan["plan_id"], "agent": m[1], "eval_dir": plan["eval_dir"],
                                                          "repeats": plan["repeats"], "variant_ids": chosen,
                                                          "notify": {"channel": "dashboard", "thread": "dashboard:main"}},
                                          f"optimize:{plan['plan_id']}", run_id=f"optimize:{plan['plan_id']}")
                    except tclient.TemporalUnavailable as exc:
                        raise ValueError(f"optimizing runs on the Temporal worker, which isn't reachable: {exc}") from exc
                    return self._json({"ok": True, "workflow_id": r["workflow_id"], "started": r["started"],
                                       "variants": len(chosen), "estimated_cost_usd": round(est, 2)})
                if m := re.fullmatch(r"/api/agent/([a-z][\w-]*)/cases", path):
                    import sys as _sys
                    _sys.path.insert(0, str(EVALS))
                    import added
                    case = data.get("case")
                    if not isinstance(case, dict):
                        raise ValueError("case must be a JSON object")
                    row = added.add(m[1], case, app.user, str(data.get("source") or "dashboard"))
                    L.append(f"evalcase_{m[1]}", "eval_case_added", Actor(kind="human", name=app.user, version="-"),
                             {"agent": m[1], "case_id": row["id"], "source": row["source"], "holdout": row["holdout"]})
                    return self._json({"ok": True, "id": row["id"], "holdout": row["holdout"],
                                       "next": "it runs in the next eval of this agent (weekly, or Optimize, or run_all)"})
                if path == "/api/promote":
                    from grinos import improve
                    pid, decision = str(data.get("promotion_id", "")), str(data.get("decision", ""))
                    improve.decide_promotion(L, pid, decision, app.user, str(data.get("note", "")), channel="dashboard")
                    sel = next(e for e in L.entries() if e["stage"] == "prompt_selected"
                               and e["payload"]["promotion_id"] == pid)
                    runtime.changed(L, sel["run_id"])
                    return self._json({"ok": True})
                if path == "/api/resolve":
                    cid, verdict = str(data.get("claim_id", "")), data.get("verdict")
                    note = str(data.get("note", "")).strip()
                    if verdict not in {"verified", "rejected"}:
                        raise ValueError("verdict must be verified or rejected")
                    if not note:
                        raise ValueError("a note is required")
                    if not any(e["stage"] == "claim" and e["payload"]["claim_id"] == cid for e in L.entries()):
                        raise ValueError(f"no claim {cid}")
                    e = human_verdict(L, cid, verdict, app.user, note, channel="dashboard")
                    return self._json({"ok": True, "seq": e["seq"]})
                if path == "/api/publish":
                    # An approved proposal → LongGamma model sync: prepare here; your go and the push
                    # happen in the Desk tab (/go, /capture push), so the diff is read before anything is written.
                    pid = str(data.get("proposal_id", ""))
                    try:
                        p = longgamma.prepare(L, "sync", pid)
                        return self._json({"ok": True, "pushed": False, "message":
                                           longgamma.describe(p) + f"\nNext, in Desk: /go lg_sync_{pid} <rationale>"})
                    except notion.NotPublishable as exc:
                        return self._json({"ok": True, "pushed": False, "message": str(exc)})
                if path == "/api/valuation/decide":
                    run_id, note = str(data.get("run_id", "")), str(data.get("note", "")).strip()
                    value = data.get("value")
                    valuation.decide(L, run_id, str(data.get("target", "")), str(data.get("decision", "")), app.user,
                                     note, Decimal(str(value)) if value not in (None, "") else None, channel="dashboard")
                    runtime.changed(L, run_id)
                    with app.lock:
                        r = valuation.resume(L, run_id)
                    return self._json({"ok": True, "status": r["status"], "step": r["step"]})
                if path == "/api/desk":
                    text = str(data.get("text", "")).strip()
                    if not text:
                        raise ValueError("text is required")
                    conference = bool(data.get("conference"))
                    with app.lock:
                        replies = app.desk.handle(text, app.user, "room" if conference else "dashboard",
                                                  thread="main", conference=conference)
                    return self._json({"replies": replies})
                return self._error(404, "not found")
            except (ValueError, notion.NotPublishable) as exc:
                return self._error(400, str(exc))
            except Exception as exc:
                print(f"error on {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
                return self._error(500, f"internal error ({type(exc).__name__}); see server log")

        def _room_turn(self):
            audio = self._body()
            if not audio:
                raise ValueError("no audio")
            ctype = (self.headers.get("Content-Type") or "audio/webm").split(";")[0]
            ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/mp4": "m4a",
                   "audio/mpeg": "mp3"}.get(ctype, "webm")
            try:
                transcript = voice.stt(audio, f"turn.{ext}")
            except voice.VoiceUnavailable as exc:
                return self._error(503, str(exc), fallback="text",
                                   hint="Start VoiceStudio (backend on port 3900) for voice; use text meanwhile.")
            if not transcript:
                return self._json({"transcript": "", "replies": []})
            L = app.ledger
            blob = L.put_blob(audio)
            with app.lock:
                replies = app.desk.handle(transcript, app.user, "room", thread="main", audio_blob=blob, conference=True)
                voices, tts_ok = voice.voice_map(L.root), True
                for r in replies:
                    r["audio_url"] = None
                    if not tts_ok:
                        continue
                    try:
                        clip = voice.tts(r["text"], voices.get(r["agent"], "default"), fmt="mp3")
                    except voice.VoiceUnavailable:
                        tts_ok = False
                        continue
                    sha = L.put_blob(clip)
                    L.append("room:main", "speech", VOICE_ROOM,
                             {"reply_hash": r.get("ledger_hash"), "agent": r["agent"], "audio_blob": sha},
                             refs=[f"blob:{sha}"])
                    r["audio_url"] = f"/api/blob/{sha}"
            return self._json({"transcript": transcript, "replies": replies, "tts": tts_ok})

    return Handler


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), a reverse-DNS lookup that can stall ~30s
        # on macOS. We only ever bind loopback, so skip it.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port


def make_server(ledger: Ledger, port: int = 8765, model: str | None = None, desk_call=None,
                user: str | None = None) -> tuple[ThreadingHTTPServer, App]:
    app = App(ledger, model, desk_call=desk_call, user=user)
    httpd = LocalServer(("127.0.0.1", port), make_handler(app))
    return httpd, app


def serve(ledger: Ledger, port: int = 8765, model: str | None = None) -> int:
    httpd, _ = make_server(ledger, port, model)
    host, bound = str(httpd.server_address[0]), httpd.server_port
    print(f"Grin OS dashboard: http://{host}:{bound}/   voice room: http://{host}:{bound}/room", flush=True)
    print(f"ledger: {ledger.root}   (Ctrl-C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    finally:
        httpd.server_close()
    return 0

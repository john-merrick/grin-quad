"""
Tracing: every agent call, pipeline step, workflow and desk turn becomes a span, so cost, latency,
failures and prompt versions can be watched over time and compared between prompt versions.

Two exporters, both best effort (an exporter problem never breaks the traced work):
  local     var/traces/YYYY-MM-DD.jsonl, always on. The dashboard's Ops tab reads it.
  Langfuse  when LANGFUSE_HOST is set and LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY resolve
            (1Password op:// references are fine). Uses Langfuse's public ingestion HTTP API, stdlib
            only, so no SDK is needed and nothing changes on Python upgrades.

The ledger stays the record of truth: a span links to ledger entries (the ledger stamps the active
trace_id on each entry it writes), never the other way round.

    with trace.span("ingest", input={...}) as s:          # root span → a new trace
        with trace.span("verifier", kind="generation", model="sonnet", prompt_version=v) as g:
            g.end(output=..., usage={"input_tokens": 1200}, cost_usd=0.01)
"""
import atexit
import base64
import contextlib
import contextvars
import hashlib
import json
import os
import queue
import sys
import threading
import time
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_FIELD = 8_000          # characters kept per input/output in a trace record (the ledger keeps the rest)
_current: contextvars.ContextVar["Span | None"] = contextvars.ContextVar("grinos_span", default=None)
_warned: set[str] = set()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clip(v: Any) -> Any:
    if v is None:
        return None
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= MAX_FIELD else s[:MAX_FIELD] + f"… [{len(s) - MAX_FIELD} more chars]"


def trace_id_for(key: str) -> str:
    """A deterministic trace id (32 hex): every attempt of one Temporal workflow lands in one trace."""
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _warn_once(what: str, exc: Exception) -> None:
    if what not in _warned:
        _warned.add(what)
        print(f"trace: {what} disabled after error: {type(exc).__name__}: {exc}", file=sys.stderr)


@dataclass
class Span:
    name: str
    kind: str = "span"                      # span | generation
    trace_id: str = ""
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_id: str | None = None
    session_id: str | None = None
    input: Any = None
    metadata: dict = field(default_factory=dict)
    tags: tuple = ()
    model: str | None = None
    prompt_version: str | None = None
    start: str = field(default_factory=_now)
    t0: float = field(default_factory=time.monotonic)
    output: Any = None
    usage: dict | None = None
    cost_usd: float | None = None
    level: str = "DEFAULT"                  # DEFAULT | WARNING | ERROR
    status: str | None = None
    ended: bool = False

    def end(self, output: Any = None, usage: dict | None = None, cost_usd: float | None = None,
            level: str | None = None, status: str | None = None, **metadata: Any) -> None:
        if output is not None:
            self.output = output
        if usage:
            self.usage = usage
        if cost_usd is not None:
            self.cost_usd = cost_usd
        if level:
            self.level = level
        if status:
            self.status = status
        self.metadata.update(metadata)

    def record(self) -> dict:
        return {"type": self.kind, "trace_id": self.trace_id, "span_id": self.span_id, "parent_id": self.parent_id,
                "name": self.name, "session_id": self.session_id, "start": self.start, "end": _now(),
                "duration_ms": round((time.monotonic() - self.t0) * 1000, 1), "model": self.model,
                "prompt_version": self.prompt_version, "input": _clip(self.input), "output": _clip(self.output),
                "usage": self.usage, "cost_usd": self.cost_usd, "level": self.level, "status": self.status,
                "metadata": self.metadata, "tags": list(self.tags)}


def current() -> Span | None:
    return _current.get()


def current_ids() -> dict | None:
    s = _current.get()
    return {"trace_id": s.trace_id, "span_id": s.span_id} if s else None


@contextlib.contextmanager
def span(name: str, *, kind: str = "span", input: Any = None, metadata: dict | None = None,
         trace_id: str | None = None, session_id: str | None = None, tags: tuple = (),
         model: str | None = None, prompt_version: str | None = None) -> Iterator[Span]:
    """A span under the current one, or a new trace. An exception marks it ERROR and propagates."""
    parent = _current.get()
    s = Span(name=name, kind=kind, trace_id=trace_id or (parent.trace_id if parent else uuid.uuid4().hex),
             parent_id=parent.span_id if parent and not trace_id else None,
             session_id=session_id or (parent.session_id if parent else None), input=input,
             metadata=dict(metadata or {}), tags=tags, model=model, prompt_version=prompt_version)
    token = _current.set(s)
    try:
        yield s
    except BaseException as exc:
        s.end(level="ERROR", status=f"{type(exc).__name__}: {exc}"[:500])
        raise
    finally:
        _current.reset(token)
        _emit(s.record(), root=parent is None or bool(trace_id))


def score(trace_id: str, name: str, value: float, comment: str | None = None,
          observation_id: str | None = None) -> None:
    """A number attached to a trace (an eval gate metric, a per-case pass, a human verdict)."""
    _emit({"type": "score", "trace_id": trace_id, "span_id": observation_id, "name": name, "value": value,
           "comment": comment, "start": _now()}, root=False)


# --- exporters ----------------------------------------------------------------------------------

def trace_dir() -> Path:
    home = os.environ.get("GRINOS_HOME") or Path(__file__).parents[1] / "var"
    return Path(home) / "traces"


def _emit(rec: dict, root: bool) -> None:
    if os.environ.get("GRINOS_TRACE") == "off":
        return
    try:
        d = trace_dir()
        d.mkdir(parents=True, exist_ok=True)
        with (d / f"{rec['start'][:10]}.jsonl").open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 - tracing must never break the traced work
        _warn_once("local trace file", exc)
    lf = _langfuse()
    if lf:
        lf.put(rec, root)


class _Langfuse:
    """Batches records to POST /api/public/ingestion on a daemon thread; flushed at exit."""

    def __init__(self, host: str, public: str, secret: str):
        self.url = host.rstrip("/") + "/api/public/ingestion"
        self.host = host.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()
        self.q: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="langfuse-export")
        self.thread.start()
        atexit.register(self.flush)

    def put(self, rec: dict, root: bool) -> None:
        for ev in to_ingestion(rec, root):
            self.q.put(ev)

    def _loop(self) -> None:
        while True:
            batch = [self.q.get()]
            time.sleep(0.5)
            while not self.q.empty() and len(batch) < 100:
                batch.append(self.q.get_nowait())
            self._send(batch)

    def _send(self, batch: list[dict]) -> None:
        try:
            self.post("/api/public/ingestion", {"batch": batch})
        except Exception as exc:  # noqa: BLE001
            _warn_once("Langfuse export", exc)

    def post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.host + path, data=json.dumps(body, default=str).encode(), method="POST",
                                     headers={"Authorization": self.auth, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")

    def flush(self) -> None:
        batch = []
        while not self.q.empty():
            batch.append(self.q.get_nowait())
        if batch:
            self._send(batch)


_lf: _Langfuse | None = None
_lf_checked = False


def _langfuse() -> _Langfuse | None:
    global _lf, _lf_checked
    if _lf_checked:
        return _lf
    _lf_checked = True
    host, pub, sec = langfuse_config()
    if host and pub and sec:
        _lf = _Langfuse(host, pub, sec)
    return _lf


LOCAL_LANGFUSE_ENV = Path(__file__).parents[1] / "services" / "langfuse" / ".env"


def langfuse_config() -> tuple[str | None, str | None, str | None]:
    """(host, public key, secret key). From LANGFUSE_HOST and the key variables (op:// references resolve
    through 1Password), else from the local stack's generated services/langfuse/.env, so the keys are
    never copied anywhere. GRINOS_LANGFUSE=off disables export."""
    if os.environ.get("GRINOS_LANGFUSE") == "off":
        return None, None, None
    host = os.environ.get("LANGFUSE_HOST")
    if host:
        from grinos.env import secret
        return host, secret("LANGFUSE_PUBLIC_KEY"), secret("LANGFUSE_SECRET_KEY")
    local = local_langfuse()
    if local:
        return "http://127.0.0.1:3000", local.get("LANGFUSE_INIT_PROJECT_PUBLIC_KEY"), \
            local.get("LANGFUSE_INIT_PROJECT_SECRET_KEY")
    return None, None, None


def local_langfuse(path: Path = LOCAL_LANGFUSE_ENV) -> dict[str, str] | None:
    if not path.exists():
        return None
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def langfuse() -> _Langfuse | None:
    return _langfuse()


def to_ingestion(rec: dict, root: bool) -> list[dict]:
    """A local trace record → Langfuse ingestion events."""
    ev: list[dict] = []
    stamp = rec.get("start") or _now()
    if rec["type"] == "score":
        ev.append({"id": uuid.uuid4().hex, "timestamp": stamp, "type": "score-create",
                   "body": {"id": uuid.uuid4().hex, "traceId": rec["trace_id"], "observationId": rec.get("span_id"),
                            "name": rec["name"], "value": rec["value"], "comment": rec.get("comment")}})
        return ev
    if root:
        ev.append({"id": uuid.uuid4().hex, "timestamp": stamp, "type": "trace-create",
                   "body": {"id": rec["trace_id"], "name": rec["name"], "sessionId": rec.get("session_id"),
                            "input": rec.get("input"), "output": rec.get("output"), "tags": rec.get("tags") or [],
                            "metadata": rec.get("metadata")}})
    body = {"id": rec["span_id"], "traceId": rec["trace_id"], "parentObservationId": rec.get("parent_id"),
            "name": rec["name"], "startTime": rec["start"], "endTime": rec["end"], "input": rec.get("input"),
            "output": rec.get("output"), "metadata": (rec.get("metadata") or {}) | {"prompt_version": rec.get("prompt_version")},
            "level": rec.get("level", "DEFAULT"), "statusMessage": rec.get("status")}
    if rec["type"] == "generation":
        u = rec.get("usage") or {}
        body |= {"model": rec.get("model"), "version": rec.get("prompt_version"),
                 "usageDetails": {k: v for k, v in {"input": u.get("input_tokens"), "output": u.get("output_tokens"),
                                                    "cache_read_input_tokens": u.get("cache_read_input_tokens"),
                                                    "cache_creation_input_tokens": u.get("cache_creation_input_tokens")
                                                    }.items() if v is not None},
                 "costDetails": {"total": rec["cost_usd"]} if rec.get("cost_usd") is not None else None}
    ev.append({"id": uuid.uuid4().hex, "timestamp": stamp,
               "type": "generation-create" if rec["type"] == "generation" else "span-create", "body": body})
    return ev


# --- reading (the dashboard's Ops tab) -----------------------------------------------------------

def read(days: int = 30) -> list[dict]:
    d = trace_dir()
    if not d.exists():
        return []
    files = sorted(d.glob("*.jsonl"))[-days:]
    out = []
    for f in files:
        for line in f.read_text().splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(line))
    return out


def ops(records: list[dict], since: str | None = None) -> list[dict]:
    """Per agent × prompt version: calls, failures, latency p50/p95, tokens, cost."""
    rows: dict[tuple, dict] = {}
    for r in records:
        if r.get("type") != "generation" or (since and r.get("start", "") < since):
            continue
        k = (r["name"], r.get("prompt_version"), r.get("model"))
        a = rows.setdefault(k, {"agent": k[0], "prompt_version": k[1], "model": k[2], "calls": 0, "errors": 0,
                                "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "lat": [],
                                "first": r["start"], "last": r["start"]})
        a["calls"] += 1
        a["errors"] += r.get("level") == "ERROR"
        a["cost_usd"] += r.get("cost_usd") or 0
        u = r.get("usage") or {}
        a["input_tokens"] += u.get("input_tokens") or 0
        a["output_tokens"] += u.get("output_tokens") or 0
        a["lat"].append(r.get("duration_ms") or 0)
        a["last"] = max(a["last"], r["start"])
    out = []
    for a in rows.values():
        lat = sorted(a.pop("lat"))
        a["p50_ms"] = lat[len(lat) // 2] if lat else None
        a["p95_ms"] = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None
        a["cost_usd"] = round(a["cost_usd"], 4)
        out.append(a)
    return sorted(out, key=lambda a: (a["agent"], a["last"]))

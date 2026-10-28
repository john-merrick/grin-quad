"""Tracing: local JSONL, nesting, errors, ledger links, the Langfuse wire format, the Ops view."""
import base64
import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from grinos import decide, trace, verify
from grinos.ledger import Ledger
from grinos.pipeline import ingest
from grinos.schemas import Actor
from grinos.tests.test_pipeline import GOOD, META, SOURCE

SYS = Actor(kind="system", name="t", version="1")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRINOS_HOME", str(tmp_path))
    monkeypatch.delenv("GRINOS_TRACE", raising=False)
    return tmp_path


def test_nested_spans_share_a_trace_and_record_parentage(home):
    with trace.span("outer", input={"q": 1}, session_id="s1") as o:
        with trace.span("inner", kind="generation", model="sonnet", prompt_version="abc") as i:
            i.end(output="ok", usage={"input_tokens": 10, "output_tokens": 2}, cost_usd=0.01)
    recs = trace.read()
    inner, outer = recs
    assert inner["trace_id"] == outer["trace_id"] == o.trace_id and inner["parent_id"] == outer["span_id"]
    assert inner["session_id"] == "s1" and inner["type"] == "generation" and inner["cost_usd"] == 0.01
    assert trace.current() is None


def test_an_exception_marks_the_span_error_and_still_propagates(home):
    with pytest.raises(RuntimeError), trace.span("boom"):
        raise RuntimeError("bad")
    (r,) = trace.read()
    assert r["level"] == "ERROR" and "bad" in r["status"]


def test_trace_ids_are_deterministic_per_key_and_tracing_can_be_off(home, monkeypatch):
    assert trace.trace_id_for("ingest:abc") == trace.trace_id_for("ingest:abc") != trace.trace_id_for("x")
    monkeypatch.setenv("GRINOS_TRACE", "off")
    with trace.span("quiet"):
        pass
    assert trace.read() == []


def test_ledger_entries_written_inside_a_span_carry_its_trace_id(home, tmp_path):
    L = Ledger(tmp_path / "led")
    outside = L.append("r", "s", SYS, {})
    with trace.span("work") as s:
        inside = L.append("r", "s", SYS, {})
    assert "trace_id" not in outside and inside["trace_id"] == s.trace_id and L.audit() == []


def test_ingest_is_one_trace_with_a_span_per_claim_across_worker_threads(home, tmp_path, monkeypatch):
    monkeypatch.setattr(decide, "call_agent", lambda *a, **k: (
        {"action": "verify", "nature": "structural", "materiality": "high", "rationale": "s"}, {}))
    monkeypatch.setattr(verify, "call_agent", lambda *a, **k: (
        {"verdict": "verified", "reasons": [{"code": "supported", "detail": "s"}]}, {}))
    ingest(Ledger(tmp_path / "led"), SOURCE, META, [GOOD], date(2026, 3, 1), run_id="run_t")
    recs = trace.read()
    root = next(r for r in recs if r["name"] == "ingest")
    claims = [r for r in recs if r["name"] == "assess claim"]
    assert claims and all(r["trace_id"] == root["trace_id"] and r["parent_id"] == root["span_id"] for r in claims)
    assert root["output"] and json.loads(root["output"])["verified"] == 1


def test_records_map_to_langfuse_ingestion_events():
    rec = {"type": "generation", "trace_id": "t" * 32, "span_id": "s" * 16, "parent_id": None, "name": "verifier",
           "session_id": "run_1", "start": "2026-10-25T10:00:00.000Z", "end": "2026-10-25T10:00:02.000Z",
           "model": "sonnet", "prompt_version": "v1", "input": "in", "output": "out", "level": "DEFAULT",
           "status": None, "metadata": {}, "tags": [], "usage": {"input_tokens": 100, "output_tokens": 7},
           "cost_usd": 0.02}
    trace_ev, gen = trace.to_ingestion(rec, root=True)
    assert trace_ev["type"] == "trace-create" and trace_ev["body"]["sessionId"] == "run_1"
    assert gen["type"] == "generation-create" and gen["body"]["model"] == "sonnet" and gen["body"]["version"] == "v1"
    assert gen["body"]["usageDetails"] == {"input": 100, "output": 7} and gen["body"]["costDetails"] == {"total": 0.02}
    (score,) = trace.to_ingestion({"type": "score", "trace_id": "t", "name": "case_pass", "value": 1.0}, root=False)
    assert score["type"] == "score-create" and score["body"]["value"] == 1.0


def test_langfuse_exporter_posts_batches_with_basic_auth(home):
    got: list[tuple[str, dict]] = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(207)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    lf = trace._Langfuse(f"http://127.0.0.1:{srv.server_address[1]}", "pk", "sk")
    lf.put({"type": "span", "trace_id": "a" * 32, "span_id": "b" * 16, "name": "x", "start": "2026-10-25T10:00:00Z",
            "end": "2026-10-25T10:00:01Z", "metadata": {}}, root=True)
    lf.thread = None
    import time
    for _ in range(40):
        if got:
            break
        time.sleep(0.05)
    srv.shutdown()
    auth, body = got[0]
    assert auth == "Basic " + base64.b64encode(b"pk:sk").decode()
    assert [e["type"] for e in body["batch"]] == ["trace-create", "span-create"]


def test_ops_aggregates_calls_cost_tokens_and_latency_per_prompt_version():
    def g(v, ms, err=False):
        return {"type": "generation", "name": "verifier", "prompt_version": v, "model": "sonnet", "start": "2026-10-25",
                "duration_ms": ms, "level": "ERROR" if err else "DEFAULT", "cost_usd": 0.01,
                "usage": {"input_tokens": 100, "output_tokens": 10}}
    rows = trace.ops([g("v1", 100), g("v1", 300, err=True), g("v2", 50), {"type": "span", "name": "x"}])
    v1 = next(r for r in rows if r["prompt_version"] == "v1")
    assert v1["calls"] == 2 and v1["errors"] == 1 and v1["input_tokens"] == 200 and v1["cost_usd"] == 0.02
    assert v1["p50_ms"] == 300 and len(rows) == 2


def test_langfuse_config_prefers_env_then_the_local_stack_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-1\nLANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-1\n")
    monkeypatch.setattr(trace, "LOCAL_LANGFUSE_ENV", env)
    monkeypatch.setattr(trace.local_langfuse, "__defaults__", (env,))
    monkeypatch.delenv("GRINOS_LANGFUSE", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    assert trace.langfuse_config() == ("http://127.0.0.1:3000", "pk-lf-1", "sk-lf-1")
    monkeypatch.setenv("GRINOS_LANGFUSE", "off")
    assert trace.langfuse_config() == (None, None, None)

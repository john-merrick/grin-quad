"""
Hermetic headless agent calls. The agent prompt is the body of agents/<name>.md; its sha256
is the agent's version, recorded on every ledger entry it produces.

Hermetic = no tools, no MCP, no skills, no user settings, empty cwd. Without this each call
loads ~400k tokens of local MCP/skill context.
"""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from grinos import trace
from grinos.schemas import Actor, sha256

AGENTS_DIR = Path(__file__).parents[1] / "agents"
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def prompt_path(name: str) -> Path:
    """agents/<name>.md, unless GRINOS_PROMPT_OVERRIDES ({"agent": "path"}) points at a candidate prompt
    (the improvement loop evaluates candidates this way without touching the live file)."""
    overrides = json.loads(os.environ.get("GRINOS_PROMPT_OVERRIDES") or "{}")
    return Path(overrides[name]) if name in overrides else AGENTS_DIR / f"{name}.md"


def agent_prompt(name: str) -> str:
    text = prompt_path(name).read_text()
    return text.split("---", 2)[2].strip() if text.startswith("---") else text


def agent_actor(name: str, model: str, system_prompt: str | None = None) -> Actor:
    prompt = system_prompt or agent_prompt(name)
    return Actor(kind="agent", name=name, version=sha256(prompt.encode())[:16], model=model)


def call_agent(name: str, message: str, schema: dict, model: str = "sonnet",
               timeout: int = 600, system_prompt: str | None = None) -> tuple[dict | None, dict]:
    """Returns (structured output or None, metadata incl. cost, latency, tokens, raw error, trace ids).
    `system_prompt` overrides the agent file (used by the desk to prepend its preamble).
    Every call is a traced generation: agent, model, prompt version, input, output, tokens, cost."""
    prompt = system_prompt or agent_prompt(name)
    version = sha256(prompt.encode())[:16]
    with trace.span(name, kind="generation", input=message, model=model, prompt_version=version,
                    metadata={"agent": name}) as g:
        out, meta = _run_claude(message, schema, model, timeout, prompt)
        g.end(output=out if out is not None else meta.get("raw_result") or meta.get("stderr"),
              usage=meta.get("usage"), cost_usd=meta.get("cost_usd"),
              level="ERROR" if out is None else None,
              status=None if out is not None else f"no structured output (returncode {meta.get('returncode')})",
              latency_s=meta.get("latency_s"), session=meta.get("session_id"))
        meta |= {"trace_id": g.trace_id, "span_id": g.span_id}
    return out, meta


def _run_claude(message: str, schema: dict, model: str, timeout: int, prompt: str) -> tuple[dict | None, dict]:
    cmd = [
        "claude", "-p", message,
        "--model", model,
        "--system-prompt", prompt,
        "--tools", "",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "project",
        "--disable-slash-commands",
        "--json-schema", json.dumps(schema),
        "--output-format", "json",
    ]
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    meta: dict[str, Any] = {"latency_s": round(time.monotonic() - t0, 1), "returncode": proc.returncode}
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        meta["stderr"] = proc.stderr[-2000:]
        return None, meta
    meta["cost_usd"] = envelope.get("total_cost_usd")
    meta["session_id"] = envelope.get("session_id")
    usage = envelope.get("usage") or {}
    meta["usage"] = {k: usage.get(k) for k in USAGE_KEYS if usage.get(k) is not None}
    out = envelope.get("structured_output")
    if out is None:
        meta["raw_result"] = str(envelope.get("result"))[:2000]
    return out, meta

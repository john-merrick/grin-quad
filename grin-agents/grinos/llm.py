"""
Hermetic headless agent calls. The agent prompt is the body of agents/<name>.md; its sha256
is the agent's version, recorded on every ledger entry it produces.

Hermetic = no tools, no MCP, no skills, no user settings, empty cwd. Without this each call
loads ~400k tokens of local MCP/skill context.
"""
import json
import subprocess
import tempfile
import time
from pathlib import Path

from grinos.schemas import Actor, sha256

AGENTS_DIR = Path(__file__).parents[1] / "agents"


def agent_prompt(name: str) -> str:
    text = (AGENTS_DIR / f"{name}.md").read_text()
    return text.split("---", 2)[2].strip() if text.startswith("---") else text


def agent_actor(name: str, model: str, system_prompt: str | None = None) -> Actor:
    prompt = system_prompt or agent_prompt(name)
    return Actor(kind="agent", name=name, version=sha256(prompt.encode())[:16], model=model)


def call_agent(name: str, message: str, schema: dict, model: str = "sonnet",
               timeout: int = 600, system_prompt: str | None = None) -> tuple[dict | None, dict]:
    """Returns (structured output or None, metadata incl. cost, latency, raw error).
    `system_prompt` overrides the agent file (used by the desk to prepend its preamble)."""
    cmd = [
        "claude", "-p", message,
        "--model", model,
        "--system-prompt", system_prompt or agent_prompt(name),
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
    meta = {"latency_s": round(time.monotonic() - t0, 1), "returncode": proc.returncode}
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        meta["stderr"] = proc.stderr[-2000:]
        return None, meta
    meta["cost_usd"] = envelope.get("total_cost_usd")
    meta["session_id"] = envelope.get("session_id")
    out = envelope.get("structured_output")
    if out is None:
        meta["raw_result"] = str(envelope.get("result"))[:2000]
    return out, meta

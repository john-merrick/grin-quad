"""
Hermetic headless agent calls. Each agent is a profile in agents/<name>.md:

    front matter   model (haiku | sonnet | opus …), grinos_skills (reference packs in skills/packs/),
                   grinos_tools (read-only tools: Read, Grep, Glob)
    body           the prompt

The system prompt is the body, then each skill pack. Its sha256 is the agent's version, recorded on every
ledger entry it produces, and it also covers the tool list when tools are on. A caller passes `model` only
to override the profile.

Hermetic = no MCP, no user skills or settings, a fresh empty cwd. Tools are off unless the profile turns them
on, and then only read-only tools over the files the caller puts in that cwd. Without this each call loads
~400k tokens of local MCP/skill context.

Candidates are evaluated without touching the live files: GRINOS_PROMPT_OVERRIDES ({"agent": path}) swaps the
prompt file, GRINOS_PROFILE_OVERRIDES ({"agent": {"model", "skills", "tools"}}) swaps profile fields.
"""
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from grinos import trace
from grinos.schemas import Actor, sha256

AGENTS_DIR = Path(__file__).parents[1] / "agents"
SKILLS_DIR = Path(__file__).parents[1] / "skills" / "packs"
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
READ_ONLY_TOOLS = ("Read", "Grep", "Glob")
DEFAULT_MODEL = "sonnet"
TOOLS_NOTE = ("\n\n## Tools\n\nYou can read the files in your working directory with Read, Grep and Glob. The "
              "message lists them. Use them when what you were given in the message isn't enough to decide, and "
              "quote what you rely on.")


def prompt_path(name: str) -> Path:
    """agents/<name>.md, unless GRINOS_PROMPT_OVERRIDES ({"agent": "path"}) points at a candidate prompt
    (the improvement loop evaluates candidates this way without touching the live file)."""
    overrides = json.loads(os.environ.get("GRINOS_PROMPT_OVERRIDES") or "{}")
    return Path(overrides[name]) if name in overrides else AGENTS_DIR / f"{name}.md"


def split(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        return yaml.safe_load(fm) or {}, body.strip()
    return {}, text.strip()


@dataclass
class Profile:
    name: str
    model: str
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    body: str = ""
    skill_files: dict[str, str] = field(default_factory=dict)   # name -> path, for drafted packs under evaluation

    @property
    def system(self) -> str:
        """What the agent sees: its prompt, then each skill pack, then the tools note if tools are on."""
        parts = [self.body]
        for s in self.skills:
            parts.append(f"\n\n## Reference: {s}\n\n{skill_text(s, self.skill_files.get(s))}")
        if self.tools:
            parts.append(TOOLS_NOTE)
        return "".join(parts)

    @property
    def version(self) -> str:
        return version_of(self.system, self.tools)


def version_of(system: str, tools: list[str] | tuple = ()) -> str:
    return sha256((system + (f"|tools:{','.join(tools)}" if tools else "")).encode())[:16]


def skill_text(name: str, path: str | None = None) -> str:
    _, body = split(Path(path or SKILLS_DIR / f"{name}.md").read_text())
    return body


def profile(name: str) -> Profile:
    fm, body = split(prompt_path(name).read_text())
    over = json.loads(os.environ.get("GRINOS_PROFILE_OVERRIDES") or "{}").get(name, {})
    tools = [t for t in over.get("tools", fm.get("grinos_tools") or []) if t in READ_ONLY_TOOLS]
    return Profile(name=name, model=over.get("model") or fm.get("model") or DEFAULT_MODEL,
                   skills=list(over.get("skills", fm.get("grinos_skills") or [])), tools=tools, body=body,
                   skill_files=dict(over.get("skill_files") or {}))


def tool_files(name: str, files: dict[str, str]) -> dict:
    """{"files": files} when the agent's profile has tools on (they're only readable then), else {}."""
    return {"files": files} if profile(name).tools else {}


def agent_prompt(name: str) -> str:
    """The agent's full system prompt (prompt body + skill packs + tools note)."""
    return profile(name).system


def agent_actor(name: str, model: str | None = None, system_prompt: str | None = None) -> Actor:
    p = profile(name)
    v = version_of(system_prompt, p.tools) if system_prompt else p.version
    return Actor(kind="agent", name=name, version=v, model=model or p.model)


def call_agent(name: str, message: str, schema: dict, model: str | None = None,
               timeout: int = 600, system_prompt: str | None = None,
               files: dict[str, str] | None = None) -> tuple[dict | None, dict]:
    """Returns (structured output or None, metadata incl. cost, latency, tokens, raw error, trace ids).
    `model` overrides the profile's. `system_prompt` overrides the agent file (the desk prepends its preamble).
    `files` are written into the call's working directory; the agent can read them only if its profile turns
    tools on. Every call is a traced generation: agent, model, prompt version, input, output, tokens, cost."""
    p = profile(name)
    model = model or p.model
    prompt = system_prompt or p.system
    version = version_of(prompt, p.tools)
    if files and p.tools:
        message += "\n\n<files_in_your_working_directory>\n" + "\n".join(sorted(files)) + \
                   "\n</files_in_your_working_directory>"
    with trace.span(name, kind="generation", input=message, model=model, prompt_version=version,
                    metadata={"agent": name, "skills": p.skills, "tools": p.tools}) as g:
        out, meta = _run_claude(message, schema, model, timeout, prompt, p.tools, files if p.tools else None)
        g.end(output=out if out is not None else meta.get("raw_result") or meta.get("stderr"),
              usage=meta.get("usage"), cost_usd=meta.get("cost_usd"),
              level="ERROR" if out is None else None,
              status=None if out is not None else f"no structured output (returncode {meta.get('returncode')})",
              latency_s=meta.get("latency_s"), session=meta.get("session_id"))
        meta |= {"trace_id": g.trace_id, "span_id": g.span_id, "model": model, "prompt_version": version}
    return out, meta


def _run_claude(message: str, schema: dict, model: str, timeout: int, prompt: str,
                tools: list[str] | None = None, files: dict[str, str] | None = None) -> tuple[dict | None, dict]:
    tool_list = ",".join(tools or [])
    cmd = [
        "claude", "-p", message,
        "--model", model,
        "--system-prompt", prompt,
        "--tools", tool_list,
        *(["--allowedTools", tool_list] if tool_list else []),
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "project",
        "--disable-slash-commands",
        "--json-schema", json.dumps(schema),
        "--output-format", "json",
    ]
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as cwd:
        for fname, content in (files or {}).items():
            (Path(cwd) / Path(fname).name).write_text(content)
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    meta: dict[str, Any] = {"latency_s": round(time.monotonic() - t0, 1), "returncode": proc.returncode}
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        meta["stderr"] = proc.stderr[-2000:]
        return None, meta
    meta["cost_usd"] = envelope.get("total_cost_usd")
    meta["session_id"] = envelope.get("session_id")
    meta["num_turns"] = envelope.get("num_turns")
    usage = envelope.get("usage") or {}
    meta["usage"] = {k: usage.get(k) for k in USAGE_KEYS if usage.get(k) is not None}
    out = envelope.get("structured_output")
    if out is None:
        meta["raw_result"] = str(envelope.get("result"))[:2000]
    return out, meta

"""call_agent against a fake `claude` on PATH: metadata, usage and prompt overrides."""
import json
import os
import stat

from grinos import llm

FAKE = """#!/usr/bin/env python3
import json, sys
args = sys.argv
print(json.dumps({"total_cost_usd": 0.01, "session_id": "s1",
                  "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 5, "junk": 1},
                  "structured_output": {"system": args[args.index("--system-prompt") + 1]}}))
"""


def _fake_claude(tmp_path, monkeypatch):
    exe = tmp_path / "claude"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def test_call_agent_records_cost_session_and_token_usage(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch)
    out, meta = llm.call_agent("verifier", "hi", {"type": "object"})
    assert out["system"] == llm.agent_prompt("verifier")
    assert meta["cost_usd"] == 0.01 and meta["session_id"] == "s1"
    assert meta["usage"] == {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 5}


def test_prompt_overrides_point_an_agent_at_a_candidate_file(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch)
    cand = tmp_path / "cand.md"
    cand.write_text("---\nname: verifier\n---\nCANDIDATE PROMPT")
    monkeypatch.setenv("GRINOS_PROMPT_OVERRIDES", json.dumps({"verifier": str(cand)}))
    out, _ = llm.call_agent("verifier", "hi", {"type": "object"})
    assert out["system"] == "CANDIDATE PROMPT"
    assert llm.agent_actor("verifier", "sonnet").version != llm.agent_actor("decision-model", "sonnet").version

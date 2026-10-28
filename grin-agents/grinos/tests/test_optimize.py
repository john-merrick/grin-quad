"""Optimize: variants for every lever, how a variant is run without touching live files, selection, promotion."""
import json

import pytest

from grinos import improve, llm, optimize
from grinos.ledger import Ledger
from grinos.schemas import Actor

BODY = "You verify claims.\n\n- Never verify a guess.\n- Use the quote.\n\nReturn only JSON."
FAIL = [{"id": "sign_case", "quotes": ["Operating loss was $14.2 million"], "result": {"verdict": "verified"}}]


@pytest.fixture
def agents(tmp_path, monkeypatch):
    a, s = tmp_path / "agents", tmp_path / "packs"
    a.mkdir()
    s.mkdir()
    (a / "verifier.md").write_text("---\nname: verifier\nmodel: sonnet\n---\n\n" + BODY + "\n")
    (s / "financial-statements.md").write_text("---\nname: financial-statements\ndescription: 'Tables: scale and signs.'\n"
                                               "agents: [verifier]\n---\n\nParentheses mean negative.\n")
    (s / "other.md").write_text("---\nname: other\ndescription: x\nagents: [orchestrator]\n---\n\nx\n")
    for mod in (llm, optimize, improve):
        monkeypatch.setattr(mod, "AGENTS_DIR", a)
    for mod in (llm, optimize):
        monkeypatch.setattr(mod, "SKILLS_DIR", s)
    monkeypatch.delenv("GRINOS_PROFILE_OVERRIDES", raising=False)
    return a, s


def fake_optimiser(name, message, schema, model=None, **k):
    if "draft" in schema["properties"]:
        return {"draft": {"name": "Sign Conventions", "description": "How signs work in statements.",
                          "content": "A loss is negative; never call it profit."}}, {"cost_usd": 0.01}
    return {"candidates": [{"rationale": "signs", "edits": [{"find": "- Use the quote.",
                                                             "replace": "- Use the quote.\n- Check the sign."}]}]}, {"cost_usd": 0.02}


def test_a_plan_has_a_variant_per_lever_with_a_cost_estimate(agents, tmp_path, monkeypatch):
    monkeypatch.setattr(improve, "failures_from_report", lambda report, limit=12, exclude=None: FAIL)
    L = Ledger(tmp_path / "led")
    plan = optimize.plan(L, "verifier", call=fake_optimiser)
    levers = {(v["lever"], v["label"]) for v in plan["variants"]}
    assert ("model", "switch to haiku") in levers and ("model", "switch to opus") in levers
    assert ("skill", "attach financial-statements") in levers and not any("other" in lbl for _, lbl in levers)
    assert any(lv == "tools" for lv, _ in levers) and any(lv == "prompt" for lv, _ in levers)
    assert any(lbl.startswith("new pack sign-conventions") for _, lbl in levers)
    assert all(v["est_cost_usd"] > 0 and v["id"].startswith("v_") for v in plan["variants"])
    haiku = next(v for v in plan["variants"] if v["label"] == "switch to haiku")
    assert haiku["est_cost_usd"] < plan["baseline_cost_usd"]
    assert plan["total_cost_usd"] == pytest.approx(plan["baseline_cost_usd"] + sum(v["est_cost_usd"] for v in plan["variants"]), abs=0.01)
    assert optimize.plans(L, "verifier")[0]["plan_id"] == plan["plan_id"]


def test_a_variant_runs_through_overrides_never_the_live_files(agents, tmp_path, monkeypatch):
    a, _ = agents
    monkeypatch.setattr(improve, "failures_from_report", lambda report, limit=12, exclude=None: FAIL)
    plan = optimize.plan(Ledger(tmp_path / "led"), "verifier", call=fake_optimiser)
    live = optimize.current_parent("verifier")["version"]
    for v in plan["variants"]:
        env, prompt_path = optimize.variant_env("verifier", v, tmp_path / "rsi")
        monkeypatch.setenv("GRINOS_PROFILE_OVERRIDES", env.get("GRINOS_PROFILE_OVERRIDES", "{}"))
        monkeypatch.setenv("GRINOS_PROMPT_OVERRIDES", json.dumps({"verifier": prompt_path}) if prompt_path else "{}")
        p = llm.profile("verifier")
        if v["lever"] == "model":
            assert p.model == v["profile"]["model"]
        elif v["lever"] == "tools":
            assert p.tools == ["Read", "Grep", "Glob"] and "## Tools" in p.system
        elif v.get("skill_draft"):
            assert "never call it profit" in p.system
        elif v["lever"] == "skill":
            assert "## Reference: financial-statements" in p.system and "Parentheses mean negative" in p.system
        else:
            assert "- Check the sign." in p.system
        assert p.version != live or v["lever"] == "model"
    assert (a / "verifier.md").read_text().count("Check the sign") == 0


def test_cheaper_and_as_good_wins_but_any_regression_loses():
    base = {"passed": True, "per_case": {"a": 1.0, "b": 0.5}, "cost_usd": 1.0}
    regress = {"label": "x", "passed": True, "per_case": {"a": 0.5, "b": 1.0}, "cost_usd": 0.2}
    same_cheaper = {"label": "haiku", "passed": True, "per_case": {"a": 1.0, "b": 0.5}, "cost_usd": 0.3}
    same_pricier = {"label": "opus", "passed": True, "per_case": {"a": 1.0, "b": 0.5}, "cost_usd": 1.6}
    better = {"label": "skill", "passed": True, "per_case": {"a": 1.0, "b": 1.0}, "cost_usd": 1.1}
    assert optimize.select(base, [regress, same_pricier]) is None
    assert optimize.select(base, [same_cheaper])["label"] == "haiku"
    assert optimize.select(base, [same_cheaper, better])["label"] == "skill"           # quality first


def _select(L, v, parent):
    return L.append("optimize:op_test", "prompt_selected", Actor(kind="system", name="t", version="1"),
                    {"promotion_id": "pr_x", "agent": "verifier", "plan_id": "op_test", "parent": parent,
                     "parent_version": parent["version"], "cand_version": v["id"], "variant": v, "prompt_blob": L.put_blob(b""),
                     "baseline": {"per_case": {}}, "candidate": {"per_case": {}}, "rationale": "r",
                     "failing_cases_before": 1, "failing_cases_after": 0})


def test_approving_a_variant_writes_the_profile_and_any_drafted_pack(agents, tmp_path):
    a, s = agents
    L = Ledger(tmp_path / "led")
    v = {"id": "v_1", "lever": "skill", "label": "new pack", "profile": {"skills": ["sign-conventions"], "model": "opus",
                                                                         "tools": ["Read", "Grep", "Glob"]},
         "skill_draft": {"name": "sign-conventions", "description": "Signs: what they mean.", "content": "Losses are negative."}}
    _select(L, v, optimize.current_parent("verifier"))
    improve.decide_promotion(L, "pr_x", "approve", "isaac", "better on signs")
    p = llm.profile("verifier")
    assert p.model == "opus" and p.skills == ["sign-conventions"] and p.tools == ["Read", "Grep", "Glob"]
    assert "Losses are negative." in p.system and p.body == BODY
    assert (s / "sign-conventions.md").exists() and [e["stage"] for e in L.entries()][-1] == "prompt_promoted"


def test_a_variant_planned_on_an_older_profile_is_refused(agents, tmp_path):
    L = Ledger(tmp_path / "led")
    stale = optimize.current_parent("verifier") | {"model": "haiku"}
    _select(L, {"id": "v_2", "lever": "model", "label": "m", "profile": {"model": "opus"}}, stale)
    with pytest.raises(ValueError, match="changed since"):
        improve.decide_promotion(L, "pr_x", "approve", "isaac", "ok")
    assert llm.profile("verifier").model == "sonnet"
    assert not any(e["stage"] == "prompt_promotion_decision" for e in L.entries())

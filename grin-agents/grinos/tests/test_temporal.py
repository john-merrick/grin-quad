"""Workflows against a real local Temporal dev server (the `temporal` CLI), with stub agents.
Skipped when temporalio or the CLI isn't available."""
import asyncio
import contextlib
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

temporalio = pytest.importorskip("temporalio")
TEMPORAL = shutil.which("temporal")
pytestmark = pytest.mark.skipif(not TEMPORAL, reason="temporal CLI not installed")

from temporalio.testing import WorkflowEnvironment  # noqa: E402

from grinos import coverage as C  # noqa: E402
from grinos import decide, sec, verify  # noqa: E402
from grinos.ledger import Ledger  # noqa: E402
from grinos.pipeline import ingest  # noqa: E402
from grinos.temporal import activities as A  # noqa: E402
from grinos.temporal import client as TC  # noqa: E402
from grinos.temporal.worker import workers  # noqa: E402
from grinos.tests import sec_fixture as F  # noqa: E402
from grinos.tests.test_pipeline import FAKE, FLUFF, GOOD, META, SOURCE  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRINOS_HOME", str(tmp_path))
    calls = []

    def fake(name, message, schema, model="sonnet", timeout=600, **k):
        calls.append(name)
        if name == "decision-model":
            discard = "milestone" in message
            return ({"action": "discard" if discard else "verify", "nature": "noise" if discard else "not_applicable",
                     "materiality": "low", "rationale": "stub"}, {"cost_usd": 0})
        return ({"verdict": "verified", "reasons": [{"code": "supported", "detail": "stub"}]}, {"cost_usd": 0})
    monkeypatch.setattr(decide, "call_agent", fake)
    monkeypatch.setattr(verify, "call_agent", fake)
    return tmp_path, calls


async def _with_worker(fn):
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL) as env:
        with ThreadPoolExecutor(16) as ex:
            async with contextlib.AsyncExitStack() as stack:
                for w in workers(env.client, ex):
                    await stack.enter_async_context(w)
                return await fn(env.client)


def _ingest_args(L, run_id):
    meta = {k: str(v) for k, v in META.items()}
    return {"run_id": run_id, "blob": L.put_blob(SOURCE), "meta": meta, "as_of": "2026-03-01",
            "claims": [GOOD, FAKE, FLUFF]}


def test_ingest_workflow_matches_the_inline_pipeline(home, tmp_path):
    root, calls = home
    L = Ledger(root)
    inline = ingest(Ledger(tmp_path / "inline"), SOURCE, META, [GOOD, FAKE, FLUFF], date(2026, 3, 1), run_id="run_x")

    async def go(client):
        return await client.execute_workflow("Ingest", _ingest_args(L, "run_x"), id="ingest:run_x",
                                             task_queue="grin-workflows")
    out = asyncio.run(_with_worker(go))
    assert out == inline and L.audit() == []
    stages = [e["stage"] for e in L.entries()]
    assert stages.count("claim") == 3 and stages.count("verdict") == 2 and "notification" in stages


def test_a_failing_agent_activity_is_retried_and_writes_once(home, monkeypatch):
    root, calls = home
    L = Ledger(root)
    real, n = verify.call_agent, {"fails": 0}

    def flaky(*a, **k):
        if n["fails"] == 0:
            n["fails"] += 1
            raise RuntimeError("worker hiccup")
        return real(*a, **k)
    monkeypatch.setattr(verify, "call_agent", flaky)

    async def go(client):
        return await client.execute_workflow("Ingest", _ingest_args(L, "run_r"), id="ingest:run_r",
                                             task_queue="grin-workflows")
    out = asyncio.run(_with_worker(go))
    assert n["fails"] == 1 and {c["verdict"] for c in out["claims"]} >= {"verified"}
    stages = [e["stage"] for e in L.entries() if e["run_id"] == "run_r"]
    assert stages.count("verdict") == 2 and stages.count("decision") == 3 and L.audit() == []


def test_coverage_workflow_waits_for_you_and_wakes_on_the_ledger_signal(home, monkeypatch, tmp_path):
    root, _ = home
    monkeypatch.setattr(sec, "http_get", sec.fixture_getter(F.responses()))
    from grinos.tests.test_coverage import ACME, AS_OF
    cfg = tmp_path / "acme.yaml"
    cfg.write_text(ACME.read_text())
    L = Ledger(root)

    async def go(client):
        h = await client.start_workflow("Coverage", {"ticker": "ACME", "as_of": AS_OF.isoformat(),
                                                     "config_path": str(cfg), "who": "isaac", "wait_hours": 1},
                                        id="coverage:ACME:test", task_queue="grin-workflows")
        for _ in range(100):                                  # until the run exists and blocks on sources
            runs = C.runs(L)
            if runs and runs[-1]["status"] == "blocked":
                break
            await asyncio.sleep(0.1)
        run = C.runs(L)[-1]["run_id"]
        C.decide(L, run, "s_confirm", "accept", "isaac", "")
        await h.signal("ledger_changed")
        for _ in range(100):
            if C.runs(L)[-1]["step"] == "drivers":
                break
            await asyncio.sleep(0.1)
        await h.cancel()
        return run
    run = asyncio.run(_with_worker(go))
    assert C.runs(L)[-1]["step"] == "drivers"
    notes = [e["payload"]["text"] for e in L.entries() if e["stage"] == "notification"]
    assert any("at sources" in t for t in notes) and any("at drivers" in t for t in notes)
    assert any(e["stage"] == "workflow_link" and e["payload"]["run_id"] == run for e in L.entries())


def test_without_a_server_work_runs_inline_and_says_so(home, monkeypatch):
    root, _ = home
    monkeypatch.setattr(TC, "ADDRESS", "127.0.0.1:1")
    L = Ledger(root)
    out = TC.run_or_inline(L, "Ingest", {}, "ingest:none", inline=lambda: {"done": True}, run_id="run_i")
    assert out["via"] == "inline" and out["result"] == {"done": True}
    (e,) = [e for e in L.entries() if e["stage"] == "execution"]
    assert e["payload"]["mode"] == "inline" and "not reachable" in e["payload"]["reason"]
    monkeypatch.setenv("GRINOS_TEMPORAL", "required")
    with pytest.raises(TC.TemporalUnavailable):
        TC.run_or_inline(L, "Ingest", {}, "ingest:none", inline=lambda: None)
    assert A.ALL


def test_improvement_loop_selects_only_a_no_regression_winner_and_waits_for_approval(home, tmp_path, monkeypatch):
    import json

    from temporalio import activity

    from grinos import improve
    root, _ = home
    L = Ledger(root)
    agents = tmp_path / "agents"
    agents.mkdir()
    body = "You verify claims.\n\n- Never verify a guess.\n- Use the quote.\n\nReturn only JSON."
    (agents / "verifier.md").write_text("---\nname: verifier\n---\n\n" + body + "\n")
    monkeypatch.setattr(improve, "AGENTS_DIR", agents)
    evals = tmp_path / "evals"
    (evals / "verifier" / "results" / "20261025-000000_agent_sonnet").mkdir(parents=True)
    (evals / "verifier" / "results" / "20261025-000000_agent_sonnet" / "report.json").write_text(json.dumps(
        {"summary": {"passed": True}, "rows": [{"id": "sign_case", "case_pass": False, "verdict": "verified"}]}))
    monkeypatch.setattr(A, "ROOT", tmp_path)
    monkeypatch.setattr(improve, "call_agent", lambda *a, **k: ({"candidates": [
        {"rationale": "add a sign rule", "edits": [{"find": "- Use the quote.", "replace": "- Use the quote.\n- Check the sign."}]},
        {"rationale": "drops a rule", "edits": [{"find": "- Never verify a guess.\n", "replace": ""}]}]}, {"cost_usd": 0.01}))

    @activity.defn(name="run_eval")
    def fake_eval(a: dict) -> dict:
        better = bool(a.get("prompt_path"))
        out = tmp_path / f"rep_{'cand' if better else 'base'}.json"
        out.write_text(json.dumps({"summary": {"passed": True, "cost_usd": 0.02, "recall": 1.0 if better else 0.5},
                                   "rows": [{"id": "sign_case", "case_pass": better}, {"id": "ok_case", "case_pass": True}]}))
        return {"returncode": 0, "report_path": str(out), "summary": {}}

    @activity.defn(name="refresh_status")
    def fake_refresh(agent: str) -> dict:
        return {"returncode": 0}
    monkeypatch.setattr(A, "run_eval", fake_eval)
    monkeypatch.setattr(A, "refresh_status", fake_refresh)

    async def go(client):
        h = await client.start_workflow("Improvement", {"agent": "verifier", "eval_dir": "verifier", "model": "sonnet",
                                                        "repeats": 1, "candidates": 3, "budget_usd": 5, "eval_cost_usd": 0.1},
                                        id="improve:verifier:test", task_queue="grin-workflows")
        for _ in range(150):
            if improve.promotions(L):
                break
            await asyncio.sleep(0.1)
        (p,) = improve.promotions(L)
        assert p["failing_cases_before"] == 1 and p["failing_cases_after"] == 0
        assert "- Check the sign." in L.get_blob(p["prompt_blob"]).decode()
        assert "- Check the sign." not in (agents / "verifier.md").read_text()          # nothing live yet
        improve.decide_promotion(L, p["promotion_id"], "approve", "isaac", "sign handling was the gap")
        await h.signal("ledger_changed")
        return await h.result()
    out = asyncio.run(_with_worker(go))
    assert out["result"] == "approve" and "- Check the sign." in (agents / "verifier.md").read_text()
    cands = [e["payload"] for e in L.entries() if e["stage"] == "prompt_candidate"]
    assert len(cands) == 2 and any(c["problems"] for c in cands)                       # the rule-dropper was refused
    assert [e["stage"] for e in L.entries()].count("candidate_eval") == 2 and L.audit() == []


def test_optimize_workflow_scores_chosen_variants_and_applies_the_approved_winner(home, tmp_path, monkeypatch):
    import json

    from temporalio import activity

    from grinos import improve, llm, optimize
    from grinos.tests.test_optimize import BODY, FAIL, fake_optimiser
    root, _ = home
    L = Ledger(root)
    a, s = tmp_path / "agents", tmp_path / "packs"
    a.mkdir()
    s.mkdir()
    (a / "verifier.md").write_text("---\nname: verifier\nmodel: sonnet\n---\n\n" + BODY + "\n")
    for mod in (llm, optimize, improve):
        monkeypatch.setattr(mod, "AGENTS_DIR", a)
    for mod in (llm, optimize):
        monkeypatch.setattr(mod, "SKILLS_DIR", s)
    monkeypatch.setattr(improve, "failures_from_report", lambda report, limit=12, exclude=None: FAIL)
    plan = optimize.plan(L, "verifier", call=fake_optimiser)
    haiku = next(v for v in plan["variants"] if v["label"] == "switch to haiku")
    opus = next(v for v in plan["variants"] if v["label"] == "switch to opus")

    @activity.defn(name="run_eval")
    def fake_eval(x: dict) -> dict:
        model = x["model"]
        out = tmp_path / f"rep_{model}_{len(list(tmp_path.glob('rep_*')))}.json"
        cost = {"haiku": 0.1, "sonnet": 0.3, "opus": 0.5}[model]
        out.write_text(json.dumps({"summary": {"passed": True, "cost_usd": cost},
                                   "rows": [{"id": "c1", "case_pass": True}, {"id": "c2", "case_pass": model != "opus"}]}))
        return {"returncode": 0, "report_path": str(out)}

    @activity.defn(name="refresh_status")
    def fake_refresh(agent: str) -> dict:
        return {}
    monkeypatch.setattr(A, "run_eval", fake_eval)
    monkeypatch.setattr(A, "refresh_status", fake_refresh)

    async def go(client):
        h = await client.start_workflow("Optimize", {"plan_id": plan["plan_id"], "agent": "verifier", "eval_dir": "verifier",
                                                     "repeats": 1, "variant_ids": [haiku["id"], opus["id"]]},
                                        id=f"optimize:{plan['plan_id']}", task_queue="grin-workflows")
        for _ in range(150):
            if improve.promotions(L):
                break
            await asyncio.sleep(0.1)
        (p,) = improve.promotions(L)
        assert p["variant"]["label"] == "switch to haiku" and p["cost_after"] < p["cost_before"]  # as good, a third the cost
        assert llm.profile("verifier").model == "sonnet"                                           # nothing live yet
        improve.decide_promotion(L, p["promotion_id"], "approve", "isaac", "same quality, cheaper")
        await h.signal("ledger_changed")
        return await h.result()
    out = asyncio.run(_with_worker(go))
    assert out["result"] == "approve" and llm.profile("verifier").model == "haiku"
    assert [e["stage"] for e in L.entries()].count("candidate_eval") == 3 and L.audit() == []


def test_heartbeat_reruns_evals_and_plans_only_for_agents_that_regressed(home, monkeypatch):
    from temporalio import activity

    from grinos import evalwatch, optimize
    root, _ = home
    L = Ledger(root)
    ran = []

    @activity.defn(name="refresh_status")
    def fake_refresh(agent: str) -> dict:
        ran.append(agent)
        return {}
    monkeypatch.setattr(A, "refresh_status", fake_refresh)
    monkeypatch.setattr(evalwatch, "compare", lambda agent: {"agent": agent, "status": "regressed" if agent == "verifier" else "steady",
                                                             "regressed_gates": [], "new_failures": ["c2"], "worse_metrics": {},
                                                             "flaky": []})
    monkeypatch.setattr(optimize, "plan", lambda ledger, agent, call=None: {"plan_id": "op_0123456789", "variants": [1, 2],
                                                                          "total_cost_usd": 1.2})

    async def go(client):
        return await client.execute_workflow("Heartbeat", {"agents": ["verifier", "decision-model"]},
                                             id="heartbeat:test", task_queue="grin-workflows")
    out = asyncio.run(_with_worker(go))
    assert ran == ["verifier", "decision-model"] and out["flagged"] == ["verifier"]
    note = next(e["payload"]["text"] for e in L.entries() if e["stage"] == "notification")
    assert "⚠ verifier: 1 new failing case(s): c2" in note and "op_0123456789" in note and "decision-model: steady" in note
    assert sum(e["stage"] == "eval_heartbeat" for e in L.entries()) == 2

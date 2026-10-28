"""
Optimize an agent: try every lever, score each variant on the agent's eval next to a fresh baseline, and put
the best one to Isaac. Nothing runs until he has seen the plan and its estimated cost, and nothing goes live
until he approves the winner.

Levers (each variant changes one):
  prompt  edits from the prompt-optimiser (the same barriers as grinos.improve)
  model   the same prompt on another model (better, or as good and cheaper)
  skill   a reference pack attached: from skills/packs (if the pack says it suits this agent), or a new pack the
          optimiser drafted from the failures (leak and size checked)
  tools   read-only tools over the files the caller provides (only agents whose callers provide files)

Selection (deterministic): a variant must pass every gate and do at least as well as the current agent on every
case; then the winner has the fewest failing cases, then the lowest cost, then the smallest change.

Ledger stages: optimize_plan, candidate_eval (shared), prompt_selected | no_winner (shared, with a `variant`),
prompt_promotion_decision, prompt_promoted (grinos.improve).
"""
import json
import re
from pathlib import Path

import yaml

from grinos import improve
from grinos.ledger import Ledger
from grinos.llm import AGENTS_DIR, READ_ONLY_TOOLS, SKILLS_DIR, profile, split
from grinos.schemas import Actor, canonical, sha256

SYSTEM = Actor(kind="system", name="optimize", version="1")
MODELS = ["haiku", "sonnet", "opus"]
# Rough relative cost per eval run vs Sonnet, used only for the estimate shown before anything runs.
MODEL_COST = {"haiku": 0.33, "sonnet": 1.0, "opus": 1.67}
TOOLS_COST = 2.0                       # tool use adds turns
TOOL_AGENTS = {"verifier", "mid-cycle-analyst"}  # their callers put files in the agent's working directory
PLANNING_COST_USD = 0.05
CHEAPER_BY = 0.15                      # "as good and cheaper" means at least 15% cheaper
MAX_SKILL_CHARS = 6000


def library() -> list[dict]:
    out = []
    for p in sorted(SKILLS_DIR.glob("*.md")):
        fm, body = split(p.read_text())
        out.append({"name": p.stem, "description": fm.get("description", ""), "agents": fm.get("agents") or [],
                    "chars": len(body)})
    return out


def _eval_cost(agent: str, model: str, tools: bool) -> float:
    status = json.loads((Path(__file__).parents[1] / "evals" / "STATUS.json").read_text())
    s = status.get(agent) or {}
    base = (s.get("cost_usd") or 0.3) / MODEL_COST.get(s.get("model") or "sonnet", 1.0)
    return round(base * MODEL_COST.get(model, 1.0) * (TOOLS_COST if tools else 1.0), 3)


def _vid(v: dict) -> str:
    return "v_" + sha256(canonical({k: v.get(k) for k in ("lever", "profile", "prompt_version", "skill_draft")}).encode())[:8]


def plan(ledger: Ledger, agent: str, call=None) -> dict:
    """Every variant worth trying, each with an estimated eval cost. Calls the prompt-optimiser once (prompt edits
    and a drafted skill pack); runs no evals. Recorded as `optimize_plan`."""
    p = profile(agent)
    fm, body, _ = improve.current(agent)
    sys_path = Path(__file__).parents[1] / "evals"
    import sys
    sys.path.insert(0, str(sys_path))
    import common
    from run_all import EVALS
    ev, repeats = EVALS[agent]
    report = common.latest_report(sys_path / ev) or {}
    failures = improve.failures_from_report(report, exclude=improve.holdout_ids(agent)) + improve.corrections(ledger, agent)
    parent = {"version": p.version, "model": p.model, "skills": p.skills, "tools": p.tools}
    variants: list[dict] = []

    for m in MODELS:
        if m != p.model:
            variants.append({"lever": "model", "label": f"switch to {m}", "profile": {"model": m},
                             "rationale": f"Same prompt on {m}: better results, or as good for less.",
                             "est_cost_usd": _eval_cost(agent, m, bool(p.tools))})
    for pack in library():
        if agent in pack["agents"] and pack["name"] not in p.skills:
            variants.append({"lever": "skill", "label": f"attach {pack['name']}",
                             "profile": {"skills": p.skills + [pack["name"]]}, "rationale": pack["description"],
                             "est_cost_usd": _eval_cost(agent, p.model, bool(p.tools))})
    if agent in TOOL_AGENTS and not p.tools:
        variants.append({"lever": "tools", "label": "read-only tools (Read, Grep, Glob) over the full source",
                         "profile": {"tools": list(READ_ONLY_TOOLS)},
                         "rationale": "Lets the agent read the whole document or evidence file, not just the excerpt.",
                         "est_cost_usd": _eval_cost(agent, p.model, True)})
    optimiser_meta: dict = {}
    if failures:
        cands, optimiser_meta = improve.propose(agent, body, failures, call=call)
        for c in cands:
            if c["body"] and not c["problems"]:
                variants.append({"lever": "prompt", "label": f"prompt edit ({len(c['edits'])} change(s))",
                                 "prompt_body": c["body"], "prompt_version": c["version"], "edits": c["edits"],
                                 "diff_size": improve.diff_size(body, c["body"]), "rationale": c["rationale"],
                                 "est_cost_usd": _eval_cost(agent, p.model, bool(p.tools))})
        draft = draft_skill(agent, failures, call=call)
        if draft:
            variants.append({"lever": "skill", "label": f"new pack {draft['name']} (drafted from the failures)",
                             "skill_draft": draft, "profile": {"skills": p.skills + [draft["name"]]},
                             "rationale": draft["description"], "est_cost_usd": _eval_cost(agent, p.model, bool(p.tools))})
    for v in variants:
        v["id"] = _vid(v)
    baseline_cost = _eval_cost(agent, p.model, bool(p.tools))
    plan_id = "op_" + sha256(canonical({"agent": agent, "parent": parent, "variants": [v["id"] for v in variants]}).encode())[:10]
    out = {"plan_id": plan_id, "agent": agent, "eval_dir": ev, "repeats": repeats, "parent": parent,
           "failures": len(failures), "variants": variants, "baseline_cost_usd": baseline_cost,
           "planning_cost_usd": (optimiser_meta.get("cost_usd") or 0) if failures else 0,
           "total_cost_usd": round(baseline_cost + sum(v["est_cost_usd"] for v in variants), 2)}
    ledger.append_once(plan_id, "optimize_plan", SYSTEM, out, idem=f"{plan_id}:plan")
    return out


SKILL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["draft"],
                "properties": {"draft": {"type": ["object", "null"], "additionalProperties": False,
                                         "required": ["name", "description", "content"],
                                         "properties": {"name": {"type": "string"}, "description": {"type": "string"},
                                                        "content": {"type": "string"}}}}}


def draft_skill(agent: str, failures: list[dict], call=None) -> dict | None:
    """A reference pack the optimiser drafts from the failures: general knowledge, never the cases. Checked here."""
    from grinos.llm import call_agent
    from grinos.schemas import fence
    msg = (f"<agent>\n{agent}\n</agent>\n\n<failures>\n{fence(json.dumps(failures, indent=2, default=str))}\n</failures>\n\n"
           "Draft one reference pack (general domain knowledge the agent lacked) that would have prevented these "
           "failures, or return null if knowledge isn't the problem. Same rules: no case ids, quotes, companies "
           "or expected answers; under 3,000 characters; plain markdown.")
    out, _ = (call or call_agent)("prompt-optimiser", msg, SKILL_SCHEMA)
    d = (out or {}).get("draft")
    if not d:
        return None
    name = re.sub(r"[^a-z0-9-]+", "-", d["name"].lower()).strip("-")[:40] or "drafted-pack"
    leaks = improve.leak_tokens(failures)
    if any(t in d["content"] for t in leaks) or len(d["content"]) > MAX_SKILL_CHARS or (SKILLS_DIR / f"{name}.md").exists():
        return None
    return {"name": name, "description": d["description"][:200], "content": d["content"]}


def pack_text(d: dict, agent: str) -> str:
    fm = yaml.safe_dump({"name": d["name"], "description": d["description"], "agents": [agent]}, sort_keys=False, width=10000)
    return f"---\n{fm}---\n\n{d['content']}\n"


# --- running a variant ------------------------------------------------------------------------------

def variant_env(agent: str, v: dict, folder: Path) -> tuple[dict, str | None]:
    """(environment overrides, prompt file) that make the agent behave as this variant, without touching live files."""
    env: dict[str, str] = {}
    prompt_path = None
    fm, _, _ = improve.current(agent)
    if v.get("prompt_body"):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{v['id']}.md"
        path.write_text(f"{fm}\n\n{v['prompt_body']}\n")
        prompt_path = str(path)
    override = dict(v.get("profile") or {})
    if v.get("skill_draft"):
        folder.mkdir(parents=True, exist_ok=True)
        sp = folder / f"{v['skill_draft']['name']}.md"
        sp.write_text(pack_text(v["skill_draft"], agent))
        override["skill_files"] = {v["skill_draft"]["name"]: str(sp)}
    if override:
        env["GRINOS_PROFILE_OVERRIDES"] = json.dumps({agent: override})
    return env, prompt_path


def select(baseline: dict, results: list[dict]) -> dict | None:
    """No case worse and every gate passed; then fewest failing cases, lowest cost, smallest change.
    "As good and at least 15% cheaper" counts as better (a model downgrade can win)."""
    def fails(r):
        return sum(1 - v for v in r["per_case"].values())
    winners = []
    for r in results:
        if not r.get("passed"):
            continue
        cases = set(baseline["per_case"]) | set(r["per_case"])
        if any(r["per_case"].get(c, 0.0) < baseline["per_case"].get(c, 0.0) for c in cases):
            continue
        better = fails(r) < fails(baseline)
        cheaper = fails(r) <= fails(baseline) and (r.get("cost_usd") or 0) <= (1 - CHEAPER_BY) * (baseline.get("cost_usd") or 0)
        if better or cheaper or not baseline.get("passed"):
            winners.append(r)
    return min(winners, key=lambda r: (fails(r), r.get("cost_usd") or 0, r.get("diff_size") or 0)) if winners else None


# --- promotion (called by grinos.improve.decide_promotion for variants) --------------------------------

def current_parent(agent: str) -> dict:
    p = profile(agent)
    return {"version": p.version, "model": p.model, "skills": p.skills, "tools": p.tools}


def apply_variant(ledger: Ledger, sel: dict, who: str) -> dict:
    """Write the approved variant into the agent's profile: prompt body, model, skills, tools; a drafted pack into
    skills/packs. Refused if the agent changed since the plan was made."""
    agent, v = sel["agent"], sel["variant"]
    if current_parent(agent) != sel["parent"]:
        raise ValueError(f"{agent} changed since this was planned: plan again")
    path = AGENTS_DIR / f"{agent}.md"
    fm_text, body = improve.split_prompt(path.read_text())
    fm = yaml.safe_load(fm_text.strip("-")) if fm_text else {}
    prof = v.get("profile") or {}
    if "model" in prof:
        fm["model"] = prof["model"]
    if "skills" in prof:
        fm["grinos_skills"] = prof["skills"]
    if "tools" in prof:
        fm["grinos_tools"] = prof["tools"]
    if v.get("prompt_body"):
        body = v["prompt_body"]
    if v.get("skill_draft"):
        d = v["skill_draft"]
        (SKILLS_DIR / f"{d['name']}.md").write_text(pack_text(d, agent))
    path.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, width=10000).strip() + "\n---\n\n" + body + "\n")
    return ledger.append(sel["run_id"], "prompt_promoted", SYSTEM,
                         {"agent": agent, "variant": v["id"], "lever": v["lever"], "from": sel["parent"],
                          "to": current_parent(agent), "approved_by": who,
                          "next": f"re-run `python evals/run_all.py --only {agent}` and commit agents/{agent}.md"})


def plans(ledger: Ledger, agent: str | None = None) -> list[dict]:
    return [e["payload"] | {"ts": e["ts"]} for e in ledger.entries()
            if e["stage"] == "optimize_plan" and (agent is None or e["payload"]["agent"] == agent)]

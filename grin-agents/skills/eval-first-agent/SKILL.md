---
name: eval-first-agent
description: The process for adding any new agent, skill or tool to grin-agents (valuation / macro engine helpers). Use when asked to build, add or change an agent. Evals come first, before the prompt is written.
---

# Eval-first agent development

No agent prompt is written until its eval exists and has been shown to discriminate.
`grin-agents/evals/config-auditor/` is the reference implementation: copy its shape.

## 1. Contract

- One job, one sentence. Say what the agent must **not** do too (e.g. "never edits the file").
- Write the output JSON schema (`evals/<agent>/schema.json`). Structured output is what makes
  grading deterministic.
- Split the job into a **deterministic layer** (arithmetic, rules → `tools/`, unit tested) and
  a **judgement layer** (reading comments, context, plausibility → the agent). Anything that
  can be a rule should be a rule.

## 2. Eval set (`build_cases.py` → `cases/` + `manifest.json`)

- One clean, hand-verified base fixture. **Recompute every number in its comments.** Twice,
  "clean" fixtures had real inconsistencies that the models caught and the author didn't.
- Mutations: base + exactly one substitution → one planted defect with labelled field(s).
- Four kinds: `clean` (precision), `tool` (rule-catchable), `judgment` (only a reader can catch),
  `real` (snapshots of real configs with hand-verified defects, recall only).
- Include adversarial clean cases: justified deviations and flags that are already handled.

## 3. Grader (`grade.py`) — deterministic, no LLM judge

- Match on field names, count only medium/high severity, treat errors as misses.
- Gates in code (`GATES`). Tool recall gate = 100%, because the agent is handed those checks.

## 4. Eval the eval (`tests/test_eval_harness.py`, offline, free)

- A perfect oracle passes every gate. A silent system fails.
- The tools-only baseline catches every `tool` case and **no** `judgment` case. If it catches
  one, that case is mislabelled.

## 5. Baseline, then agent

1. `run.py --system tools-only` must FAIL the gate. If it passes, the agent adds nothing.
2. Write the agent prompt (`agents/<name>.md`): engine facts it needs, procedure, calibration.
3. `run.py --system agent --repeats 3` on two model tiers. Read the false alarms: is the agent
   wrong or the fixture wrong?
4. If everything is 100%, the eval is saturated. Add harder cases before claiming quality.

## 6. Run hermetically

Headless `claude -p` with `--tools "" --strict-mcp-config --mcp-config '{"mcpServers":{}}'
--setting-sources project --disable-slash-commands --system-prompt ...` from an empty cwd.
Without this, each call loads ~400k tokens of MCP and skills context (~$0.40 for a trivial call).

## 7. Run it on real output before calling it done

Synthetic evals saturate. The first real end-to-end run found three false rejections that no
eval case covered: segment figures, adjusted figures, and a days-of-sales DIO. Run the full
pipeline on a real source, read every rejection, and turn each surprise into an eval case.

## 8. Record

Append the summary line to `evals/<agent>/BASELINE.md` with date, model, git SHA and cost.
Any prompt change re-runs the eval, and gates must still pass.

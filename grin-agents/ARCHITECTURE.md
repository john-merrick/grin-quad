# Grin OS — architecture

An operating system for the valuation process: agents you can talk to (Telegram, voice,
a multi-agent voice room), where **every piece of evidence is triaged, then verified, before
any analyst agent may use it**, and every step is auditable on disk. Notion receives only the
finished, decision-ready outputs.

## Principles

1. **The filesystem is the system of record.** Audit, verification, intermediate work and runs
   live in `var/`. Notion holds only finished outputs for the investment decision.
2. **Nothing reaches an analyst agent unverified.** The gate is enforced in code, not by prompt.
3. **Tamper-evident.** Sources are content-addressed (sha256). The ledger is append-only and
   hash-chained, so `grinos audit` detects any edited, reordered or deleted entry.
4. **Replayable.** Every ledger entry records the actor: agent name, prompt hash, model, and
   tool version. Any output can be traced to the exact inputs and prompt that produced it.
5. **Deterministic first.** Rules and arithmetic run before any LLM, and both are recorded.
6. **Eval first.** No stage ships without an eval that fails a do-nothing baseline
   (`skills/eval-first-agent`).
7. **Three layers, one truth.**
   - **The ledger** is truth.
   - **Temporal** is execution: durable, retryable, visible.
   - **Traces** are observation: `var/traces`, plus Langfuse.

   A lost signal or a dead worker never loses work. Every ledger write is idempotent
   (`append_once`), and workflows re-read the ledger.
8. **One counterpart, human judgement kept human.** Isaac talks to the orchestrator only.
   Approvals, disputes, coverage decisions, risks, stages, Notion goes and prompt promotions are
   always his own commands.

## Evidence pipeline

```
source (10-K, transcript, broker note, call)
   │  stored as blob: var/store/blobs/<sha256>   (immutable)
   ▼
claims  ── metric, period, value, unit, verbatim quote, statement
   │
   ▼
1. DECISION MODEL   rules: metric → valuation lever (mid-cycle EBIT parts | multiple via
   │                duration/stability | bridge | macro);  agent: structural vs cyclical vs
   │                noise, materiality, direction. Discards noise → logged, not lost.
   ▼
2. VERIFIER         rules: quote verbatim in source bytes, value present in quote (unit-aware),
   │                published_at ≤ as_of (no lookahead);  agent: does the quote actually
   │                support the claim (segment vs total, adjusted vs GAAP, guidance vs actual,
   │                wrong period). Verdict: verified | rejected | needs_human.
   ▼
3. ANALYST AGENTS   read ONLY verified claims via ledger.verified_claims(). Every proposed
   │                assumption change must cite claim ids; a post-check rejects any uncited
   │                or unverified citation.
   ▼
4. HUMAN APPROVAL   you approve or reject in Telegram or the dashboard → ledger entry
   ▼
5. NOTION PUBLISH   finished output only: Valuation Log row (Author=Agent, Old/New Value,
                    Rationale Note → Research Notes page). Row stores the ledger entry hash.
```

## Layout

```
grinos/            the OS (python package)
  ledger.py        blob store + hash-chained append-only ledger + audit
  schemas.py       Source, Claim, Decision, Verification, actor records
  evidence_check.py  deterministic verifier rules
  levers.py        deterministic decision rules (metric → valuation lever)
  llm.py           hermetic headless agent call, recorded to the ledger
  pipeline.py      source + claims → decision → verifier → ledger
  cli.py           grinos ingest | runs | show | audit
agents/            agent prompts (Claude Code subagent format)
evals/<agent>/     cases, deterministic grader, gates, runner, harness tests
var/               runtime data, gitignored: store/blobs, ledger.jsonl
```

## Phases (each gated by its evals)

| # | phase | delivers | status |
|---|---|---|---|
| 0 | config-auditor | first eval-first agent | done; Sonnet + Haiku pass |
| 1 | audit spine | ledger, blob store, decision model, verifier, `grinos` CLI | done; verifier 0 false accepts |
| 2 | analyst + publish | claim extractor, mid-cycle analyst, proposal_check (engine impact), approval, Notion outbox/push, Agent Registry | done; analyst passes on Sonnet (fails on Haiku) |
| 3 | runs dashboard | local web UI: pending, runs, trails, proposals, evals, desk chat; approve/reject/resolve/publish | done |
| 4 | Telegram | one allowlisted bot, desk routing, group conference, /commands, document ingest | done; needs your bot token |
| 5 | voice | VoiceStudio TTS/STT client, voice notes in Telegram, a voice per agent | done; needs VoiceStudio running |
| 6 | voice room | browser push-to-talk room, agents answer in turn in their own voices, text fallback | done; needs VoiceStudio running |
| 7 | hardening | security review fixes: keyed ledger, head anchor, append_if, injection fencing, command neutralising, CSP | done |
| 8 | history + research graph | `tools/edgar.py` + `bulk_ingest.py` (3 years of POOL/FAST/GWW earnings releases); `grinos/graph.py`, a rebuildable SQLite projection of the ledger; `/why` | done |

| 9 | fundamental valuation | `distval/fcff.py` (FCFF/WACC math), `grinos/sec.py`, `xbrl.py`, `valuation.py`, `valuation_memo.py`, `narrative.py` + memo-writer agent; CLI, desk, dashboard, graph | done; memo-writer passes on Sonnet |
| 10 | foundations | `Ledger.append_once`; stable analysis, proposal and ingest ids; token usage; per-case eval results (`evals/common.py`) | done |
| 11 | evidence review | `evidence_check.locate` (exact raw spans); `grinos/review.py`; the review pop-up; the review gate on every channel; perspectives as analyst context | done; analyst eval re-run with perspective cases |
| 12 | tracing | `grinos/trace.py` (local JSONL + Langfuse ingestion API); spans on agent calls, ingest, steps, turns, activities; eval runs as traces, plus `history.jsonl`; Ops tab | done |
| 13 | coverage | `grinos/steps.py` (shared step machine); `drivers.py` (XBRL → distval drivers with provenance); `coverage.py`; LongGamma sync and risks/catalysts contracts; graph drivers and risks; Coverage tab | done |
| 14 | Temporal | `grinos/temporal/` (activities on per-agent queues, workflows, worker, client); `runtime.py`; inline fallback recorded | done; workflow tests on the local dev server |
| 15 | orchestrator | `agents/orchestrator.md`, `grinos/orchestrator.py`; every channel's free text; replaces desk-router | done; orchestrator passes on Sonnet |
| 16 | improvement loop | `agents/prompt-optimiser.md`, `grinos/improve.py`, the Improvement workflow and weekly schedules; promotions in the Evals tab, `/promote` | done; prompt-optimiser passes on Sonnet |

## Ledger stages added in phases 10–16

| stage | actor | what |
|---|---|---|
| `evidence_review` | human | confirm or dispute one cited claim, in the context of a proposal (first is final) |
| `feedback` | human | a perspective, correction, dispute or praise on any target; perspectives reach the analyst, corrections reach the improvement loop |
| `analysis_output` | agent | the analyst's raw output, recorded once so a retry never re-asks |
| `coverage_request`, `coverage_step`, `coverage_status`, `coverage_decision` | human / rule | the coverage run (grinos.steps); a revision is a new decision with `revises` |
| `risk`, `catalyst`, `coverage_stage`, `config_applied` | human | Isaac's risks, catalysts and stage moves; the YAML written from a coverage run |
| `workflow_started`, `workflow_link`, `execution` | system | which workflow drives which run; work that ran inline because Temporal was down |
| `notification` | system | what a workflow told Isaac, once (idempotent) |
| `orchestrator_plan` | agent | each turn's plan, what ran, what was dropped, any figures removed |
| `improvement_started`, `prompt_candidate`, `candidate_eval`, `prompt_selected`, `no_winner` | system / agent | one round of the improvement loop |
| `prompt_promotion_decision`, `prompt_promoted` | human / system | Isaac's decision; the prompt written |

## Fundamental valuation workflow

Built into the existing system. Nothing new runs alongside it.

| need | reused | added, and why |
|---|---|---|
| state, checkpoints, traces | ledger entries per step (`valuation_request/step/decision/status/narrative`), stable run id | — |
| immutable as-of snapshots | content-addressed blobs (raw SEC bytes and the filtered view) | — |
| DCF math | distval conventions (pure, Decimal, `MacroAssumptions` defaults) | `fcff.py` beside `engine.py`: the existing engine is exit-multiple at an equity rate, and the workflow needs FCFF at WACC with Gordon growth |
| SEC data | `tools/edgar.py` client (User-Agent, throttle, retry) → `grinos/sec.py` | `xbrl.py`: distval's edgartools ingest drops accession, concept and period, and provenance is required per fact |
| human decisions | `append_if` first-decision-final guard, human actors, Telegram allowlist, dashboard token | — |
| evidence | `Ledger.verified_claims()` as the only door, plus the published-date check | — |
| model calls | `llm.call_agent`, hermetic, eval-first | memo-writer agent and eval; `narrative.check` validator |
| entry points | CLI, desk `/commands`, dashboard `/api` | `value` / `valuation`, `/value` `/valuation` `/vdecide`, Valuations tab |
| graph | `graph.project` | `valuation` and `xfact` nodes; `USES`, `CITES`, `SUPERSEDES` edges; `/why <run_id>` |

No database, queue, vector store or service. The step runner in `valuation.py` (about 40 lines) is
the one piece of workflow machinery: the evidence pipeline is a single pass and can't pause for
review and resume.

## Research graph

`var/graph.sqlite` is a view, never a source: `Graph.open()` rebuilds it whenever the ledger head
moves, and two builds of the same ledger are byte-identical (tested).

    source ─ABOUT→ company            claim ─EXTRACTED_FROM→ source
    claim ─ASSERTS→ fact              fact = company | metric | period | segment | adjusted
    claim ─BEARS_ON→ assumption       from the decision model's lever (levers.LEVERS)
    proposal ─CITES→ claim            proposal ─CHANGES→ assumption (status, approver, Notion link)
    claim ─CONFIRMS / CONFLICTS→ the first verified claim of the same fact

Only claims served by `Ledger.verified_claims()` confirm or conflict, so the gate still holds.
Agreement allows for rounding in the less precise figure ("$1.8 billion" ≈ 1,822.9). Metric
`other` asserts no fact, since it would equate unrelated numbers.

`/why POOL mid_cycle_ebit` walks assumption → approved proposals → cited claims → quote → filing,
with any later contradictions; `/why <claim_id>`, `/why POOL revenue Q2 FY2026` and
`/why POOL conflicts` answer the other directions.

Voice and messaging are thin clients. They call the same pipeline and agents, and every turn
is written to the ledger, so a call is as auditable as a CLI run.

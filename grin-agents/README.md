# grin-agents — Grin OS

An operating system for the valuation process: agents you can talk to in Telegram, the
dashboard, or a multi-agent voice room. **Every piece of evidence is triaged by a decision
model, then verified, before any analyst agent may use it.** Every step is recorded in a keyed,
hash-chained ledger on disk. Notion receives only the finished, human-approved output.

Design and phases: [`ARCHITECTURE.md`](ARCHITECTURE.md). Rule: every agent starts with its eval
([`skills/eval-first-agent`](skills/eval-first-agent/SKILL.md)).

## The flow

```
filing / transcript / note
  → claim-extractor        facts as printed, verbatim quotes      (untrusted)
  → decision-model         lever + structural/cyclical/one-off/noise
  → rules → verifier       quote in source bytes, value, no lookahead → does the quote support it?
  → mid-cycle-analyst      proposals citing verified claims only
  → proposal_check         citations, field, schema, config checks, distval engine impact
  → you approve            Telegram / dashboard / CLI
  → Notion (LongGamma)     results note / model sync, per the longgamma-* skill contracts
```

Every completed piece of research is captured in the LongGamma Notion workflow, in its house
format: an ingested result becomes a Research Note with a *proposed* valuation impact; a finished
valuation or an approved proposal becomes a model sync (Companies drivers, plus Valuation Log rows
linked to your rationale). `grinos capture` lists what isn't in Notion yet. See step 6c below.

## Agents and their gates

Run all gates on production models: `$V evals/run_all.py`, which writes `evals/STATUS.json`.
`grinos registry` marks an agent Live only if its **current** prompt passed.

| agent | model | gate | baseline without the agent |
|---|---|---|---|
| claim-extractor | sonnet | ≥95% of claims pass verifier rules, ≥80% recall, 0 analyst-question claims | empty: 0% recall |
| decision-model | sonnet | 0 lost evidence, ≥90% action, ≥80% nature | rules-only: 30% nature |
| verifier | sonnet | **0 false accepts**, 100% rule rejects, ≥85% valid accepted, ≥90% right reason | rules-only: 54% false accepts |
| mid-cycle-analyst | sonnet | expected changes found, **no change on cyclical/confirming evidence**, 0 injection, valid citations | naive: fails restraint. Haiku also fails (too conservative) |
| desk-router | haiku | ≥90% required agents, ≥85% precision | always-analyst: 33% |
| memo-writer | sonnet | 100% drafts valid within 2 attempts (numbers only from the valuation, sheet citations, disconfirming + reverse DCF cited), 0 recommendation language, 0 injection | naive prompt: recommends in 100%, follows injection 25%. Haiku fails (buy-type language) |
| config-auditor | sonnet | 100% rule, ≥80% judgment, 0 clean false alarms | tools-only: 0% judgment |

Histories: `evals/*/BASELINE.md`.

## Running it

Everything runs on this Mac from `grin-agents/`, with the val-machine virtualenv:

```bash
cd ~/grin-quad/grin-agents
V=../val-machine/.venv/bin/python          # every command below uses $V
```

### 1. One-time setup

| what | how |
|---|---|
| Python env | `cd ../val-machine && python -m venv .venv && .venv/bin/pip install -e ".[dev]"` (distval is the engine; grinos imports it) |
| Claude Code CLI | `claude` on your PATH and logged in: every agent runs as a hermetic `claude -p` call |
| Agents in Claude Code | `./install.sh` links `agents/*.md` and the eval-first skill into `grin-quad/.claude/` |
| Secrets | `cp .env.example .env`. Values may be 1Password references (`op://vault/item/field`), resolved only when a command needs them |
| SEC identity | `EDGAR_IDENTITY="Your Name you@example.com"` in `.env` or your shell (SEC requires a name and email) |
| Notion (optional) | `NOTION_TOKEN`, with the integration added to Valuation Log, Research Notes, Companies and Agent Registry. Check with `$V -m grinos notion-check` (read-only) |
| Telegram (optional) | `TELEGRAM_BOT_TOKEN`, then pair from your own terminal: `$V -m grinos telegram-pair` (approve the 1Password prompt, then send the printed code to the bot) |
| Voice (optional) | Start [VoiceStudio](https://github.com/debpalash/VoiceStudio) on `127.0.0.1:3900` and create the voices listed under [What needs you](#what-needs-you) |

The ledger key is created on first use at `~/.config/grinos/ledger.key`. Keep it out of synced folders.

### 2. Check the system before using it

```bash
$V -m pytest -q                            # grin-agents: ledger, pipeline, graph, valuation, web, telegram
(cd ../val-machine && .venv/bin/python -m pytest -q)   # distval engine + FCFF math
$V evals/run_all.py                        # every agent's eval on its production model → evals/STATUS.json
$V -m grinos audit                         # ledger chain + HMAC + head anchor + every source blob
```

Evals call the real models, so they cost usage (each run's cost is printed and kept in `BASELINE.md`). An agent is **Live** only if its current prompt
passed: `$V -m grinos registry` shows the status, and `--push` writes it to Notion.

### 3. Smoke-test offline (no network, no secrets)

```bash
$V -m grinos value --assumptions examples/valuation/acme.yaml --offline examples/valuation/sec
#   → blocked at adjustments: needs you: adj_ff95e662 …
$V -m grinos valuation decide <run_id> adj_ff95e662 approve --note "restructuring is a one-off"
#   → complete: bear $16.47, base $31.02, bull $46.11; memo path printed
```

Point `GRINOS_HOME` at a scratch folder first if you don't want the test run in your real ledger.

### 4. Feed evidence in

Every source goes extractor → decision model → rules → verifier before any analyst can use it.

```bash
# one document (text: a filing, transcript or note)
$V -m grinos ingest path/to/release.txt --company POOL --kind other --published 2026-07-23 \
   --as-of 2026-10-19 --config ../val-machine/distval/companies/pool.yaml

# history: earnings releases (8-K item 2.02, exhibit 99.1) straight from EDGAR, oldest first
$V tools/edgar.py POOL FAST GWW --since 2023-10-01 --out var/inbox
$V tools/bulk_ingest.py var/inbox/POOL var/inbox/FAST var/inbox/GWW --as-of 2026-10-19
#   resumable: sources already in the ledger are skipped; log in var/logs/bulk_ingest.log
```

A release costs about $1 in agent calls and takes 3–5 minutes. For a filing saved as HTML, convert
it first: `$V tools/html_to_text.py filing.htm out.txt`.

### 5. Review the evidence

```bash
$V -m grinos runs                          # one line per ingest: claims, verified, rejected, needs_human
$V -m grinos show <run_id>                 # every entry of one run
$V -m grinos trail <claim_id>              # source → claim → decision → rule checks → verdict
$V -m grinos evidence --company POOL       # what the analysts are allowed to see
$V -m grinos resolve <claim_id> verified --note "checked note 7"    # settle a needs_human claim
$V -m grinos graph --conflicts             # verified facts that disagree across filings
$V -m grinos why POOL revenue Q2 FY2026    # one fact across every filing that states it
```

Read the rejections on any new kind of source. Each surprise becomes an eval case
([`skills/eval-first-agent`](skills/eval-first-agent/SKILL.md), step 7).

### 6a. Update the mid-cycle model → Notion

```bash
$V -m grinos analyse POOL --config ../val-machine/distval/companies/pool.yaml --net-debt 1312.0 --shares 36.13
$V -m grinos proposals                     # each with its checks and distval EV impact
$V -m grinos approve <proposal_id> --note "why"      # or: reject
$V -m grinos capture sync <proposal_id>              # LongGamma model sync: see 6c
$V -m grinos why POOL mid_cycle_ebit                 # assumption → approval → cited claims → filing
```

"No change" is a valid result: the analyst is gated to leave the model alone on cyclical or
confirming evidence.

### 6b. Run a fundamental FCFF valuation

```bash
mkdir -p var/assumptions && cp examples/valuation/acme.yaml var/assumptions/pool.yaml   # private, gitignored: edit every value
$V -m grinos value --assumptions var/assumptions/pool.yaml [--date 2026-09-21 --price 167.96] [--narrative]
$V -m grinos valuation show <run_id>                         # status, and what it needs from you
$V -m grinos valuation decide <run_id> <item> accept|reject|approve|value [--value 250] --note "why"
$V -m grinos valuation memo <run_id>                         # the dated memo (also var/valuations/<run_id>/)
$V -m grinos why <run_id>                                    # value per share → facts → accession → URL
```

A run stops (`blocked`) whenever it needs you: an adjustment to approve, conflicting or missing
data, or a newer filing that isn't in SEC's XBRL data yet. Decide, and it carries on from that step.
Re-running the same command resumes the same run. A different price, different assumptions or
`--refresh` with newly available filings creates a new version. Details are in
[Fundamental valuation](#fundamental-valuation-fcff--wacc) below.

### 6c. Capture completed research in Notion (LongGamma)

Each capture follows its `longgamma-*` skill contract and the
[Schema Contract](https://app.notion.com/p/3e4bece59e6b81ccb594f86afb2657c7). It reads Notion
first (search before create), writes only the fields the contract allows, and tags everything
Author = Agent. Run these from your own terminal, since reading Notion needs the 1Password approval.

```bash
$V -m grinos capture                                   # completed research not yet in Notion

# a results release or transcript you ingested → longgamma-results-transcript
$V -m grinos capture results <run_id>                  # shows the note: takeaways, proposed impact, task
$V -m grinos capture push lg_results_<run_id>          # Research Note (+ "Update model" task if impact ≠ None)

# a complete valuation, or an approved proposal → longgamma-model-sync
$V -m grinos capture sync <val_run_id | proposal_id>   # diff vs Notion, PW Target before → after
$V -m grinos capture go lg_sync_<id> --rationale "your words"     # your go; [--confirm TICKER] if PW moves > 30%
$V -m grinos capture push lg_sync_<id>                 # Companies drivers + Model Synced, Valuation Log rows
```

| grinos output | LongGamma field |
|---|---|
| base-case year 1 / year 3 EBIT | EBIT FY1 / EBIT FY3 |
| year 2 EBIT by scenario | EBIT FY2 Bear / Base / Bull |
| normalised latest-year operating income | Sustainable EBIT |
| EV ÷ EBIT FY2 by scenario | Target Multiple Bear / Base / Bull, so Notion's target formula reproduces the DCF when its net debt and shares match the valuation's bridge |
| `probabilities: {bear, base, bull}` in the assumptions file | Prob Bear / Base / Bull (left empty otherwise; Notion defaults to 25/50/25) |

A results note leaves Thesis Impact unset, and never applies its proposed impact. Changes under
0.5% are ignored. A company missing from Companies stops the capture: run
`longgamma-initiate-coverage` first. Meetings and calls go through `longgamma-event-note`.

### 7. Work from the interfaces

```bash
$V -m grinos serve            # dashboard http://127.0.0.1:8765 (Pending, Runs, Trail, Proposals,
                              #   Valuations, Evals, Desk); voice room at /room
$V -m grinos telegram         # the bot; allowlisted users only (pair first, step 1)
services/install.sh           # optional: both at login as LaunchAgents (--uninstall to remove)
```

In Telegram and the voice room, `/commands` act: `/pending`, `/approve`, `/reject`, `/resolve`,
`/analyse`, `/value`, `/valuation`, `/vdecide`, `/capture`, `/go`, `/why`, `/audit`. Anything else is a
question for the desk. `@analyst`, `@verifier`, `@decision`, `@auditor` or `@extractor` pick who
answers; otherwise the router does. In a group or the voice room, several agents answer in turn.
To ingest from Telegram, send a text file captioned `/ingest TICKER KIND PUBLISHED [AS_OF]`.
Agent replies can never trigger an action: only your own `/commands` do.

### 8. The routine

1. **New filing or release:** ingest it (step 4), then read its rejections and conflicts (step 5).
2. **Evidence that moves an assumption:** `analyse`, then approve or reject with a note (6a).
   Then capture it: a results note for the source, and a model sync for an approved change (6c).
3. **Valuation date or price changes:** `value` with the new date or price. That's a new version,
   which `supersedes` the old one (6b).
4. **Any prompt change:** re-run that agent's eval. It shows Paused until it passes again.
5. **Weekly, or before relying on anything:** `$V -m grinos audit`.

### Troubleshooting

| symptom | cause and fix |
|---|---|
| a command hangs about 2 minutes, then "authorization timeout" | 1Password is waiting for approval. Run the command from your own terminal (or `! …` in Claude Code). Unattended services need a 1Password service account |
| `REFUSED: refusing to serve evidence from a broken ledger` | `grinos audit` lists the problem. The ledger or a blob was changed outside grinos |
| valuation blocked at `retrieve` with "not yet in SEC's XBRL companyfacts" | SEC's XBRL data lags filings. Accept to value on the earlier filing, or wait and re-run with `--refresh` |
| `capture` stops: "not in Notion Companies" | run `longgamma-initiate-coverage` for it first (you set Direction) |
| many rejections on a new kind of source | read `grinos trail` for a few. Formatting (tables, units, signs) is the usual cause: see `tools/html_to_text.py` |
| HTTP 429/503 from SEC | throttled. The client backs off and retries 5 times; re-run, and bulk ingest resumes |

## Fundamental valuation (FCFF / WACC)

One US-listed nonfinancial company, a valuation date, a market price you supply, and bear/base/bull
assumptions → a five-year FCFF forecast discounted at WACC, a Gordon-growth terminal value, the equity
bridge, a WACC × terminal-growth grid, a reverse DCF, and a dated memo whose every reported number
carries its SEC fact id. **Analysis only: no trades, no buy/sell call.** Financials and insurers stop
at an explicit `unsupported` outcome.

Commands: [Running it, steps 3 and 6b](#3-smoke-test-offline-no-network-no-secrets).

Telegram and the voice room: `/value POOL [DATE PRICE]`, `/valuation [run_id]`, `/vdecide …`. The
dashboard has a Valuations tab (read-only view; decisions need the session token).

**Steps** (each a ledger entry, re-recorded only when its output changes): resolve (ticker → CIK,
sector gate) → retrieve (SEC submissions + XBRL companyfacts, as blobs) → reconcile → adjustments →
evidence → forecast → valuation → sensitivity → validate → memo. A step that needs you returns
`blocked`; decide, and the run resumes from there.

**Inputs** (`examples/valuation/acme.yaml` documents every key): ticker, valuation date, price,
`defaults` and per-scenario `revenue_growth`, `ebit_margin`, `tax_rate`, `net_capex_pct_revenue`
(capex − D&A), `nwc_pct_revenue`, `terminal_growth`; a `wacc` build-up (risk-free, ERP, beta,
pre-tax cost of debt, optional target debt weight) or a supplied `wacc`; `adjustments`; and
`narrative` drivers, risks and disconfirming evidence, each with a source (a verified claim id, a fact
id or a URL). `reported` and `normalised` derive an input from the filings, with fact ids.

**What it enforces.**
- Only facts *filed* on or before the valuation date: the as-of snapshot is filtered and hashed,
  and `validate` re-checks every fact. Cited claims must be published by then too.
- Each material input records currency (USD only), unit, period, concept, accession and URL.
  Restated values: the latest filing wins, and the superseded values are kept and flagged.
- Missing or conflicting material data (a value absent, rival debt formulas, totals that don't
  add up, the newest 10-Q not yet in XBRL) is routed to you. A missing number can only be
  resolved with a value and its source, never accepted as a gap.
- Reported figures, analyst adjustments and forecasts are separate outputs. Adjustments, whether
  rule-proposed (impairments, restructuring) or yours, apply only once approved.
- Terminal growth must be below WACC, or the run is `invalid`. Terminal value above 75% of EV,
  growth above the risk-free rate, a stale balance sheet and analyst-supplied inputs are flagged.
- **Versions.** The run id hashes the inputs and the as-of snapshot. Re-running resumes; a new
  price or new assumptions, or `--refresh` finding newly available filings, gives a new run that
  `supersedes` the old one.
- **The narrative is bounded.** `--narrative` (memo-writer) gets at most 2 attempts and is
  checked deterministically. A failed draft is dropped, and the result is checkpointed so a
  resume never calls the model again.

**Data limitations.**
- SEC companyfacts can lag filings by weeks: POOL's Q2 2026 10-Q, filed 2026-07-29, was absent on
  2026-10-19. The workflow routes this to review rather than silently valuing stale data.
- Only standard us-gaap concepts are read. Company-specific extension concepts are invisible, so
  an item a company reports only under its own tag shows as missing.
- Some concepts drift over time; POOL reports cash including restricted cash after 2018. The
  memo names the concept used.
- Cash flows are discounted at whole years from the valuation date (no mid-year convention or
  stub period). One currency (USD). The price is yours: nothing fetches a quote.
- `as_of` filtering uses the date SEC received the filing. A filing restated later is seen as
  it was originally filed only if the valuation date precedes the restatement.

Example output: [`examples/valuation/acme_memo.md`](examples/valuation/acme_memo.md) (synthetic company).

## Guarantees, and how each is enforced

| guarantee | enforced by | test |
|---|---|---|
| analysts see only verified evidence | `Ledger.verified_claims()`, the only door | `test_ledger`, `test_pipeline` |
| a rule rejection is final | `verify.py` never calls the agent after a reject | `test_pipeline` |
| crashed verifier ≠ pass | fails closed to `needs_human` | `test_pipeline` |
| human verdicts outrank agents | `verified_claims` precedence | `test_ledger` |
| no LLM output triggers an action | actions only from parsed human `/commands`, dashboard POSTs, CLI; agent text has `/approve`-style commands neutralised | `test_desk` |
| untrusted text can't break prompt structure | `schemas.fence()` on every source and chat block | `test_desk` |
| proposals cite only verified claims | `proposal_check` | `test_publish`, analyst eval |
| nothing publishes without checks + human approval + intact ledger | `notion.build_publication`; a model sync needs your go with your rationale | `test_publish`, `test_longgamma` |
| Notion writes follow the LongGamma contracts | `longgamma`: allowed fields only, no formulas, Author = Agent, search before create, rationale verbatim, results never applied | `test_longgamma` |
| one decision and one publication per proposal, across processes | `Ledger.append_if` under flock, a publish lock | `test_ledger`, `test_publish` |
| tampering is detected | sha256 chain + HMAC (key in `~/.config/grinos`) + head anchor | `test_ledger` (incl. full chain rewrite) |
| a valuation never uses data published after its date | as-of snapshot filtered on SEC filed dates, hashed into the run id; `validate` re-checks every fact | `test_valuation` |
| no missing financial number is invented | missing material items block until a value with a note is supplied; `accept` is refused | `test_valuation` |
| adjustments never alter reported figures | separate `reported` / `adjustments` / `normalised` outputs; approval required | `test_valuation` |
| the memo narrative can't add numbers or advice | `narrative.check`: sheet numbers and ids only, no recommendation language; 2 attempts max | memo-writer eval, `test_memo_harness` |
| the research graph can't drift from the ledger | rebuilt from the ledger whenever its head moves; confirms/conflicts only between gate-served claims | `test_graph` |
| Telegram strangers can't act | allowlist before every branch; refusals rate-limited | `test_telegram` |
| dashboard can't be driven by other sites | 127.0.0.1 only, Host check, per-process POST token, CSP, no innerHTML | `test_web` |

Limit: anyone who can read the ledger key can forge entries. Keep `~/.config/grinos/ledger.key`
out of synced folders.

## What needs you

| step | why it's yours |
|---|---|
| `NOTION_TOKEN` in `.env`, and the integration added to Valuation Log, Research Notes, Companies and Agent Registry | Only you can create a Notion integration. Until then `publish` stops at the outbox |
| Telegram bot token and your user id in `.env` | from @BotFather and @userinfobot |
| Install and start [VoiceStudio](https://github.com/debpalash/VoiceStudio), and create designed voices `grin-analyst`, `grin-verifier`, `grin-decision`, `grin-auditor`, `grin-extractor` | about 5 GB of models; AGPL-3.0. Without these voices, every agent uses the default voice |
| `services/install.sh` | changes your login items |
| `grinos registry --push` | writes Agent Registry rows to Notion |

## Layout

```
grinos/          the OS: ledger, schemas, rules, pipeline, decide, verify, extract, analyst,
                 proposal_check, proposals, notion, desk, voice, telegram, web/, cli, graph,
                 sec + xbrl + valuation + valuation_memo + narrative (the FCFF workflow)
agents/          agent prompts (Claude Code subagent format; ./install.sh links them into .claude/)
evals/<agent>/   cases, deterministic grader, gates, runner, BASELINE.md; run_all.py → STATUS.json
tools/           config_facts.py (config-auditor's rules), edgar.py + html_to_text.py (SEC sources),
                 bulk_ingest.py
examples/valuation/  acme.yaml (assumptions, documented), sec/ (synthetic SEC responses), acme_memo.md
services/        LaunchAgent installer
examples/        sample claims and a synthetic investor-day source
var/             runtime, gitignored: ledger.jsonl, ledger.head, store/blobs, graph.sqlite, inbox, outbox, logs
```

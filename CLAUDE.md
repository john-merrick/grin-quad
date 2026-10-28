# grin-quad

- `val-machine/`: distval, the deterministic valuation engine (mid-cycle EBIT × multiple; FCFF/WACC in `distval/fcff.py`).
- `grin-agents/`: Grin OS, with eval-first agents, the evidence ledger, the valuation workflow, and the Telegram, dashboard and voice interfaces. See `grin-agents/README.md`.

## How the system runs

Isaac talks to the orchestrator only. It plans the work, which runs on Temporal (`grinos worker`; UI `127.0.0.1:8233`), and the ledger stays the record of truth. Proposals are approved only after every cited claim is confirmed in the review pop-up. Valuations go through `grinos coverage`, one verified step at a time. Prompts improve through `grinos improve`, and go live only on his approval.

## Keep the Home page current

The dashboard's Home page is how Isaac follows along. With every change to the app, update
`grin-agents/docs/guide.yaml` in the same change:
- add a `whats_new` entry, newest first, with the date, a short title, plain-language items and the PR number once it's known;
- update the `features` entry it touches (what it does, and how to use it), or add one for a new tab or capability.

The page reads the file live, so the change shows on reload.

## Every agent starts with its eval

Follow `grin-agents/skills/eval-first-agent/SKILL.md`. The naive baseline must fail the gate, and any prompt change re-runs the eval.

## Completed research is captured in Notion (LongGamma workflow)

Notion (LongGamma) is the decision record. When a piece of research is finished, capture it with
the matching `longgamma-*` skill contract. Read the Schema Contract first:
https://app.notion.com/p/3e4bece59e6b81ccb594f86afb2657c7

| research completed | capture with | how |
|---|---|---|
| a results release, filing or transcript ingested (`grinos ingest` / `bulk_ingest`) | `longgamma-results-transcript` | `grinos capture results <run_id>`, then `grinos capture push lg_results_<run_id>` |
| a complete FCFF valuation (`grinos value`) or an approved mid-cycle proposal | `longgamma-model-sync` | `grinos capture sync <run_id or proposal_id>`; show Isaac the diff; then `grinos capture go … --rationale "<his words>"`; then `grinos capture push …` |
| a meeting, call, expert call or sell-side note | `longgamma-event-note` | follow the skill directly (Notion connector) |
| a company not yet in Companies | `longgamma-initiate-coverage` | follow the skill first. Ask Isaac for Direction (the skill's default is Long) |
| a complete coverage run (`grinos coverage`) | `longgamma-model-sync` + the risks contract | `grinos capture sync <cov_run>` (go with his rationale, then push); `grinos capture risks <cov_run>` then push (his risks, Author Human) |

`grinos capture` lists completed research that isn't in Notion yet.

Rules, from the skills:
- Tag every agent-written row `Author` = Agent (on Companies, `Updated By` = Agent).
- Never write formula or rollup fields.
- Percents are decimals; money is in millions.
- Search before you create.
- Never invent a rationale, and never apply a proposed valuation impact.
- Thesis Impact, Direction, kill criteria, model assumptions and catalyst verdicts are Isaac's calls.
- Audit data (the ledger, blobs and evals) stays on the filesystem, not in Notion.

## Secrets and git

- Secrets are 1Password `op://` references in `grin-agents/.env`; never write a secret value anywhere.
- `var/` and `var/assumptions/` (private research) are gitignored.
- Commit only when asked, on a branch.

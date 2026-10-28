# mid-cycle-analyst — eval history

8 scenarios vs the ACME clean config: cyclical-only, confirming-only, durability evidence,
structural margin, structural tax, a prompt-injection quote, and (from 2026-10-25) two with reviewer
perspectives: one asking for an unsupported change, one pointing at evidence that does support one. Proposals are graded after the
deterministic proposal_check (citations, field, shape, schema, config checks, engine impact).

| date | system | model | repeats | expected recall | restraint | raw citations valid | injection followed | checks pass | cost | gate |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-18 | naive | — | 1 | 67% | 67% | 100% | 0 | 100% | $0 | FAIL (expected) |
| 2026-10-18 | agent | sonnet | 3 | 100% | 100% | 100% | 0 | 100% | $0.19 | PASS |
| 2026-10-18 | agent | haiku | 3 | 67% | 100% | 100% | 0 | 100% | $0.02 | FAIL |
| 2026-10-25 | naive | — | 1 | 50% | 63% | 100% | 0 | 100% | $0 | FAIL (expected) |
| 2026-10-25 | agent | sonnet | 2 | 100% | 100% | 100% | 0 | 100% | $0.18 | PASS |

**The first eval that separates the models.** Haiku never moved the duration score on three
independent structural signals: too conservative for this role. The analyst runs on Sonnet.

**2026-10-25: reviewer perspectives.** The analyst now also sees `<human_perspectives>`. It held
gross margin at 30.2% when the reviewer asked for 36% without evidence, and moved the duration score
to 7 only where the reviewer pointed at retention and contract evidence that supports it.

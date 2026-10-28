# memo-writer — baseline history

Validator: `grinos.narrative.check` (deterministic). Numbers only from the valuation sheet (magnitudes
allowed, dates ignored), citations only to sheet ids, no recommendation language, disconfirming
evidence and reverse DCF cited. Production: at most 2 attempts, the second with the validator's
feedback; a failed draft is dropped and the memo goes without a narrative.

| date | system | model | repeats | first valid | final valid | recommendation | injection followed | cost | gate |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-19 | naive ("summarise for an IC") | sonnet | 1 | 0% | 25% | 100% | 25% | $0.19 | FAIL (expected) |
| 2026-10-19 | agent v1 | sonnet | 3 | 25% | 100% | 0% | 0% | $0.31 | FAIL: validator false positive |
| 2026-10-19 | agent v1, validator accepts magnitudes ("48.3% below") and ignores ISO dates | sonnet | 3 | 100% | 100% | 0% | 0% | $0.11 | PASS |
| 2026-10-19 | naive, same validator | sonnet | 1 | 0% | 100% | 100% | 25% | $0.16 | FAIL (expected) |
| 2026-10-19 | agent v1 | haiku | 3 | 83% | 100% | 17% | 0% | $0.03 | FAIL: uses buy-type language |

Both validator fixes were false positives found by reading every rejection, not model errors:
"2026-06-30" was split into numbers, and "48.3% below the price" was rejected against a sheet
value of -48.3. Each became a harness test. Haiku is not a production tier for this agent.

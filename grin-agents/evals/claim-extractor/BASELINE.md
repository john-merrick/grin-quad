# claim-extractor — eval history

2 real sources (10-K excerpt, earnings call). Precision is graded by the verifier's own rules,
so it needs no labels. Recall is graded against 18 expected facts; the analyst's question
("pricing of 5%") must never be extracted.

| date | system | model | repeats | rule pass | recall | forbidden | cost | gate |
|---|---|---|---|---|---|---|---|---|
| 2026-10-18 | empty | — | 1 | 0% | 0% | 0 | $0 | FAIL (expected) |
| 2026-10-18 | agent | sonnet | 2 | 100% | 100% | 0 | $0.11 | PASS |
| 2026-10-18 | agent | haiku | 2 | 100% | 100% | 0 | $0.01 | PASS |
| 2026-10-18 | agent | sonnet (+segment/adjusted fields) | 2 | 100% | 98% | 0 | $0.12 | PASS |

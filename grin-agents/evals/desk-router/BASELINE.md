# desk-router — eval history

12 messages labelled with required and acceptable agents.

| date | system | model | repeats | must recall | precision | gate |
|---|---|---|---|---|---|---|
| 2026-10-18 | always-analyst | — | 1 | 33% | 50% | FAIL (expected) |
| 2026-10-18 | agent | haiku | 3 | 97% | 100% | PASS |
| 2026-10-18 | agent | sonnet | 2 | 100% | 100% | PASS |

Router runs on Haiku (fast, for voice). Haiku's one miss: the LIFO question went to the analyst
only, without the decision model.

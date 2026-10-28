# verifier — eval history

24 claims against 3 real source files: 11 valid, 6 tool (rules must reject), 7 judgment
(rules pass; only reading in context catches them). As-of 2026-03-01.

| date | system | model | repeats | false accept | tool reject | valid accept | judgment reject | reason match | cost | gate |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-18 | rules-only | — | 1 | 54% | 100% | 100% | 0% | — | $0 | FAIL (expected) |
| 2026-10-18 | agent | sonnet | 3 | 0% | 100% | 100% | 100% | 100% | $0.28 | PASS |
| 2026-10-18 | agent | haiku | 3 | 0% | 100% | 100% | 100% | 100% | $0.02 | PASS |

| 2026-10-18 | agent (segment/adjusted/DIO-basis fix) | sonnet | 3 | 0% | 100% | 97% | 100% | 100% | $0.33 | PASS |
| 2026-10-18 | agent (same) | haiku | 3 | 0% | 100% | 91% | 100% | 100% | $0.02 | PASS |
| 2026-10-18 | agent (customer groups ≠ segments) | sonnet | 3 | 0% | 100% | 100% | 100% | 100% | $0.32 | PASS |
| 2026-10-18 | agent (same) | haiku | 3 | 0% | 100% | 100% | 100% | 100% | $0.02 | PASS |
| 2026-10-19 | rules-only (+3 sign cases, sign_differs warns) | - | 1 | 57% | 100% | 100% | - | 0% | $0 | FAIL (expected) |
| 2026-10-19 | agent (+wrong_sign code) | sonnet | 3 | 0% | 100% | 100% | 100% | 100% | $0.40 | PASS |
| 2026-10-19 | agent (same) | haiku | 3 | 0% | 100% | 100% | 100% | 100% | $0.03 | PASS |

**Found by a real run, not the eval:** the first end-to-end ingest falsely rejected labelled
segment figures, labelled adjusted figures, and a correct days-of-sales DIO (the prompt wrongly
implied a COGS basis). Fixed with `segment` and `adjusted` claim fields and a prompt correction.
All three became valid eval cases. The fix then overcorrected ("retention on contract accounts"
was treated as a segment), which was caught by the eval and fixed.

Saturated: both tiers are perfect. Next harder cases: restated prior-year figures, "approximately"
vs exact, mixed units in tables, quotes split across page breaks, long real 10-K text.

2026-10-19, first real bulk run (POOL Q2 2026 release): the value rule rejected sign conventions
("cash used $0.7M" as -0.7, capex "(36,569)" as positive). The rule now only warns
(`sign_differs`) when the magnitude matches, and the agent judges with a new `wrong_sign` code.
Three cases added: two valid conventions and one loss claimed as a gain, which rules-only
accepts. The rules-only false-accept rate rose from 54% to 57%.

# decision-model — eval history

14 claims vs the ACME clean config: 10 relevant (must route to verification), 4 irrelevant.
Nature labels are sets where analysts could reasonably differ.

| date | system | model | repeats | lost evidence | action acc | nature acc | cost | gate |
|---|---|---|---|---|---|---|---|---|
| 2026-10-18 | rules-only | — | 1 | 0% | 79% | 30% | $0 | FAIL (expected) |
| 2026-10-18 | agent | sonnet | 3 | 0% | 100% | 100% | $0.24 | PASS |
| 2026-10-18 | agent | haiku | 3 | 0% | 100% | 100% | $0.02 | PASS |

Saturated. Next: materiality graded against an engine sensitivity (Δ value/share when the
config field moves to the claimed value), which makes materiality deterministic.

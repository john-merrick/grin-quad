# config-auditor — eval history

18 cases: 3 clean, 7 tool, 5 judgment, 3 real (POOL, FAST, GWW snapshots).

| date | system | model | repeats | tool | judgment | real | clean FA | verdict | cost | gate |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-18 | tools-only | — | 1 | 100% | 0% | 20% | 0% | 61% | $0 | FAIL (expected) |
| 2026-10-18 | agent | sonnet | 3 | 100% | 100% | 100% | 0% | 100% | $0.68 | PASS |
| 2026-10-18 | agent | haiku | 3 | 100% | 100% | 100% | 0% | 100% | $0.10 | PASS |

## Notes

- **Fixture bugs found by the agents.** The first two versions of the clean base had
  inconsistent mid-cycle revenue comments ("midpoint of forecast range" = 1,100, really
  1,127.5; then "10-yr average" = 1,100, impossible for a business growing to 1,000).
  Haiku flagged the first in 5 of 6 runs; Sonnet missed it in all 9. Both flagged the second.
  The base now anchors mid-cycle revenue to the forecast average (1,126).
- **The eval is saturated.** Both tiers score 100%, so it can't rank models yet. Next: harder
  judgment cases (two defects per file, subtle ~5% mismatches, industrial/SaaS configs).
- **Real configs have defects beyond the labels** (found by the agent, verified by hand):
  FAST DIO arithmetic is wrong even on its own COGS basis (1538/3713×365 = 151, not 126), and
  FAST's "mean excl. 2020–22" is 50.3%, not the stated 49.5%. GWW `dio` 50.6 matches neither
  the FY2025 actual (48.7) nor the 10-yr average (51.1).

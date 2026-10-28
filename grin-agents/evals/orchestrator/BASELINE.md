# orchestrator — eval history

26 messages to the desk with the state the orchestrator sees (tickers with configs, work in progress,
verified evidence). Graded on the raw plan before `grinos.orchestrator.check_plan`:
- does it start the work asked for, with the right ticker, agent and date?
- does it start nothing that wasn't asked for?
- does it suggest the exact command when the request is Isaac's decision, starting nothing?
- does every action pass the deterministic check?
- does it consult the right specialist?
- does its reply contain no number it wasn't given?

| date | system | model | repeats | action recall | spurious | human respected | arg validity | consult recall | consult precision | invented numbers | cost | gate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-25 | naive | — | 1 | 0% | 0.00 | 0% | 100% | 20% | 17% | 0 | $0 | FAIL (expected) |
| 2026-10-25 | agent | sonnet | 1 | 92% | 0.04 | 100% | 100% | 100% | 71% | 0 | $0.25 | FAIL |
| 2026-10-25 | agent | sonnet | 2 | 100% | 0.02 | 100% | 100% | 100% | 100% | 0 | $0.35 | PASS |

**First run (FAIL).**
- **Prompt gap:** asked to improve the verifier, it also started an eval. The improvement loop runs the eval
  itself; the prompt now says so.
- **Cases that were too strict:**
  - consulting the extractor about a quoted filing;
  - asking the analyst when there's no price target yet;
  - reporting the status of a POOL coverage run already in progress instead of starting another.

  The prompt now says to do the last of these, and a separate case checks it.

**Passing run.** In one of the two repeats, asked whether GWW's duration score should go up, it also started an
analysis. That is within the spurious-work gate.

"""
Grin OS on Temporal: every agent call and long-running process is a Temporal activity or workflow.

  workflows.py   orchestration only: deterministic, no I/O (IngestWorkflow, AnalysisWorkflow, CoverageWorkflow,
                 EvalWorkflow, ImprovementWorkflow, OrchestratorTurnWorkflow)
  activities.py  the work: each wraps existing grinos code and writes the ledger idempotently, so a
                 retried activity never writes twice and never pays an agent twice
  worker.py      `grinos worker`: one process serving every task queue
  client.py      starting workflows and signalling them; falls back to running inline when Temporal is down

Task queues: grin-workflows (workflows), agent.<name> (one per agent, so each agent's load, latency and
failures show separately in the Temporal UI), grin-io (ledger, SEC, Notion), grin-eval (eval runs).

The ledger is the record of truth. Temporal is execution: a lost signal costs at most one timer interval,
because workflows always re-read the ledger.
"""
WORKFLOW_QUEUE = "grin-workflows"
IO_QUEUE = "grin-io"
EVAL_QUEUE = "grin-eval"
AGENTS = ("claim-extractor", "decision-model", "verifier", "mid-cycle-analyst", "memo-writer", "config-auditor",
          "orchestrator", "prompt-optimiser")


def agent_queue(name: str) -> str:
    return f"agent.{name}"

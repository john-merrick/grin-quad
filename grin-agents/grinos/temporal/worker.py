"""
`grinos worker`: one process serving every Grin OS task queue against the local Temporal server.

    temporal server start-dev --ip 127.0.0.1 --db-filename var/temporal/temporal.db   (services/install.sh --temporal)
    grinos worker
"""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from grinos.temporal import AGENTS, EVAL_QUEUE, IO_QUEUE, WORKFLOW_QUEUE, agent_queue
from grinos.temporal import activities as A
from grinos.temporal.workflows import WORKFLOWS

ADDRESS = os.environ.get("GRINOS_TEMPORAL_ADDRESS", "127.0.0.1:7233")
NAMESPACE = os.environ.get("GRINOS_TEMPORAL_NAMESPACE", "default")

# Which activities each queue serves. An agent's queue serves only that agent's calls.
AGENT_ACTIVITIES = {
    "claim-extractor": [A.ingest_extract],
    "decision-model": [A.decide_claim],
    "verifier": [A.verify_claim],
    "mid-cycle-analyst": [A.analyse],
    "prompt-optimiser": [A.improve_propose],
}
IO_ACTIVITIES = [A.ingest_source, A.ingest_unassessed, A.ingest_record, A.analysis_open, A.coverage_start,
                 A.coverage_resume, A.valuation_resume, A.notify, A.improve_collect, A.improve_record_eval,
                 A.improve_select, A.improve_decision]
CONCURRENCY = {"claim-extractor": 2, "decision-model": 6, "verifier": 6, "mid-cycle-analyst": 2}


def workers(client: Client, executor: ThreadPoolExecutor) -> list[Worker]:
    ws = [Worker(client, task_queue=WORKFLOW_QUEUE, workflows=WORKFLOWS),
          Worker(client, task_queue=IO_QUEUE, activities=IO_ACTIVITIES, activity_executor=executor,
                 max_concurrent_activities=8),
          Worker(client, task_queue=EVAL_QUEUE, activities=[A.run_eval, A.refresh_status], activity_executor=executor,
                 max_concurrent_activities=2)]
    for name in AGENTS:
        acts = AGENT_ACTIVITIES.get(name, []) + [A.agent_call]
        ws.append(Worker(client, task_queue=agent_queue(name), activities=acts, activity_executor=executor,
                         max_concurrent_activities=CONCURRENCY.get(name, 2)))
    return ws


async def serve() -> None:
    client = await Client.connect(ADDRESS, namespace=NAMESPACE)
    with ThreadPoolExecutor(max_workers=32) as executor:
        ws = workers(client, executor)
        print(f"grinos worker: {len(ws)} task queues on {ADDRESS} ({NAMESPACE}); Ctrl-C to stop", flush=True)
        await asyncio.gather(*(w.run() for w in ws))


def main() -> int:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    return 0

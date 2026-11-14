"""Workflow code contains no database, HTTP, filesystem or model calls."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


RETRY = RetryPolicy(initial_interval=timedelta(seconds=10), maximum_interval=timedelta(minutes=2), maximum_attempts=3)


async def execute(name: str, payload: dict, *, minutes: int = 10):
    return await workflow.execute_activity(
        name, payload, start_to_close_timeout=timedelta(minutes=minutes),
        heartbeat_timeout=timedelta(seconds=60), retry_policy=RETRY,
    )


async def finish(payload: dict, status: str, error: str | None = None):
    # A storage outage must not discard the terminal status projection.
    await workflow.execute_activity(
        "finish_job", {**payload, "status": status, "error": error},
        start_to_close_timeout=timedelta(seconds=30),
        retry_policy=RetryPolicy(maximum_interval=timedelta(minutes=1)),
    )


@workflow.defn
class ProcessingWorkflow:
    @workflow.run
    async def run(self, payload: dict):
        try:
            kind = payload["kind"]
            if kind == "import":
                for _ in range(100):
                    state = await execute("import_control", payload)
                    if state == "paused":
                        await workflow.sleep(timedelta(seconds=10))
                        continue
                    if state == "terminal":
                        await finish(payload, "completed")
                        return
                    if not await execute("import_discover", payload, minutes=120):
                        continue
                    if not await execute("import_step", payload):
                        await finish(payload, "completed")
                        return
                workflow.continue_as_new(payload)
            elif kind == "index":
                cursor = payload.get("cursor", 0)
                if not payload.get("prepared"):
                    if not await execute("prepare_index", payload):
                        await finish(payload, "superseded")
                        return
                for _ in range(100):
                    cursor = await execute("index_batch", {**payload, "cursor": cursor})
                    if cursor is None:
                        await finish(payload, "completed")
                        return
                workflow.continue_as_new({**payload, "prepared": True, "cursor": cursor})
            else:
                current = await execute(f"process_{kind}", payload, minutes=45 if kind == "summary" else 10)
                await finish(payload, "completed" if current else "superseded")
        except ActivityError as error:
            await finish(payload, "failed", str(error.cause or error)[:2000])
            raise

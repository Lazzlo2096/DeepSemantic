"""HTTP adapter to Temporal. There is no local SQL copy of execution state."""
import asyncio
from datetime import timedelta

from fastapi import HTTPException
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError, TemporalError
from temporalio.service import RPCError, RPCStatusCode

from processing.config import PIPELINE_QUEUE, temporal_client


async def client_for(request):
    client = getattr(request.app.state, "temporal_client", None)
    if client is None:
        try:
            client = await asyncio.wait_for(temporal_client(), 5)
        except (TemporalError, OSError, RuntimeError, TimeoutError) as error:
            raise HTTPException(503, "Temporal is unavailable.") from error
        request.app.state.temporal_client = client
    return client


async def start(request, name, payload, identity):
    client = await client_for(request)
    try:
        return await client.start_workflow(name, payload, id=identity, task_queue=PIPELINE_QUEUE,
            memo={"workflow_type": name, "input": payload},
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            rpc_timeout=timedelta(seconds=5))
    except WorkflowAlreadyStartedError:
        return client.get_workflow_handle(identity)
    except TemporalError as error:
        raise HTTPException(503, "Temporal did not confirm the request. Retry with the same request_id.") from error


async def describe(request, identity):
    client = await client_for(request)
    handle = client.get_workflow_handle(identity)
    try:
        info = await handle.describe(rpc_timeout=timedelta(seconds=3))
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return None
        raise HTTPException(503, "Temporal is unavailable.") from error
    progress = {}
    try:
        progress = await handle.query("progress", rpc_timeout=timedelta(seconds=2))
    except TemporalError:
        # A queued workflow has no query handler until a worker executes it.
        pass
    status = {
        WorkflowExecutionStatus.COMPLETED: "completed",
        WorkflowExecutionStatus.FAILED: "failed", WorkflowExecutionStatus.TIMED_OUT: "failed",
        WorkflowExecutionStatus.CANCELED: "failed", WorkflowExecutionStatus.TERMINATED: "failed",
    }.get(info.status, "paused" if progress.get("paused") else "running" if progress else "queued")
    error_text = None
    if status == "failed":
        try:
            await handle.result(rpc_timeout=timedelta(seconds=2))
        except TemporalError as error:
            error_text = str(getattr(error, "cause", None) or error)
    return {"status": status, "started_at": info.execution_time or info.start_time,
            "finished_at": info.close_time, "progress": progress, "last_error": error_text}


async def signal_pause(request, identity, paused):
    client = await client_for(request)
    try:
        await client.get_workflow_handle(identity).signal("set_paused", paused, rpc_timeout=timedelta(seconds=3))
    except TemporalError as error:
        raise HTTPException(409, "Temporal could not apply this command to the workflow.") from error

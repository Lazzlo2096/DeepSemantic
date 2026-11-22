"""The only owner of pipeline status, pause, progress, retries and transitions."""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError, WorkflowAlreadyStartedError
from processing.config import PIPELINE_QUEUE, QUEUES, fingerprint, paper_workflow_id, summary_workflow_id

RETRY = RetryPolicy(initial_interval=timedelta(seconds=10), maximum_interval=timedelta(minutes=2), maximum_attempts=3)


async def execute(name, payload, queue=PIPELINE_QUEUE, minutes=10):
    return await workflow.execute_activity(name, payload, task_queue=queue,
        start_to_close_timeout=timedelta(minutes=minutes), heartbeat_timeout=timedelta(seconds=60), retry_policy=RETRY)


async def child(name, payload, identity, wait=True, allow_completed=False):
    try:
        handle = await workflow.start_child_workflow(name, payload, id=identity, task_queue=PIPELINE_QUEUE,
            id_reuse_policy=(WorkflowIDReusePolicy.ALLOW_DUPLICATE if allow_completed
                             else WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY),
            parent_close_policy=workflow.ParentClosePolicy.ABANDON, memo={"workflow_type": name, "input": payload})
        if not wait:
            # Detached children report errors through their own Temporal execution.
            handle.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await handle if wait else None
    except (ChildWorkflowError, WorkflowAlreadyStartedError) as error:
        cause = error.cause if isinstance(error, ChildWorkflowError) else error
        if not isinstance(cause, WorkflowAlreadyStartedError):
            raise
        if not wait:
            return None
        # A concurrent import/manual request can own this exact artifact already.
        # Its outcome is obtained from Temporal, never from a SQL job row.
        while True:
            result = await execute("dependency_result", {"id": identity})
            if result["done"]:
                if result.get("error"):
                    raise ApplicationError(f"Dependency {identity} ended with {result['error']}")
                return result.get("result")
            await workflow.sleep(timedelta(seconds=10))


@workflow.defn
class ImportWorkflow:
    @workflow.init
    def __init__(self, p: dict):
        self.paused = p.get("paused", False)
        self.state = p.get("progress") or {
            "attempts": 1, "processed_articles": 0, "articles_found": None,
            "pdf_available": 0, "pdf_reused": 0, "pdf_failures": 0, "html_unavailable": 0,
        }

    @workflow.signal
    def set_paused(self, value: bool):
        self.paused = value

    @workflow.query
    def progress(self):
        return {**self.state, "paused": self.paused}

    @workflow.run
    async def run(self, p: dict):
        await workflow.wait_condition(lambda: not self.paused)
        manifest = p.get("manifest") or await execute("discover_import", p, QUEUES["import"], minutes=120)
        self.state["articles_found"] = manifest["count"]
        cursor = p.get("cursor", 0)
        for _ in range(4):
            items = await execute("manifest_page", {"manifest": manifest, "cursor": cursor}, QUEUES["import"])
            if not items:
                if self.state["pdf_failures"]:
                    raise ApplicationError(f"{self.state['pdf_failures']} source downloads failed")
                return self.progress()
            for article in items:
                await workflow.wait_condition(lambda: not self.paused)
                try:
                    result = await execute("download_paper", {**p, "article": article}, QUEUES["import"])
                    self.state["attempts"] = max(self.state["attempts"], result["attempt"])
                    if result["outcome"] == "unavailable":
                        self.state["html_unavailable"] += 1
                    else:
                        self.state["pdf_available"] += 1
                        self.state["pdf_reused"] += int(result["outcome"] == "reused")
                        paper = result["paper"]
                        await child("PaperWorkflow", {"paper": paper, "settings": p["settings"]},
                                    paper_workflow_id(paper, p["settings"]), wait=False)
                except ActivityError:
                    self.state["pdf_failures"] += 1
                    self.state["attempts"] = max(self.state["attempts"], 3)
                cursor += 1
                self.state["processed_articles"] += 1
        workflow.continue_as_new({**p, "manifest": manifest, "cursor": cursor,
                                  "progress": self.state, "paused": self.paused})


@workflow.defn
class PaperWorkflow:
    def __init__(self):
        self.branches = {"article_index": "pending", "summary_index": "pending"}

    @workflow.query
    def progress(self):
        return self.branches

    @workflow.run
    async def run(self, p: dict):
        paper = p.get("paper") or await execute("paper_context", p)
        if paper is None or not paper["object_key"].lower().endswith(".html"):
            return {"status": "superseded"}
        if not paper["text_object_key"]:
            paper = await execute("extract_markdown", {"paper": paper}, QUEUES["text"])
        if paper is None:
            return {"status": "superseded"}
        source = {"arxiv_id": paper["arxiv_id"], "source_kind": "article", "source_version": paper["markdown_version"]}
        async def index_article():
            self.branches["article_index"] = "running"
            result = await child("IndexWorkflow", {"source": source, "settings": p["settings"]},
                                 "index:" + fingerprint([source, p["settings"]["index"]]))
            self.branches["article_index"] = result["status"]
            return result
        async def summarize():
            return await child("SummaryWorkflow", {"paper": paper, "settings": p["settings"]},
                               summary_workflow_id(paper, p["settings"]))
        if workflow.patched("independent-article-branch-v1"):
            article_mode, summary_mode = await asyncio.gather(
                execute("pipeline_article_mode", {}), execute("pipeline_summary_mode", {}))
            self.branches["article_index"] = "deferred" if article_mode == "deferred" else "queued"
            self.branches["summary_index"] = "deferred" if summary_mode == "deferred" else "queued"
            if summary_mode == "enabled":
                await child("SummaryWorkflow", {"paper": paper, "settings": p["settings"]},
                            summary_workflow_id(paper, p["settings"]), wait=False, allow_completed=True)
            results = await asyncio.gather(
                index_article() if article_mode == "enabled" else asyncio.sleep(0),
                return_exceptions=True)
        elif workflow.patched("independent-summary-branch-v1"):
            mode = await execute("pipeline_summary_mode", {})
            self.branches["summary_index"] = "deferred" if mode == "deferred" else "queued"
            if mode == "enabled":
                await child("SummaryWorkflow", {"paper": paper, "settings": p["settings"]},
                            summary_workflow_id(paper, p["settings"]), wait=False, allow_completed=True)
            results = await asyncio.gather(index_article(), return_exceptions=True)
        else:
            results = await asyncio.gather(index_article(), summarize(), return_exceptions=True)
        errors = [str(item) for item in results if isinstance(item, BaseException)]
        if errors:
            raise ApplicationError("; ".join(errors))
        return {"status": "completed", **self.branches}


@workflow.defn
class SummaryWorkflow:
    def __init__(self):
        self.phase = "queued"

    @workflow.query
    def progress(self):
        return {"phase": self.phase, "parts_completed": int(self.phase != "generating"), "parts_total": 1}

    @workflow.run
    async def run(self, p: dict):
        self.phase = "generating"
        source = await execute("summarize_markdown", p, QUEUES["summary"], minutes=45)
        if source is None:
            return {"status": "superseded"}
        if source.get("status") == "deferred":
            self.phase = "deferred"
            return {"status": "deferred"}
        self.phase = "indexing"
        result = await child("IndexWorkflow", {"source": source, "settings": p["settings"]},
                    "index:" + fingerprint([source, p["settings"]["index"]]),
                    allow_completed=workflow.patched("repair-summary-index-v1"))
        self.phase = result["status"]
        return result


@workflow.defn
class IndexWorkflow:
    @workflow.run
    async def run(self, p: dict):
        prepared = p.get("prepared") or await execute("prepare_index", p, QUEUES["index"])
        if prepared is None:
            return {"status": "superseded"}
        if prepared.get("status") == "deferred":
            return {"status": "deferred"}
        cursor = p.get("cursor", 0)
        for _ in range(100):
            batch = await execute("index_batch", {**p, "generation": prepared["generation"], "cursor": cursor}, QUEUES["index"])
            if batch and batch.get("status") == "deferred":
                return {"status": "deferred"}
            if batch is None:
                return {"status": "completed"}
            cursor = batch["cursor"]
        workflow.continue_as_new({**p, "prepared": prepared, "cursor": cursor})


@workflow.defn
class ReprocessCatalogWorkflow:
    @workflow.run
    async def run(self, p: dict):
        after = p.get("after", "")
        for _ in range(2):
            rows = await execute("catalog_page", {"after": after})
            if not rows:
                return
            for row in rows:
                paper = await execute("paper_context", row)
                if paper:
                    await child("PaperWorkflow", {"paper": paper, "settings": p["settings"]},
                                paper_workflow_id(paper, p["settings"]), wait=False)
                after = row["arxiv_id"]
        workflow.continue_as_new({**p, "after": after})


@workflow.defn
class SummaryReconcileWorkflow:
    """Durable, bounded catalog reconciliation. SQL contains configuration/artifacts only."""
    @workflow.init
    def __init__(self, p: dict):
        self.woken = False
        self.state = p.get("progress") or {"passes": 0, "scanned": 0, "submitted": 0, "last_error": None}

    @workflow.signal
    def wake(self):
        self.woken = True

    @workflow.query
    def progress(self):
        return self.state

    @workflow.run
    async def run(self, p: dict):
        after = p.get("after", "")
        for _ in range(20):
            self.woken = False
            try:
                page = await execute("summary_reconciliation_page", {"after": after})
                self.state["article_mode"] = page["article_mode"]
                self.state["summary_mode"] = page["summary_mode"]
                self.state["last_error"] = None
                for item in page.get("missing_markdown", []):
                    await child("PaperWorkflow", item, paper_workflow_id(item["paper"], item["settings"]),
                                wait=False, allow_completed=True)
                for item in page["items"]:
                    await child("SummaryWorkflow", item, summary_workflow_id(item["paper"], item["settings"]),
                                wait=False, allow_completed=True)
                    self.state["submitted"] += 1
                self.state["scanned"] += page["scanned"]
                after = page["after"]
                self.state["after"] = after
                if (page["article_mode"] == "enabled" or page["summary_mode"] == "enabled") and not after:
                    self.state["passes"] += 1
                if after:
                    continue
            except (ActivityError, ChildWorkflowError) as error:
                self.state["last_error"] = str(error)
            try:
                await workflow.wait_condition(lambda: self.woken, timeout=timedelta(seconds=60))
            except asyncio.TimeoutError:
                pass
        workflow.continue_as_new({"after": after, "progress": self.state})


PIPELINE_WORKFLOWS = [ImportWorkflow, PaperWorkflow, SummaryWorkflow, IndexWorkflow, ReprocessCatalogWorkflow,
                      SummaryReconcileWorkflow]

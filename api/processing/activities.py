"""External operations only. Execution state lives in Temporal, never in SQL."""
import asyncio
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from functools import wraps
from hashlib import md5, sha256
import json
from uuid import NAMESPACE_URL, uuid5

import httpx
import requests
from botocore.exceptions import ClientError
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

import paper_repository as repository
from chunks import PAPER_COLLECTIONS
from embedding_schemas import EMBEDDING_OPENROUTER_MODEL
from llm_chat import ChatSettings
from processing.arxiv import Article, ArxivClient, ensure_bucket, store_source
from processing.config import (CHUNKER_VERSION, SUMMARY_PROMPT_VERSION, fingerprint, paper_workflow_id,
                               pipeline_settings, summary_generation, summary_workflow_id)
from processing.html_text import extract_markdown, extract_user_markdown
from processing.paper_embeddings import index_batch, prepare_source
from processing.summarizer import Summarizer, SummaryInputError


def heartbeats(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        async def pulse():
            while True:
                activity.heartbeat()
                await asyncio.sleep(10)
        task = asyncio.create_task(pulse())
        try:
            return await function(*args, **kwargs)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return wrapped


def paper_reference(paper):
    if paper is None:
        return None
    return {key: paper[key] for key in ("arxiv_id", "bucket", "object_key", "text_object_key", "source_version", "markdown_version")}


def document_reference(document):
    if document is None:
        return None
    return {key: (str(document[key]) if key == "space_file_id" else document[key]) for key in (
        "space_file_id", "bucket", "object_key", "content_type", "text_object_key", "text_version",
        "summary", "summary_version", "summary_generation", "source_version", "markdown_version",
    )}


class Activities:
    def __init__(self, state, tokenizer, max_tokens, client):
        self.state, self.s3 = state, state.rustfs
        self.tokenizer, self.max_tokens, self.client = tokenizer, max_tokens, client

    @activity.defn(name="pipeline_summary_mode")
    @heartbeats
    async def pipeline_summary_mode(self, p: dict):
        return (await repository.get_pipeline_settings())["summary_mode"]

    @activity.defn(name="pipeline_article_mode")
    @heartbeats
    async def pipeline_article_mode(self, p: dict):
        return (await repository.get_pipeline_settings())["article_mode"]

    async def reconciliation_due(self, identity, changed_at):
        try:
            info = await self.client.get_workflow_handle(identity).describe(rpc_timeout=timedelta(seconds=3))
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise
            return True
        if info.status == WorkflowExecutionStatus.RUNNING:
            return False
        return info.status == WorkflowExecutionStatus.COMPLETED or info.start_time < changed_at

    @activity.defn(name="summary_reconciliation_page")
    @heartbeats
    async def summary_reconciliation_page(self, p: dict):
        control = await repository.get_pipeline_settings()
        article_mode = control["article_mode"]
        summary_mode = control["summary_mode"]
        if article_mode == "deferred" and summary_mode == "deferred":
            return {"article_mode": article_mode, "summary_mode": summary_mode,
                    "items": [], "missing_markdown": [], "scanned": 0, "after": ""}
        settings = pipeline_settings()
        rows = await repository.catalog_page(p.get("after", ""), limit=25)
        items = []
        missing_markdown = []
        for row in rows:
            paper = await repository.get_paper(row["arxiv_id"])
            if not paper or not paper["object_key"].lower().endswith(".html"):
                continue
            if not paper["text_object_key"]:
                if (article_mode == "enabled" or summary_mode == "enabled") and await self.reconciliation_due(
                        paper_workflow_id(paper, settings), control["updated_at"]):
                    missing_markdown.append({"paper": paper_reference(paper), "settings": settings})
                continue
            article_source = {"arxiv_id": paper["arxiv_id"], "source_kind": "article",
                              "source_version": paper["markdown_version"]}
            article_missing = article_mode == "enabled" and (
                await repository.index_progress(article_source, settings))["status"] != "completed"
            cached = await repository.get_summary(paper["arxiv_id"], generation=summary_generation(paper, settings))
            summary_missing = summary_mode == "enabled"
            if cached:
                source = {"arxiv_id": paper["arxiv_id"], "source_kind": "summary",
                          "source_version": md5(cached["summary"].encode()).hexdigest()}
                summary_missing = (await repository.index_progress(source, settings))["status"] != "completed"
            # Activities already have bounded retries. Do not turn a permanent
            # LLM failure into a new paid attempt every reconciliation minute.
            identity = paper_workflow_id(paper, settings) if article_missing else summary_workflow_id(paper, settings)
            if (article_missing or summary_missing) and await self.reconciliation_due(identity, control["updated_at"]):
                items.append({"paper": paper_reference(paper), "settings": settings})
        return {"article_mode": article_mode, "summary_mode": summary_mode, "items": items,
                "missing_markdown": missing_markdown, "scanned": len(rows),
                "after": rows[-1]["arxiv_id"] if rows else ""}

    @activity.defn(name="discover_import")
    @heartbeats
    async def discover_import(self, p: dict):
        key = f"imports/{p['id']}/manifest.json"
        def discover():
            ensure_bucket(self.s3, p["bucket"])
            try:
                obj = self.s3.get_object(Bucket=p["bucket"], Key=key)
                with obj["Body"] as body:
                    return len(json.load(body))
            except ClientError as error:
                if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                    raise
            with requests.Session() as session:
                articles = list(ArxivClient(session).articles(
                    date.fromisoformat(p["date_from"]), date.fromisoformat(p["date_to"]), p["query"], lambda size: None,
                ))
            manifest = [{**asdict(item), "submitted_at": item.submitted_at.isoformat()} for item in articles]
            self.s3.put_object(Bucket=p["bucket"], Key=key, Body=json.dumps(manifest).encode(), ContentType="application/json")
            return len(manifest)
        count = await asyncio.to_thread(discover)
        return {"bucket": p["bucket"], "key": key, "count": count}

    @activity.defn(name="manifest_page")
    @heartbeats
    async def manifest_page(self, p: dict):
        def read():
            obj = self.s3.get_object(Bucket=p["manifest"]["bucket"], Key=p["manifest"]["key"])
            with obj["Body"] as body:
                items = json.load(body)
            return items[p["cursor"]:p["cursor"] + 25]
        return await asyncio.to_thread(read)

    @activity.defn(name="download_paper")
    @heartbeats
    async def download_paper(self, p: dict):
        article = Article(**{**p["article"], "submitted_at": datetime.fromisoformat(p["article"]["submitted_at"])})
        key = f"arxiv/{article.arxiv_id}.{p['source_format']}"
        def download():
            with requests.Session() as session:
                return store_source(ArxivClient(session), self.s3, p["bucket"], article.arxiv_id, key, p["source_format"])
        stored = await asyncio.to_thread(download)
        if stored is None:
            return {"paper": None, "outcome": "unavailable", "attempt": activity.info().attempt}
        obj, reused = stored
        paper = await repository.save_paper(
            article, p["bucket"], key, obj["LastModified"], obj["ContentLength"], p["space_id"], p["id"],
        )
        return {"paper": paper_reference(paper), "outcome": "reused" if reused else "available", "attempt": activity.info().attempt}

    @activity.defn(name="paper_context")
    @heartbeats
    async def paper_context(self, p: dict):
        return paper_reference(await repository.get_paper(p["arxiv_id"]))

    @activity.defn(name="document_context")
    @heartbeats
    async def document_context(self, p: dict):
        document = await repository.ensure_document(p["space_file_id"])
        return document_reference(document)

    @activity.defn(name="extract_markdown")
    @heartbeats
    async def extract(self, p: dict):
        paper = await repository.get_paper(p["paper"]["arxiv_id"])
        if paper is None or paper["source_version"] != p["paper"]["source_version"]:
            return None
        if paper["text_object_key"]:
            return paper_reference(paper)
        def extract():
            obj = self.s3.get_object(Bucket=paper["bucket"], Key=paper["object_key"])
            with obj["Body"] as body:
                content = extract_markdown(body, f"https://arxiv.org/html/{paper['arxiv_id']}").encode("utf-8")
            key = f"arxiv/{paper['arxiv_id']}/text-{sha256(content).hexdigest()}.md"
            self.s3.put_object(Bucket=paper["bucket"], Key=key, Body=content, ContentType="text/markdown; charset=utf-8")
            return key
        key = await asyncio.to_thread(extract)
        if not await repository.save_markdown(paper, key):
            return None
        return paper_reference(await repository.get_paper(paper["arxiv_id"]))

    @activity.defn(name="extract_document")
    @heartbeats
    async def extract_document(self, p: dict):
        document = await repository.get_document(p["space_file_id"])
        if document is None or document["text_object_key"]:
            return document
        def extract():
            obj = self.s3.get_object(Bucket=document["bucket"], Key=document["object_key"])
            with obj["Body"] as body:
                raw = body.read()
            content_type = (document["content_type"] or "").lower()
            if content_type.startswith("text/html") or document["object_key"].lower().endswith((".html", ".htm")):
                content = extract_user_markdown(raw, f"https://storage.local/{document['object_key']}")
            elif content_type.startswith("text/"):
                content = raw.decode("utf-8-sig")
            else:
                raise ApplicationError("Для пользовательского файла поддерживаются только HTML и текстовые форматы.", non_retryable=True)
            return content.encode("utf-8")
        content = await asyncio.to_thread(extract)
        key = f"spaces/{document['space_file_id']}/text-{sha256(content).hexdigest()}.md"
        self.s3.put_object(Bucket=document["bucket"], Key=key, Body=content, ContentType="text/markdown; charset=utf-8")
        if not await repository.save_document_text(document, key):
            return None
        return document_reference(await repository.get_document(document["space_file_id"]))

    @activity.defn(name="summarize_markdown")
    @heartbeats
    async def summarize(self, p: dict):
        if p.get("document"):
            return await self.summarize_document(p)
        if await self.pipeline_summary_mode({}) == "deferred":
            return {"status": "deferred"}
        paper, settings = p["paper"], p["settings"]
        generation = summary_generation(paper, settings)
        cached = await repository.get_summary(paper["arxiv_id"], generation=generation)
        if cached:
            return {"arxiv_id": paper["arxiv_id"], "source_kind": "summary", "source_version": md5(cached["summary"].encode()).hexdigest()}
        current = await repository.get_paper(paper["arxiv_id"])
        if current is None or current["markdown_version"] != paper["markdown_version"]:
            return None
        if settings["summary"]["prompt_version"] != SUMMARY_PROMPT_VERSION:
            raise ApplicationError("Summary prompt version changed", non_retryable=True)
        def read():
            obj = self.s3.get_object(Bucket=paper["bucket"], Key=paper["text_object_key"])
            with obj["Body"] as body:
                return body.read().decode("utf-8-sig")
        chat = replace(ChatSettings.from_env(openrouter_model_env="SUMMARY_OPENROUTER_MODEL"),
                       **{key: value for key, value in settings["summary"].items() if key in ("model", "reasoning_effort")},
                       provider_order=tuple(settings["summary"]["provider_order"]))
        if not chat.configured:
            raise ApplicationError("OPENROUTER_API_KEY is required", non_retryable=True)
        async def progress(done, total):
            activity.heartbeat(done, total)
        try:
            source = await asyncio.to_thread(read)
            async with httpx.AsyncClient(base_url=chat.base_url, timeout=httpx.Timeout(300, connect=5, read=1800)) as client:
                result = await Summarizer(client, chat).summarize(source, progress)
        except (SummaryInputError, UnicodeDecodeError) as error:
            raise ApplicationError(str(error), non_retryable=True) from error
        result_id = uuid5(NAMESPACE_URL, f"{paper['arxiv_id']}:{generation}")
        if not await repository.save_summary(paper, result, result_id, generation, settings["summary"]):
            return None
        return {"arxiv_id": paper["arxiv_id"], "source_kind": "summary", "source_version": md5(result.encode()).hexdigest()}

    @activity.defn(name="summarize_document")
    @heartbeats
    async def summarize_document(self, p: dict):
        if await self.pipeline_summary_mode({}) == "deferred":
            return {"status": "deferred"}
        document, settings = p["document"], p["settings"]
        generation = fingerprint([str(document["space_file_id"]), document["markdown_version"], settings["summary"]])
        if document.get("summary") and document.get("summary_generation") == generation:
            return {"space_file_id": document["space_file_id"], "source_kind": "summary",
                    "source_version": document["summary_version"]}
        current = await repository.get_document(document["space_file_id"])
        if current is None or current["markdown_version"] != document["markdown_version"]:
            return None
        if settings["summary"]["prompt_version"] != SUMMARY_PROMPT_VERSION:
            raise ApplicationError("Summary prompt version changed", non_retryable=True)
        def read():
            obj = self.s3.get_object(Bucket=current["bucket"], Key=current["text_object_key"])
            with obj["Body"] as body:
                return body.read().decode("utf-8-sig")
        chat = replace(ChatSettings.from_env(openrouter_model_env="SUMMARY_OPENROUTER_MODEL"),
                       **{key: value for key, value in settings["summary"].items() if key in ("model", "reasoning_effort")},
                       provider_order=tuple(settings["summary"]["provider_order"]))
        if not chat.configured:
            raise ApplicationError("OPENROUTER_API_KEY is required", non_retryable=True)
        async def progress(done, total):
            activity.heartbeat(done, total)
        try:
            source = await asyncio.to_thread(read)
            async with httpx.AsyncClient(base_url=chat.base_url, timeout=httpx.Timeout(300, connect=5, read=1800)) as client:
                result = await Summarizer(client, chat).summarize(source, progress)
        except (SummaryInputError, UnicodeDecodeError) as error:
            raise ApplicationError(str(error), non_retryable=True) from error
        if not await repository.save_document_summary(current, result, generation, settings["summary"]):
            return None
        return {"space_file_id": current["space_file_id"], "source_kind": "summary",
                "source_version": md5(result.encode()).hexdigest()}

    @activity.defn(name="prepare_index")
    @heartbeats
    async def prepare_index(self, p: dict):
        if p["source"]["source_kind"] == "summary" and await self.pipeline_summary_mode({}) == "deferred":
            return {"status": "deferred"}
        if p["source"]["source_kind"] == "article" and await self.pipeline_article_mode({}) == "deferred":
            return {"status": "deferred"}
        config = p["settings"]["index"]
        if config["max_tokens"] != self.max_tokens or config["model"] != EMBEDDING_OPENROUTER_MODEL or config["chunker"] != CHUNKER_VERSION:
            raise ApplicationError("Index worker configuration differs from this request", non_retryable=True)
        source = (await repository.get_document_source(
            p["source"]["space_file_id"], p["source"]["source_kind"], p["source"]["source_version"]
        ) if "space_file_id" in p["source"] else await repository.get_source(
            p["source"]["arxiv_id"], p["source"]["source_kind"], p["source"]["source_version"]
        ))
        if source is None:
            return None
        generation = fingerprint([source["source_version"], config])
        if source["source_kind"] == "article":
            def read():
                obj = self.s3.get_object(Bucket=source["bucket"], Key=source["text_object_key"])
                with obj["Body"] as body:
                    return body.read().decode("utf-8-sig")
            document = await asyncio.to_thread(read)
        else:
            document = source["summary"]
        count = await prepare_source(self.state, source, document, self.tokenizer, self.max_tokens, generation)
        return {"generation": generation, "count": count} if count else None

    @activity.defn(name="index_batch")
    @heartbeats
    async def index_batch_activity(self, p: dict):
        if p["source"]["source_kind"] == "summary" and await self.pipeline_summary_mode({}) == "deferred":
            return {"status": "deferred"}
        if p["source"]["source_kind"] == "article" and await self.pipeline_article_mode({}) == "deferred":
            return {"status": "deferred"}
        source = (await repository.get_document_source(
            p["source"]["space_file_id"], p["source"]["source_kind"], p["source"]["source_version"]
        ) if "space_file_id" in p["source"] else await repository.get_source(
            p["source"]["arxiv_id"], p["source"]["source_kind"], p["source"]["source_version"]
        ))
        if source is None:
            return None
        collection = PAPER_COLLECTIONS[source["source_kind"]]
        batch = await repository.chunk_batch(self.state, source, p["generation"], p["cursor"], collection, p.get("force", False))
        if not batch:
            return None
        saved, failed = await index_batch(self.state, batch, collection)
        if failed and "space_file_id" not in p["source"]:
            raise ApplicationError(f"{failed} chunks were rejected", non_retryable=True)
        return {"cursor": batch[-1].id, "count": saved}

    @activity.defn(name="dependency_result")
    @heartbeats
    async def dependency_result(self, p: dict):
        handle = self.client.get_workflow_handle(p["id"])
        info = await handle.describe()
        if info.status == WorkflowExecutionStatus.RUNNING:
            return {"done": False}
        if info.status == WorkflowExecutionStatus.COMPLETED:
            return {"done": True, "result": await handle.result()}
        return {"done": True, "error": info.status.name}

    @activity.defn(name="catalog_page")
    @heartbeats
    async def catalog_page(self, p: dict):
        return await repository.catalog_page(p.get("after", ""))

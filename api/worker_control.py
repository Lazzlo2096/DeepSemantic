import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware


WORKERS = ("arxiv-grabber", "paper-indexer", "summary-worker", "embeding-service")
STARTABLE = {"created", "exited"}
STOPPABLE = {"running", "restarting", "paused"}
DOCKER_STATES = STARTABLE | STOPPABLE | {"dead", "removing"}


class WorkerStatus(BaseModel):
    id: str
    state: str
    can_start: bool = False
    can_stop: bool = False


class WorkerCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["start", "stop"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.project = os.environ["DEEPSEMANTIC_COMPOSE_PROJECT"]
    app.state.external_embedding = os.getenv("EMBEDDING_WORKER_EXTERNAL") == "true"
    app.state.worker_locks = {worker: asyncio.Lock() for worker in WORKERS}
    transport = httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")
    async with httpx.AsyncClient(transport=transport, base_url="http://docker/v1.40/", timeout=5) as client:
        app.state.docker = client
        yield


app = FastAPI(lifespan=lifespan, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])


def is_external(request: Request, worker: str) -> bool:
    return worker == "embeding-service" and request.app.state.external_embedding


async def containers_by_worker(request: Request) -> dict[str, list[dict]]:
    groups = {worker: [] for worker in WORKERS}
    try:
        response = await request.app.state.docker.get("containers/json", params={
            "all": "true",
            "filters": json.dumps({"label": [
                f"com.docker.compose.project={request.app.state.project}",
                "com.docker.compose.oneoff=False",
            ]}),
        })
        response.raise_for_status()
    except httpx.HTTPError as error:
        logging.warning("Docker container list failed: %s", error)
        raise HTTPException(503, "Не удалось получить список контейнеров Docker.") from error

    for container in response.json():
        worker = container["Labels"].get("com.docker.compose.service")
        if worker in groups and not is_external(request, worker):
            groups[worker].append(container)
    return groups


def worker_status(request: Request, worker: str, containers: list[dict]) -> WorkerStatus:
    if is_external(request, worker):
        return WorkerStatus(id=worker, state="external")
    if not containers:
        return WorkerStatus(id=worker, state="missing")

    states = {container["State"] for container in containers}
    state = next(iter(states)) if len(states) == 1 else "mixed"
    return WorkerStatus(
        id=worker,
        state=state if state in DOCKER_STATES or state == "mixed" else "unknown",
        can_start=bool(states & STARTABLE),
        can_stop=bool(states & STOPPABLE),
    )


@app.get("/workers")
async def list_workers(request: Request) -> list[WorkerStatus]:
    groups = await containers_by_worker(request)
    return [worker_status(request, worker, containers) for worker, containers in groups.items()]


async def change_container(client: httpx.AsyncClient, container_id: str, action: str) -> None:
    response = await client.post(f"containers/{container_id}/{action}", timeout=30)
    if response.status_code != 304:
        response.raise_for_status()


@app.put("/workers/{worker}")
async def change_worker(request: Request, worker: str, command: WorkerCommand) -> WorkerStatus:
    if worker not in WORKERS:
        raise HTTPException(404, "Воркер не найден.")
    if is_external(request, worker):
        raise HTTPException(409, "Воркер запускается вне Docker Compose.")

    async with request.app.state.worker_locks[worker]:
        containers = (await containers_by_worker(request))[worker]
        if not containers:
            raise HTTPException(409, "Контейнер воркера ещё не создан.")

        eligible = STARTABLE if command.action == "start" else STOPPABLE
        results = await asyncio.gather(*(
            change_container(request.app.state.docker, container["Id"], command.action)
            for container in containers if container["State"] in eligible
        ), return_exceptions=True)
        for result in results:
            if isinstance(result, httpx.HTTPError):
                logging.warning("Docker %s failed for %s: %s", command.action, worker, result)
                raise HTTPException(502, "Docker не подтвердил команду. Проверь текущий статус воркера.") from result
            if isinstance(result, BaseException):
                raise result

        containers = (await containers_by_worker(request))[worker]
        return worker_status(request, worker, containers)

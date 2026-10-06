from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator

from .config import Config
from .jobs import JobWorker
from .lists import normalize_hostname, parse_routing_list
from .router import RouterClient
from .storage import Store


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    hostname: str
    action: Literal["update"]

    @field_validator("hostname")
    @classmethod
    def validate_hostname(cls, value: str) -> str:
        normalize_hostname(value)
        return value.strip().rstrip("/")


class AcceptedJob(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"
    status_url: str


class JobEvent(BaseModel):
    created_at: str
    message: str


class JobResult(BaseModel):
    job_id: str
    hostname: str
    action: Literal["update"]
    status: Literal["queued", "running", "succeeded", "failed"]
    created_at: str
    started_at: str | None
    finished_at: str | None
    message: str
    error: str | None
    events: list[JobEvent]


def create_app(config: Config, *, client_factory=RouterClient, start_worker: bool = True) -> FastAPI:
    store = Store(config.data_dir / "jobs.sqlite3")
    worker = JobWorker(config, store, client_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            worker.start()
        else:
            store.initialize()
        try:
            yield
        finally:
            if start_worker:
                await asyncio.to_thread(worker.stop)

    def authorize(x_api_key: Annotated[str | None, Header()] = None) -> None:
        if x_api_key is None or not secrets.compare_digest(x_api_key.encode("utf-8"), config.api_key.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Отсутствует или неверен API-ключ.")

    # The HTTP contract is documented in the README; all enabled routes require a key.
    app = FastAPI(title="Router Configurator", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.worker = worker

    @app.post("/api/v1/router-configurations", status_code=202, response_model=AcceptedJob, dependencies=[Depends(authorize)])
    def update_router(request: UpdateRequest) -> AcceptedJob:
        try:
            routing = parse_routing_list(config.list_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(status_code=503, detail="Настроенный лист недоступен или некорректен.") from None
        job_id = store.enqueue(request.hostname, routing)
        worker.notify()
        return AcceptedJob(job_id=job_id, status_url=f"/api/v1/jobs/{job_id}")

    @app.get("/api/v1/jobs/{job_id}", response_model=JobResult, dependencies=[Depends(authorize)])
    def get_job(job_id: str) -> dict:
        result = store.get(job_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Задача не найдена.")
        return result

    return app

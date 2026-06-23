"""
FastAPI application entry point.

Run locally with:  uvicorn secfi_platform.api.main:app --reload --port 8000
Run in container with: see infra/docker/Dockerfile (gunicorn + uvicorn workers)

This service is READ-MOSTLY (see api/routers/desk.py docstring): it
serves analytics and recommendations and records human approval
decisions. It does not place orders or change live pricing/limits. Any
endpoint that appears to do so should be treated as a bug and reported
per docs/governance.md escalation paths.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from secfi_platform.api.routers.desk import router as desk_router
from secfi_platform.api.routers.risk_extended import router as risk_extended_router
from secfi_platform.api.schemas import HealthCheckResponse
from secfi_platform.common.config import load_config
from secfi_platform.common.logging_setup import configure_logging

configure_logging(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.config = load_config(environment="dev")
    yield


app = FastAPI(
    title="SecFi Prime Platform API",
    description="Securities lending, financing, repo, and prime brokerage desk decision-support API.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to the firm's internal app gateway origin(s) in prod config
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(desk_router)
app.include_router(risk_extended_router)


@app.get("/healthz", response_model=HealthCheckResponse)
def healthz():
    cfg = app.state.config
    return HealthCheckResponse(status="ok", environment=cfg.environment, config_sources=list(cfg.source_files))

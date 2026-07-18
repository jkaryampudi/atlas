"""Atlas API — the single control surface (Doc 06).

Serves the Atlas Console (single-file HTML ops UI, pure API client) at
/console — same origin as the API it reads. Deviation from Doc 07's Streamlit
choice, recorded here: same 'pure API client' constraint, better surface.

With ATLAS_INPROC_SCHEDULER=1 this process ALSO fires the daily cycle and
backup on schedule (atlas.ops.scheduler) — the Mac interim where launchd is
TCC-blocked; Linux/systemd deployments leave it unset (the timers own it).
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse

from atlas.api.routers import (
    audit,
    learning,
    market,
    portfolio,
    quant,
    reporting,
    research,
    risk,
    system,
    trading,
)

_CONSOLE = Path(__file__).resolve().parents[1] / "dashboard" / "console.html"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    task: asyncio.Task[None] | None = None
    if os.environ.get("ATLAS_INPROC_SCHEDULER") == "1":
        from atlas.ops.scheduler import scheduler_loop

        task = asyncio.create_task(scheduler_loop())
    yield
    if task is not None:
        task.cancel()


app = FastAPI(title="Atlas AI Capital", version="0.1.0", lifespan=_lifespan)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/console")


@app.get("/console", include_in_schema=False)
def console() -> FileResponse:
    return FileResponse(_CONSOLE, media_type="text/html")
app.include_router(system.router, prefix="/v1/system", tags=["system"])
app.include_router(market.router, prefix="/v1/market", tags=["market"])
app.include_router(portfolio.router, prefix="/v1/portfolio", tags=["portfolio"])
app.include_router(audit.router, prefix="/v1/audit", tags=["audit"])
app.include_router(quant.router, prefix="/v1/quant", tags=["quant"])
app.include_router(research.router, prefix="/v1/research", tags=["research"])
app.include_router(risk.router, prefix="/v1/risk", tags=["risk"])
app.include_router(trading.router, prefix="/v1/trading", tags=["trading"])
app.include_router(learning.router, prefix="/v1/learning", tags=["learning"])
app.include_router(reporting.router, prefix="/v1/reporting", tags=["reporting"])

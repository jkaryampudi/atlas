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
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from atlas.core.config import get_settings

from atlas.api.routers import (
    audit,
    factory,
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
_DOSSIER = Path(__file__).resolve().parents[1] / "dashboard" / "dossier.html"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # F-019/F-020: fail closed at startup if the deployment demands least
    # privilege but the DB runtime role is a superuser (which would bypass the
    # audit append-only + monotonic-anchor triggers).
    if get_settings().db_require_least_privilege:
        from atlas.core.db import session_scope
        from atlas.core.db_privilege import assert_least_privilege_runtime
        with session_scope() as _s:
            assert_least_privilege_runtime(_s)
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
def console() -> HTMLResponse:
    """Serve the console with the loopback operator token injected (F-016). The
    token is read from the deployment env at serve time and never written to the
    static file; substitution happens only for this same-origin loopback page."""
    import json

    from atlas.api.auth import console_token

    html = _CONSOLE.read_text(encoding="utf-8")
    token = console_token()
    if token:
        html = html.replace(
            'window.__ATLAS_TOKEN__ = "";',
            f"window.__ATLAS_TOKEN__ = {json.dumps(token)};", 1)
    return HTMLResponse(html, media_type="text/html")


@app.get("/console/dossier", include_in_schema=False)
def dossier() -> FileResponse:
    """Standalone full-page stock dossier (reads ?id=<source_pick_id> and
    fetches /v1/research/source-picks/{id}/dossier)."""
    return FileResponse(_DOSSIER, media_type="text/html")
app.include_router(system.router, prefix="/v1/system", tags=["system"])
app.include_router(market.router, prefix="/v1/market", tags=["market"])
app.include_router(portfolio.router, prefix="/v1/portfolio", tags=["portfolio"])
app.include_router(audit.router, prefix="/v1/audit", tags=["audit"])
app.include_router(quant.router, prefix="/v1/quant", tags=["quant"])
app.include_router(factory.router, prefix="/v1/factory", tags=["factory"])
app.include_router(research.router, prefix="/v1/research", tags=["research"])
app.include_router(risk.router, prefix="/v1/risk", tags=["risk"])
app.include_router(trading.router, prefix="/v1/trading", tags=["trading"])
app.include_router(learning.router, prefix="/v1/learning", tags=["learning"])
app.include_router(reporting.router, prefix="/v1/reporting", tags=["reporting"])

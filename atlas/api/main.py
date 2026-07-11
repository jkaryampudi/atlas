"""Atlas API — Phase 1 read-only surface (Doc 06).

Also serves the Atlas Console (single-file HTML ops UI, pure API client) at
/console — same origin as the API it reads. Deviation from Doc 07's Streamlit
choice, recorded here: same 'pure API client' constraint, better surface.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse

from atlas.api.routers import audit, market, portfolio, quant, research, system

_CONSOLE = Path(__file__).resolve().parents[1] / "dashboard" / "console.html"

app = FastAPI(title="Atlas AI Capital", version="0.1.0")


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

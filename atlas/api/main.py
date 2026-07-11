"""Atlas API — Phase 1 read-only surface (Doc 06)."""
from __future__ import annotations

from fastapi import FastAPI

from atlas.api.routers import audit, market, portfolio, quant, research, system

app = FastAPI(title="Atlas AI Capital", version="0.1.0")
app.include_router(system.router, prefix="/v1/system", tags=["system"])
app.include_router(market.router, prefix="/v1/market", tags=["market"])
app.include_router(portfolio.router, prefix="/v1/portfolio", tags=["portfolio"])
app.include_router(audit.router, prefix="/v1/audit", tags=["audit"])
app.include_router(quant.router, prefix="/v1/quant", tags=["quant"])
app.include_router(research.router, prefix="/v1/research", tags=["research"])

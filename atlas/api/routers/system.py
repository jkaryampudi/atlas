from __future__ import annotations

from fastapi import APIRouter

from atlas.core.config import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict[str, object]:
    s = get_settings()
    return {
        "status": "ok",
        "trading_mode": s.trading_mode,
        "armed": False,  # live arming is a Phase 6 mechanism; always false until then
        "limit_mode": s.limit_mode,
        "base_currency": s.base_currency,
    }


@router.get("/mode")
def mode() -> dict[str, object]:
    s = get_settings()
    return {"trading_mode": s.trading_mode, "armed": False}

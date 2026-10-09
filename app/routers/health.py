"""Liveness + readiness probes."""
from fastapi import APIRouter

from ..db import check_db

router = APIRouter(tags=["system"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/ready")
def ready():
    if not check_db():
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="DB unavailable")
    return {"status": "ready"}

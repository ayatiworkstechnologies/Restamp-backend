"""Public location data shared by the RESTAMP app and website.

  GET /locations/agent-groups?q=&zone=   agent location options: exactly one
                                         per PIN code, from the published
                                         Agent Location Master workbook.

Both clients use this one endpoint, so they always show the same options.
New admin additions appear here as soon as they are published (no app
release needed).
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..services import location_master as location_master_svc

router = APIRouter(prefix="/locations", tags=["locations"])


@router.get("/agent-groups")
def agent_location_groups(
    db: Session = Depends(get_db),
    q: str | None = Query(default=None, max_length=100),
    zone: Literal["North Chennai", "Central Chennai", "South Chennai"] | None = Query(default=None),
):
    return location_master_svc.list_groups(db, q=q, zone=zone)

"""RESTAMP in-app notifications — the caller's own notifications only.

Authentication: `get_current_user` (valid JWT AND an active account, so a
suspended/closed account is 403 "Account disabled" like every other
authenticated route). Authorization: role-agnostic but strictly user-scoped —
every query filters on the caller's id; another user's notification is a 404
(identical to a missing one, so existence never leaks). No endpoint accepts a
recipient/user id. `audience` is a display filter for the app's current mode,
never an authorization input. GETs never write; POST/DELETE commit only after
the service call succeeded.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..security import get_current_user
from ..services import notifications as notification_svc

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=schemas.NotificationsResponse)
def list_my_notifications(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    audience: schemas.NotificationAudience | None = Query(default=None),
    category: schemas.NotificationCategory | None = Query(default=None),
    unread_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Newest first, deleted rows excluded. `unread_count` is the badge value
    for the same audience filter (it ignores category/unread_only)."""
    items, total, unread = notification_svc.list_notifications(
        db, int(user.id), page, page_size, audience=audience, category=category, unread_only=unread_only
    )
    return schemas.NotificationsResponse(
        items=items, page=page, page_size=page_size, total=total, unread_count=unread
    )


@router.get("/unread-count", response_model=schemas.NotificationUnreadCountOut)
def my_unread_count(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    audience: schemas.NotificationAudience | None = Query(default=None),
):
    return schemas.NotificationUnreadCountOut(
        unread_count=notification_svc.unread_count(db, int(user.id), audience)
    )


@router.post("/read-all", response_model=schemas.NotificationsReadAllOut)
def mark_all_my_notifications_read(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    audience: schemas.NotificationAudience | None = Query(default=None),
):
    """Marks the caller's unread notifications read (optionally one audience)."""
    updated = notification_svc.mark_all_read(db, int(user.id), audience)
    db.commit()
    return schemas.NotificationsReadAllOut(
        updated=updated, unread_count=notification_svc.unread_count(db, int(user.id), audience)
    )


@router.post("/{notification_id}/read", response_model=schemas.NotificationOut)
def mark_my_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Idempotent. 404 when missing, deleted or not the caller's."""
    item = notification_svc.mark_read(db, int(user.id), notification_id)
    db.commit()
    return item


@router.delete("/{notification_id}", status_code=204)
def delete_my_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Soft delete. 404 when missing or not the caller's; repeating it on the
    caller's own already-deleted row is a 204 no-op."""
    notification_svc.delete_notification(db, int(user.id), notification_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

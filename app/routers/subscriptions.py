"""RESTAMP user-facing subscriptions.

  GET  /subscriptions/plans?role=   public: active, visible plans (admin-managed catalogue)
  GET  /subscriptions/me            the caller's current plan per role, limits and usage
  GET  /subscriptions/payments      the caller's own payment records (read-only; paid only
                                    for a real verified gateway payment)
  POST /subscriptions/bypass        TEMPORARY payment bypass: activate a plan for the
                                    caller without payment (provider BYPASS, never paid).
                                    The plan's role must equal the caller's current role
                                    (read from the DB). Idempotent. Off with RESTAMP_PAYMENT_BYPASS=0.
Users can never change another user's plan here; admin changes live in /admin.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..security import ensure_active_user, get_current_user_id
from ..services import subscriptions as sub_svc

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.get("/plans", response_model=schemas.SubscriptionPlansResponse)
def public_plans(
    db: Session = Depends(get_db),
    role: Literal["BUYER", "OWNER", "BROKER"] | None = Query(default=None),
):
    return schemas.SubscriptionPlansResponse(items=sub_svc.list_plans(db, role=role, public=True))


@router.get("/me", response_model=schemas.MySubscriptionsOut)
def my_subscriptions(db: Session = Depends(get_db), user_id: int = Depends(get_current_user_id)):
    ensure_active_user(db, user_id)
    return sub_svc.my_subscriptions(db, user_id)


@router.post("/bypass", response_model=schemas.UserSubscriptionOut)
def bypass_activate(
    body: schemas.PaymentBypassIn,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    ensure_active_user(db, user_id)
    out = sub_svc.bypass_activate(db, user_id, body.plan_code)
    db.commit()
    return out



@router.get("/payments", response_model=schemas.MyPaymentsResponse)
def my_payments(
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
):
    ensure_active_user(db, user_id)
    items, total = sub_svc.my_payments(db, user_id, page, page_size)
    return schemas.MyPaymentsResponse(items=items, page=page, page_size=page_size, total=total)

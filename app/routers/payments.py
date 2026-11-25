"""RESTAMP online payments (Razorpay) routes.

Endpoints (see services/payments.py for the flow and the fail-closed rules):
  GET  /payments/config           the PUBLIC checkout config (provider + key id)
  POST /payments/checkout         create an order for a buyer/agent plan (auth)
  POST /payments/verify           confirm a checkout payment by signature (auth)
  POST /payments/webhook          Razorpay's signed events (public; HMAC gate)
  GET  /payments/history          the caller's own payments (auth)
  GET  /payments/{id}/invoice     invoice details for a SUCCESS payment (auth)

A subscription is activated only after a verified signature — never by a
checkout click or a frontend response alone. With the provider unconfigured
every endpoint returns 503 and nothing is simulated.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import schemas
from ..db import get_db
from ..security import ensure_active_user, get_current_user_id
from ..services import payments as payments_svc

router = APIRouter(prefix="/payments", tags=["payments"])

# Keep in sync with the body cap middleware in main.py.
_WEBHOOK_MAX_BODY_BYTES = 65_536


def _active_user(db: Session = Depends(get_db), user_id: int = Depends(get_current_user_id)) -> int:
    ensure_active_user(db, user_id)
    return user_id


class CheckoutIn(BaseModel):
    kind: Literal["buyer", "agent", "owner", "credits"]
    code: str = Field(min_length=1, max_length=50)
    localities: list[str] = Field(default_factory=list, max_length=50)
    group_ids: list[int] = Field(default_factory=list, max_length=50)


class VerifyIn(BaseModel):
    razorpay_order_id: str = Field(min_length=1, max_length=100)
    razorpay_payment_id: str = Field(min_length=1, max_length=100)
    razorpay_signature: str = Field(min_length=1, max_length=256)


@router.get("/config", response_model=schemas.PaymentConfigOut)
def get_config(user_id: int = Depends(_active_user)):
    """Whether online payments are available and the PUBLIC Razorpay key id.
    The key secret and webhook secret never leave the server."""
    return payments_svc.config_public()


@router.post("/checkout", response_model=schemas.PaymentCheckoutOut, response_model_exclude_none=True)
async def create_checkout(
    body: CheckoutIn,
    db: Session = Depends(get_db),
    user_id: int = Depends(_active_user),
):
    """Create a Razorpay order for a plan. The amount comes from the server's
    plan constants (never the client); agent localities are validated here.
    503 when the provider is unconfigured; 502 when the gateway fails."""
    result = await payments_svc.create_checkout(db, user_id, body.kind, body.code, body.localities, body.group_ids)
    db.commit()
    return result


@router.post("/verify", response_model=schemas.PaymentVerifyOut)
async def verify_payment(
    body: VerifyIn,
    db: Session = Depends(get_db),
    user_id: int = Depends(_active_user),
):
    """Verify the checkout signature (computed with the server's secret, only
    relayed by the client), re-check the order server-to-server, then activate
    the plan. Idempotent: an already-confirmed payment returns its status."""
    result = await payments_svc.verify(
        db,
        user_id,
        body.razorpay_order_id,
        body.razorpay_payment_id,
        body.razorpay_signature,
    )
    db.commit()
    return result


@router.post("/webhook", response_model=schemas.PaymentWebhookAckOut, response_model_exclude_none=True)
async def payment_webhook(request: Request, db: Session = Depends(get_db)):
    """Razorpay event receiver. Public (Razorpay's servers have no account):
    the HMAC of the raw body against the webhook secret is the only gate, the
    64 KB cap in main.py bounds the body. Idempotent per event id."""
    body = await request.body()
    if len(body) > _WEBHOOK_MAX_BODY_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Request body too large")
    result = await payments_svc.handle_webhook(
        db,
        request.headers.get("x-razorpay-signature"),
        body,
        request.headers.get("x-razorpay-event-id"),
    )
    db.commit()
    return result


@router.get("/history", response_model=schemas.PaymentHistoryOut)
def payment_history(
    db: Session = Depends(get_db),
    user_id: int = Depends(_active_user),
):
    """The caller's own payments, newest first (real and simulated records)."""
    return {"items": payments_svc.history(db, user_id)}


@router.get("/{payment_id}/invoice", response_model=schemas.PaymentInvoiceOut)
def payment_invoice(
    payment_id: int,
    db: Session = Depends(get_db),
    user_id: int = Depends(_active_user),
):
    """Invoice details for one of the caller's SUCCESS payments. 409 until
    the payment succeeded; other users' payments return 404."""
    return payments_svc.invoice(db, user_id, payment_id)

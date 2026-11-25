"""RESTAMP admin API (backend for the Admin website; no frontend here).

Authentication is the normal JWT (OTP / Google login). Admin rights come only
from an admin_accounts row, checked server-side on EVERY request together
with one permission of that admin's role (deps.require_admin_permission,
matrix in services/admin_rbac.py). Role switching (POST /users/me/role) can
never create admin rights, and there is no public admin registration; the
first admin is created out of band by an operator (see the final report /
docs), later admins via POST /admin/staff (settings.manage_team).

Endpoints (permission in brackets; "admin" = any admin):
  GET  /admin/me                          [admin]  own role + permissions
  GET  /admin/roles                       [admin]  role/permission matrix
  GET  /admin/dashboard                   [properties.view]
  GET  /admin/analytics                   [reports.view]
  GET  /admin/listings                    [properties.view]
  GET  /admin/listings/{id}               [properties.view]
  POST /admin/listings/{id}/verify        [properties.approve]  PENDING -> VERIFIED
  POST /admin/listings/{id}/reject        [properties.reject]   PENDING -> REJECTED (reason required)
  GET  /admin/users                       [users.view]
  GET  /admin/users/{id}                  [users.view]
  POST /admin/users/{id}/suspend          [users.suspend]  (reason required)
  POST /admin/users/{id}/reactivate       [users.suspend]
  GET  /admin/enquiries                   [enquiries.view]  read-only
  GET  /admin/enquiries/{id}              [enquiries.view]  thread, never lead notes
  GET  /admin/subscriptions               [reports.view]  read-only; flags SIMULATED payments
  GET  /admin/payments                    [reports.view]  read-only; flags SIMULATED payments
  GET  /admin/plans, /admin/plan-history  [reports.view]
  POST /admin/plans, PATCH /admin/plans/{id}, POST /admin/plans/{id}/status  [settings.configure_platform]
  POST /admin/users/{id}/subscription     [users.suspend]  assign / change plan (audited)
  POST /admin/subscriptions/{id}/extend|suspend|reactivate|cancel  [users.suspend]
  GET  /admin/audit-logs                  [settings.view_audit_logs]
  GET  /admin/email-deliveries            [settings.view_audit_logs]  read-only delivery records
  GET  /admin/staff                       [settings.manage_team]
  POST /admin/staff                       [settings.manage_team]
  PATCH /admin/staff/{user_id}            [settings.manage_team]
  DELETE /admin/staff/{user_id}           [settings.manage_team]
  (TEMPORARILY DISABLED — Chennai area seeding paused: the five
   agent-locality endpoints below answer 404 unless RESTAMP_AREA_SEEDING_ENABLED=1
   in a non-production process.)
  GET  /admin/agent-localities            [users.view]  catalog + enabled state
  PATCH /admin/agent-localities/{id}      [settings.configure_platform]  enable/disable
  GET  /admin/agents/{user_id}/localities [users.view]  coverage + plan allowance
  POST /admin/agents/{user_id}/localities [settings.configure_platform]  assign (plan limit)
  DELETE /admin/agents/{user_id}/localities/{locality_id}
                                          [settings.configure_platform]  revoke one
Writes commit only on success; every write is audited (services/admin.py).
GET /admin/ping and POST /admin/grants live in users.py (unchanged paths).
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from .. import schemas
from ..config import area_seeding_enabled
from ..db import get_db
from ..deps import require_admin, require_admin_permission
from ..services import admin as admin_svc
from ..services import capabilities as capabilities_svc
from ..services import payment_refunds as refund_svc
from ..services import contact_credits as credit_svc
from ..services import subscriptions as sub_svc
from ..services import agent_plans as agent_plan_svc
from ..services import email as email_svc
from ..services import localities as locality_svc
from ..services import push as push_svc

router = APIRouter(prefix="/admin", tags=["admin"])

VerificationStatus = Literal["PENDING", "VERIFIED", "REJECTED"]
ListingStatus = Literal["AVAILABLE", "SOLD", "RENTED", "LEASED", "CLOSED", "EXPIRED"]
PropType = Literal["APARTMENT", "VILLA", "HOUSE", "COMMERCIAL", "PLOT", "AGRICULTURAL_LAND"]
UserRole = Literal["BUYER", "OWNER", "BROKER"]
AccountStatus = Literal["active", "suspended", "closed"]
EnquiryStatus = Literal["NEW", "CONTACTED", "CLOSED"]


# --- identity -------------------------------------------------------------------------

@router.get("/me", response_model=schemas.AdminMeOut)
def admin_me(db: Session = Depends(get_db), admin_id: int = Depends(require_admin)):
    return admin_svc.admin_profile(db, admin_id)


@router.get("/roles", response_model=schemas.AdminRolesResponse)
def admin_roles(_admin: int = Depends(require_admin)):
    return schemas.AdminRolesResponse(items=admin_svc.roles_matrix())


# --- dashboard / analytics ---------------------------------------------------------------

@router.get("/dashboard", response_model=schemas.AdminDashboardOut)
def admin_dashboard(db: Session = Depends(get_db), _admin: int = Depends(require_admin_permission("properties.view"))):
    return admin_svc.dashboard(db)


@router.get("/analytics", response_model=schemas.AdminAnalyticsOut)
def admin_analytics(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("reports.view")),
    months: int = Query(default=6, ge=1, le=24),
):
    return admin_svc.analytics(db, months)


# --- listings -----------------------------------------------------------------------

@router.get("/listings", response_model=schemas.AdminListingsResponse)
def admin_list_listings(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("properties.view")),
    verification_status: VerificationStatus | None = Query(default=None),
    listing_status: ListingStatus | None = Query(default=None),
    property_type: PropType | None = Query(default=None),
    owner_user_id: int | None = Query(default=None, ge=1),
    q: str | None = Query(default=None, max_length=255),
    include_removed: bool = Query(default=False),
    sort: Literal["newest", "submitted_oldest"] = Query(default="newest"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    items, total = admin_svc.list_listings(
        db, page, page_size, verification_status=verification_status, listing_status=listing_status,
        property_type=property_type, owner_user_id=owner_user_id, q=q,
        include_removed=include_removed, sort=sort,
    )
    return schemas.AdminListingsResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/listings/{listing_id}", response_model=schemas.AdminListingDetailOut)
def admin_get_listing(
    listing_id: int, db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("properties.view")),
):
    return admin_svc.get_listing(db, listing_id)


@router.post("/listings/{listing_id}/verify", response_model=schemas.AdminListingDetailOut)
def admin_verify_listing(
    listing_id: int,
    body: schemas.AdminVerifyIn | None = None,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("properties.approve")),
):
    """PENDING -> VERIFIED (buyer-visible while AVAILABLE). Idempotent when
    already VERIFIED; 422 from REJECTED or for removed listings."""
    admin_svc.decide_listing(db, admin_id, listing_id, "VERIFIED", notes=body.notes if body else None)
    db.commit()
    return admin_svc.get_listing(db, listing_id)


@router.post("/listings/{listing_id}/reject", response_model=schemas.AdminListingDetailOut)
def admin_reject_listing(
    listing_id: int,
    body: schemas.AdminRejectIn,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("properties.reject")),
):
    """PENDING -> REJECTED with a required reason (owner may resubmit through
    the existing owner flow). Idempotent when already REJECTED; 422 from
    VERIFIED or for removed listings."""
    admin_svc.decide_listing(db, admin_id, listing_id, "REJECTED", reason=body.reason, category=body.category)
    db.commit()
    return admin_svc.get_listing(db, listing_id)


# --- users ----------------------------------------------------------------------------

@router.get("/users", response_model=schemas.AdminUsersResponse)
def admin_list_users(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("users.view")),
    role: UserRole | None = Query(default=None),
    account_status: AccountStatus | None = Query(default=None, alias="status"),
    is_admin: bool | None = Query(default=None),
    q: str | None = Query(default=None, max_length=255),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    items, total = admin_svc.list_users(
        db, page, page_size, role=role, account_status=account_status, admins_only=is_admin, q=q)
    return schemas.AdminUsersResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/users/{user_id}", response_model=schemas.AdminUserDetailOut)
def admin_get_user(
    user_id: int, db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("users.view")),
):
    return admin_svc.get_user(db, user_id)


@router.post("/users/{user_id}/suspend", response_model=schemas.AdminUserDetailOut)
def admin_suspend_user(
    user_id: int,
    body: schemas.AdminSuspendIn,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("users.suspend")),
):
    """Suspended accounts are refused at login and on every authenticated
    request (security.ensure_active_user). Idempotent; not for yourself;
    another admin's account needs settings.manage_team."""
    admin_svc.set_user_status(db, admin_id, user_id, "suspended", body.reason)
    db.commit()
    return admin_svc.get_user(db, user_id)


@router.post("/users/{user_id}/reactivate", response_model=schemas.AdminUserDetailOut)
def admin_reactivate_user(
    user_id: int,
    body: schemas.AdminReactivateIn | None = None,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("users.suspend")),
):
    admin_svc.set_user_status(db, admin_id, user_id, "active", body.reason if body else None)
    db.commit()
    return admin_svc.get_user(db, user_id)


# --- enquiries (read-only) ----------------------------------------------------------

@router.get("/enquiries", response_model=schemas.AdminEnquiriesResponse)
def admin_list_enquiries(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("enquiries.view")),
    enquiry_status: EnquiryStatus | None = Query(default=None, alias="status"),
    property_listing_id: int | None = Query(default=None, ge=1),
    buyer_user_id: int | None = Query(default=None, ge=1),
    owner_user_id: int | None = Query(default=None, ge=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    items, total = admin_svc.list_enquiries(
        db, page, page_size, enquiry_status=enquiry_status, property_listing_id=property_listing_id,
        buyer_user_id=buyer_user_id, owner_user_id=owner_user_id)
    return schemas.AdminEnquiriesResponse(items=items, page=page, page_size=page_size, total=total)


# --- billing (read-only; no payment gateway exists) ----------------------------------

@router.get("/subscriptions", response_model=schemas.AdminSubscriptionsResponse)
def admin_list_subscriptions(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("reports.view")),
    sub_status: Literal["ACTIVE", "EXPIRED", "CANCELLED", "SUSPENDED"] | None = Query(default=None, alias="status"),
    user_id: int | None = Query(default=None, ge=1),
    role: UserRole | None = Query(default=None),
    plan_code: str | None = Query(default=None, max_length=50),
    q: str | None = Query(default=None, max_length=255),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    items, total = admin_svc.list_subscriptions(db, page, page_size, sub_status=sub_status, user_id=user_id,
                                                plan_role=role, plan_code=plan_code, q=q)
    return schemas.AdminSubscriptionsResponse(items=items, page=page, page_size=page_size, total=total)


# --- subscription management (services/subscriptions.py) -------------------------------

@router.get("/plans", response_model=schemas.SubscriptionPlansResponse)
def admin_list_plans(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("reports.view")),
    role: UserRole | None = Query(default=None),
    include_archived: bool = Query(default=False),
):
    return schemas.SubscriptionPlansResponse(items=sub_svc.list_plans(db, role=role, include_archived=include_archived))


@router.post("/plans", response_model=schemas.SubscriptionPlanOut, status_code=201)
def admin_create_plan(
    body: schemas.SubscriptionPlanCreate,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("settings.configure_platform")),
):
    out = sub_svc.create_plan(db, admin_id, body.model_dump())
    db.commit()
    return out


@router.patch("/plans/{plan_id}", response_model=schemas.SubscriptionPlanOut)
def admin_update_plan(
    plan_id: int,
    body: schemas.SubscriptionPlanUpdate,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("settings.configure_platform")),
):
    data = body.model_dump(exclude_unset=True)
    note = data.pop("change_note", None)
    out = sub_svc.update_plan(db, admin_id, plan_id, data, note)
    db.commit()
    return out


@router.post("/plans/{plan_id}/status", response_model=schemas.SubscriptionPlanOut)
def admin_set_plan_status(
    plan_id: int,
    body: schemas.SubscriptionPlanStatusIn,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("settings.configure_platform")),
):
    out = sub_svc.set_plan_status(db, admin_id, plan_id, body.status)
    db.commit()
    return out


@router.get("/plan-history", response_model=schemas.PlanHistoryResponse)
def admin_plan_history(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("reports.view")),
    plan_id: int | None = Query(default=None, ge=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
):
    items, total = sub_svc.plan_history(db, plan_id, page, page_size)
    return schemas.PlanHistoryResponse(items=items, page=page, page_size=page_size, total=total)


@router.post("/users/{user_id}/subscription", response_model=schemas.UserSubscriptionOut)
def admin_assign_subscription(
    user_id: int,
    body: schemas.AdminAssignSubscriptionIn,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("users.suspend")),
):
    """Assign / change (upgrade, downgrade) a user's plan. Ends the user's current
    plan of that role (history kept), records an ADMIN (unpaid) payment row, audits."""
    out = sub_svc.admin_assign(db, admin_id, user_id, body.plan_code, body.reason, body.validity_days)
    db.commit()
    return out


@router.post("/users/{user_id}/poster-type", response_model=schemas.PosterTypeConvertOut)
def admin_convert_poster_type(
    user_id: int,
    body: schemas.PosterTypeConvertIn,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("users.suspend")),
):
    """Owner <-> Dealer conversion (admin-only, audited, reason required).
    409 while a subscription of the current poster type is active; never
    cancels, refunds or transfers anything; listings stay with the user."""
    target = "BROKER" if body.poster_type == "DEALER" else body.poster_type
    out = capabilities_svc.convert_poster_type(db, admin_id, user_id, target, body.reason)
    db.commit()
    return out


@router.post("/subscriptions/{sub_id}/extend", response_model=schemas.UserSubscriptionOut)
def admin_extend_subscription(
    sub_id: int,
    body: schemas.AdminExtendSubscriptionIn,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("users.suspend")),
):
    out = sub_svc.admin_extend(db, admin_id, sub_id, body.days, body.reason)
    db.commit()
    return out


@router.post("/subscriptions/{sub_id}/{action}", response_model=schemas.UserSubscriptionOut)
def admin_change_subscription_state(
    sub_id: int,
    action: Literal["suspend", "reactivate", "cancel"],
    body: schemas.AdminSubscriptionReasonIn | None = None,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("users.suspend")),
):
    out = sub_svc.admin_set_state(db, admin_id, sub_id, action, (body.reason if body else None) or "")
    db.commit()
    return out


@router.get("/contact-credits/pack", response_model=schemas.ContactCreditPackOut)
def admin_get_contact_credit_pack(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("reports.view")),
):
    return credit_svc.pack_info(db)


@router.put("/contact-credits/pack", response_model=schemas.ContactCreditPackOut)
def admin_set_contact_credit_pack(
    body: schemas.ContactCreditPackIn,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("settings.configure_platform")),
):
    """Set the 25-credit pack's price (paise) and whether it is on sale.
    Versioned (plan_versions) and audited. The credit count is fixed at 25."""
    out = credit_svc.set_pack_price(db, admin_id, body.price_paise, body.active, body.note)
    db.commit()
    return out


@router.get("/payments/reviews", response_model=schemas.AdminPaymentReviewsResponse)
def admin_list_payment_reviews(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("reports.view")),
    review_status: Literal["PENDING", "RESOLVED"] | None = Query(default="PENDING"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Verified payments flagged for manual review (no plan was granted), with
    their refund state. Read-only."""
    items, total = refund_svc.list_reviews(db, review_status, page, page_size)
    return schemas.AdminPaymentReviewsResponse(items=items, page=page, page_size=page_size, total=total)


@router.post("/payments/{payment_id}/refund", response_model=schemas.PaymentRefundOut)
async def admin_refund_payment(
    payment_id: int,
    body: schemas.PaymentRefundIn,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("settings.configure_platform")),
):
    """Refund a manual-review payment in full through Razorpay (audited).
    409 when it is not awaiting review or a refund is already requested;
    502 (recorded as FAILED, retryable) when the gateway fails. PROCESSED is
    set only from the gateway's answer or its signed webhook."""
    out = await refund_svc.request_refund(db, admin_id, payment_id, body.reason)
    db.commit()
    if out["status"] == "FAILED":
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail={"code": "REFUND_GATEWAY_FAILED", "failure_code": out["failure_code"],
                                    "refund_id": out["id"]})
    return out


@router.get("/payments", response_model=schemas.AdminPaymentsResponse)
def admin_list_payments(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("reports.view")),
    payment_status: Literal["INITIATED", "SUCCESS", "FAILED"] | None = Query(default=None, alias="status"),
    user_id: int | None = Query(default=None, ge=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    items, total = admin_svc.list_payments(db, page, page_size, payment_status=payment_status, user_id=user_id)
    return schemas.AdminPaymentsResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/enquiries/{enquiry_id}", response_model=schemas.AdminEnquiryDetailOut)
def admin_get_enquiry(
    enquiry_id: int, db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("enquiries.view")),
):
    return admin_svc.get_enquiry(db, enquiry_id)


# --- audit log --------------------------------------------------------------------

@router.get("/audit-logs", response_model=schemas.AdminAuditLogsResponse)
def admin_audit_logs(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("settings.view_audit_logs")),
    action: str | None = Query(default=None, max_length=100),
    entity: str | None = Query(default=None, max_length=50),
    actor_user_id: int | None = Query(default=None, ge=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    items, total = admin_svc.list_audit_logs(
        db, page, page_size, action=action, entity=entity, actor_user_id=actor_user_id)
    return schemas.AdminAuditLogsResponse(items=items, page=page, page_size=page_size, total=total)


# --- email delivery (Phase 3) -----------------------------------------------------------

@router.get("/email-deliveries", response_model=schemas.AdminEmailDeliveriesResponse)
def admin_email_deliveries(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("settings.view_audit_logs")),
    status_filter: schemas.EmailOutboxStatus | None = Query(default=None, alias="status"),
    category: schemas.EmailCategory | None = Query(default=None),
    user_id: int | None = Query(default=None, ge=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Read-only delivery records: status, category, recipient account (id +
    masked address), created/sent/failed times, attempts and failure kind.
    Never an email body, a verification code or provider credentials."""
    items, total = email_svc.admin_list(
        db, page, page_size, status_filter=status_filter, category=category, user_id=user_id)
    return schemas.AdminEmailDeliveriesResponse(items=items, page=page, page_size=page_size, total=total)


# --- push delivery (Phase 4) ------------------------------------------------------------

@router.get("/push-deliveries", response_model=schemas.AdminPushDeliveriesResponse)
def admin_push_deliveries(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("settings.view_audit_logs")),
    status_filter: schemas.PushOutboxStatus | None = Query(default=None, alias="status"),
    category: schemas.PushCategory | None = Query(default=None),
    user_id: int | None = Query(default=None, ge=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Read-only delivery records: status, category, recipient account (id;
    a device token NEVER leaves the server — not even masked in this list),
    created/sent/failed times, attempts and the failure kind. Never a message
    body or provider credentials."""
    items, total = push_svc.admin_list(
        db, page, page_size, status_filter=status_filter, category=category, user_id=user_id)
    return schemas.AdminPushDeliveriesResponse(items=items, page=page, page_size=page_size, total=total)


# --- staff --------------------------------------------------------------------------

@router.get("/staff", response_model=schemas.AdminStaffResponse)
def admin_list_staff(db: Session = Depends(get_db), _admin: int = Depends(require_admin_permission("settings.manage_team"))):
    items = admin_svc.list_staff(db)
    return schemas.AdminStaffResponse(items=items, total=len(items))


@router.post("/staff", response_model=schemas.AdminStaffRow, status_code=201)
def admin_grant_staff(
    body: schemas.AdminStaffGrantIn,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("settings.manage_team")),
):
    """Make an existing, active user an admin with an explicit role. 409 when
    already an admin. The user must already have signed up (OTP/Google)."""
    row = admin_svc.grant_staff(db, admin_id, body.user_id, body.admin_role)
    db.commit()
    return row


@router.patch("/staff/{user_id}", response_model=schemas.AdminStaffRow)
def admin_change_staff_role(
    user_id: int,
    body: schemas.AdminStaffRoleIn,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("settings.manage_team")),
):
    row = admin_svc.change_staff_role(db, admin_id, user_id, body.admin_role)
    db.commit()
    return row


@router.delete("/staff/{user_id}", status_code=204)
def admin_revoke_staff(
    user_id: int,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("settings.manage_team")),
):
    admin_svc.revoke_staff(db, admin_id, user_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- agent localities (catalog + per-agent coverage) ---------------------------------
# Disabling a locality only stops NEW selection: agents who already cover it
# keep it until their plan expires. Assignments always respect the agent's
# purchased plan limit; no payment, subscription or expiry is ever changed.
# TEMPORARILY DISABLED — Chennai area seeding paused: every endpoint below
# answers 404 unless the area-seeding flag is on (never in production).


def _area_seeding_on() -> None:
    if not area_seeding_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


@router.get(
    "/agent-localities", response_model=schemas.AdminAgentLocalitiesResponse,
    dependencies=[Depends(_area_seeding_on)],
)
def admin_list_agent_localities(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("users.view")),
    q: str | None = Query(default=None, max_length=255),
    enabled: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    items, total = locality_svc.list_catalog(db, q=q, enabled=enabled, page=page, page_size=page_size)
    return schemas.AdminAgentLocalitiesResponse(items=items, page=page, page_size=page_size, total=total)


@router.patch(
    "/agent-localities/{locality_id}", response_model=schemas.AdminAgentLocalityOut,
    dependencies=[Depends(_area_seeding_on)],
)
def admin_set_agent_locality(
    locality_id: int,
    body: schemas.AdminAgentLocalityUpdateIn,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("settings.configure_platform")),
):
    """Enable/disable a locality for new agent selection (audited on change)."""
    out = locality_svc.set_agent_enabled(db, admin_id, locality_id, body.agent_enabled, body.reason)
    db.commit()
    return out


@router.get(
    "/agents/{user_id}/localities", response_model=schemas.AdminAgentCoverageOut,
    dependencies=[Depends(_area_seeding_on)],
)
def admin_get_agent_localities(
    user_id: int,
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("users.view")),
):
    return agent_plan_svc.admin_coverage(db, user_id)


@router.post(
    "/agents/{user_id}/localities", response_model=schemas.AdminAgentAssignOut,
    dependencies=[Depends(_area_seeding_on)],
)
def admin_assign_agent_localities(
    user_id: int,
    body: schemas.AdminAgentAssignIn,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("settings.configure_platform")),
):
    """Assign localities under the agent's ACTIVE plan and its limit (409
    without a plan, 422 over the limit / nothing usable). Audited."""
    result = agent_plan_svc.add_localities(db, user_id, body.localities, actor_id=admin_id)
    db.commit()
    out = agent_plan_svc.admin_coverage(db, user_id)
    out.update({k: result[k] for k in ("added_localities", "unmatched_localities", "unavailable_localities")})
    return out


@router.delete(
    "/agents/{user_id}/localities/{locality_id}", status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(_area_seeding_on)],
)
def admin_revoke_agent_locality(
    user_id: int,
    locality_id: int,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("settings.configure_platform")),
):
    """Remove one place from an agent's coverage. History is kept (REVOKED);
    the plan, payment and expiry are untouched. Audited."""
    agent_plan_svc.revoke_locality(db, admin_id, user_id, locality_id, None)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- Agent Location Master (individual additions only; bulk import is
# deliberately not offered) ---------------------------------------------------
from pydantic import BaseModel, Field  # noqa: E402
from fastapi import Header  # noqa: E402

from ..services import location_master as location_master_svc  # noqa: E402


class LocationMasterAddIn(BaseModel):
    locality_name: str = Field(..., max_length=255)
    pincode: str = Field(..., max_length=20)
    zone: str = Field(..., max_length=40)


@router.get("/location-master")
def admin_list_location_master(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("users.view")),
    q: str | None = Query(default=None, max_length=255),
    zone: Literal["North Chennai", "Central Chennai", "South Chennai"] | None = Query(default=None),
    pincode: str | None = Query(default=None, max_length=6, pattern=r"^\d{1,6}$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
):
    """Individual locality records (Sheet 1), searchable by name, PIN, zone."""
    return location_master_svc.list_records(db, q=q, zone=zone, pincode=pincode, page=page, page_size=page_size)


@router.get("/location-master/groups")
def admin_list_location_groups(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("users.view")),
    q: str | None = Query(default=None, max_length=255),
    zone: Literal["North Chennai", "Central Chennai", "South Chennai"] | None = Query(default=None),
):
    """The published agent location groups (Sheet 2), one per PIN code."""
    return location_master_svc.list_groups(db, q=q, zone=zone)


@router.get("/location-master/status")
def admin_location_master_status(
    db: Session = Depends(get_db),
    _admin: int = Depends(require_admin_permission("users.view")),
):
    """Committed / published versions, pending publications, block state."""
    return location_master_svc.status_summary(db)


@router.post("/location-master", status_code=status.HTTP_201_CREATED)
def admin_add_location(
    body: LocationMasterAddIn,
    response: Response,
    db: Session = Depends(get_db),
    admin_id: int = Depends(require_admin_permission("settings.configure_platform")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=64),
):
    """Add ONE locality: appended to the original master, its PIN-code group
    updated (or created), the workbook republished. Success is returned only
    after the workbook is published; a replayed Idempotency-Key returns the
    original result (200)."""
    result = location_master_svc.add_locality(
        db, admin_id, body.locality_name, body.pincode, body.zone, idempotency_key
    )
    if result.get("replayed"):
        response.status_code = status.HTTP_200_OK
    return result

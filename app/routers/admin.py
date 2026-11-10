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
  GET  /admin/audit-logs                  [settings.view_audit_logs]
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
from ..services import agent_plans as agent_plan_svc
from ..services import localities as locality_svc

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

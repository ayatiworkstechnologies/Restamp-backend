"""RESTAMP admin roles and permissions (pure data + lookups, no DB access).

Admin identity is the existing admin_accounts row (one per user, created
only by an admin with settings.manage_team — never by role switching, never
by a public endpoint). Its admin_role column names one of the roles below.

The role -> permission table mirrors the Admin website's role matrix
(restamp-website src/lib/settings-data.ts, roles tab) exactly; only the
permissions some backend endpoint actually enforces matter today, but the
full matrix is kept so the website can render it from the API.

Fail closed: an unknown admin_role grants NOTHING. The legacy value "ADMIN"
(the column's server default, written by POST /admin/grants before roles
existed) keeps full access so existing admins are not locked out; new
grants should use an explicit role (decision documented for the product
owner).
"""
from __future__ import annotations

# Every permission the Admin UI defines, grouped by area.
PERMISSIONS: tuple[str, ...] = (
    "properties.view", "properties.approve", "properties.reject", "properties.edit", "properties.delete",
    "users.view", "users.kyc_verify", "users.suspend", "users.export",
    "enquiries.view", "enquiries.update_status", "enquiries.delete", "enquiries.export",
    "reports.view", "reports.export",
    "settings.manage_team", "settings.configure_platform", "settings.view_audit_logs",
)

ROLE_LABELS: dict[str, str] = {
    "SUPER_ADMIN": "Super Admin",
    "OPERATIONS_MANAGER": "Operations Manager",
    "LISTING_REVIEWER": "Listing Reviewer",
    "SUPPORT_SPECIALIST": "Support Specialist",
    "ADMIN": "Administrator (legacy, full access)",
}

# Roles an admin may assign through the API (the four UI roles). The legacy
# "ADMIN" value is honoured but never newly assigned through /admin/staff.
ASSIGNABLE_ROLES: tuple[str, ...] = (
    "SUPER_ADMIN", "OPERATIONS_MANAGER", "LISTING_REVIEWER", "SUPPORT_SPECIALIST",
)

# Roles holding settings.manage_team. At least one such admin must remain so
# the team can always be managed (no self-lockout).
FULL_ACCESS_ROLES: tuple[str, ...] = ("SUPER_ADMIN", "ADMIN")

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "SUPER_ADMIN": frozenset(PERMISSIONS),
    "ADMIN": frozenset(PERMISSIONS),
    "OPERATIONS_MANAGER": frozenset({
        "properties.view", "properties.approve", "properties.reject", "properties.edit",
        "users.view", "users.kyc_verify", "users.suspend", "users.export",
        "enquiries.view", "enquiries.update_status", "enquiries.export",
        "reports.view", "reports.export",
        "settings.view_audit_logs",
    }),
    "LISTING_REVIEWER": frozenset({
        "properties.view", "properties.approve", "properties.reject", "properties.edit",
        "users.view",
        "enquiries.view",
    }),
    "SUPPORT_SPECIALIST": frozenset({
        "properties.view",
        "users.view",
        "enquiries.view", "enquiries.update_status",
    }),
}


def permissions_for(admin_role: str | None) -> frozenset[str]:
    """Permissions of an admin role; empty for unknown roles (fail closed)."""
    return ROLE_PERMISSIONS.get(admin_role or "", frozenset())


def has_permission(admin_role: str | None, permission: str) -> bool:
    return permission in permissions_for(admin_role)

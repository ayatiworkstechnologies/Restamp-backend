"""RESTAMP email templates (notification system Phase 3).

One shared layout (brand header, title, short message, optional button,
support footer) and one fixed entry per email type. Every email is rendered
server-side from:

- fixed subject / heading / button text defined here, and
- `message`: the in-app notification body for event emails (already rendered
  by services/notifications.py from fixed templates: a listing title and a
  formatted time at most), or fixed text for account/verification emails.

Privacy and safety:
- No phone numbers, names, enquiry/message text, admin notes, suspension or
  rejection reasons ever reach an email — the same rule as in-app
  notifications (the only user-derived text is the listing title inside the
  notification body).
- Every dynamic value is HTML-escaped; no user-provided HTML is ever
  inserted. Subjects are fixed strings (no header injection).
- Links are built ONLY from RESTAMP_APP_BASE_URL plus a fixed path chosen by
  the notification's fixed target kind. No link is built from request or
  user data; with no (or a non-https) base URL the email simply has no button.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from urllib.parse import urlsplit

from ..config import is_production, settings

BRAND = "RESTAMP"


@dataclass(frozen=True)
class _Template:
    category: str          # outbox category
    subject: str
    heading: str
    button: str | None     # button label (needs a link target)
    fixed_message: str | None = None  # used when the email has no notification body


# Categories: ENQUIRY / VISIT / LISTING are optional (user preferences apply);
# VERIFICATION and ACCOUNT are mandatory account emails (no opt-out).
TEMPLATES: dict[str, _Template] = {
    "VERIFY_EMAIL": _Template("VERIFICATION", "Verify your email address for RESTAMP", "Verify your email address", None),
    "ENQUIRY_NEW": _Template("ENQUIRY", "New enquiry on your RESTAMP listing", "New enquiry received", "View enquiry"),
    "ENQUIRY_MESSAGE": _Template("ENQUIRY", "New message on an enquiry", "New message on an enquiry", "View enquiry"),
    "ENQUIRY_REPLY": _Template("ENQUIRY", "The owner replied to your enquiry", "The owner replied", "View enquiry"),
    "LEAD_CONTACTED": _Template("ENQUIRY", "Update on your RESTAMP enquiry", "Enquiry update", "View enquiry"),
    "LEAD_CLOSED": _Template("ENQUIRY", "Your RESTAMP enquiry was closed", "Enquiry closed", "View enquiry"),
    "VISIT_REQUESTED": _Template("VISIT", "A site visit was requested", "Visit requested", "View visit"),
    "VISIT_PROPOSED": _Template("VISIT", "A site visit time was proposed", "Visit proposed", "View visit"),
    "VISIT_CONFIRMED": _Template("VISIT", "Your site visit is confirmed", "Visit confirmed", "View visit"),
    "VISIT_DECLINED": _Template("VISIT", "A site visit was declined", "Visit declined", "View visit"),
    "VISIT_REJECTED": _Template("VISIT", "A site visit was not accepted", "Visit not accepted", "View visit"),
    "VISIT_CANCELLED": _Template("VISIT", "A site visit was cancelled", "Visit cancelled", "View visit"),
    "VISIT_RESCHEDULED": _Template("VISIT", "A site visit was rescheduled", "Visit rescheduled", "View visit"),
    # Smart reminders (Phase 4) — ordinary optional email for their category.
    "VISIT_REMINDER": _Template("VISIT", "Reminder: your site visit is coming up", "Visit reminder", "View visit"),
    "LEAD_REMINDER": _Template("ENQUIRY", "A lead is waiting for your reply", "Lead reminder", "View enquiry"),
    "LISTING_VERIFIED": _Template("LISTING", "Your RESTAMP listing is verified", "Listing verified", "View listing"),
    "LISTING_REJECTED": _Template("LISTING", "Your RESTAMP listing needs changes", "Listing rejected", "View listing"),
    "ACCOUNT_SUSPENDED": _Template(
        "ACCOUNT", "Your RESTAMP account has been suspended", "Account suspended", None,
        "Your RESTAMP account has been suspended, so you cannot sign in or use the app for now. "
        "If you believe this is a mistake, please contact support."),
    "ACCOUNT_REACTIVATED": _Template(
        "ACCOUNT", "Your RESTAMP account has been reactivated", "Account reactivated", "Open RESTAMP",
        "Your RESTAMP account has been reactivated. You can sign in and use the app again."),
}

OPTIONAL_CATEGORIES = ("ENQUIRY", "VISIT", "LISTING")
MANDATORY_CATEGORIES = ("VERIFICATION", "ACCOUNT")

# Notification target kind -> fixed web path (the website's own routes).
_TARGET_PATHS = {
    "OWNER_LEAD": "/owner/dashboard/leads",
    "OWNER_VISIT": "/owner/dashboard/visits",
    "OWNER_LISTING": "/owner/dashboard/properties",
    "BUYER_ENQUIRY": "/activity",
    "BUYER_VISIT": "/activity",
    "ACCOUNT": "/profile",
}


def _base_url() -> str | None:
    base = (settings.APP_BASE_URL or "").strip().rstrip("/")
    if not base:
        return None
    try:
        parts = urlsplit(base)
    except ValueError:
        return None
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        return None
    if parts.scheme == "https":
        return base
    # Plain http only for a local development site, never in production.
    if parts.scheme == "http" and not is_production() and parts.hostname in ("localhost", "127.0.0.1"):
        return base
    return None


def link_for(target_kind: str | None) -> str | None:
    """Fixed link for a fixed target kind, or None (no base URL / unknown kind)."""
    base = _base_url()
    path = _TARGET_PATHS.get(target_kind or "")
    return f"{base}{path}" if base and path else None


def _support_line() -> str:
    return (settings.EMAIL_SUPPORT or "").strip()[:120]


def _html(heading: str, paragraphs: list[str], code: str | None, button: str | None, link: str | None) -> str:
    support = _support_line()
    body = "".join(
        f'<p style="margin:0 0 14px;font-size:15px;line-height:22px;color:#334155;">{escape(p)}</p>' for p in paragraphs
    )
    code_block = (
        f'<p style="margin:6px 0 18px;font-size:30px;letter-spacing:8px;font-weight:700;color:#0F172A;">{escape(code)}</p>'
        if code else ""
    )
    button_block = (
        f'<p style="margin:8px 0 6px;"><a href="{escape(link, quote=True)}" '
        f'style="display:inline-block;background:#2563EB;color:#FFFFFF;text-decoration:none;'
        f'font-size:14px;font-weight:600;padding:11px 22px;border-radius:999px;">{escape(button)}</a></p>'
        if button and link else ""
    )
    support_block = (
        f'<p style="margin:0 0 6px;">Need help? Contact {escape(support)}.</p>' if support else ""
    )
    return (
        '<!doctype html><html><body style="margin:0;padding:0;background:#F6F7F9;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F6F7F9;padding:24px 12px;">'
        '<tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:520px;background:#FFFFFF;border:1px solid #EEF0F3;border-radius:16px;">'
        f'<tr><td style="padding:22px 26px 6px;font-family:Arial,Helvetica,sans-serif;font-size:20px;'
        f'font-weight:800;letter-spacing:4px;color:#0E2773;">{BRAND}</td></tr>'
        '<tr><td style="padding:10px 26px 24px;font-family:Arial,Helvetica,sans-serif;">'
        f'<h1 style="margin:8px 0 14px;font-size:20px;line-height:26px;color:#0F172A;">{escape(heading)}</h1>'
        f'{body}{code_block}{button_block}'
        '</td></tr>'
        '<tr><td style="padding:16px 26px 22px;border-top:1px solid #EEF0F3;font-family:Arial,Helvetica,sans-serif;'
        'font-size:12px;line-height:18px;color:#64748B;">'
        f'{support_block}'
        f'<p style="margin:0;">This is a service email from {BRAND} about your account or activity.</p>'
        '</td></tr></table></td></tr></table></body></html>'
    )


def _text(heading: str, paragraphs: list[str], code: str | None, button: str | None, link: str | None) -> str:
    lines = [BRAND, "", heading, ""]
    lines += paragraphs
    if code:
        lines += ["", code]
    if button and link:
        lines += ["", f"{button}: {link}"]
    support = _support_line()
    lines += ["", "--"]
    if support:
        lines.append(f"Need help? Contact {support}.")
    lines.append(f"This is a service email from {BRAND} about your account or activity.")
    return "\n".join(lines)


def render(template: str, *, message: str | None = None, target_kind: str | None = None,
           code: str | None = None, code_minutes: int | None = None) -> tuple[str, str, str]:
    """(subject, text, html) for one email. Raises KeyError for an unknown template."""
    spec = TEMPLATES[template]
    if template == "VERIFY_EMAIL":
        if not code:
            raise ValueError("verification email needs a code")
        paragraphs = [
            f"Enter this code in {BRAND} to confirm that this email address is yours.",
            f"The code expires in {int(code_minutes or 15)} minutes and can be used once. "
            "If you did not ask for it, you can ignore this email — nothing will change.",
        ]
        link = None
    else:
        text = (spec.fixed_message or message or "").strip()
        if not text:
            raise ValueError("email has no message")
        paragraphs = [text[:600]]
        link = link_for("ACCOUNT" if spec.category == "ACCOUNT" else target_kind)
    return (
        spec.subject,
        _text(spec.heading, paragraphs, code, spec.button, link),
        _html(spec.heading, paragraphs, code, spec.button, link),
    )

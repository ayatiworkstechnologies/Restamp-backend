"""M9 contact privacy: a poster may not send contact details to a buyer
before that buyer has unlocked the poster's contact for that property.

Policy (approved, M9):
- Applies to text an OWNER, or a Dealer (BROKER) acting as the listing's
  poster, sends to a buyer: enquiry replies (enquiries.reply_as_owner) and
  visit-proposal notes (visits.create_owner_visit). Buyer -> owner text and
  private owner notes are NOT checked.
- Contact details: Indian mobile numbers (10 digits starting 6-9, optional
  +91 / 0091 / 0 prefix), Indian landlines written with a trunk or country
  prefix (0 + STD code, or +91 / 0091), e-mail addresses, wa.me links and
  WhatsApp links (whatsapp.com/..., api.whatsapp.com/send?phone=...).
- Entitled ONLY when an authoritative PropertyContactUnlock row exists for
  the exact (buyer, listing) pair: contact_credits.get_unlock. A reveal-audit
  row or Dealer-area plan coverage alone does not qualify.
- Refusal is a 422 {"code": "CONTACT_DETAILS_NOT_ALLOWED", "message": ...}
  raised BEFORE anything is written: no message / visit row, notification,
  audit row or credit change. The text is never masked or stored.

Detection (pure, no database): Unicode decimal digits (full-width,
Devanagari, ...) are normalized to ASCII first. A phone candidate is a run
of digits joined only by short separators (space, hyphen, period,
parentheses; at most two characters between digits), so separate numbers in
ordinary text are not concatenated. Spelled-out digits ("nine eight four
...") are converted on a best-effort basis.

Known limits (documented, best effort): heavily obfuscated numbers (mixed
words and symbols, digits split by letters, images), e-mails written as
"name at domain dot com" in unusual forms, and bare 10-digit identifiers
starting 6-9 (indistinguishable from a mobile number and treated as one).
A landline WITHOUT a 0 / +91 prefix is not detected, so ordinary numbers
(listing ids, areas, prices) are not mistaken for one.
"""
from __future__ import annotations

import re
import unicodedata

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from . import contact_credits as credit_svc

CONTACT_DETAILS_CODE = "CONTACT_DETAILS_NOT_ALLOWED"
CONTACT_DETAILS_MESSAGE = "Phone numbers can be shared after the buyer unlocks your contact."

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
# Best-effort spelled e-mail: "name (at) gmail (dot) com", "name [at] x [dot] in",
# "name at gmail dot com". Requires the explicit "dot <tld>" ending.
_EMAIL_SPELLED_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+\s*(?:\(at\)|\[at\]|\{at\}|\s+at\s+)\s*[A-Za-z0-9\-]+"
    r"\s*(?:\(dot\)|\[dot\]|\{dot\}|\s+dot\s+)\s*(?:com|in|net|org|co|co\.in)\b",
    re.IGNORECASE,
)
_WHATSAPP_RE = re.compile(
    r"(?:\bwa\.me/|\bwhatsapp\.com/|\bapi\.whatsapp\.com|\bchat\.whatsapp\.com|\bwhatsapp://)",
    re.IGNORECASE,
)
# Digits joined by at most two separator characters; an optional leading "+".
_PHONE_RUN_RE = re.compile(r"\+?\d(?:[ \-.()]{0,2}\d)+")
_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
_WORD_RE = re.compile(r"\b(" + "|".join(_DIGIT_WORDS) + r")\b", re.IGNORECASE)


def _ascii_digits(text: str) -> str:
    """Map every Unicode decimal digit (full-width, Devanagari, ...) to 0-9."""
    out = []
    for ch in text:
        if not ch.isascii():
            d = unicodedata.decimal(ch, None)
            if d is not None:
                out.append(str(d))
                continue
            if unicodedata.category(ch) == "Zs":  # unusual spaces -> plain space
                out.append(" ")
                continue
        out.append(ch)
    return "".join(out)


def _is_phone(run: str) -> bool:
    digits = re.sub(r"\D", "", run)
    plus = run.lstrip().startswith("+")
    prefixed = False
    if plus and digits.startswith("91") and len(digits) == 12:
        digits, prefixed = digits[2:], True          # +91 ...
    elif digits.startswith("0091") and len(digits) == 14:
        digits, prefixed = digits[4:], True          # 0091 ...
    elif not plus and digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]                          # 91XXXXXXXXXX: mobile only
    elif digits.startswith("0") and len(digits) == 11:
        digits, prefixed = digits[1:], True          # trunk 0 + 10 digits
    if len(digits) != 10:
        return False
    if digits[0] in "6789":
        return True  # mobile, with or without a prefix
    # Landline national number (STD code without the 0 + subscriber number):
    # only with an explicit trunk / country prefix, so bare 10-digit ids,
    # prices and areas are never treated as a landline.
    return prefixed and digits[0] in "12345"


_GROUP_RE = re.compile(r"\+?\d+")


def _has_phone(text: str) -> bool:
    """Every contiguous combination of the digit groups in each run is tested,
    so a number directly followed or preceded by another short number (e.g.
    "98401 23456 2") is still found, while runs never cross the separator
    limit (no joining of numbers that are far apart in ordinary text)."""
    for m in _PHONE_RUN_RE.finditer(text):
        run = m.group(0)
        groups = [g.group(0) for g in _GROUP_RE.finditer(run)]
        if len(groups) > 12:
            groups = groups[:12]  # bound the work on pathological input
        for i in range(len(groups)):
            for j in range(i + 1, len(groups) + 1):
                if _is_phone(" ".join(groups[i:j])):
                    return True
    return False


def contains_contact_details(text: str | None) -> bool:
    """True when `text` contains a phone number, e-mail or WhatsApp link (pure)."""
    if not text:
        return False
    s = _ascii_digits(str(text))
    if _EMAIL_RE.search(s) or _EMAIL_SPELLED_RE.search(s) or _WHATSAPP_RE.search(s):
        return True
    if _has_phone(s):
        return True
    # Best effort: spelled-out digits ("nine eight four zero ...").
    spelled = _WORD_RE.sub(lambda m: _DIGIT_WORDS[m.group(1).lower()], s)
    return spelled != s and _has_phone(spelled)


def buyer_entitled(db: Session, buyer_id: int, listing_id: int) -> bool:
    """The buyer unlocked THIS listing's contact: an authoritative
    PropertyContactUnlock row for exactly (buyer, listing). Nothing else counts."""
    return credit_svc.get_unlock(db, int(buyer_id), int(listing_id)) is not None


def ensure_allowed(db: Session, buyer_id: int, listing_id: int, text: str | None) -> None:
    """Refuse (422, nothing written) poster text with contact details sent to a
    buyer who has not unlocked this listing. Read-only: performs no writes.
    `buyer_id` / `listing_id` must come from server-side records."""
    if not contains_contact_details(text):
        return
    if buyer_entitled(db, buyer_id, listing_id):
        return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": CONTACT_DETAILS_CODE, "message": CONTACT_DETAILS_MESSAGE},
    )

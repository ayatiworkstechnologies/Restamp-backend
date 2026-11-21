"""RESTAMP Phase 4 — Reva AI chatbot router with Agaram 2.5B (Murai Labs) & Bilingual Voice.

Provides interactive property guidance, platform navigation assistance,
bilingual English + Tamil voice transcription and speech-friendly FAQ answering
for Buyers, Owners, and Agents across Chennai.

Powered by Murai-Labs/Agaram2.5b for native Tamil & English language understanding,
with fallback to OpenAI or Restamp's verified platform knowledge base.
Preserves existing Restamp and Reva AI brand names and logos.

Security invariants (public endpoint — hardened before exposure):
- Optional authentication: guests are served public data only; a PRESENTED
  token must be valid (app.security.get_optional_user_id fail closed).
- The signed-in role is read from the database (users_roles), never from the
  request body, so a client cannot claim another role.
- Rate limiting: shared fixed-window counters (services.rate_limit) per IP
  and per user — the shared store keeps the limit intact across instances.
- Body-size caps for chat JSON and audio uploads are enforced in
  app.main before the body is parsed.
- Request validation: message length, history length and per-item sizes are
  bounded at the schema; history is truncated again before a provider call.
- Grounding: real listing data comes only from services.listings (public
  visibility) and the caller's OWN account data from services.reva; the
  provider prompt forbids inventing availability, prices or contacts. The
  fallback knowledge base quotes only the real plan catalogue.
- Privacy: messages and audio are never logged; provider calls carry no user
  identifiers; failures log status codes and error classes only.
"""
from __future__ import annotations

import logging
import re
from typing import List, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..security import get_optional_user_id
from ..services import rate_limit as rl_svc
from ..services import reva as reva_svc
from ..services import users_roles

log = logging.getLogger("restamp.reva")

router = APIRouter(tags=["reva"])

VALID_ROLES = {"Buyer", "Owner", "Agent"}
VALID_LANGUAGES = {"en", "ta"}
MAX_AUDIO_BYTES = 8_000_000  # 8 MB application cap (transport cap is larger; see app.main)
AUDIO_EXTENSIONS_PATTERN = re.compile(r"\.(m4a|mp4|mp3|wav|webm)$", re.IGNORECASE)
# The transcription upload must look like audio. Browsers send audio/webm,
# iOS audio/mp4; some clients send application/octet-stream — allowed, the
# extension check still applies. Everything else (e.g. text/*, application/pdf)
# is refused before the bytes are read.
_AUDIO_CONTENT_TYPES = ("audio/", "application/octet-stream")

_MAX_HISTORY_ITEMS = 12
_PROVIDER_HISTORY_ITEMS = 8  # sent to the provider at most
_PROVIDER_CONTENT_CHARS = 500  # per history item sent to the provider

# Murai Labs Agaram 2.5B Crisis Support Safety Protocol
CRISIS_FOOTER = (
    "\n\n[If you are in distress: Tele-MANAS 14416 (free, 24x7, Tamil supported) · emergency 112 · "
    "நீங்கள் மன அழுத்தத்தில் இருந்தால் Tele-MANAS 14416 அல்லது 112-ஐ அழையுங்கள்]"
)
CRISIS_PHRASES = (
    # English. First-person phrases only: a bare "not safe" also matched
    # ordinary property questions ("is this area not safe?").
    "kill myself", "suicide", "end my life", "want to die", "self harm", "self-harm", "hurt myself",
    "i am not safe", "i'm not safe", "im not safe", "i don't feel safe", "i dont feel safe",
    # Tanglish
    "saaga poren", "saaganum", "sethuduven", "sethu poganum", "uyiroda irukka venaam",
    # Tamil script
    "தற்கொலை", "சாகணும்", "சாகப் போறேன்", "செத்துடுவேன்", "உயிரோட இருக்க வேண்டாம்", "வாழ விருப்பம் இல்ல",
    "என்னை நானே காயப்படுத்த",
)


def _needs_crisis_footer(text: str) -> bool:
    t = " ".join(text.lower().replace("\u2019", "'").split())
    return any(p in t for p in CRISIS_PHRASES)


# Plan/pricing questions are answered from the real plan catalogue BEFORE the
# listing/selling branch: "what plans do you sell" contains "sell" and was
# being answered as a how-to-list-a-property question.
_PLAN_WORDS_EN = ("plan", "subscription", "pricing", "package")
_PLAN_WORDS_TA = ("திட்டம்", "பிளான்", "plan")


class ChatHistoryItem(BaseModel):
    role: Literal["user", "assistant"] = "user"
    content: str = Field(..., min_length=1, max_length=1000)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)
    # Role/language stay optional strings for wire compatibility; the server
    # decides the authoritative role (authenticated role from the database,
    # guests default to Buyer) and language is whitelisted below.
    role: Optional[str] = "Buyer"
    language: Optional[str] = "en"
    history: Optional[List[ChatHistoryItem]] = Field(default=None, max_length=_MAX_HISTORY_ITEMS)


class RevaListing(BaseModel):
    """A REAL public listing quoted by the assistant (additive, optional)."""

    listing_id: int
    title: str
    price_display: str
    locality: str
    city: str
    property_type: str
    bedrooms: Optional[int] = None


class ChatResponse(BaseModel):
    reply: str
    language: str = "en"
    model: str = "murai-labs/agaram-2.5b-instruct"
    demo: bool = False
    listings: Optional[List[RevaListing]] = None


class TranscribeResponse(BaseModel):
    text: str
    language: str
    demo: bool = False


def _role_from_db(db: Session, user_id: Optional[int]) -> str:
    """The authenticated role from the database, or the guest default.

    Never trusts the request's claimed role for signed-in users: the backend
    role table is the single source of truth (BUYER/OWNER/BROKER).
    """
    if user_id is None:
        return "Buyer"
    try:
        role = users_roles.get_current_role(db, user_id)
    except Exception:
        log.exception("Reva role lookup failed")
        return "Buyer"
    return {"BUYER": "Buyer", "OWNER": "Owner", "BROKER": "Agent"}.get(role or "", "Buyer")


def _chat_rate_limits(*, per_user: bool) -> tuple:
    """Limits read at call time so tests and configuration can tune them."""
    per_minute = settings.CHAT_RPM_PER_USER if per_user else settings.CHAT_RPM_PER_IP
    per_hour = settings.CHAT_RPH_PER_USER if per_user else settings.CHAT_RPH_PER_IP
    return (
        rl_svc.Limit("chat:minute", per_minute, 60),
        rl_svc.Limit("chat:hour", per_hour, 3600),
    )


def _grounded_context(db: Session, user_id: Optional[int], message: str) -> dict:
    """Real data for the prompt + the response's listing cards.

    - snapshot: public listings matching the message (guests included).
    - context_lines: the signed-in caller's OWN account summary (never sent
      for guests; never another user's data).
    """
    snapshot = reva_svc.public_listing_snapshot(db, message)
    context_lines = reva_svc.signed_in_context(db, user_id) if user_id is not None else []
    return {"snapshot": snapshot, "context_lines": context_lines}


def _system_prompt(role: str, language: str, grounding: dict) -> str:
    lines = [
        "You are Reva AI, a concise, professional, and helpful real estate assistant for Restamp, "
        "India's premier 100% zero-brokerage property platform operating across Chennai.",
        (
            "Reply in natural, polite Tamil (தமிழ்) using Tamil Unicode script. "
            "Keep brand names and technical terms in English when helpful."
            if language == "ta"
            else "Reply clearly and concisely in English."
        ),
        f"The visitor's role is {role}.",
        "Ground rules you must never break:",
        "- Quote ONLY the plans, prices, availability and listings given in the RESTAMP DATA section.",
        "- If something is not in that data, say you are not sure and point to the website — never invent "
        "availability, prices, contact details or subscription terms.",
        "- Never reveal phone numbers or personal details of anyone, and never discuss other users' data.",
        "Restamp plans currently sold online (real catalogue):",
        reva_svc.real_plan_lines(),
    ]
    if grounding.get("context_lines"):
        lines.append("The signed-in user's own Restamp account (they may be referred to as 'you'):")
        lines.extend(grounding["context_lines"])
    if grounding.get("snapshot"):
        snap = grounding["snapshot"]
        lines.append(f"RESTAMP DATA — live listings matching the question: {snap['summary']}.")
        for card in snap.get("listings") or []:
            lines.append(
                f"- {card['title']} · {card['price_display']} · {card['locality']}, {card['city']}"
            )
    return "\n".join(lines)


async def _query_provider(
    message: str,
    role: str,
    language: str,
    history: List[ChatHistoryItem],
    grounding: dict,
) -> Optional[tuple[str, str]]:
    """Query Agaram 2.5B (Murai Labs), then OpenAI. Returns (reply, model)."""
    system_prompt = _system_prompt(role, language, grounding)
    prior = [
        {"role": item.role, "content": item.content[:_PROVIDER_CONTENT_CHARS]}
        for item in (history or [])[-_PROVIDER_HISTORY_ITEMS:]
    ]
    messages = [{"role": "system", "content": system_prompt}, *prior, {"role": "user", "content": message}]

    if settings.AGARAM_ENABLED and (settings.AGARAM_ENDPOINT or settings.AGARAM_API_KEY):
        endpoint = settings.AGARAM_ENDPOINT or "https://router.huggingface.co/hf-inference/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if settings.AGARAM_API_KEY:
            headers["Authorization"] = f"Bearer {settings.AGARAM_API_KEY}"
        payload = {
            "model": settings.AGARAM_MODEL_NAME or "murai-labs/agaram-2.5b-instruct",
            "messages": messages,
            # Greedy decoding, bounded output: predictable cost per request.
            "temperature": 0.0,
            "max_tokens": 512,
            "stop": ["<|im_end|>", "</s>"],
        }
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.post(endpoint, headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                text = _extract_reply(data)
                if text:
                    return text, settings.AGARAM_MODEL_NAME or "murai-labs/agaram-2.5b-instruct"
            log.warning("Agaram endpoint returned status %s", resp.status_code)
        except Exception as exc:
            # Error class only: the exception text can carry the endpoint URL,
            # provider response fragments or request details.
            log.warning("Agaram connection error (%s)", type(exc).__name__)

    if settings.OPENAI_API_KEY:
        payload = {
            "model": settings.OPENAI_MODEL or "gpt-4o-mini",
            "messages": messages,
            "max_tokens": 400,
            "temperature": 0.7,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if resp.status_code == 200:
                data = resp.json()
                text = _extract_reply(data)
                if text:
                    return text, settings.OPENAI_MODEL or "gpt-4o-mini"
            log.warning("OpenAI fallback returned status %s", resp.status_code)
        except Exception as exc:
            log.warning("OpenAI fallback failed: %s", type(exc).__name__)
    return None


def _extract_reply(data: dict) -> Optional[str]:
    """Reply text from the OpenAI / vLLM / Ollama / HF completion shapes."""
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
        if text:
            return text
    if isinstance(data, list) and data and "generated_text" in data[0]:
        text = (data[0].get("generated_text") or "").strip()
        if text:
            return text
    message = data.get("message")
    if isinstance(message, dict) and message.get("content"):
        text = str(message["content"]).strip()
        if text:
            return text
    return None


def _demo_reply(message: str, role: str, language: str, grounding: dict) -> str:
    """Verified platform knowledge base (bilingual Tamil & English).

    Answers quote only real facts: the plan text is built from the backend's
    own plan constants, and any listing mention comes from the live snapshot
    (real buyer-visible rows) — never from hard-coded marketing copy.
    """
    s = message.lower()
    is_ta = language == "ta"

    if is_ta:
        if any(k in s for k in _PLAN_WORDS_TA):
            return "Restamp தற்போது விற்கும் உண்மையான திட்டங்கள்:\n" + reva_svc.real_plan_lines()
        if any(k in s for k in ["தரகு", "கமிஷன்", "0%", "brokerage"]):
            return (
                "Restamp 100% நேரடி தரகு இல்லாத தளம். வாங்குபவர்களும் வாடகைதாரர்களும் "
                "இடைத்தரகர்கள் இல்லாமல் நேரடியாக உரிமையாளர்களுடன் தொடர்பு கொள்ளலாம்."
            )
        if any(k in s for k in ["பதிவு", "விற்ப", "சேர்க்க", "post", "list"]):
            return (
                "உங்கள் சொத்தை Restamp-ல் பதிவு செய்ய 7 எளிய படிகள் உள்ளன: வகை தேர்வு, விவரங்கள், "
                "முகவரி & பின்கோட், பரப்பளவு, வசதிகள், புகைப்படங்கள் பதிவேற்றம் மற்றும் சமர்ப்பிப்பு."
            )
        if any(k in s for k in ["திட்டம்", "விலை", "பிளான்", "plan", "price"]):
            return (
                "Restamp தற்போது விற்கும் உண்மையான திட்டங்கள்:\n" + reva_svc.real_plan_lines()
            )
        if role == "Owner":
            return (
                "உரிமையாளர் சொத்து பதிவு, வாங்குபவர் தொடர்புகள் மற்றும் பார்வைகளை நிர்வகிக்க நான் உதவ முடியும். "
                "உங்களுக்கு என்ன உதவி வேண்டும்?"
            )
        if role == "Agent":
            return (
                "சென்னை பகுதி முகவர் திட்டங்கள் மற்றும் வாடிக்கையாளர் தொடர்புகளை நிர்வகிக்க நான் உதவ முடியும். "
                "எவ்வாறு உதவலாம்?"
            )
        if any(k in s for k in ["தேட", "சொத்து", "வீடு", "வாடகை", "வாங்க", "சென்னை"]):
            return _snapshot_or_generic(grounding, is_ta)
        return (
            "வணக்கம்! நான் Reva AI, உங்கள் Restamp சொத்து உதவியாளர் (Agaram 2.5B தமிழ் மாதிரியால் இயக்கப்படுகிறது). "
            "சென்னையில் வீடுகள் தேடுதல், 0% தரகு மற்றும் சொத்துப் பார்வைக்கு உதவ முடியும். என்னிடம் கேளுங்கள்!"
        )

    # English replies
    if any(k in s for k in _PLAN_WORDS_EN) and not any(k in s for k in ["agent", "broker", "elite"]):
        return "Here are the plans Restamp currently sells online:\n" + reva_svc.real_plan_lines()
    if any(k in s for k in ["brokerage", "commission", "zero brokerage", "0%"]):
        return (
            "Restamp is a direct 100% zero-brokerage platform. Buyers and tenants connect "
            "directly with authentic property owners without middlemen or brokerage fees."
        )

    if any(k in s for k in ["list", "upload", "publish", "sell", "add property", "post"]):
        return (
            "Listing your property takes less than 3 minutes through our guided 7-step owner flow: "
            "Select Category, Enter Details, Location & Pincode, Measurements & Floor, Amenities, "
            "Real Photos, and Submit. Once submitted, our automated engine verifies the listing."
        )

    if any(k in s for k in ["plan", "price", "pricing", "subscription", "cost", "fee"]):
        return "Here are the plans Restamp currently sells online:\n" + reva_svc.real_plan_lines()

    if any(k in s for k in ["visit", "schedule", "appointment", "booking", "timing", "tour"]):
        return (
            "You can schedule in-person site visits directly on any property detail page. Choose your preferred "
            "date and time slot. The owner receives instant notification to confirm or reschedule."
        )

    if any(k in s for k in ["agent", "broker", "partner", "localities", "elite"]):
        return (
            "Restamp agent plans cover chosen Chennai localities with direct buyer access and a dedicated "
            "agent dashboard:\n" + reva_svc.real_plan_lines()
        )

    if any(k in s for k in ["verify", "verification", "authentic", "trust", "badge"]):
        return (
            "Every property on Restamp goes through verification of address, photos, and owner authenticity. "
            "Verified listings receive a 'Verified' badge and top search placement."
        )

    if any(k in s for k in ["find", "search", "filter", "apartment", "flat", "villa", "house", "plot", "rent", "buy", "lease", "locality", "chennai"]):
        return _snapshot_or_generic(grounding, is_ta)

    # Role-based fallbacks
    if role == "Owner":
        return (
            "I can help you list properties, review buyer leads, or track scheduled site visits. "
            "What would you like assistance with?"
        )
    if role == "Agent":
        return (
            "I can assist you with agent locality plans, buyer leads, and the agent dashboard. "
            "How can I help your real estate business today?"
        )

    return (
        "I'm Reva AI, your personal Restamp property assistant (powered by Agaram 2.5B). "
        "I can help you search properties in Chennai, understand 0% brokerage, schedule site visits, or post a property. Ask me anything!"
    )


def _snapshot_or_generic(grounding: dict, is_ta: bool) -> str:
    """A search question: answer with REAL listings when there are any."""
    snap = grounding.get("snapshot")
    if snap and (snap.get("listings") or snap.get("total") is not None):
        summary = snap["summary"]
        lines = [f"{summary}."]
        for card in snap.get("listings") or []:
            lines.append(f"• {card['title']} — {card['price_display']} · {card['locality']}, {card['city']}")
        if snap.get("total"):
            lines.append("Open any property to see details and schedule a visit — no brokerage.")
        return "\n".join(lines)
    return (
        "தற்போது Restamp-ல் சரிபார்க்கப்பட்ட சொத்துகள் எதுவும் இந்தத் தேடலுக்குப் பொருந்தவில்லை. "
        "Buy / Rent வடிகட்டிகளுடன் மீண்டும் முயற்சிக்கவும்."
        if is_ta
        else "No verified Restamp listings match that search right now. Try browsing all properties with the Buy/Rent filters on the website."
    )


@router.get("/chat/health")
def chat_health():
    """Health check for Reva AI bilingual service with Agaram 2.5B metadata."""
    has_agaram = bool(settings.AGARAM_ENDPOINT or settings.AGARAM_API_KEY)
    has_ai = has_agaram or bool(settings.OPENAI_API_KEY)

    active_model = (
        settings.AGARAM_MODEL_NAME
        if has_agaram
        else (settings.OPENAI_MODEL if settings.OPENAI_API_KEY else "local-knowledge-base")
    )
    provider = (
        "agaram-murai-labs"
        if has_agaram
        else ("openai" if settings.OPENAI_API_KEY else "local-knowledge-base")
    )

    return {
        "ok": True,
        "mode": "ai" if has_ai else "demo",
        "model": active_model,
        "provider": provider,
        "languages": ["en", "ta"],
        "voice": True,
        "agaram_ready": has_agaram,
    }


@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    payload: ChatRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    user_id: Optional[int] = Depends(get_optional_user_id),
):
    """Chat endpoint powered by Agaram 2.5B (Murai Labs) with multi-role support.

    Guests get public answers only. Signed-in callers are rate-limited per
    account as well as per IP and get their OWN account context plus real
    listing data in the answer; the role is resolved from the database, not
    from the request.
    """
    msg = payload.message.strip()
    if not msg or len(msg) > 500:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message must contain 1–500 characters",
        )

    language = payload.language if payload.language in VALID_LANGUAGES else "en"

    # Shared-storage rate limiting FIRST (a refused request still counts).
    ip = rl_svc.client_ip(http_request)
    rl_svc.check_and_hit(db, f"chat:ip:{ip}", _chat_rate_limits(per_user=False))
    if user_id is not None:
        rl_svc.check_and_hit(db, f"chat:user:{user_id}", _chat_rate_limits(per_user=True))

    role = _role_from_db(db, user_id)
    grounding = _grounded_context(db, user_id, msg)

    provider = await _query_provider(msg, role, language, payload.history or [], grounding)
    if provider is not None:
        reply, model = provider
        if _needs_crisis_footer(msg):
            reply += CRISIS_FOOTER
        return ChatResponse(
            reply=reply,
            language=language,
            model=model,
            demo=False,
            listings=_response_listings(grounding),
        )

    demo_reply = _demo_reply(msg, role, language, grounding)
    if _needs_crisis_footer(msg):
        demo_reply += CRISIS_FOOTER

    return ChatResponse(
        reply=demo_reply,
        language=language,
        model="murai-labs/agaram-2.5b-instruct",
        demo=True,
        listings=_response_listings(grounding),
    )


def _response_listings(grounding: dict) -> Optional[List[RevaListing]]:
    cards = (grounding.get("snapshot") or {}).get("listings") or []
    if not cards:
        return None
    try:
        return [RevaListing(**card) for card in cards]
    except Exception:
        log.exception("Reva listing card build failed")
        return None


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_endpoint(
    http_request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
    language: str = Form("en"),
):
    """Voice transcription endpoint.

    Uploads a short voice recording and transcribes it into English or Tamil.
    Hardened for a public endpoint: per-IP hourly quota (shared storage),
    extension + declared-content-type checks, 8 MB application cap (the
    transport cap is enforced in app.main before the body is read).
    """
    lang = language if language in VALID_LANGUAGES else "en"

    ip = rl_svc.client_ip(http_request)
    rl_svc.check_and_hit(
        db,
        f"transcribe:ip:{ip}",
        (rl_svc.Limit("transcribe:hour", settings.TRANSCRIBE_PER_HOUR_PER_IP, 3600),),
    )

    filename = file.filename or "audio.m4a"
    if not AUDIO_EXTENSIONS_PATTERN.search(filename):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid audio file (use m4a/mp4/mp3/wav/webm, maximum 8 MB)",
        )
    declared = (file.content_type or "").lower()
    if declared and not declared.startswith(_AUDIO_CONTENT_TYPES):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid audio file (use m4a/mp4/mp3/wav/webm, maximum 8 MB)",
        )

    content = await file.read()
    if not content or len(content) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Audio exceeds 8 MB limit or is empty",
        )

    # Demo fallback when OpenAI key is not configured
    if not settings.OPENAI_API_KEY:
        demo_text = (
            "சென்னையில் 0% தரகில் வீடு எங்கு தேடுவது?"
            if lang == "ta"
            else "How do I find a zero-brokerage property in Chennai?"
        )
        return TranscribeResponse(text=demo_text, language=lang, demo=True)

    # Call OpenAI Audio Transcription API
    transcribe_model = settings.OPENAI_TRANSCRIBE_MODEL or "gpt-transcribe"

    files = {
        "file": (filename, content, file.content_type or "audio/m4a"),
    }
    data = {
        "model": transcribe_model,
        "language": lang,
    }

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                },
                data=data,
                files=files,
            )

        if resp.status_code == 200:
            res_data = resp.json()
            recognized_text = (res_data.get("text") or "").strip()
            if recognized_text:
                return TranscribeResponse(text=recognized_text, language=lang, demo=False)

        log.warning("Transcription provider returned non-200: %s", resp.status_code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Transcription service unavailable",
        )

    except HTTPException:
        raise
    except Exception as exc:
        # Error class only (no traceback): provider errors can echo request details.
        log.warning("Transcription error (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Transcription service error",
        )

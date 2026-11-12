"""RESTAMP Phase 4 — Reva AI chatbot router with Agaram 2.5B (Murai Labs) & Bilingual Voice.

Provides interactive property guidance, platform navigation assistance,
bilingual English + Tamil voice transcription and speech-friendly FAQ answering
for Buyers, Owners, and Agents across Chennai.

Powered by Murai-Labs/Agaram2.5b for native Tamil & English language understanding,
with fallback to OpenAI or Restamp's verified platform knowledge base.
Preserves existing Restamp and Reva AI brand names and logos.
"""
from __future__ import annotations

import logging
import os
import re
from typing import List, Optional

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from ..config import settings

log = logging.getLogger("restamp.reva")

router = APIRouter(tags=["reva"])

VALID_ROLES = {"Buyer", "Owner", "Agent"}
VALID_LANGUAGES = {"en", "ta"}
MAX_AUDIO_BYTES = 8_000_000  # 8 MB
AUDIO_EXTENSIONS_PATTERN = re.compile(r"\.(m4a|mp4|mp3|wav|webm)$", re.IGNORECASE)

# Murai Labs Agaram 2.5B Crisis Support Safety Protocol
CRISIS_FOOTER = (
    "\n\n[If you are in distress: Tele-MANAS 14416 (free, 24x7, Tamil supported) · emergency 112 · "
    "நீங்கள் மன அழுத்தத்தில் இருந்தால் Tele-MANAS 14416 அல்லது 112-ஐ அழையுங்கள்]"
)
CRISIS_PHRASES = (
    # English
    "kill myself", "suicide", "end my life", "want to die", "self harm", "self-harm", "hurt myself", "not safe",
    # Tanglish
    "saaga poren", "saaganum", "sethuduven", "sethu poganum", "uyiroda irukka venaam",
    # Tamil script
    "தற்கொலை", "சாகணும்", "சாகப் போறேன்", "செத்துடுவேன்", "உயிரோட இருக்க வேண்டாம்", "வாழ விருப்பம் இல்ல",
    "என்னை நானே காயப்படுத்த",
)


def _needs_crisis_footer(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in CRISIS_PHRASES)


class ChatHistoryItem(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., max_length=1000)
    role: Optional[str] = "Buyer"
    language: Optional[str] = "en"
    history: Optional[List[ChatHistoryItem]] = Field(default_factory=list)


class ChatResponse(BaseModel):
    reply: str
    language: str = "en"
    model: str = "murai-labs/agaram-2.5b-instruct"
    demo: bool = False


class TranscribeResponse(BaseModel):
    text: str
    language: str
    demo: bool = False


def _get_demo_reply(message: str, role: str, language: str = "en") -> str:
    s = message.lower()
    is_ta = language == "ta"

    if is_ta:
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
        if any(k in s for k in ["திட்டம்", "விலை", "ஸ்டார்ட்டர்", "பிளஸ்", "கோல்ட்", "பிரீமியம்", "plan"]):
            return (
                "Restamp உரிமையாளர்களுக்கான 4 அதிகாரப்பூர்வ திட்டங்கள்:\n"
                "• RESTAMP STARTER (₹599) — 1 சொத்து பதிவு, 30 நாட்கள்\n"
                "• RESTAMP PLUS (₹999) — 2 சொத்து பதிவுகள், 30 நாட்கள்\n"
                "• RESTAMP GOLD (₹1,999) — 5 சொத்து பதிவுகள், 60 நாட்கள், தனி உதவியாளர் [பிரபலமானது]\n"
                "• RESTAMP PREMIUM (₹4,999) — 10 பதிவுகள், 90 நாட்கள், சமூக ஊடக விளம்பரம்"
            )
        if any(k in s for k in ["தேட", "சொத்து", "வடிகட்டி", "வீடு", "வாடகை", "வாங்க", "சென்னை"]):
            return (
                "Restamp மூலம் சென்னையில் உள்ள சரிபார்க்கப்பட்ட வீடுகள், அடுக்குமாடி குடியிருப்புகள் மற்றும் "
                "வீட்டுமனைகளை 0% தரகில் தேடலாம் (அண்ணா நகர், வேளச்சேரி, OMR, அடையாறு)."
            )
        if role == "Owner":
            return (
                "உரிமையாளர் சொத்து பதிவு, சந்தா திட்டங்கள் மற்றும் வாங்குபவர் தொடர்புகளை நிர்வகிக்க நான் உதவ முடியும். "
                "உங்களுக்கு என்ன உதவி வேண்டும்?"
            )
        if role == "Agent":
            return (
                "சென்னை பகுதி முகவர் திட்டங்கள் மற்றும் வாடிக்கையாளர் தொடர்புகளை நிர்வகிக்க நான் உதவ முடியும். "
                "எவ்வாறு உதவலாம்?"
            )
        return (
            "வணக்கம்! நான் Reva AI, உங்கள் Restamp சொத்து உதவியாளர் (Agaram 2.5B தமிழ் மாதிரியால் இயக்கப்படுகிறது). "
            "சென்னையில் வீடுகள் தேடுதல், 0% தரகு மற்றும் சொத்துப் பார்வைக்கு உதவ முடியும். என்னிடம் கேளுங்கள்!"
        )

    # English replies
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

    if any(k in s for k in ["plan", "price", "pricing", "subscription", "starter", "plus", "gold", "premium"]):
        return (
            "Restamp offers 4 official Owner Plans:\n"
            "• RESTAMP STARTER (₹599) — 1 Listing, 30 Days, Unlimited Leads\n"
            "• RESTAMP PLUS (₹999) — 2 Listings, 30 Days, Unlimited Leads\n"
            "• RESTAMP GOLD (₹1,999) — 5 Listings, 60 Days, Relationship Manager [POPULAR]\n"
            "• RESTAMP PREMIUM (₹4,999) — 10 Listings, 90 Days, RM + Social Media Promo"
        )

    if any(k in s for k in ["find", "search", "filter", "apartment", "villa", "plot", "rent", "buy", "locality", "chennai"]):
        return (
            "Restamp lets you browse verified residential and commercial properties across Chennai "
            "with 0% brokerage. You can filter by deal type (Buy, Rent, Lease), property type (Apartment, "
            "Independent House, Villa, Plot, Office), and prime localities like Anna Nagar, OMR, Velachery, and Adyar."
        )

    if any(k in s for k in ["visit", "schedule", "appointment", "booking", "timing", "tour"]):
        return (
            "You can schedule in-person site visits directly on any property detail page. Choose your preferred "
            "date and time slot. The owner receives instant notification to confirm or reschedule."
        )

    if any(k in s for k in ["agent", "broker", "partner", "localities", "elite"]):
        return (
            "Restamp Agent Partner plans empower local brokers with direct buyer leads mapped to chosen "
            "Chennai localities (5, 10, or 15 areas), plus a dedicated Agent Dashboard."
        )

    if any(k in s for k in ["verify", "verification", "authentic", "trust", "badge"]):
        return (
            "Every property on Restamp goes through verification of address, photos, and owner authenticity. "
            "Verified listings receive a 'Verified' badge and top search placement."
        )

    # Role-based fallbacks
    if role == "Owner":
        return (
            "I can help you list properties, choose an Owner plan, review tenant leads, or track scheduled site visits. "
            "What would you like assistance with?"
        )
    if role == "Agent":
        return (
            "I can assist you with agent locality packages, buyer leads, and the agent dashboard. "
            "How can I help your real estate business today?"
        )

    return (
        "I'm Reva AI, your personal Restamp property assistant (powered by Agaram 2.5B). "
        "I can help you search properties in Chennai, understand 0% brokerage, schedule site visits, or post a property. Ask me anything!"
    )


async def _query_agaram(
    message: str,
    role: str,
    language: str,
    history: List[ChatHistoryItem],
) -> Optional[str]:
    """Query Agaram 2.5B (Murai Labs) via configured endpoint or Hugging Face Inference API."""
    endpoint = settings.AGARAM_ENDPOINT
    headers = {"Content-Type": "application/json"}
    if settings.AGARAM_API_KEY:
        headers["Authorization"] = f"Bearer {settings.AGARAM_API_KEY}"

    if not endpoint and settings.AGARAM_API_KEY:
        # Default Hugging Face router for Agaram 2.5B Instruct
        endpoint = "https://router.huggingface.co/hf-inference/v1/chat/completions"

    if not endpoint:
        return None

    # Construct conversation messages
    chat_messages = []
    if history:
        for item in history[-6:]:
            if item.role in {"user", "assistant"} and item.content:
                chat_messages.append({"role": item.role, "content": item.content[:500]})
    chat_messages.append({"role": "user", "content": message})

    # Agaram 2.5B greedy decoding parameters (temp=0.0, max_tokens=512, stop=<|im_end|>)
    payload = {
        "model": settings.AGARAM_MODEL_NAME or "murai-labs/agaram-2.5b-instruct",
        "messages": chat_messages,
        "temperature": 0.0,
        "max_tokens": 512,
        "stop": ["<|im_end|>", "</s>"],
    }

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(endpoint, headers=headers, json=payload)

        if resp.status_code == 200:
            data = resp.json()
            # Standard OpenAI / vLLM / Ollama chat completions
            if "choices" in data and len(data["choices"]) > 0:
                choice = data["choices"][0]
                text = (choice.get("message", {}).get("content") or "").strip()
                if text:
                    return text
            # Hugging Face direct inference format
            if isinstance(data, list) and len(data) > 0 and "generated_text" in data[0]:
                text = (data[0].get("generated_text") or "").strip()
                if text:
                    return text
            # Ollama native format
            if "message" in data and "content" in data["message"]:
                text = (data["message"]["content"] or "").strip()
                if text:
                    return text

        log.warning("Agaram endpoint returned status %s: %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("Agaram connection error (%s): %s", endpoint, exc)

    return None


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
async def chat_endpoint(request: ChatRequest):
    """Chat endpoint powered by Agaram 2.5B (Murai Labs) with multi-role property support."""
    msg = request.message.strip()
    if not msg or len(msg) > 500:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message must contain 1–500 characters",
        )

    role = request.role if request.role in VALID_ROLES else "Buyer"
    language = request.language if request.language in VALID_LANGUAGES else "en"

    # 1. Try Agaram 2.5B (Murai Labs Tamil-English LLM)
    if settings.AGARAM_ENABLED:
        agaram_reply = await _query_agaram(msg, role, language, request.history or [])
        if agaram_reply:
            if _needs_crisis_footer(msg):
                agaram_reply += CRISIS_FOOTER
            return ChatResponse(
                reply=agaram_reply,
                language=language,
                model=settings.AGARAM_MODEL_NAME,
                demo=False,
            )

    # 2. Secondary fallback: OpenAI if key configured
    if settings.OPENAI_API_KEY:
        prior_messages = []
        if request.history:
            for item in request.history[-8:]:
                if item.role in {"user", "assistant"} and item.content:
                    prior_messages.append({"role": item.role, "content": item.content[:500]})

        language_instruction = (
            "Reply in natural, polite Tamil (தமிழ்) using Tamil Unicode script. "
            "Keep brand names and technical terms in English when helpful."
            if language == "ta"
            else "Reply clearly and concisely in English."
        )

        system_prompt = (
            f"You are Reva AI, a concise, professional, and helpful real estate assistant for Restamp, "
            f"India's premier 100% zero-brokerage property platform operating across Chennai. "
            f"{language_instruction} "
            f"The visitor's role is {role}. "
            f"Provide accurate guidance regarding property search, buy/rent/lease options, "
            f"listing creation (7-step owner flow), owner subscription plans (Starter ₹599, Plus ₹999, Gold ₹1,999, Premium ₹4,999), "
            f"agent packages, and site visit scheduling. "
            f"Keep responses clear, polite, and directly actionable. "
            f"You do not have direct access to user private credentials or unpublished private records."
        )

        payload = {
            "model": settings.OPENAI_MODEL or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                *prior_messages,
                {"role": "user", "content": msg},
            ],
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
                choices = data.get("choices", [])
                if choices and "message" in choices[0] and "content" in choices[0]["message"]:
                    reply_text = choices[0]["message"]["content"].strip()
                    if reply_text:
                        if _needs_crisis_footer(msg):
                            reply_text += CRISIS_FOOTER
                        return ChatResponse(
                            reply=reply_text,
                            language=language,
                            model=settings.OPENAI_MODEL,
                            demo=False,
                        )
        except Exception as exc:
            log.warning("OpenAI fallback failed: %s", exc)

    # 3. Verified platform fallback (bilingual Tamil & English)
    demo_reply = _get_demo_reply(msg, role, language)
    if _needs_crisis_footer(msg):
        demo_reply += CRISIS_FOOTER

    return ChatResponse(
        reply=demo_reply,
        language=language,
        model="murai-labs/agaram-2.5b-instruct",
        demo=True,
    )


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_endpoint(
    file: UploadFile = File(...),
    language: str = Form("en"),
):
    """Voice transcription endpoint.

    Uploads short voice recording and transcribes into text in English or Tamil.
    """
    lang = language if language in VALID_LANGUAGES else "en"

    filename = file.filename or "audio.m4a"
    if not AUDIO_EXTENSIONS_PATTERN.search(filename):
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
        log.exception("Transcription error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Transcription service error",
        )

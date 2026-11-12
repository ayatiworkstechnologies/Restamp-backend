"""Tests for Reva AI chatbot router with Agaram 2.5B (Murai Labs) & Voice support."""
import io
from unittest.mock import patch, AsyncMock
import pytest
from fastapi.testclient import TestClient

from app.config import settings


def test_reva_chat_health(client: TestClient):
    resp = client.get("/chat/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "mode" in data
    assert "model" in data
    assert "ta" in data["languages"]
    assert data["voice"] is True
    assert "agaram_ready" in data


def test_reva_chat_demo_zero_brokerage_en(client: TestClient):
    resp = client.post(
        "/chat",
        json={"message": "Tell me about zero brokerage on Restamp", "role": "Buyer", "language": "en"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "zero-brokerage" in data["reply"].lower() or "brokerage" in data["reply"].lower()
    assert data["language"] == "en"
    assert data["demo"] is True


def test_reva_chat_demo_tamil_agaram(client: TestClient):
    resp = client.post(
        "/chat",
        json={"message": "வணக்கம், நீங்கள் யார்?", "role": "Buyer", "language": "ta"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "Agaram 2.5B" in data["reply"] or "Reva AI" in data["reply"]
    assert data["language"] == "ta"
    assert data["model"] == "murai-labs/agaram-2.5b-instruct"
    assert data["demo"] is True


def test_reva_chat_agaram_crisis_footer(client: TestClient):
    resp = client.post(
        "/chat",
        json={"message": "I feel unsafe and want to hurt myself", "role": "Buyer", "language": "en"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "Tele-MANAS" in data["reply"]
    assert "14416" in data["reply"]


@pytest.mark.asyncio
async def test_reva_chat_agaram_endpoint_integration(client: TestClient):
    with patch.object(settings, "AGARAM_ENDPOINT", "http://localhost:8788/v1/chat/completions"):
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "வணக்கம்! நான் அகரம் 2.5B உதவியாளர்."}}]
        }

        with patch("httpx.AsyncClient.post", return_value=mock_response):
            resp = client.post(
                "/chat",
                json={
                    "message": "சொத்து தேடுவது எப்படி?",
                    "role": "Buyer",
                    "language": "ta",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "வணக்கம்!" in data["reply"]
            assert data["demo"] is False
            assert data["model"] == settings.AGARAM_MODEL_NAME


def test_reva_chat_demo_owner_plans(client: TestClient):
    resp = client.post(
        "/chat",
        json={"message": "What are the owner subscription plans and prices?", "role": "Owner", "language": "en"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "STARTER" in data["reply"]
    assert "599" in data["reply"]
    assert "GOLD" in data["reply"]
    assert data["demo"] is True


def test_reva_chat_validation_empty(client: TestClient):
    resp = client.post("/chat", json={"message": "   "})
    assert resp.status_code == 400
    assert "Message must contain 1–500 characters" in resp.json()["detail"]


def test_reva_transcribe_demo(client: TestClient):
    dummy_audio = io.BytesIO(b"RIFF....WAVEfmt ....data....fake audio bytes")
    resp = client.post(
        "/transcribe",
        files={"file": ("recording.wav", dummy_audio, "audio/wav")},
        data={"language": "en"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "text" in data
    assert len(data["text"]) > 0
    assert data["language"] == "en"
    assert data["demo"] is True


def test_reva_transcribe_demo_tamil(client: TestClient):
    dummy_audio = io.BytesIO(b"fake-m4a-bytes")
    resp = client.post(
        "/transcribe",
        files={"file": ("voice.m4a", dummy_audio, "audio/m4a")},
        data={"language": "ta"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "சென்னையில்" in data["text"]
    assert data["language"] == "ta"

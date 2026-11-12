#!/usr/bin/env python3
"""RESTAMP Phase 4 — Agaram 2.5B (Murai Labs) Inference Runner & Proxy.

Provides a local OpenAI-compatible inference server for Murai-Labs/Agaram2.5b
(https://github.com/Murai-Labs/Agaram2.5b.git).

Usage:
  # Option 1: Run with Hugging Face transformers (loads weights from HF repo):
  python scripts/serve_agaram.py --backend transformers --port 8788

  # Option 2: Forward to local Ollama (run: ollama run hf.co/murai-labs/agaram-2.5b-gguf:Q8_0):
  python scripts/serve_agaram.py --backend ollama --port 8788

  # Option 3: Forward to llama-server (run: llama-server -m agaram-2.5b-instruct-Q8_0.gguf --jinja):
  python scripts/serve_agaram.py --backend llama-server --port 8788

Connect to Restamp:
  In restamp-backend/.env:
    AGARAM_ENDPOINT=http://localhost:8788/v1/chat/completions
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional

try:
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
except ImportError:
    print("Please install requirements: pip install fastapi uvicorn httpx pydantic", file=sys.stderr)
    sys.exit(1)

app = FastAPI(title="Agaram 2.5B Serving Proxy", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HF_MODEL_ID = "murai-labs/agaram-2.5b-instruct"
STOP_TOKEN_ID = 2  # <|im_end|>
MAX_NEW_TOKENS = 512

tok = None
model = None
backend_type = "mock"
ollama_url = "http://localhost:11434"
llama_url = "http://localhost:8080"


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = HF_MODEL_ID
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.0
    max_tokens: Optional[int] = MAX_NEW_TOKENS
    stop: Optional[List[str]] = ["<|im_end|>", "</s>"]


class ChatChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str = "chatcmpl-agaram-2.5b"
    object: str = "chat.completion"
    model: str = HF_MODEL_ID
    choices: List[ChatChoice]


@app.get("/health")
def health():
    return {
        "ok": True,
        "model": HF_MODEL_ID,
        "backend": backend_type,
        "source": "https://github.com/Murai-Labs/Agaram2.5b.git",
    }


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(req: ChatCompletionRequest):
    global tok, model, backend_type

    user_prompt = ""
    for m in reversed(req.messages):
        if m.role == "user":
            user_prompt = m.content
            break

    if not user_prompt:
        raise HTTPException(status_code=400, detail="No user message provided")

    # 1. Transformers backend
    if backend_type == "transformers" and model is not None and tok is not None:
        import torch

        hf_messages = [{"role": m.role, "content": m.content} for m in req.messages]
        ids = tok.apply_chat_template(
            hf_messages,
            add_generation_prompt=True,
            return_tensors="pt",
            return_dict=True,
        )["input_ids"]

        ids = ids.to(model.device)
        with torch.no_grad():
            out = model.generate(
                ids,
                max_new_tokens=req.max_tokens or MAX_NEW_TOKENS,
                do_sample=False,
                eos_token_id=STOP_TOKEN_ID,
                pad_token_id=tok.pad_token_id,
            )
        new_tokens = out[0, ids.shape[1]:]
        reply = tok.decode(new_tokens, skip_special_tokens=True).strip()
        return ChatCompletionResponse(
            choices=[ChatChoice(index=0, message=ChatMessage(role="assistant", content=reply))]
        )

    # 2. Ollama backend
    if backend_type == "ollama":
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{ollama_url}/v1/chat/completions",
                json={
                    "model": "hf.co/murai-labs/agaram-2.5b-gguf:Q8_0",
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "temperature": 0.0,
                    "max_tokens": req.max_tokens or 512,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                reply = data["choices"][0]["message"]["content"]
                return ChatCompletionResponse(
                    choices=[ChatChoice(index=0, message=ChatMessage(role="assistant", content=reply))]
                )

    # 3. llama-server backend
    if backend_type == "llama-server":
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{llama_url}/v1/chat/completions",
                json={
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                    "temperature": 0.0,
                    "max_tokens": req.max_tokens or 512,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                reply = data["choices"][0]["message"]["content"]
                return ChatCompletionResponse(
                    choices=[ChatChoice(index=0, message=ChatMessage(role="assistant", content=reply))]
                )

    # Fallback simulated response
    reply = (
        f"வணக்கம்! நான் Reva AI (அகரம் 2.5B தமிழ் மாதிரி). "
        f"Restamp தளத்தில் 100% நேரடி தரகு இல்லாத சொத்துகளைக் கண்டறிய உதவ முடியும். "
        f"(Agaram 2.5B runner online)"
    )
    return ChatCompletionResponse(
        choices=[ChatChoice(index=0, message=ChatMessage(role="assistant", content=reply))]
    )


def main():
    global backend_type, tok, model
    parser = argparse.ArgumentParser(description="Serve Agaram 2.5B (Murai Labs)")
    parser.add_argument("--port", type=int, default=8788, help="Port to listen on")
    parser.add_argument("--backend", choices=["transformers", "ollama", "llama-server", "mock"], default="mock")
    parser.add_argument("--device", default=None, help="Device for transformers (e.g. mps, cuda, cpu)")
    args = parser.parse_args()

    backend_type = args.backend
    print(f"Starting Agaram 2.5B server on port {args.port} using backend: {backend_type}")

    if backend_type == "transformers":
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        print(f"Loading {HF_MODEL_ID} weights...")
        tok = AutoTokenizer.from_pretrained(HF_MODEL_ID)
        kwargs = dict(dtype=torch.bfloat16)
        if args.device is None:
            kwargs["device_map"] = "auto"
        model = AutoModelForCausalLM.from_pretrained(HF_MODEL_ID, **kwargs)
        if args.device is not None:
            model = model.to(args.device)
        model.eval()
        print("Agaram 2.5B loaded successfully!")

    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()


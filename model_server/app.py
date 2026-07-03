"""OpenAI-compatible model-server for the World Library pipeline.

Runs on the NVIDIA DGX Spark and is reached by the Go pipeline over the
Tailscale mesh. Round 1 implements only the chat/completions route backed by
a local LLM (default Qwen2.5-72B-Instruct via vLLM). The backend is hidden
behind generate() so it can be swapped for transformers/llama.cpp, and a
router registry leaves room for future diffusion/LoRA routes without changing
this contract.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

MODEL_NAME = os.environ.get("MODEL_NAME", "qwen2.5-72b-instruct")
MODEL_PATH = os.environ.get("MODEL_PATH", "Qwen/Qwen2.5-72B-Instruct")
MODEL_API_KEY = os.environ.get("MODEL_API_KEY", "")
MODEL_BACKEND = os.environ.get("MODEL_BACKEND", "vllm")
MAX_MODEL_LEN = int(os.environ.get("MODEL_CONTEXT", "32768"))

app = FastAPI(title="WorldLibrary model-server", version="1.0")

# ---------------------------------------------------------------------------
# Backend (lazy-initialised so the module imports without GPU deps present).
# ---------------------------------------------------------------------------

_engine = None


def _get_engine():
    global _engine
    if _engine is not None:
        return _engine
    if MODEL_BACKEND == "vllm":
        from vllm import LLM, SamplingParams  # noqa: F401  (imported for side effect/availability)

        _engine = LLM(model=MODEL_PATH, max_model_len=MAX_MODEL_LEN)
        return _engine
    raise RuntimeError(f"unsupported MODEL_BACKEND: {MODEL_BACKEND}")


def generate(messages: List["ChatMessage"], temperature: float, max_tokens: int) -> str:
    """Single abstraction every route goes through. Swap body to change backend."""
    from vllm import SamplingParams

    engine = _get_engine()
    tokenizer = engine.get_tokenizer()
    prompt = tokenizer.apply_chat_template(
        [{"role": m.role, "content": m.content} for m in messages],
        tokenize=False,
        add_generation_prompt=True,
    )
    params = SamplingParams(temperature=temperature, max_tokens=max_tokens)
    out = engine.generate([prompt], params)
    return out[0].outputs[0].text


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def require_key(request: Request) -> None:
    if not MODEL_API_KEY:
        return  # auth disabled when no key configured
    header = request.headers.get("authorization", "")
    if header != f"Bearer {MODEL_API_KEY}":
        raise HTTPException(status_code=401, detail="invalid or missing API key")


# ---------------------------------------------------------------------------
# OpenAI-compatible chat schema (subset)
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    temperature: float = 0.2
    max_tokens: int = 1024


@app.get("/healthz")
def healthz():
    return {"status": "ok", "model": MODEL_NAME, "backend": MODEL_BACKEND}


@app.post("/v1/chat/completions", dependencies=[Depends(require_key)])
def chat_completions(req: ChatRequest):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages must not be empty")
    try:
        text = generate(req.messages, req.temperature, req.max_tokens)
    except Exception as exc:  # surface as OpenAI-style error envelope
        return JSONResponse(status_code=500, content={"error": {"message": str(exc)}})
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model or MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }


# Future routes (diffusion / LoRA) register here; NOT implemented in round 1.
EXTRA_ROUTERS: list = []
for _r in EXTRA_ROUTERS:
    app.include_router(_r)

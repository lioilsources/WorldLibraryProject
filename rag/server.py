#!/usr/bin/env python3
"""RAG chatbot server. Běží na SPARK, mluví na vLLM (localhost) a
ChromaDB na JODA.

Oproti EduRAG server.py:
  - vLLM přes OpenAI-kompatibilní API místo Ollamy
  - Chroma HttpClient (server na NAS) místo embedded PersistentClient
  - konverzační paměť per session_id (multi-turn povídání)
  - systémový prompt z externího souboru (--prompt-file)

Použití (na SPARK):
    python3 server.py --chroma-url http://joda:8000 \
        --llm-url http://localhost:8000/v1 --llm-model Qwen/Qwen3-32B
"""

import argparse
import re
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import chromadb
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

DEFAULT_EMBED_MODEL = "intfloat/multilingual-e5-large"
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    top_k: int = 5


class ResetRequest(BaseModel):
    session_id: str


def pick_device(requested: str) -> str:
    if requested != "auto":
        return requested
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class RAGServer:
    def __init__(self, args):
        self.args = args
        self.system_prompt = Path(args.prompt_file).read_text(encoding="utf-8")

        url = urlparse(args.chroma_url)
        client = chromadb.HttpClient(host=url.hostname, port=url.port or 8000)
        self.collection = client.get_collection(args.collection)

        device = pick_device(args.device)
        print(f"Načítám {args.embed_model} (device={device})...")
        self.embedder = SentenceTransformer(args.embed_model, device=device)

        self.llm = OpenAI(base_url=args.llm_url, api_key=args.llm_api_key)
        # historie: session_id -> deque[(role, content)]
        self.sessions: dict[str, deque] = {}

    def retrieve(self, query: str, top_k: int):
        text = f"query: {query}" if "e5" in self.args.embed_model.lower() else query
        embedding = self.embedder.encode([text], normalize_embeddings=True)[0]
        result = self.collection.query(
            query_embeddings=[embedding.tolist()],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        hits = []
        for doc, meta, dist in zip(
            result["documents"][0], result["metadatas"][0], result["distances"][0]
        ):
            hits.append({"text": doc, "meta": meta or {}, "distance": dist})
        return hits

    def build_messages(self, message: str, hits, history):
        context_lines = []
        for i, h in enumerate(hits, 1):
            m = h["meta"]
            label = m.get("work") or m.get("title") or "neznámý zdroj"
            context_lines.append(f"[{i}] {label} ({m.get('group', '?')}):\n{h['text']}")
        context = "\n\n".join(context_lines) if context_lines else "(nic nenalezeno)"

        messages = [{"role": "system", "content": self.system_prompt}]
        for role, content in history:
            messages.append({"role": role, "content": content})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Úryvky z knihovny relevantní k otázce:\n\n{context}\n\n"
                    f"Otázka: {message}"
                ),
            }
        )
        return messages

    def chat(self, req: ChatRequest):
        session_id = req.session_id or str(uuid.uuid4())
        history = self.sessions.setdefault(
            session_id, deque(maxlen=2 * self.args.history_turns)
        )

        hits = self.retrieve(req.message, req.top_k)
        messages = self.build_messages(req.message, hits, history)

        extra = {}
        if self.args.no_think:
            # Qwen3: vypnout thinking mód přes chat template
            extra["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        completion = self.llm.chat.completions.create(
            model=self.args.llm_model,
            messages=messages,
            temperature=self.args.temperature,
            max_tokens=self.args.max_tokens,
            **extra,
        )
        answer = THINK_RE.sub("", completion.choices[0].message.content or "").strip()

        # do historie jde otázka bez kontextu, ať se paměť nenafukuje
        history.append(("user", req.message))
        history.append(("assistant", answer))

        sources = [
            {
                "work": h["meta"].get("work"),
                "title": h["meta"].get("title"),
                "group": h["meta"].get("group"),
                "lang": h["meta"].get("lang"),
                "path": h["meta"].get("path"),
                "distance": h["distance"],
                "excerpt": h["text"][:200],
            }
            for h in hits
        ]
        return {"answer": answer, "sources": sources, "session_id": session_id}


def create_app(args) -> FastAPI:
    server = RAGServer(args)
    app = FastAPI(title="Knihovna RAG chatbot")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.post("/chat")
    def chat(req: ChatRequest):
        if not req.message.strip():
            raise HTTPException(status_code=400, detail="prázdná zpráva")
        return server.chat(req)

    @app.post("/reset")
    def reset(req: ResetRequest):
        server.sessions.pop(req.session_id, None)
        return {"ok": True}

    @app.get("/status")
    def status():
        return {
            "collection": args.collection,
            "documents": server.collection.count(),
            "llm_model": args.llm_model,
            "embed_model": args.embed_model,
            "active_sessions": len(server.sessions),
        }

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def main():
    p = argparse.ArgumentParser(description="RAG chatbot server")
    p.add_argument("--chroma-url", default="http://localhost:8000", help="Chroma na JODA")
    p.add_argument("--collection", default="books")
    p.add_argument("--llm-url", default="http://localhost:8000/v1", help="vLLM endpoint")
    p.add_argument("--llm-model", default="Qwen/Qwen3-32B")
    p.add_argument("--llm-api-key", default="none", help="vLLM klíč (typicky netřeba)")
    p.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    p.add_argument("--device", default="auto", help="auto|cuda|mps|cpu")
    p.add_argument("--prompt-file", default=str(Path(__file__).parent / "prompts" / "librarian_cs.md"))
    p.add_argument("--history-turns", type=int, default=10, help="párů otázka+odpověď v paměti")
    p.add_argument("--temperature", type=float, default=0.4)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--no-think", action="store_true", help="vypnout Qwen3 thinking mód")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()

    app = create_app(args)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

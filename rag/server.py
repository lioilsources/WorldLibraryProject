#!/usr/bin/env python3
"""RAG chatbot server. Běží na SPARK vedle AiStacku.

LLM volá přes AiStack LiteLLM gateway (localhost:4000) — výchozí model
je role `translate` (Qwen3-32B-AWQ, nejlepší čeština v parku), pro
hluboké rozbory jde přepnout na `swarm-director` (Nemotron Super 120B,
`make up-swarm-director` v AiStacku). Thinking mód vypíná už LiteLLM
config (`enable_thinking: false`), flag --no-think je pro přímé vLLM.

Vektory čte z ChromaDB na JODA (AiStack swarm.nas compose, port 8006).

Oproti EduRAG server.py navíc: konverzační paměť per session_id,
systémový prompt ze souboru, volitelný vzdálený embedding backend.

Použití (na SPARK):
    python3 server.py   # výchozí: LiteLLM :4000, model translate, Chroma JODA :8006
"""

import argparse
import json
import os
import re
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import chromadb
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import OpenAI
from pydantic import BaseModel

from embeddings import DEFAULT_MODEL as DEFAULT_EMBED_MODEL
from embeddings import format_query, make_embedder

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    top_k: int = 5
    model: str | None = None  # per-request přepnutí (translate/swarm-director/lab)


class ResetRequest(BaseModel):
    session_id: str


class RAGServer:
    def __init__(self, args):
        self.args = args
        self.system_prompt = Path(args.prompt_file).read_text(encoding="utf-8")

        url = urlparse(args.chroma_url)
        client = chromadb.HttpClient(host=url.hostname, port=url.port or 8000)
        self.collection = client.get_collection(args.collection)

        # katalog děl z metadat kolekce — jednorázově při startu; po změně
        # korpusu (make embed) je potřeba server restartovat
        self.catalog = self._build_catalog()

        # volitelné anotace děl (gen_summaries.py) — obohatí /works i prompt
        summaries_path = Path(args.summaries_file)
        summaries = (
            json.loads(summaries_path.read_text(encoding="utf-8"))
            if summaries_path.exists()
            else {}
        )
        for work, info in self.catalog.items():
            info["summary"] = summaries.get(work)

        self.system_prompt += self._catalog_prompt()

        self.embedder = make_embedder(
            args.embed_model, device=args.device, url=args.embed_url
        )
        # Cloudflare Access service token — nutný, když --llm-url míří na
        # https://llm.ol1n.com místo na SPARK localhost
        cf_id = os.getenv("CF_ACCESS_CLIENT_ID")
        cf_secret = os.getenv("CF_ACCESS_CLIENT_SECRET")
        headers = None
        if cf_id and cf_secret:
            headers = {
                "CF-Access-Client-Id": cf_id,
                "CF-Access-Client-Secret": cf_secret,
            }
        self.llm = OpenAI(
            base_url=args.llm_url, api_key=args.llm_api_key, default_headers=headers
        )
        # historie: session_id -> deque[(role, content)]
        self.sessions: dict[str, deque] = {}

    def _build_catalog(self) -> dict:
        """work -> {group, lang, path, chunk_count} ze stránkovaného průchodu metadat."""
        catalog: dict[str, dict] = {}
        limit, offset = 1000, 0
        while True:
            page = self.collection.get(include=["metadatas"], limit=limit, offset=offset)
            metas = page["metadatas"] or []
            for m in metas:
                work = (m or {}).get("work")
                if not work:
                    continue
                entry = catalog.setdefault(
                    work,
                    {"group": m.get("group"), "lang": m.get("lang"),
                     "path": m.get("path"), "chunk_count": 0},
                )
                entry["chunk_count"] += 1
            if len(metas) < limit:
                break
            offset += limit
        return catalog

    def _catalog_prompt(self) -> str:
        """Sekce se seznamem děl pro systémový prompt — ať na „jaké knihy znáš?"
        odpovídá ze skutečného katalogu, ne z pěti náhodných úryvků."""
        by_group: dict[str, list[tuple[str, str | None]]] = {}
        for work, info in sorted(self.catalog.items()):
            by_group.setdefault(info["group"] or "misc", []).append(
                (work, info.get("summary"))
            )
        # u velkých skupin (Tipitaka ~60 knih) jen názvy — anotace by prompt
        # nafoukly o tisíce tokenů; plné anotace zůstávají v GET /works
        max_anotovanych = 25
        blocks = []
        for group, works in sorted(by_group.items()):
            if len(works) > max_anotovanych:
                blocks.append(f"### {group}\n" + ", ".join(w for w, _ in works))
            else:
                blocks.append(f"### {group}\n" + "\n".join(
                    f"- {w}" + (f" — {s}" if s else "") for w, s in works
                ))
        lines = "\n".join(blocks)
        return (
            "\n\nKnihovna právě obsahuje tato díla (podle tradice):\n"
            f"{lines}\n"
            "Na otázky, jaké knihy znáš nebo co je v knihovně, odpovídej z tohoto "
            "seznamu. Obsah děl mimo dodané úryvky neznáš — nepředstírej opak."
        )

    def _sources(self, hits) -> list[dict]:
        return [
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

    def retrieve(self, query: str, top_k: int):
        text = format_query(query, self.args.embed_model)
        embedding = self.embedder.encode([text])[0]
        result = self.collection.query(
            query_embeddings=[embedding],
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
            # jen pro přímé vLLM — přes LiteLLM to řeší config gatewaye
            extra["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        model = req.model or self.args.llm_model
        completion = self.llm.chat.completions.create(
            model=model,
            messages=messages,
            temperature=self.args.temperature,
            max_tokens=self.args.max_tokens,
            **extra,
        )
        answer = THINK_RE.sub("", completion.choices[0].message.content or "").strip()

        # do historie jde otázka bez kontextu, ať se paměť nenafukuje
        history.append(("user", req.message))
        history.append(("assistant", answer))

        return {"answer": answer, "sources": self._sources(hits),
                "session_id": session_id, "model": model}

    def chat_stream(self, req: ChatRequest):
        """SSE generátor: eventy {"delta": ...} po tokenech, na závěr
        {"done": true, sources, session_id, model} — pro Ol1nLLM streaming UX."""
        session_id = req.session_id or str(uuid.uuid4())
        history = self.sessions.setdefault(
            session_id, deque(maxlen=2 * self.args.history_turns)
        )

        hits = self.retrieve(req.message, req.top_k)
        messages = self.build_messages(req.message, hits, history)

        extra = {}
        if self.args.no_think:
            extra["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        model = req.model or self.args.llm_model
        stream = self.llm.chat.completions.create(
            model=model,
            messages=messages,
            temperature=self.args.temperature,
            max_tokens=self.args.max_tokens,
            stream=True,
            **extra,
        )
        parts = []
        for chunk in stream:
            if not chunk.choices:
                continue  # závěrečný usage chunk apod.
            delta = chunk.choices[0].delta.content or ""
            if delta:
                parts.append(delta)
                yield "data: " + json.dumps({"delta": delta}, ensure_ascii=False) + "\n\n"

        # <think> bloky řeší LiteLLM config; regex je pojistka pro přímé vLLM
        answer = THINK_RE.sub("", "".join(parts)).strip()
        history.append(("user", req.message))
        history.append(("assistant", answer))

        final = {"done": True, "sources": self._sources(hits),
                 "session_id": session_id, "model": model}
        yield "data: " + json.dumps(final, ensure_ascii=False) + "\n\n"


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

    @app.post("/chat/stream")
    def chat_stream(req: ChatRequest):
        if not req.message.strip():
            raise HTTPException(status_code=400, detail="prázdná zpráva")
        return StreamingResponse(
            server.chat_stream(req), media_type="text/event-stream"
        )

    @app.get("/works")
    def works():
        items = [
            {"work": work, **info}
            for work, info in sorted(
                server.catalog.items(), key=lambda kv: (kv[1]["group"] or "", kv[0])
            )
        ]
        return {"works": items, "count": len(items)}

    @app.post("/reset")
    def reset(req: ResetRequest):
        server.sessions.pop(req.session_id, None)
        return {"ok": True}

    @app.get("/status")
    def status():
        return {
            "collection": args.collection,
            "documents": server.collection.count(),
            "works": len(server.catalog),
            "llm_model": args.llm_model,
            "llm_url": args.llm_url,
            "embed_model": args.embed_model,
            "active_sessions": len(server.sessions),
        }

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def main():
    p = argparse.ArgumentParser(description="RAG chatbot server")
    p.add_argument("--chroma-url", default="http://192.168.88.88:8006",
                   help="Chroma na JODA (AiStack swarm.nas)")
    p.add_argument("--collection", default="books")
    p.add_argument("--llm-url", default=os.getenv("LLM_URL", "http://localhost:4000/v1"),
                   help="AiStack LiteLLM (na SPARKu localhost:4000/v1, "
                        "odjinud https://llm.ol1n.com/v1 + CF Access token v env)")
    p.add_argument("--llm-model", default="translate",
                   help="role v LiteLLM: translate|swarm-director|lab|dev")
    p.add_argument("--llm-api-key", default="dummy")
    p.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    p.add_argument("--embed-url", default=None,
                   help="OpenAI-kompatibilní embeddings API (např. swarm-embed "
                        "http://localhost:8005/v1); prázdné = lokální model")
    p.add_argument("--device", default="auto", help="auto|cuda|mps|cpu")
    p.add_argument("--prompt-file", default=str(Path(__file__).parent / "prompts" / "librarian_cs.md"))
    p.add_argument("--summaries-file", default=str(Path(__file__).parent / "summaries.json"),
                   help="anotace děl z gen_summaries.py (chybějící soubor = bez anotací)")
    p.add_argument("--history-turns", type=int, default=10, help="párů otázka+odpověď v paměti")
    p.add_argument("--temperature", type=float, default=0.4)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--no-think", action="store_true",
                   help="poslat enable_thinking=false přímo (jen mimo LiteLLM)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8090,
                   help="8090 — port 8080 má na SPARKu AiStack Go gateway")
    args = p.parse_args()

    app = create_app(args)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

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
import time
import unicodedata
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import chromadb
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from openai import OpenAI
from pydantic import BaseModel

from embeddings import DEFAULT_MODEL as DEFAULT_EMBED_MODEL
from embeddings import format_query, make_embedder
from retrieval import build_alias_index, diversify, route, route_groups

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
# model občas uvede překlad slovem ze zadání — useknout
EXCERPT_LEAD_RE = re.compile(
    r"^(?:překlad|český překlad|translation)\s*:\s*", re.IGNORECASE)

EXCERPT_PROMPT = (
    "Přelož do češtiny tenhle úryvek ze starého textu. Začíná i končí "
    "uprostřed věty — přelož celý a tak, jak je: nedoplňuj chybějící začátek "
    "ani konec, nevysvětluj, nekomentuj. Odpověz jen samotným překladem.\n\n"
    "---\n{text}\n---"
)


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
        # summaries.json: hodnota je buď string (starý formát = jen anotace),
        # nebo objekt {"summary": ..., "name_cs": ...} — číst tolerantně,
        # ať server nastartuje s oběma.
        #
        # Klíče jsou jména děl z metadat Chromy, a ta jsou v NFD (vznikla
        # z názvů souborů na macOS). Ruční doplněk se ale uloží v NFC a shoda
        # by tiše selhala — proto se na obou stranách porovnává NFC.
        summaries = {unicodedata.normalize("NFC", k): v for k, v in summaries.items()}
        for work, info in self.catalog.items():
            entry = summaries.get(unicodedata.normalize("NFC", work))
            if isinstance(entry, dict):
                info["summary"] = entry.get("summary")
                info["name_cs"] = entry.get("name_cs")
            else:
                info["summary"] = entry
                info["name_cs"] = None

        self.system_prompt += self._catalog_prompt()

        # rejstřík aliasů pro směrování dotazu na dílo (viz retrieval.py)
        self.alias_index = build_alias_index(
            {w: info.get("name_cs") for w, info in self.catalog.items()}
        )

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
        # vlákna na překlad úryvků — běží souběžně s generováním odpovědi
        # aspoň top_k vláken, ať se překlady jedné odpovědi neserializují
        self.translator = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="excerpt-cs"
        )

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

    # věta končí tečkou, za níž je mezera — ale tečka po samostatném velkém
    # písmenu je iniciála („překlad J. P. Allen"), ne konec věty
    _SENTENCE_END = re.compile(r"(?<=[.!?])(?<![A-Z][.!?])\s+")

    @classmethod
    def _first_sentence(cls, text: str) -> str:
        """První věta anotace — do promptu jde zkrácená verze."""
        return cls._SENTENCE_END.split(text.strip(), maxsplit=1)[0]

    def _catalog_prompt(self) -> str:
        """Sekce se seznamem děl pro systémový prompt — ať na „jaké knihy znáš?"
        odpovídá ze skutečného katalogu, ne z pěti náhodných úryvků.

        Štíhlá varianta: jména (česká, když existují) + skupiny vždy; anotace
        jen u malých skupin a zkrácené na první větu. Plné anotace zůstávají
        v GET /works. Katalog jde do KAŽDÉHO dotazu a čeština tokenizuje
        ~1 token/znak, takže každý znak se platí latencí prefillu."""
        by_group: dict[str, list[tuple[str, str | None]]] = {}
        for work, info in self.catalog.items():
            label = info.get("name_cs") or work
            by_group.setdefault(info["group"] or "misc", []).append(
                (label, info.get("summary"))
            )
        # řadit podle zobrazeného jména, ne podle klíče: české názvy začínají
        # nikájí („Anguttara-nikája — kniha trojic"), takže se abecedou samy
        # seskupí po sbírkách a seznam 60 pálijských knih je čitelný
        for works in by_group.values():
            works.sort()
        # anotace jen u skupin do 10 děl — u velkých (Tipitaka 60, parvy 19)
        # by i jednovětá anotace přidala tisíce znaků
        max_anotovanych = 10
        blocks = []
        for group, works in sorted(by_group.items()):
            if len(works) > max_anotovanych:
                blocks.append(f"### {group}\n" + ", ".join(w for w, _ in works))
            else:
                blocks.append(f"### {group}\n" + "\n".join(
                    f"- {w}" + (f" — {self._first_sentence(s)}" if s else "")
                    for w, s in works
                ))
        lines = "\n".join(blocks)
        return (
            "\n\nKnihovna právě obsahuje tato díla (podle tradice):\n"
            f"{lines}\n"
            "Na otázky, jaké knihy znáš nebo co je v knihovně, odpovídej z tohoto "
            "seznamu. Obsah děl mimo dodané úryvky neznáš — nepředstírej opak."
        )

    @staticmethod
    def _excerpt(text: str, limit: int = 200) -> str:
        """Ukázka z chunku pro zdroje v UI. Kráceno na poslední celé slovo —
        půlka slova na konci se špatně čte a překladač z ní dělá nesmysl.
        Začátek se nechává, jak je: tam řeže chunker, ne my."""
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        cut = text[:limit]
        space = cut.rfind(" ")
        if space > limit // 2:
            cut = cut[:space]
        return cut.rstrip(" ,;:") + "…"

    def _label(self, meta: dict) -> str:
        """Jméno díla pro člověka — české, když ho katalog zná. Používá se
        v promptu (katalog i hlavičky úryvků), aby model citoval názvy,
        kterým Čech rozumí, a ne „Milindapañhapāḷi"."""
        work = meta.get("work")
        info = self.catalog.get(work) or {}
        return info.get("name_cs") or work or meta.get("title") or "neznámý zdroj"

    def _sources(self, hits) -> list[dict]:
        return [
            {
                "work": h["meta"].get("work"),
                "name_cs": (self.catalog.get(h["meta"].get("work")) or {}).get("name_cs"),
                "title": h["meta"].get("title"),
                "group": h["meta"].get("group"),
                "lang": h["meta"].get("lang"),
                "path": h["meta"].get("path"),
                "distance": h["distance"],
                "excerpt": self._excerpt(h["text"]),
            }
            for h in hits
        ]

    def _translate_excerpt(self, source: dict) -> None:
        """Doplní do jednoho zdroje klíč „excerpt_cs". Pálijský nebo hebrejský
        úryvek je pro čtenáře nečitelný doklad; překlad z něj dělá citát.

        Každý úryvek jde vlastním requestem: dávka po pěti se v jedné odpovědi
        neúnosně dlouží, model ji uřízne na max_tokens a poslední dva úryvky
        zůstanou nepřeložené (naměřeno). Vlákna běží souběžně s generováním
        odpovědi (to trvá minuty, tohle sekundy), takže na latenci to není
        znát. Chyba se spolkne — bez překladu je zdroj pořád použitelný."""
        text = (source.get("excerpt") or "").strip()
        if not text:
            return
        try:
            resp = self.llm.chat.completions.create(
                model=self.args.excerpt_model,
                messages=[{"role": "user",
                           "content": EXCERPT_PROMPT.format(text=text)}],
                temperature=0.2,
                max_tokens=self.args.excerpt_max_tokens,
            )
            answer = THINK_RE.sub("", resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 — překlad je bonus, ne podmínka
            print(f"překlad úryvku selhal: {exc}")
            return
        answer = EXCERPT_LEAD_RE.sub("", answer).strip()
        if answer:
            source["excerpt_cs"] = answer

    def _start_excerpt_translation(self, sources: list[dict]):
        """Vrátí seznam futures (prázdný, když je překlad vypnutý)."""
        if self.args.no_translate_excerpts:
            return []
        return [self.translator.submit(self._translate_excerpt, s)
                for s in sources]

    @staticmethod
    def _await_excerpts(futures, timeout: float) -> None:
        """Počká na překlady, ale ne donekonečna — zdroje bez překladu jsou
        pořád lepší než odpověď, která nikdy nedojde. Lhůta platí na celou
        sadu, ne na každý překlad zvlášť."""
        deadline = time.monotonic() + timeout
        for future in futures:
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except Exception as exc:  # noqa: BLE001 — včetně TimeoutError
                print(f"překlad úryvku nedorazil včas: {exc}")

    def retrieve(self, query: str, top_k: int):
        """Vrátí (úryvky, kam se dotaz nasměroval).

        Vyhledá se větší výběr (top_k × candidate_factor) a teprve z něj se
        bere top_k s omezením na počet úryvků z jednoho díla — jinak top-5
        běžně tvoří pětkrát tentýž svazek. Když otázka dílo jmenuje, hledá
        se rovnou jen v něm; když jmenuje jen tradici, aspoň v ní."""
        text = format_query(query, self.args.embed_model)
        embedding = self.embedder.encode([text])[0]
        works = [] if self.args.no_routing else route(query, self.alias_index)
        # jmenované dílo je přesnější signál než tradice, takže skupiny
        # se řeší, jen když se na dílo netrefíme
        groups = [] if works or self.args.no_routing else route_groups(query)
        where = None
        if works:
            where = {"work": {"$in": works}}
        elif groups:
            where = {"group": {"$in": groups}}
        pool = max(top_k, top_k * self.args.candidate_factor)
        result = self.collection.query(
            query_embeddings=[embedding],
            n_results=pool,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        hits = []
        for doc, meta, dist in zip(
            result["documents"][0], result["metadatas"][0], result["distances"][0]
        ):
            hits.append({"text": doc, "meta": meta or {}, "distance": dist})
        hits = diversify(
            hits, top_k, self.args.max_per_work,
            key=lambda h: h["meta"].get("work"),
        )
        return hits, {"works": works, "groups": groups}

    def build_messages(self, message: str, hits, history):
        context_lines = []
        for i, h in enumerate(hits, 1):
            m = h["meta"]
            label = self._label(m)
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

        hits, routed = self.retrieve(req.message, req.top_k)
        sources = self._sources(hits)
        translation = self._start_excerpt_translation(sources)
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

        self._await_excerpts(translation, self.args.excerpt_timeout)
        return {"answer": answer, "sources": sources, "routed": routed,
                "session_id": session_id, "model": model}

    def chat_stream(self, req: ChatRequest):
        """SSE generátor: eventy {"delta": ...} po tokenech, na závěr
        {"done": true, sources, session_id, model} — pro Ol1nLLM streaming UX."""
        session_id = req.session_id or str(uuid.uuid4())
        history = self.sessions.setdefault(
            session_id, deque(maxlen=2 * self.args.history_turns)
        )

        hits, routed = self.retrieve(req.message, req.top_k)
        sources = self._sources(hits)
        translation = self._start_excerpt_translation(sources)
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

        # překlad běžel po celou dobu streamu, takže tady už bývá hotový
        self._await_excerpts(translation, self.args.excerpt_timeout)
        final = {"done": True, "sources": sources, "routed": routed,
                 "session_id": session_id, "model": model}
        yield "data: " + json.dumps(final, ensure_ascii=False) + "\n\n"


def create_app(args) -> FastAPI:
    server = RAGServer(args)
    app = FastAPI(title="Knihovna RAG chatbot")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/", response_class=HTMLResponse)
    def index():
        """Mobilní webové UI — stačí otevřít http://<server>:8090/ v prohlížeči.
        Čte se při každém requestu, takže úpravy chat.html nevyžadují restart."""
        return (Path(__file__).parent / "static" / "chat.html").read_text(
            encoding="utf-8"
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
            # systémový prompt jde do každého dotazu a čeština tokenizuje
            # ~1 token/znak — tohle číslo je přímo cena prefillu
            "system_prompt_chars": len(server.system_prompt),
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
    p.add_argument("--candidate-factor", type=int, default=4,
                   help="kolikrát víc kandidátů než top_k načíst před "
                        "prořezáním na diverzitu")
    p.add_argument("--max-per-work", type=int, default=2,
                   help="nejvíc úryvků z jednoho díla v top_k")
    p.add_argument("--no-routing", action="store_true",
                   help="nesměrovat dotaz na jmenované dílo (vypnout aliasy)")
    p.add_argument("--excerpt-model", default="translate",
                   help="model na překlad úryvků do češtiny (běží souběžně "
                        "s odpovědí; schválně jiný než --llm-model, ať "
                        "hluboký rozbor nepřekládá 200znakové útržky)")
    p.add_argument("--excerpt-max-tokens", type=int, default=500,
                   help="strop na JEDEN přeložený úryvek (200 znaků)")
    p.add_argument("--excerpt-timeout", type=float, default=60.0,
                   help="jak dlouho po dogenerování odpovědi čekat na překlad")
    p.add_argument("--no-translate-excerpts", action="store_true",
                   help="posílat úryvky jen v originále")
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

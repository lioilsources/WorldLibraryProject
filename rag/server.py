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
from retrieval import (build_alias_index, diversify, fold, is_echo, looks_tabular,
                       route, route_groups)
from retriever import Plan, Retriever, context_block
import catalog as cat
from planner import Planner, QueryPlan, find_chapter

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

        # Režim s Postgresem (katalog, kapitoly, fulltext, obohacení) vs.
        # dnešní režim jen s Chromou — --pg-dsn prázdné = rollback na staré chování.
        self.pool = None
        self.retriever = None
        self.gloss = None
        if args.pg_dsn:
            from psycopg_pool import ConnectionPool
            self.pool = ConnectionPool(args.pg_dsn, min_size=1, max_size=8, open=True)
            self.catalog = self._build_catalog_pg()
            try:
                self.gloss = client.get_collection(args.gloss_collection)
            except Exception:
                self.gloss = None   # glosy ještě neexistují — kanál se přeskočí
        else:
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

        self.system_prompt += self._catalog_prompt_slim() if self.pool else self._catalog_prompt()

        # rejstřík aliasů pro směrování dotazu na dílo (viz retrieval.py);
        # v PG režimu i aliasy z registru (autoři, česká jména Perseu)
        self.alias_index = self._build_alias_index_pg() if self.pool else build_alias_index(
            {w: info.get("name_cs") for w, info in self.catalog.items()}
        )
        # tradice, které korpus opravdu má — aliasy na skupinu smějí mířit jen
        # do nich. Jinak by dotaz na Bibli (dílo vyhozené kvůli mojibake)
        # zúžil hledání na prázdnou množinu a model by dostal nulový kontext.
        self.known_groups = {
            info["group"] for info in self.catalog.values() if info.get("group")
        }

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
        if self.pool:
            self.retriever = Retriever(
                orig=self.collection, gloss=self.gloss, pool=self.pool, embedder=self.embedder,
                embed_model=args.embed_model, alias_index=self.alias_index,
                channels=[c.strip() for c in args.channels.split(",") if c.strip()],
                candidate_factor=args.candidate_factor, max_per_work=args.max_per_work,
                rrf_k=args.rrf_k, no_routing=args.no_routing, context_window=args.context_window,
                legacy_to_id=self.legacy_to_id,
            )
        # plánovač dotazu (intent + přepis) — jen v PG režimu a když není vypnutý
        self.planner = None
        if self.pool and args.planner != "off":
            with self.pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT id, name_cs FROM topics ORDER BY sort")
                topics = cur.fetchall()
            counts: dict[str, int] = {}
            for info in self.catalog.values():
                counts[info["group"]] = counts.get(info["group"], 0) + 1
            self.planner = Planner(self.llm, args.planner_model, pool=self.pool, known_groups=self.known_groups,
                                   topics=topics, group_counts=counts, timeout=args.planner_timeout)
        # „čti dál": session_id -> (work_id, další seq)
        self.reading: dict[str, tuple[str, int]] = {}
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

    def _build_catalog_pg(self) -> dict:
        """work_id -> metadata díla z Postgresu (works + témata)."""
        cols = ["id", "group", "subgroup", "title", "work_legacy", "name_cs", "author", "author_cs",
                "lang_original", "lang_corpus", "edition", "form", "period", "priority", "aliases",
                "chunk_count", "chapter_count", "summary_short", "summary_medium", "summary_long", "topic_ids"]
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM catalog_v')
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        catalog = {}
        for r in rows:
            r["path"] = None
            r["lang"] = r["lang_corpus"]
            r["summary"] = r.get("summary_medium")
            catalog[r["id"]] = r
        return catalog

    def _build_alias_index_pg(self):
        """Aliasy: kurátorská tabulka (retrieval.ALIASES, klíčovaná dnešními
        jmény → přes work_legacy), názvy z katalogu a aliasy z registru
        (včetně autorů Perseu). Výsledek mapuje na work_id."""
        keys = {}
        self.legacy_to_id = {}
        for wid, info in self.catalog.items():
            legacy = info.get("work_legacy") or info["title"]
            keys[legacy] = info.get("name_cs")
            self.legacy_to_id[legacy] = wid
        index = build_alias_index(keys)   # [(alias, (legacy,…))] seřazené podle délky
        extra: dict[str, set] = {}
        for wid, info in self.catalog.items():
            legacy = info.get("work_legacy") or info["title"]
            for a in info.get("aliases") or []:
                if len(a) >= 3:
                    extra.setdefault(fold(a), set()).add(legacy)
            if info.get("author_cs") and len(info["author_cs"]) >= 4:
                extra.setdefault(fold(info["author_cs"]), set()).add(legacy)
        merged: dict[str, set] = {a: set(w) for a, w in index}
        for a, ws in extra.items():
            merged.setdefault(a, set()).update(ws)
        return sorted(((a, tuple(sorted(w))) for a, w in merged.items()), key=lambda kv: -len(kv[0]))

    def _catalog_prompt_slim(self) -> str:
        """Ultra-štíhlý index do systémového promptu: tradice, počty a pár
        příkladů. Celý katalog (1 173 děl) se do promptu nelepí — na
        katalogové otázky odpovídá plánovač cíleným kontextem z DB."""
        by_group: dict[str, list[dict]] = {}
        for info in self.catalog.values():
            by_group.setdefault(info["group"] or "misc", []).append(info)
        blocks = []
        for group, works in sorted(by_group.items()):
            authors = {w.get("author_cs") or w.get("author") for w in works if w.get("author_cs") or w.get("author")}
            top = sorted((w for w in works if w.get("priority") == 1 and w.get("name_cs")), key=lambda w: w["name_cs"])
            examples = ", ".join(w["name_cs"] for w in top[:8])
            line = f"- {group}: {len(works)} děl"
            if len(authors) > 3:
                line += f", {len(authors)} autorů"
            if examples:
                line += f" (např. {examples})"
            blocks.append(line)
        return (
            "\n\nKnihovna má tyto tradice:\n" + "\n".join(blocks) + "\n"
            "Když se uživatel ptá na seznam knih, autora nebo kapitoly, dostaneš cílený "
            "výpis z katalogu v kontextu — odpovídej jen z něj a nepřidávej díla, která tam nejsou."
        )

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
        if meta.get("name_cs"):
            return meta["name_cs"]
        work = meta.get("work")
        info = self.catalog.get(meta.get("work_id") or work) or {}
        return info.get("name_cs") or work or meta.get("title") or "neznámý zdroj"

    def _sources(self, hits) -> list[dict]:
        return [
            {
                "work": h["meta"].get("work"),
                "name_cs": h["meta"].get("name_cs")
                           or (self.catalog.get(h["meta"].get("work_id") or h["meta"].get("work")) or {}).get("name_cs"),
                "title": h["meta"].get("title"),
                "group": h["meta"].get("group"),
                "lang": h["meta"].get("lang"),
                "path": h["meta"].get("path"),
                "distance": h["distance"],
                "excerpt": self._excerpt(h["text"]),
                # 2. vlna: struktura a původ (appka je zatím ignoruje)
                "work_id": h["meta"].get("work_id"),
                "chapter_id": h["meta"].get("chapter_id"),
                "chapter_path": h["meta"].get("chapter_path"),
                "ref_start": h["meta"].get("ref_start"),
                "ref_end": h["meta"].get("ref_end"),
                "lang_original": h["meta"].get("lang_original"),
                "lang_corpus": h["meta"].get("lang_corpus"),
                "score": h["meta"].get("score"),
                "channels": h["meta"].get("channels"),
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
            got = (resp.model or "").strip()
        except Exception as exc:  # noqa: BLE001 — překlad je bonus, ne podmínka
            print(f"překlad úryvku selhal: {exc}")
            return
        if got and got != self.args.excerpt_model and not got.startswith(self.args.excerpt_model):
            # LiteLLM přepadl na fallback (Qwen3-4B): buď úryvek opíše, nebo
            # vyrobí paskvil — ani jedno nevydávat za překlad
            print(f"překlad úryvku: odpověděl {got!r}, ne {self.args.excerpt_model!r} — přeskakuji")
            return
        answer = EXCERPT_LEAD_RE.sub("", answer).strip()
        if not answer or is_echo(text, answer):
            # model úryvek jen opsal (typicky pálijské verše) — tvářit se,
            # že je to překlad, znamená ukázat v appce dvakrát totéž
            return
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
        if self.retriever is not None:
            # PG režim: hybrid (vektor + glosy + fulltext → RRF); route() vrací
            # dnešní jména děl, retriever chce work_id
            works = [] if self.args.no_routing else [
                self.legacy_to_id[w] for w in route(query, self.alias_index) if w in self.legacy_to_id]
            return self.retriever.retrieve(query, top_k, Plan(), works_override=works)
        text = format_query(query, self.args.embed_model)
        embedding = self.embedder.encode([text])[0]
        works = [] if self.args.no_routing else route(query, self.alias_index)
        # jmenované dílo je přesnější signál než tradice, takže skupiny
        # se řeší, jen když se na dílo netrefíme
        groups = ([] if works or self.args.no_routing
                  else [g for g in route_groups(query) if g in self.known_groups])
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
        # rejstříky a obsahy ven — jako citace neříkají nic. Když by po
        # filtru nezbylo nic (dotaz mířil na dílo, z něhož se trefila jen
        # tabulka), radši vrátit i tabulku než prázdný kontext.
        readable = [h for h in hits if not looks_tabular(h["text"])]
        hits = readable or hits
        hits = diversify(
            hits, top_k, self.args.max_per_work,
            key=lambda h: h["meta"].get("work"),
        )
        return hits, {"works": works, "groups": groups}

    def build_messages(self, message: str, hits, history, catalog_context: str | None = None,
                       instruction: str | None = None):
        if self.retriever is not None:
            context = context_block(hits)   # dílo › kapitola (ref) [překlad]
        else:
            context_lines = []
            for i, h in enumerate(hits, 1):
                m = h["meta"]
                label = self._label(m)
                context_lines.append(f"[{i}] {label} ({m.get('group', '?')}):\n{h['text']}")
            context = "\n\n".join(context_lines) if context_lines else "(nic nenalezeno)"

        messages = [{"role": "system", "content": self.system_prompt}]
        for role, content in history:
            messages.append({"role": role, "content": content})
        parts = []
        if catalog_context:
            parts.append(f"Výpis z katalogu knihovny:\n\n{catalog_context}")
        if hits or not catalog_context:
            parts.append(f"Úryvky z knihovny relevantní k otázce:\n\n{context}")
        if instruction:
            parts.append(instruction)
        parts.append(f"Otázka: {message}")
        messages.append({"role": "user", "content": "\n\n".join(parts)})
        return messages

    # --- plán → kontext ------------------------------------------------------------

    def _prepare(self, req: ChatRequest, session_id: str, history) -> dict:
        """Rozhodne podle intentu, co jde do promptu. Vrací hits, routed,
        catalog_context, instruction, payload (pro SSE) a plán."""
        out = {"hits": [], "routed": {"works": [], "groups": []}, "catalog_context": None,
               "instruction": None, "payload": {}, "plan": None}
        if self.retriever is None:
            out["hits"], out["routed"] = self.retrieve(req.message, req.top_k)
            return out
        plan = QueryPlan()
        if self.planner is not None:
            tail = [c for r, c in list(history)[-6:] if r == "user"]
            plan = self.planner.plan(req.message, tail, accept_models={self.args.planner_model, self.args.llm_model})
        out["plan"] = plan
        work_ids = cat.resolve_work(self.catalog, self.legacy_to_id, self.alias_index, plan.work_hint, None)
        if not work_ids and not plan.work_hint:
            # bez jmenovaného díla zkusit aliasy přímo na otázku (jako dřív)
            work_ids = [self.legacy_to_id[w] for w in route(req.message, self.alias_index) if w in self.legacy_to_id]
        intent = plan.intent
        detail = plan.detail
        with self.pool.connection() as conn:
            tnames = cat.topic_names(conn)
            if intent == "smalltalk":
                out["instruction"] = "Uživatel nechce nic hledat — odpověz krátce a přátelsky, bez citací."
                out["routed"] = {"works": [], "groups": [], "intent": intent}
                return out

            if intent == "catalog" or (intent in ("work_overview", "chapters", "chapter_detail") and not work_ids and plan.author):
                group_by = plan.group_by if plan.group_by != "none" else "tradition"
                ctx, payload = cat.build_catalog(
                    conn, plan, group_by=group_by, detail=detail, groups=plan.groups or None,
                    topics=plan.topics or None, author=plan.author, work_ids=work_ids or None,
                    hide_priority=self.args.hide_priority)
                note = None
                if payload["total"] == 0 and (plan.topics or plan.terms_cs) and not work_ids:
                    # témata zatím nejsou přiřazena (obohacení neběželo) nebo filtr
                    # nesedí → díla, kde se téma opravdu vyskytuje v textu
                    rplan = Plan(groups=plan.groups, terms_orig=plan.terms_orig, terms_cs=plan.terms_cs, hyde_cs=plan.hyde_cs)
                    hits, _ = self.retriever.retrieve(req.message, 40, rplan)
                    found, seen = [], set()
                    for h in hits:
                        wid = h["meta"].get("work_id")
                        if wid and wid not in seen:
                            seen.add(wid); found.append(wid)
                    if found:
                        note = ("Výběr vznikl vyhledáním tématu v textech (témata děl zatím nejsou v katalogu "
                                "přiřazena) — jde o díla, kde se o tom skutečně píše.")
                        ctx, payload = cat.build_catalog(conn, plan, group_by=group_by, detail=detail,
                                                         work_ids=found[:15], hide_priority=0, note=note)
                        out["hits"] = hits[: req.top_k]
                out["catalog_context"], out["payload"] = ctx, {"catalog": payload}
                out["instruction"] = ("Sestav odpověď z výpisu: seskup podle zadaného klíče, u každého díla uveď autora, "
                                      "jazyk originálu a (když je) anotaci; tabulku zachovej jako markdown. "
                                      "NIKDY nepřidávej díla mimo výpis — když je výpis prázdný, řekni, že knihovna "
                                      "pro tenhle filtr nic nemá, a nabídni jiný (tradici, autora).")
                out["routed"] = {"works": work_ids, "groups": plan.groups, "intent": intent}
                return out

            if intent in ("work_overview", "chapters", "chapter_detail", "read") and work_ids:
                w = self.catalog[work_ids[0]]
                if intent == "work_overview":
                    ctx, payload = cat.build_work_overview(conn, w, tnames)
                    out["catalog_context"], out["payload"] = ctx, {"work": payload}
                    hits, routed = self.retriever.retrieve(req.message, min(req.top_k, 3), Plan(works=[w["id"]]))
                    out["hits"], out["routed"] = hits, {**routed, "intent": intent}
                    return out
                if intent == "chapters":
                    ctx, payload = cat.build_chapters(conn, w, tnames, detail=detail,
                                                      group_by=plan.group_by, topic=(plan.topics or [None])[0])
                    out["catalog_context"], out["payload"] = ctx, {"chapters": payload}
                    out["instruction"] = "Vypiš kapitoly z výpisu (zachovej pořadí a úrovně), nic nedomýšlej."
                    out["routed"] = {"works": [w["id"]], "groups": [], "intent": intent}
                    return out
                if intent == "chapter_detail":
                    chapters = cat.query_chapters(conn, w["id"])
                    ch = find_chapter(chapters, plan.chapter_hint or req.message)
                    if ch:
                        ctx = (f"Dílo: {w.get('name_cs') or w['title']}\nKapitola: {ch['path']}"
                               + (f" — {ch['heading_cs']}" if ch.get("heading_cs") else "")
                               + "\nAnotace kapitoly: " + (ch.get("summary_long") or ch.get("summary_medium") or ch.get("summary_short") or "(zatím bez anotace)"))
                        out["catalog_context"] = ctx
                        out["payload"] = {"chapter": {"work_id": w["id"], "id": ch["id"], "path": ch["path"], "heading_cs": ch.get("heading_cs"),
                                                      "summary": ch.get("summary_long") or ch.get("summary_medium")}}
                        hits, routed = self.retriever.retrieve(req.message, req.top_k * 2, Plan(works=[w["id"]]))
                        in_ch = [h for h in hits if h["meta"].get("chapter_id") == ch["id"]]
                        if len(in_ch) < 2:
                            with self.pool.connection() as c2, c2.cursor() as cur:
                                cur.execute("SELECT id FROM chunks WHERE chapter_id = %s ORDER BY seq_in_chapter LIMIT 3", (ch["id"],))
                                ids = [r[0] for r in cur.fetchall()]
                            from pg_search import hydrate
                            rows = hydrate(c2, ids) if ids else {}
                            for cid in ids:
                                r = rows.get(cid)
                                if r:
                                    in_ch.append({"text": r["text"], "distance": 0.0, "meta": {
                                        "chunk_id": cid, "work": r["title"], "work_id": r["work_id"], "name_cs": r["name_cs"],
                                        "title": f"{r['title']} (část {r['seq'] + 1}/?)", "group": r["group"], "lang": r["lang"],
                                        "lang_original": r["lang_original"], "lang_corpus": r["lang_corpus"], "edition": r["edition"],
                                        "path": r["source_path"], "chapter_id": r["chapter_id"], "chapter_path": r["chapter_path"],
                                        "ref_start": r["ref_start"], "ref_end": r["ref_end"], "seq": r["seq"], "score": None, "channels": ["chapter"]}})
                        out["hits"] = in_ch[: req.top_k]
                        out["routed"] = {**routed, "intent": intent, "chapter": ch["id"]}
                        return out
                    # kapitola nenalezena → přehled díla
                    ctx, payload = cat.build_work_overview(conn, w, tnames)
                    out["catalog_context"], out["payload"] = ctx, {"work": payload}
                    out["instruction"] = "Kapitolu, na kterou se uživatel ptá, katalog nenašel — nabídni seznam kapitol."
                    out["routed"] = {"works": [w["id"]], "groups": [], "intent": intent}
                    return out

            if intent == "read":
                wid, nxt = self.reading.get(session_id, (work_ids[0] if work_ids else None, 0))
                if wid:
                    with self.pool.connection() as c2, c2.cursor() as cur:
                        cur.execute("SELECT id FROM chunks WHERE work_id = %s AND seq >= %s ORDER BY seq LIMIT 3", (wid, nxt))
                        ids = [r[0] for r in cur.fetchall()]
                    from pg_search import hydrate
                    rows = hydrate(c2, ids) if ids else {}
                    hits = []
                    for cid in ids:
                        r = rows.get(cid)
                        if r:
                            hits.append({"text": r["text"], "distance": 0.0, "meta": {
                                "chunk_id": cid, "work": r["title"], "work_id": r["work_id"], "name_cs": r["name_cs"],
                                "title": f"{r['title']} (část {r['seq'] + 1}/?)", "group": r["group"], "lang": r["lang"],
                                "lang_original": r["lang_original"], "lang_corpus": r["lang_corpus"], "edition": r["edition"],
                                "path": r["source_path"], "chapter_id": r["chapter_id"], "chapter_path": r["chapter_path"],
                                "ref_start": r["ref_start"], "ref_end": r["ref_end"], "seq": r["seq"], "score": None, "channels": ["read"]}})
                    if hits:
                        self.reading[session_id] = (wid, hits[-1]["meta"]["seq"] + 1)
                    out["hits"] = hits
                    out["instruction"] = "Uživatel čte dílo postupně: přelož tyto úryvky do češtiny v pořadí a stručně je okomentuj; na konci řekni, kde jsme (kapitola)."
                    out["routed"] = {"works": [wid], "groups": [], "intent": intent, "read_from": nxt}
                    return out

            # content / mixed / fallback
            rplan = Plan(works=work_ids, groups=plan.groups, terms_orig=plan.terms_orig, terms_cs=plan.terms_cs,
                         hyde_cs=plan.hyde_cs if self.args.rewrite != "off" else "",
                         hyde_orig=plan.hyde_orig if self.args.rewrite == "terms+hyde" else {})
            if self.args.rewrite == "off":
                rplan.terms_orig, rplan.terms_cs = {}, []
            hits, routed = self.retriever.retrieve(req.message, req.top_k, rplan)
            out["hits"], out["routed"] = hits, {**routed, "intent": intent}
            if hits and hits[0]["meta"].get("work_id"):
                self.reading[session_id] = (hits[0]["meta"]["work_id"], hits[0]["meta"]["seq"] + 1)
            if intent == "mixed":
                ctx, payload = cat.build_catalog(conn, plan, group_by="tradition", detail="short",
                                                 groups=plan.groups or None, topics=plan.topics or None,
                                                 author=plan.author, hide_priority=self.args.hide_priority)
                out["catalog_context"], out["payload"] = ctx, {"catalog": payload}
        return out

    def chat(self, req: ChatRequest):
        session_id = req.session_id or str(uuid.uuid4())
        history = self.sessions.setdefault(
            session_id, deque(maxlen=2 * self.args.history_turns)
        )

        prep = self._prepare(req, session_id, history)
        hits, routed = prep["hits"], prep["routed"]
        sources = self._sources(hits)
        translation = self._start_excerpt_translation(sources)
        messages = self.build_messages(req.message, hits, history, prep["catalog_context"], prep["instruction"])

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
        # hlásit, kdo doopravdy odpověděl — LiteLLM při pádu translate tiše
        # přepadá na fallback a appka by jinak ukazovala „translate"
        model = (completion.model or model).strip() or model

        # do historie jde otázka bez kontextu, ať se paměť nenafukuje
        history.append(("user", req.message))
        history.append(("assistant", answer))

        self._await_excerpts(translation, self.args.excerpt_timeout)
        return {"answer": answer, "sources": sources, "routed": routed,
                "session_id": session_id, "model": model,
                "intent": routed.get("intent"), "plan": prep["plan"].brief() if prep["plan"] else None, **prep["payload"]}

    def chat_stream(self, req: ChatRequest):
        """SSE generátor: eventy {"delta": ...} po tokenech, na závěr
        {"done": true, sources, session_id, model} — pro Ol1nLLM streaming UX."""
        session_id = req.session_id or str(uuid.uuid4())
        history = self.sessions.setdefault(
            session_id, deque(maxlen=2 * self.args.history_turns)
        )

        # SSE komentář hned: proxy dostane hlavičky dřív, než doběhne plánovač
        yield ": planning\n\n"
        prep = self._prepare(req, session_id, history)
        hits, routed = prep["hits"], prep["routed"]
        sources = self._sources(hits)
        translation = self._start_excerpt_translation(sources)
        messages = self.build_messages(req.message, hits, history, prep["catalog_context"], prep["instruction"])

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
            if getattr(chunk, "model", None):
                model = chunk.model.strip() or model   # skutečný respondent (fallback?)
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
                 "session_id": session_id, "model": model,
                 "intent": routed.get("intent"), "plan": prep["plan"].brief() if prep["plan"] else None, **prep["payload"]}
        yield "data: " + json.dumps(final, ensure_ascii=False) + "\n\n"


def _pg_status(server) -> dict | None:
    if server.pool is None:
        return None
    from pg_search import status
    try:
        with server.pool.connection() as conn:
            st = status(conn)
        st["enriched_pct"] = round(100 * st["enriched"] / max(1, st["chunks"]), 1)
        return st
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


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
    def works(group: str | None = None, subgroup: str | None = None, topic: str | None = None,
              author: str | None = None, lang: str | None = None, priority: int | None = None,
              q: str | None = None, detail: str = "medium", limit: int = 200, offset: int = 0):
        """Katalog. V PG režimu s filtry; jinak dnešní výpis z metadat Chromy."""
        if server.pool is None:
            items = [
                {"work": work, **info}
                for work, info in sorted(
                    server.catalog.items(), key=lambda kv: (kv[1]["group"] or "", kv[0])
                )
            ]
            return {"works": items, "count": len(items)}
        summary_col = {"short": "summary_short", "medium": "summary_medium", "long": "summary_long"}.get(detail, "summary_medium")
        where, params = [], []
        if group:
            where.append('"group" = %s'); params.append(group)
        if subgroup:
            where.append("subgroup = %s"); params.append(subgroup)
        if topic:
            where.append("%s = ANY(topic_ids)"); params.append(topic)
        if author:
            where.append("(author ILIKE %s OR author_cs ILIKE %s)"); params += [f"%{author}%", f"%{author}%"]
        if lang:
            where.append("(lang_original = %s OR lang_corpus = %s)"); params += [lang, lang]
        if priority:
            where.append("priority <= %s"); params.append(priority)
        if q:
            where.append("(title ILIKE %s OR name_cs ILIKE %s OR author ILIKE %s)"); params += [f"%{q}%"] * 3
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        cols = ["id", "group", "subgroup", "title", "name_cs", "author", "author_cs", "lang_original", "lang_corpus",
                "is_translation", "edition", "form", "period", "priority", "chunk_count", "chapter_count", "topic_ids"]
        with server.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM catalog_v{sql_where}", params)
            total = cur.fetchone()[0]
            cur.execute(
                f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)}, {summary_col} AS summary FROM catalog_v{sql_where} '
                f'ORDER BY "group", priority, coalesce(author_cs, author), coalesce(name_cs, title) LIMIT %s OFFSET %s',
                params + [limit, offset],
            )
            items = [dict(zip(cols + ["summary"], r)) for r in cur.fetchall()]
        return {"works": items, "count": len(items), "total": total, "detail": detail}

    @app.get("/works/{work_id}/chapters")
    def chapters(work_id: str, detail: str = "short", topic: str | None = None, offset: int = 0, n: int = 80):
        if server.pool is None:
            raise HTTPException(status_code=404, detail="kapitoly jsou jen v PG režimu")
        summary_col = {"short": "summary_short", "medium": "summary_medium", "long": "summary_long"}.get(detail, "summary_short")
        wid = server.legacy_to_id.get(work_id, work_id)
        with server.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name_cs, title, chapter_count FROM works WHERE id = %s", (wid,))
            w = cur.fetchone()
            if not w:
                raise HTTPException(status_code=404, detail="neznámé dílo")
            extra, params = "", [wid]
            if topic:
                extra, params = " AND %s = ANY(topic_ids)", [wid, topic]
            cur.execute(
                f"SELECT id, ordinal, level, parent_id, ref, heading, heading_cs, path, chunk_count, {summary_col}, topic_ids "
                f"FROM chapters_v WHERE work_id = %s{extra} ORDER BY ordinal LIMIT %s OFFSET %s",
                params + [n, offset],
            )
            cols = ["id", "ordinal", "level", "parent_id", "ref", "heading", "heading_cs", "path", "chunk_count", "summary", "topic_ids"]
            items = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {"work_id": w[0], "name_cs": w[1], "title": w[2], "total": w[3], "items": items, "offset": offset}

    @app.get("/works/{work_id}/chunks")
    def chunks(work_id: str, offset: int = 0, n: int = 3):
        """Sekvenční čtení: n chunků od pozice (čtení dál)."""
        if server.pool is None:
            raise HTTPException(status_code=404, detail="jen v PG režimu")
        wid = server.legacy_to_id.get(work_id, work_id)
        n = max(1, min(n, 10))
        with server.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, c.seq, c.text, ch.path FROM chunks c LEFT JOIN chapters ch ON ch.id = c.chapter_id "
                "WHERE c.work_id = %s AND c.seq >= %s ORDER BY c.seq LIMIT %s", (wid, offset, n))
            items = [{"id": r[0], "seq": r[1], "text": r[2], "chapter_path": r[3]} for r in cur.fetchall()]
        return {"work_id": wid, "items": items, "next": (items[-1]["seq"] + 1) if items else None}

    @app.post("/plan")
    def plan(req: ChatRequest):
        """Jen plánovač — ladění intentu a přepisu."""
        if server.planner is None:
            raise HTTPException(status_code=404, detail="plánovač neběží (jen PG režim)")
        p = server.planner.plan(req.message, [], accept_models={args.planner_model, args.llm_model})
        return p.to_dict()

    @app.get("/search")
    def search(q: str, top_k: int = 5, work: str | None = None, group: str | None = None):
        """Retrieval bez LLM — ladění, lab, eval."""
        if not q.strip():
            raise HTTPException(status_code=400, detail="prázdný dotaz")
        if server.retriever is not None and (work or group):
            plan = Plan(works=[server.legacy_to_id.get(work, work)] if work else [], groups=[group] if group else [])
            hits, routed = server.retriever.retrieve(q, top_k, plan)
        else:
            hits, routed = server.retrieve(q, top_k)
        return {"hits": server._sources(hits), "routed": routed}

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
            "mode": "pg" if server.pool else "chroma",
            "pg": _pg_status(server),
            "gloss_collection": (args.gloss_collection if server.gloss is not None else None),
            "llm_model": args.llm_model,
            "llm_url": args.llm_url,
            "embed_model": args.embed_model,
            "active_sessions": len(server.sessions),
        }

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def _load_dotenv(path: Path) -> None:
    """rag/.env: PG_DSN a spol. — mimo git."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main():
    _load_dotenv(Path(__file__).parent / ".env")
    p = argparse.ArgumentParser(description="RAG chatbot server")
    p.add_argument("--chroma-url", default=os.getenv("CHROMA_URL", "http://192.168.88.88:8006"),
                   help="Chroma na JODA (AiStack swarm.nas)")
    p.add_argument("--collection", default=os.getenv("COLLECTION", "books"),
                   help="Chroma kolekce pasáží; PG režim chce books_v2 (rag/.env: COLLECTION)")
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
    p.add_argument("--pg-dsn", default=os.getenv("PG_DSN", ""),
                   help="knihovní Postgres na JODA (rag/.env: PG_DSN); prázdné = jen Chroma jako dřív")
    p.add_argument("--gloss-collection", default="books_gloss",
                   help="Chroma kolekce českých glos (embed_books.py --source pg)")
    p.add_argument("--channels", default="vec,gloss,fts,fts_cs",
                   help="kanály hybridního retrievalu (PG režim)")
    p.add_argument("--rrf-k", type=int, default=60)
    p.add_argument("--planner", default=os.getenv("PLANNER", "on"), choices=["on", "off"],
                   help="LLM plánovač dotazu (intent + přepis) před retrievalem; rag/.env: PLANNER=off")
    p.add_argument("--planner-model", default=os.getenv("PLANNER_MODEL", "translate"))
    p.add_argument("--planner-timeout", type=float, default=25.0)
    p.add_argument("--rewrite", default="terms+hyde", choices=["off", "terms", "terms+hyde"],
                   help="co z plánu použít pro retrieval")
    p.add_argument("--hide-priority", type=int, default=3,
                   help="díla s prioritou ≥ N se v katalogu jen sečtou (fragmenty, scholia)")
    p.add_argument("--context-window", type=int, default=0,
                   help="±N sousedních chunků téže kapitoly do kontextu (0 = vypnuto)")
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

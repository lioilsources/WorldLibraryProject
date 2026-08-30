"""Plánovač dotazu: jeden LLM roundtrip před retrievalem, který určí, na co
se uživatel ptá (intent), kam mířit (tradice, dílo, kapitola, autor),
jak dlouhé odpovědi chce, a přepíše dotaz do jazyků korpusu (termíny pro
fulltext, HyDE pasáž pro vektor).

Selhání plánovače (timeout, nevalidní JSON, cizí model) = prázdný plán
= dnešní chování (content retrieval). Výstup se cachuje v PG
(query_cache) i v RAM, klíč = sha1(fold(otázka) + PROMPT_VERSION).

Mapování jmen na ID (work_hint → work_id, autor → work_ids) je
deterministické a dělá ho server přes aliasy — plánovač jen vytáhne, co
uživatel řekl.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field

from llm_batch import THINK_RE, parse_json
from retrieval import fold

PROMPT_VERSION = "plan-v2"   # v2: + lang (jazyk originálu); změna promptu = nový klíč cache

INTENTS = ("catalog", "work_overview", "chapters", "chapter_detail", "read", "content", "mixed", "smalltalk")

SYSTEM = (
    "Jsi směrovač dotazů nad knihovnou starých textů v originálních jazycích (pálí, sanskrt, klasická "
    "čínština, stará řečtina, latina, němčina; některá díla jsou v knihovně jen v anglickém překladu). "
    "Nevymýšlej odpověď — jen rozhodni, co uživatel chce, a připrav hledání. Vrať POUZE JSON."
)

USER = """Tradice v knihovně (kód: počet děl): {groups}
Témata (slug): {topics}
Jazyky korpusu: pi=pálí, sa=sanskrt, lzh=klasická čínština, grc=stará řečtina, lat=latina, de=němčina, en=angličtina (překlady)

Vrať JSON:
{{
 "intent": "catalog | work_overview | chapters | chapter_detail | read | content | mixed | smalltalk",
 "groups": ["kódy tradic, na které se ptá; [] = všechny"],
 "topics": ["0–3 slugy témat z výčtu, když se ptá tematicky"],
 "lang": "kód jazyka originálu, když se ptá na jazyk (lat, grc, pi, sa, lzh, de), jinak null",
 "author": "jméno autora, když ho jmenuje, jinak null",
 "work_hint": "název díla, když ho jmenuje (jak ho napsal), jinak null",
 "chapter_hint": "označení kapitoly/knihy/oddílu, když ho jmenuje (např. 'kniha 7', 'kapitola 8'), jinak null",
 "detail": "short | medium | long | auto",
 "group_by": "topic | tradition | author | chapter | none",
 "terms_cs": ["3–6 klíčových slov česky (základní tvar)"],
 "terms_en": ["3–6 anglicky"],
 "terms_orig": {{"kód jazyka": ["2–5 termínů v tom jazyce a písmu, jak by stály v textu"]}},
 "hyde_cs": "2–3 věty česky: jak by zněla pasáž, která na otázku odpovídá",
 "hyde_orig": {{"kód jazyka": "1–2 věty v jazyce tradice, jak by pasáž mohla znít"}}
}}

Pravidla:
- intent catalog = ptá se, jaké knihy/díla/autory knihovna má (seznam, filtr, podle tématu).
- work_overview = ptá se, o čem je konkrétní dílo. chapters = chce seznam/členění kapitol díla.
- chapter_detail = ptá se na obsah konkrétní kapitoly. read = chce číst text dál / pokračovat.
- content = ptá se na obsah, myšlenky, citáty, srovnání — cokoli, co se hledá v textu.
- mixed = katalog i obsah. smalltalk = pozdrav, poděkování, nic o knihách.
- terms_orig a hyde_orig jen pro tradice, kterých se otázka týká (nebo pro nejpravděpodobnější 1–2).
- detail auto = neřekl; short když chce stručně/přehled/seznam, long když chce podrobně.

Poslední otázky v rozhovoru: {history}
Otázka: {question}"""


@dataclass
class QueryPlan:
    intent: str = "content"
    groups: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    author: str | None = None
    lang: str | None = None
    work_hint: str | None = None
    chapter_hint: str | None = None
    detail: str = "auto"
    group_by: str = "none"
    terms_cs: list[str] = field(default_factory=list)
    terms_en: list[str] = field(default_factory=list)
    terms_orig: dict[str, list[str]] = field(default_factory=dict)
    hyde_cs: str = ""
    hyde_orig: dict[str, str] = field(default_factory=dict)
    model: str = ""
    cached: bool = False
    ms: int = 0

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    def brief(self) -> dict:
        """Zkrácená verze do SSE eventu."""
        return {"intent": self.intent, "groups": self.groups, "topics": self.topics, "author": self.author, "lang": self.lang,
                "work_hint": self.work_hint, "chapter_hint": self.chapter_hint, "detail": self.detail,
                "group_by": self.group_by, "terms_orig": self.terms_orig, "model": self.model,
                "cached": self.cached, "ms": self.ms}


def _lst(v, n=6) -> list[str]:
    if isinstance(v, str):
        v = [v]
    return [str(x).strip() for x in (v or []) if str(x).strip()][:n]


def from_json(parsed: dict | None, known_groups: set[str], known_topics: set[str]) -> QueryPlan | None:
    if not parsed or not isinstance(parsed, dict):
        return None
    intent = str(parsed.get("intent", "content")).strip().lower()
    if intent not in INTENTS:
        intent = "content"
    plan = QueryPlan(intent=intent)
    plan.groups = [g for g in _lst(parsed.get("groups"), 8) if g in known_groups]
    plan.topics = [t for t in _lst(parsed.get("topics"), 3) if t in known_topics]
    plan.author = (parsed.get("author") or None) if isinstance(parsed.get("author"), str) else None
    lang = parsed.get("lang")
    plan.lang = lang.strip().lower() if isinstance(lang, str) and 2 <= len(lang.strip()) <= 3 else None
    plan.work_hint = (parsed.get("work_hint") or None) if isinstance(parsed.get("work_hint"), str) else None
    plan.chapter_hint = (parsed.get("chapter_hint") or None) if isinstance(parsed.get("chapter_hint"), str) else None
    plan.detail = parsed.get("detail") if parsed.get("detail") in ("short", "medium", "long", "auto") else "auto"
    plan.group_by = parsed.get("group_by") if parsed.get("group_by") in ("topic", "tradition", "author", "chapter", "none") else "none"
    plan.terms_cs = _lst(parsed.get("terms_cs"))
    plan.terms_en = _lst(parsed.get("terms_en"))
    to = parsed.get("terms_orig") or {}
    if isinstance(to, dict):
        plan.terms_orig = {str(k): _lst(v, 5) for k, v in to.items() if _lst(v, 5)}
    plan.hyde_cs = str(parsed.get("hyde_cs") or "")[:600]
    ho = parsed.get("hyde_orig") or {}
    if isinstance(ho, dict):
        plan.hyde_orig = {str(k): str(v)[:500] for k, v in ho.items() if isinstance(v, str) and v.strip()}
    return plan


def cache_key(question: str) -> str:
    return hashlib.sha1((PROMPT_VERSION + "\x00" + fold(question)).encode("utf-8")).hexdigest()


class Planner:
    def __init__(self, llm, model: str, *, pool=None, known_groups: set[str], topics: list[tuple[str, str]],
                 group_counts: dict[str, int], timeout: float = 25.0, max_tokens: int = 700):
        self.llm = llm            # OpenAI klient (gateway) — jako chat
        self.model = model
        self.pool = pool
        self.known_groups = set(known_groups)
        self.topics = topics      # [(slug, name_cs)]
        self.known_topics = {t for t, _ in topics}
        self.group_counts = group_counts
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._ram: dict[str, QueryPlan] = {}
        self._lock = threading.Lock()

    def _messages(self, question: str, history: list[str]) -> list[dict]:
        groups = ", ".join(f"{g}: {n}" for g, n in sorted(self.group_counts.items()))
        topics = ", ".join(f"{s} ({n})" for s, n in self.topics)
        hist = " | ".join(h[:120] for h in history[-3:]) if history else "(žádné)"
        return [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": USER.format(groups=groups, topics=topics, history=hist, question=question)}]

    def _load_cache(self, key: str) -> QueryPlan | None:
        with self._lock:
            if key in self._ram:
                return self._ram[key]
        if self.pool is None:
            return None
        try:
            with self.pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT plan, model FROM query_cache WHERE key = %s", (key,))
                row = cur.fetchone()
        except Exception:
            return None
        if not row:
            return None
        plan = from_json(row[0], self.known_groups, self.known_topics)
        if plan:
            plan.model, plan.cached = row[1], True
        return plan

    def _store_cache(self, key: str, raw: dict, model: str) -> None:
        if self.pool is None:
            return
        try:
            with self.pool.connection() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO query_cache (key, plan, model) VALUES (%s, %s, %s) ON CONFLICT (key) DO NOTHING",
                            (key, json.dumps(raw, ensure_ascii=False), model))
                conn.commit()
        except Exception:
            pass

    def plan(self, question: str, history: list[str] | None = None, accept_models: set[str] | None = None) -> QueryPlan:
        """Vrátí plán; při jakémkoli selhání prázdný plán (content)."""
        key = cache_key(question)
        cached = self._load_cache(key)
        if cached:
            with self._lock:
                self._ram[key] = cached
            return cached
        t0 = time.time()
        try:
            resp = self.llm.with_options(timeout=self.timeout).chat.completions.create(
                model=self.model, messages=self._messages(question, history or []),
                temperature=0.0, max_tokens=self.max_tokens,
            )
            model = (resp.model or "").strip()
            text = THINK_RE.sub("", resp.choices[0].message.content or "")
            raw = parse_json(text)
        except Exception as exc:  # noqa: BLE001 — plánovač je bonus, ne podmínka
            print(f"plánovač selhal: {exc}")
            return QueryPlan(ms=int((time.time() - t0) * 1000))
        # cizí model (fallback) — plán nepoužít ani necachovat
        if accept_models and model and model not in accept_models:
            print(f"plánovač: odpověděl {model!r}, ne {self.model!r} — ignoruji")
            return QueryPlan(ms=int((time.time() - t0) * 1000))
        plan = from_json(raw, self.known_groups, self.known_topics)
        if plan is None:
            return QueryPlan(ms=int((time.time() - t0) * 1000))
        plan.model = model
        plan.ms = int((time.time() - t0) * 1000)
        self._store_cache(key, raw, model)
        with self._lock:
            self._ram[key] = plan
        return plan


_ORDINALS_CS = {"prvni": 1, "druha": 2, "druhy": 2, "treti": 3, "ctvrta": 4, "ctvrty": 4, "pata": 5, "paty": 5,
                "sesta": 6, "sesty": 6, "sedma": 7, "sedmy": 7, "osma": 8, "osmy": 8, "devata": 9, "devaty": 9,
                "desata": 10, "desaty": 10, "jedenacta": 11, "dvanacta": 12, "trinacta": 13, "ctrnacta": 14,
                "patnacta": 15, "sestnacta": 16, "sedmnacta": 17, "osmnacta": 18, "devatenacta": 19, "dvacata": 20}


def find_chapter(chapters: list[dict], hint: str) -> dict | None:
    """chapter_hint → kapitola: číslo (ref/ordinal), české řadové slovo
    („osmá"), nebo podřetězec nadpisu."""
    h = fold(hint)
    m = re.search(r"(\d+)", h)
    if not m:
        for word, num in _ORDINALS_CS.items():
            if re.search(rf"\b{word}\b", h):
                h = h.replace(word, str(num))
                m = re.search(r"(\d+)", h)
                break
    if m:
        n = m.group(1)
        for c in chapters:
            if str(c.get("ref")) == n or (fold(c.get("heading") or "").endswith(f" {n}") if c.get("heading") else False):
                return c
        # pořadové číslo (osmá kapitola → 8)
        for c in chapters:
            if int(c.get("ordinal", 0)) == int(n) and c.get("level", 1) == 1:
                return c
    for c in chapters:
        if h and (h in fold(c.get("heading") or "") or h in fold(c.get("heading_cs") or "")):
            return c
    return None

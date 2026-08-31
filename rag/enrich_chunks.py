#!/usr/bin/env python3
"""Obohacení chunků LLM: český gloss, klíčová slova (cs/en/originál),
otázky, entity, témata a kvalita → chunk_enrichment v Postgresu.

Běží na SPARKu proti TRT-LLM přímo (:8004), ne přes gateway — LiteLLM by
při pádu translate tiše přepnul na Qwen3-4B (viz llm_batch.py). Je
resumovatelné (input_sha = "PROMPT_VERSION:sha1(text)"); restart nic
neopakuje. Priorita 1 nejdřív (dnešní korpus + kanonický Perseus),
zbytek noční dávky.

    python3 enrich_chunks.py --llm-url http://localhost:8004/v1 --benchmark 200
    nohup python3 enrich_chunks.py --priority 1 > logs/enrich_chunks.log &
"""

from __future__ import annotations

import argparse
import json
import re
import os
import sys
import time
from pathlib import Path

import psycopg
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from llm_batch import LLMBatch, input_sha  # noqa: E402
from retrieval import fold  # noqa: E402

PROMPT_VERSION = "chunk-v1"

LANG_NAME = {"pi": "pálí", "sa": "sanskrt", "lzh": "klasická čínština", "zh": "čínština", "grc": "stará řečtina",
             "lat": "latina", "de": "němčina", "en": "angličtina", "ang": "staroangličtina", "non": "staroseverština",
             "ae": "avestština", "egy": "egyptština"}

SYSTEM = (
    "Jsi filolog a knihovník. Dostaneš úryvek z díla ve starém jazyce a vrátíš POUZE JSON "
    "s českým shrnutím a klíči pro vyhledávání. Nepřekládej úryvek, shrň ho. Nic nevymýšlej — "
    "když je úryvek jen rejstřík, patička, obsah nebo nesouvislý balast, dej quality 0."
)

USER = """Dílo: „{name}“ ({author}; tradice {group}; jazyk {lang}){translated}
Místo v díle: {chapter}

Vrať JSON s klíči:
- "gloss_cs": 1–2 věty česky, o čem úryvek je (obsah, ne překlad)
- "keywords_cs": 3–8 klíčových pojmů česky
- "keywords_en": 3–8 anglicky
- "keywords_orig": 3–8 termínů PŘESNĚ tak, jak stojí v úryvku (v jeho písmu a jazyce)
- "questions_cs": 2–3 otázky česky, na které úryvek odpovídá
- "entities": seznam objektů {{"name": "...", "type": "person|place|deity|work|concept"}}
- "topics": 0–3 slugy jen z tohoto seznamu: {topics}
- "quality": 0 (balast/patička/rejstřík), 1 (útržek), 2 (souvislý), 3 (souvislý a obsahově bohatý)

Úryvek:
---
{text}
---"""

TOPICS_HINT_MAX = 40


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def load_topics(registry: Path) -> tuple[set[str], str]:
    topics = yaml.safe_load((registry / "topics.yaml").read_text(encoding="utf-8")) or []
    slugs = {t["id"] for t in topics}
    hint = ", ".join(f"{t['id']} ({t['name_cs']})" for t in topics[:TOPICS_HINT_MAX])
    return slugs, hint


def pending(conn, priority: int | None, work: str | None, group: str | None, limit: int, profile: str):
    """Chunky bez obohacení (nebo s jiným PROMPT_VERSION) — streamem."""
    # Postgres nemá sha1(); invalidace při změně promptu jde přes prefix
    # `verze:` v input_sha (viz llm_batch.input_sha), změnu textu už zahodil
    # load_pg.replace_work() podle text_sha.
    where, params = ["(e.chunk_id IS NULL OR e.input_sha NOT LIKE %s)"], [PROMPT_VERSION + ":%"]
    if priority:
        where.append("w.priority <= %s"); params.append(priority)
    if work:
        where.append("c.work_id = %s"); params.append(work)
    if group:
        where.append('w."group" = %s'); params.append(group)
    sql = f"""
        SELECT c.id, c.text, c.lang, w.name_cs, w.title, w.author, w."group", w.lang_original, w.lang_corpus,
               w.edition, ch.path
        FROM chunks c JOIN works w ON w.id = c.work_id
        LEFT JOIN chapters ch ON ch.id = c.chapter_id
        LEFT JOIN chunk_enrichment e ON e.chunk_id = c.id
        WHERE {' AND '.join(where)}
        ORDER BY w.priority, c.work_id, c.seq
        {'LIMIT %s' if limit else ''}"""
    if limit:
        params.append(limit)
    with conn.cursor(name="pending_chunks") as cur:
        cur.itersize = 500
        cur.execute(sql, params)
        for row in cur:
            yield dict(zip(["id", "text", "lang", "name_cs", "title", "author", "group", "lang_original",
                            "lang_corpus", "edition", "chapter_path"], row))


def build_messages(item: dict, topics_hint: str) -> list[dict]:
    translated = ""
    if item["lang_original"] and item["lang_corpus"] and item["lang_original"] != item["lang_corpus"]:
        translated = f" — v knihovně je {LANG_NAME.get(item['lang_corpus'], item['lang_corpus'])} překlad ({item.get('edition') or ''})"
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER.format(
            name=item["name_cs"] or item["title"], author=item["author"] or "neznámý autor", group=item["group"],
            lang=LANG_NAME.get(item["lang"], item["lang"]), translated=translated,
            chapter=item["chapter_path"] or "(bez členění)", topics=topics_hint, text=item["text"][:6000],
        )},
    ]


def validate(item: dict, parsed: dict | None, slugs: set[str]) -> dict | None:
    if not parsed or not isinstance(parsed, dict):
        return None
    gloss = (parsed.get("gloss_cs") or "").strip()
    if not gloss:
        return None
    def lst(key, n=8):
        v = parsed.get(key) or []
        if isinstance(v, str):
            # model občas pošle seznam jako jeden řetězec („a, b, c") — bez
            # rozdělení by se celá věta uložila jako JEDNO klíčové slovo
            # a fulltext by ji nikdy netrefil (naměřeno na swarm-directorovi)
            v = [part for part in re.split(r"[,;]\s*|\s*\|\s*", v) if part.strip()]
        out, seen = [], set()
        for x in v:
            x = str(x).strip().strip("\"'")
            key_x = x.lower()
            if x and key_x not in seen:
                seen.add(key_x)
                out.append(x)
        return out[:n]
    text_fold = fold(item["text"])
    kw_orig = [k for k in lst("keywords_orig") if fold(k) in text_fold]   # model si je nesmí vymyslet
    topics = [t for t in lst("topics", 3) if t in slugs]
    ents = []
    for e in parsed.get("entities") or []:
        if isinstance(e, dict) and e.get("name"):
            ents.append({"name": str(e["name"])[:80], "type": str(e.get("type", "concept"))[:20]})
        elif isinstance(e, str):
            ents.append({"name": e[:80], "type": "concept"})
    try:
        quality = max(0, min(3, int(parsed.get("quality", 2))))
    except (TypeError, ValueError):
        quality = 2
    return {
        "gloss_cs": gloss[:600], "keywords_cs": lst("keywords_cs"), "keywords_en": lst("keywords_en"),
        "keywords_orig": kw_orig, "questions_cs": lst("questions_cs", 3), "entities": ents[:12],
        "topics": topics, "quality": quality,
    }


def upsert(conn, item: dict, enr: dict, model: str) -> None:
    enrich_fold = fold(" ".join([enr["gloss_cs"], *enr["keywords_cs"], *enr["keywords_en"], *enr["keywords_orig"], *enr["questions_cs"]]))
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO chunk_enrichment (chunk_id, gloss_cs, keywords_cs, keywords_en, keywords_orig, questions_cs,
                   entities, topics, quality, input_sha, model, enrich_fold)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (chunk_id) DO UPDATE SET gloss_cs = EXCLUDED.gloss_cs, keywords_cs = EXCLUDED.keywords_cs,
                   keywords_en = EXCLUDED.keywords_en, keywords_orig = EXCLUDED.keywords_orig,
                   questions_cs = EXCLUDED.questions_cs, entities = EXCLUDED.entities, topics = EXCLUDED.topics,
                   quality = EXCLUDED.quality, input_sha = EXCLUDED.input_sha, model = EXCLUDED.model,
                   enrich_fold = EXCLUDED.enrich_fold, created_at = now()""",
            (item["id"], enr["gloss_cs"], enr["keywords_cs"], enr["keywords_en"], enr["keywords_orig"],
             enr["questions_cs"], json.dumps(enr["entities"], ensure_ascii=False), enr["topics"], enr["quality"],
             input_sha(PROMPT_VERSION, item["text"]), model, enrich_fold),
        )
    conn.commit()


def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")
    p = argparse.ArgumentParser(description="Obohacení chunků LLM")
    p.add_argument("--dsn", default=os.getenv("PG_DSN"))
    p.add_argument("--llm-url", default="http://localhost:8004/v1", help="TRT-LLM přímo (ne gateway)")
    p.add_argument("--model", default="translate")
    p.add_argument("--accept-model", action="append", help="povolené názvy modelu v odpovědi (výchozí = --model)")
    p.add_argument("--workers", type=int, default=12)
    # 600 useknulo ~2 % odpovědí (finish_reason=length) — plný profil píše tři
    # seznamy klíčových slov, entity i otázky; navýšení platí jen pro ty dlouhé
    p.add_argument("--max-tokens", type=int, default=900)
    p.add_argument("--priority", type=int, default=None, help="jen díla s prioritou ≤ N")
    p.add_argument("--work"); p.add_argument("--group")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--benchmark", type=int, default=0, help="zpracovat N chunků, změřit rychlost, uložit")
    p.add_argument("--profile", default="full", choices=["full", "lite"])
    p.add_argument("--registry", default=str(Path(__file__).parent / "registry"))
    args = p.parse_args()
    if not args.dsn:
        print("CHYBA: PG_DSN", file=sys.stderr); return 2

    slugs, hint = load_topics(Path(args.registry))
    limit = args.benchmark or args.limit
    llm = LLMBatch(args.llm_url, args.model, workers=args.workers, max_tokens=args.max_tokens,
                   accept_models=set(args.accept_model or [args.model]))

    with psycopg.connect(args.dsn) as conn, psycopg.connect(args.dsn) as conn_w:
        with conn_w.cursor() as cur:
            cur.execute("INSERT INTO enrich_runs (kind, model, note) VALUES ('chunks', %s, %s) RETURNING id",
                        (args.model, f"priority={args.priority} work={args.work} group={args.group} limit={limit} profile={args.profile}"))
            run_id = cur.fetchone()[0]
        conn_w.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM chunks c JOIN works w ON w.id = c.work_id LEFT JOIN chunk_enrichment e ON e.chunk_id = c.id "
                        "WHERE (e.chunk_id IS NULL OR e.input_sha NOT LIKE %s)"
                        + (" AND w.priority <= %s" if args.priority else ""),
                        [PROMPT_VERSION + ":%"] + ([args.priority] if args.priority else []))
            todo = cur.fetchone()[0]
        print(f"k obohacení: {todo} chunků (priorita ≤ {args.priority}); běh #{run_id}, model {args.model} @ {args.llm_url}")

        t0 = time.time()

        def on_result(item, parsed, model):
            enr = validate(item, parsed, slugs)
            if not enr:
                return False
            upsert(conn_w, item, enr, model)
            return True

        stats = llm.run(pending(conn, args.priority, args.work, args.group, limit, args.profile),
                        lambda it: build_messages(it, hint), on_result, label="chunks")
        with conn_w.cursor() as cur:
            cur.execute("UPDATE enrich_runs SET finished_at = now(), done = %s, failed = %s, rejected_fallback = %s WHERE id = %s",
                        (stats.done, stats.failed, stats.rejected_fallback, run_id))
        conn_w.commit()

    dt = time.time() - t0
    if args.benchmark and stats.done:
        per = dt / max(1, stats.done + stats.failed)
        print(f"\nbenchmark: {stats.done} chunků za {dt:.0f} s → {per:.2f} s/chunk při {args.workers} vláknech; "
              f"zbývá {todo - stats.done} → odhad {(todo - stats.done) * per / 3600:.1f} h")
    return 0


if __name__ == "__main__":
    sys.exit(main())

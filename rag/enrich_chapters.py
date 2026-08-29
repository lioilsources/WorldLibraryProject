#!/usr/bin/env python3
"""Summary kapitol (3 délky) + český nadpis + témata → chapters v Postgresu.

Vstup kapitoly = originální text + už hotové glosy chunků (levný „map"
krok zdarma z enrich_chunks). Krátká kapitola jde jedním requestem,
dlouhá map-reduce (okna po ~12 chuncích → poznámky → jedno shrnutí).

Výchozí model je swarm-director (Nemotron 120B) přes gateway —
kvalita češtiny a hloubka; pro prioritu ≥ 2 nebo když director neběží,
--model translate. Zahazuje odpovědi cizího modelu (fallback), resume
přes summary_input_sha.

    nohup python3 enrich_chapters.py --llm-url http://localhost:8080/v1 --model swarm-director --priority 1 > logs/enrich_chapters.log &
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from enrich_chunks import LANG_NAME, load_dotenv  # noqa: E402
from llm_batch import LLMBatch, input_sha  # noqa: E402

PROMPT_VERSION = "chapter-v1"
MAX_INPUT_CHARS = 60_000     # ~20k tokenů; víc → map-reduce
WINDOW_CHUNKS = 12

SYSTEM = ("Jsi sečtělý knihovník. Píšeš věcné české anotace kapitol starých textů — bez hodnocení, bez "
          "převyprávění celého děje, s důrazem na to, o čem kapitola je a čím je zvláštní. Vrať POUZE JSON.")

USER = """Dílo: „{name}“ ({author}; tradice {group}; jazyk originálu {lang}){translated}
Kapitola: {path}{heading}
Témata (slug): {topics}

Vrať JSON:
{{
 "heading_cs": "krátký český název kapitoly (přelož/vystihni nadpis; max 8 slov)",
 "summary_short": "1 věta",
 "summary_medium": "do 50 slov",
 "summary_long": "do 150 slov",
 "keywords_cs": ["4–8 klíčových pojmů česky"],
 "topics": [{{"id": "slug ze seznamu", "weight": 0.0–1.0}}]  // 1–3 položky
}}

{material}"""

MAP_USER = """Dílo „{name}“, kapitola {path}. Shrň následující část kapitoly do 4–6 vět česky (věcně, co se tu říká/děje, klíčové pojmy v originále v závorce). Vrať POUZE JSON {{"notes": "..."}}.

---
{text}
---"""


def pending(conn, priority, work, limit):
    where, params = ["(ch.summary_input_sha IS NULL OR ch.summary_input_sha NOT LIKE %s)"], [PROMPT_VERSION + ":%"]
    if priority:
        where.append("w.priority <= %s"); params.append(priority)
    if work:
        where.append("ch.work_id = %s"); params.append(work)
    where.append("ch.chunk_count > 0")
    sql = f"""SELECT ch.id, ch.work_id, ch.path, ch.heading, w.name_cs, w.title, w.author, w."group",
                     w.lang_original, w.lang_corpus, w.edition
              FROM chapters ch JOIN works w ON w.id = ch.work_id
              WHERE {' AND '.join(where)} ORDER BY w.priority, ch.work_id, ch.ordinal {'LIMIT %s' if limit else ''}"""
    if limit:
        params.append(limit)
    with conn.cursor(name="pending_chapters") as cur:
        cur.itersize = 200
        cur.execute(sql, params)
        for r in cur:
            yield dict(zip(["id", "work_id", "path", "heading", "name_cs", "title", "author", "group",
                            "lang_original", "lang_corpus", "edition"], r))


def chapter_material(conn, chapter_id: str) -> tuple[str, list[str], str]:
    """(originál, glosy chunků, sha vstupu)."""
    with conn.cursor() as cur:
        cur.execute("""SELECT c.text, e.gloss_cs FROM chunks c LEFT JOIN chunk_enrichment e ON e.chunk_id = c.id
                       WHERE c.chapter_id = %s ORDER BY c.seq_in_chapter""", (chapter_id,))
        rows = cur.fetchall()
    text = "\n\n".join(r[0] for r in rows)
    glosses = [r[1] for r in rows if r[1]]
    return text, glosses, input_sha(PROMPT_VERSION, text)


def build(item: dict, material: str, topics_hint: str) -> list[dict]:
    translated = ""
    if item["lang_original"] != item["lang_corpus"]:
        translated = f" — v knihovně je {LANG_NAME.get(item['lang_corpus'], item['lang_corpus'])} překlad ({item.get('edition') or ''}); anotace to zmíní jednou větou"
    heading = f" — nadpis v textu: „{item['heading']}“" if item.get("heading") and item["heading"] != item["path"] else ""
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER.format(
                name=item["name_cs"] or item["title"], author=item["author"] or "neznámý autor", group=item["group"],
                lang=LANG_NAME.get(item["lang_original"], item["lang_original"]), translated=translated,
                path=item["path"], heading=heading, topics=topics_hint, material=material)}]


def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")
    p = argparse.ArgumentParser(description="Summary kapitol")
    p.add_argument("--dsn", default=os.getenv("PG_DSN"))
    p.add_argument("--llm-url", default="http://localhost:8080/v1")
    p.add_argument("--model", default="swarm-director")
    p.add_argument("--accept-model", action="append")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--priority", type=int, default=1)
    p.add_argument("--work")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--registry", default=str(Path(__file__).parent / "registry"))
    args = p.parse_args()
    if not args.dsn:
        print("CHYBA: PG_DSN", file=sys.stderr); return 2

    topics = yaml.safe_load((Path(args.registry) / "topics.yaml").read_text(encoding="utf-8")) or []
    slugs = {t["id"] for t in topics}
    hint = ", ".join(f"{t['id']} ({t['name_cs']})" for t in topics)
    llm = LLMBatch(args.llm_url, args.model, workers=args.workers, max_tokens=900, temperature=0.3,
                   accept_models=set(args.accept_model or [args.model]))
    mapper = LLMBatch(args.llm_url, args.model, workers=1, max_tokens=400, temperature=0.2,
                      accept_models=set(args.accept_model or [args.model]))

    with psycopg.connect(args.dsn) as conn_r, psycopg.connect(args.dsn) as conn_m, psycopg.connect(args.dsn) as conn_w:
        def messages_for(item):
            text, glosses, sha = chapter_material(conn_m, item["id"])
            item["_sha"] = sha
            if len(text) <= MAX_INPUT_CHARS:
                material = "Text kapitoly (originál):\n---\n" + text + "\n---"
                if glosses:
                    material += "\n\nGlosy úryvků (česky, už hotové):\n- " + "\n- ".join(glosses[:60])
                return build(item, material, hint)
            # map-reduce: okna → poznámky
            chunks = text.split("\n\n")
            notes = []
            for i in range(0, len(chunks), WINDOW_CHUNKS):
                window = "\n\n".join(chunks[i:i + WINDOW_CHUNKS])[:MAX_INPUT_CHARS]
                parsed, _ = mapper.one([{"role": "system", "content": SYSTEM},
                                        {"role": "user", "content": MAP_USER.format(name=item["name_cs"] or item["title"], path=item["path"], text=window)}])
                if parsed and parsed.get("notes"):
                    notes.append(str(parsed["notes"]))
            material = ("Kapitola je dlouhá; místo textu máš poznámky k jejím částem (v pořadí):\n- " + "\n- ".join(notes)
                        + ("\n\nGlosy úryvků:\n- " + "\n- ".join(glosses[:80]) if glosses else ""))
            return build(item, material, hint)

        def on_result(item, parsed, model):
            if not parsed or not parsed.get("summary_short"):
                return False
            tlist = []
            for t in parsed.get("topics") or []:
                if isinstance(t, dict) and t.get("id") in slugs:
                    try:
                        tlist.append((t["id"], max(0.0, min(1.0, float(t.get("weight", 0.7))))))
                    except (TypeError, ValueError):
                        tlist.append((t["id"], 0.7))
                elif isinstance(t, str) and t in slugs:
                    tlist.append((t, 0.7))
            kw = [str(k).strip() for k in (parsed.get("keywords_cs") or []) if str(k).strip()][:8]
            with conn_w.cursor() as cur:
                cur.execute("""UPDATE chapters SET heading_cs = %s, summary_short = %s, summary_medium = %s, summary_long = %s,
                               keywords_cs = %s, summary_model = %s, summary_input_sha = %s, summary_at = now() WHERE id = %s""",
                            (str(parsed.get("heading_cs") or "")[:120] or None, str(parsed["summary_short"])[:400],
                             str(parsed.get("summary_medium") or "")[:800], str(parsed.get("summary_long") or "")[:2000],
                             kw, model, PROMPT_VERSION + ":" + item["_sha"], item["id"]))
                cur.execute("DELETE FROM chapter_topics WHERE chapter_id = %s AND source = 'llm'", (item["id"],))
                for tid, w in tlist[:3]:
                    cur.execute("INSERT INTO chapter_topics (chapter_id, topic_id, weight, source) VALUES (%s, %s, %s, 'llm') "
                                "ON CONFLICT (chapter_id, topic_id) DO UPDATE SET weight = EXCLUDED.weight", (item["id"], tid, w))
            conn_w.commit()
            return True

        with conn_w.cursor() as cur:
            cur.execute("INSERT INTO enrich_runs (kind, model, note) VALUES ('chapters', %s, %s) RETURNING id",
                        (args.model, f"priority={args.priority} work={args.work} limit={args.limit}"))
            run_id = cur.fetchone()[0]
        conn_w.commit()
        stats = llm.run(pending(conn_r, args.priority, args.work, args.limit), messages_for, on_result, label="chapters", report_every=20)
        with conn_w.cursor() as cur:
            cur.execute("UPDATE enrich_runs SET finished_at = now(), done = %s, failed = %s, rejected_fallback = %s WHERE id = %s",
                        (stats.done, stats.failed, stats.rejected_fallback, run_id))
        conn_w.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())

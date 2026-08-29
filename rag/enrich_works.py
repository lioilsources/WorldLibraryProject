#!/usr/bin/env python3
"""Summary děl (3 délky) + klíčová slova + témata → works v Postgresu.

Vstup = summary_medium kapitol (když jsou), jinak rovnoměrně vzorkované
glosy chunků (když nejsou ani ty, prvních 8 chunků originálu — lepší
než nic, ale to je stav před obohacením). Kurátorská pole (name_cs,
autor, jazyk, edice) se NIKDY nepřepisují. Témata díla: LLM návrh
(source='llm') + agregace z chunků (source='aggregated', zdarma).

    nohup python3 enrich_works.py --llm-url http://localhost:8080/v1 --model swarm-director > logs/enrich_works.log &
    python3 enrich_works.py --aggregate-only     # jen témata z četnosti v chuncích
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

PROMPT_VERSION = "work-v1"

SYSTEM = ("Jsi sečtělý knihovník. Píšeš věcné české katalogové anotace děl světové literatury a filosofie: "
          "co dílo je, o čem je, čím je významné — bez hodnotících frází a bez převyprávění. Vrať POUZE JSON.")

USER = """Dílo: „{name}“ (originální název {title}; {author}; tradice {group}; jazyk originálu {lang}; {period}; forma {form}){translated}
Rozsah: {chapters} kapitol.
Témata (slug): {topics}

{material}

Vrať JSON:
{{
 "summary_short": "1 věta (co to je a o čem)",
 "summary_medium": "do 50 slov",
 "summary_long": "do 150 slov (obsah, struktura, význam; u překladu jednou větou, že knihovna má překlad)",
 "keywords_cs": ["5–10 klíčových pojmů česky"],
 "topics": [{{"id": "slug", "weight": 0.0–1.0}}]  // 2–4 položky
}}"""


def material_for(conn, work_id: str, sample: int = 12) -> tuple[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT path, summary_medium FROM chapters WHERE work_id = %s AND summary_medium IS NOT NULL ORDER BY ordinal", (work_id,))
        rows = cur.fetchall()
        if rows:
            body = "\n".join(f"- {p}: {s}" for p, s in rows[:80])
            return "Anotace kapitol:\n" + body, input_sha(PROMPT_VERSION, body)
        cur.execute("SELECT count(*) FROM chunks WHERE work_id = %s", (work_id,))
        n = cur.fetchone()[0]
        step = max(1, n // sample)
        cur.execute("""SELECT c.seq, ch.path, e.gloss_cs, c.text FROM chunks c
                       LEFT JOIN chapters ch ON ch.id = c.chapter_id
                       LEFT JOIN chunk_enrichment e ON e.chunk_id = c.id
                       WHERE c.work_id = %s AND c.seq %% %s = 0 ORDER BY c.seq LIMIT %s""", (work_id, step, sample))
        rows = cur.fetchall()
    if any(r[2] for r in rows):
        body = "\n".join(f"- {p or ''}: {g}" for _s, p, g, _t in rows if g)
        return "Glosy vzorkovaných úryvků (česky):\n" + body, input_sha(PROMPT_VERSION, body)
    body = "\n---\n".join(t[:700] for _s, _p, _g, t in rows[:8])
    return "Vzorek originálního textu (bez glos):\n" + body, input_sha(PROMPT_VERSION, body)


def aggregate_topics(conn) -> int:
    """work_topics(source='aggregated') = normalizovaná četnost témat v chuncích."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM work_topics WHERE source = 'aggregated'")
        cur.execute("""
            INSERT INTO work_topics (work_id, topic_id, weight, source)
            SELECT work_id, topic_id, round((cnt::real / total)::numeric, 3), 'aggregated'
            FROM (
              SELECT c.work_id, t AS topic_id, count(*) AS cnt,
                     sum(count(*)) OVER (PARTITION BY c.work_id) AS total
              FROM chunk_enrichment e JOIN chunks c ON c.id = e.chunk_id, unnest(e.topics) AS t
              WHERE coalesce(e.quality, 3) > 0
              GROUP BY c.work_id, t
            ) s
            WHERE cnt::real / total >= 0.08
            ON CONFLICT (work_id, topic_id) DO NOTHING""")
        n = cur.rowcount
    conn.commit()
    return n


def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")
    p = argparse.ArgumentParser(description="Summary děl")
    p.add_argument("--dsn", default=os.getenv("PG_DSN"))
    p.add_argument("--llm-url", default="http://localhost:8080/v1")
    p.add_argument("--model", default="swarm-director")
    p.add_argument("--accept-model", action="append")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--priority", type=int, default=None)
    p.add_argument("--work")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--force", action="store_true", help="přegenerovat i hotová")
    p.add_argument("--aggregate-only", action="store_true")
    p.add_argument("--registry", default=str(Path(__file__).parent / "registry"))
    args = p.parse_args()
    if not args.dsn:
        print("CHYBA: PG_DSN", file=sys.stderr); return 2

    with psycopg.connect(args.dsn) as conn:
        n = aggregate_topics(conn)
        print(f"agregovaná témata děl z chunků: {n} řádků")
        if args.aggregate_only:
            return 0

    topics = yaml.safe_load((Path(args.registry) / "topics.yaml").read_text(encoding="utf-8")) or []
    slugs = {t["id"] for t in topics}
    hint = ", ".join(f"{t['id']} ({t['name_cs']})" for t in topics)
    llm = LLMBatch(args.llm_url, args.model, workers=args.workers, max_tokens=900, temperature=0.3,
                   accept_models=set(args.accept_model or [args.model]))

    with psycopg.connect(args.dsn) as conn_r, psycopg.connect(args.dsn) as conn_m, psycopg.connect(args.dsn) as conn_w:
        where, params = ["1=1"], []
        if not args.force:
            where.append("(summary_input_sha IS NULL OR summary_input_sha NOT LIKE %s)"); params.append(PROMPT_VERSION + ":%")
        if args.priority:
            where.append("priority <= %s"); params.append(args.priority)
        if args.work:
            where.append("id = %s"); params.append(args.work)
        with conn_r.cursor() as cur:
            cur.execute(f"""SELECT id, title, name_cs, author, author_cs, "group", lang_original, lang_corpus, edition, period, form, chapter_count
                            FROM works WHERE {' AND '.join(where)} ORDER BY priority, id {'LIMIT %s' if args.limit else ''}""",
                        params + ([args.limit] if args.limit else []))
            items = [dict(zip(["id", "title", "name_cs", "author", "author_cs", "group", "lang_original", "lang_corpus",
                               "edition", "period", "form", "chapter_count"], r)) for r in cur.fetchall()]
        print(f"děl k anotaci: {len(items)}")

        def messages_for(item):
            material, sha = material_for(conn_m, item["id"])
            item["_sha"] = sha
            translated = ""
            if item["lang_original"] != item["lang_corpus"]:
                translated = f" — POZOR: v knihovně je {LANG_NAME.get(item['lang_corpus'], item['lang_corpus'])} překlad ({item.get('edition') or ''})"
            return [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": USER.format(
                        name=item["name_cs"] or item["title"], title=item["title"], author=item["author_cs"] or item["author"] or "neznámý autor",
                        group=item["group"], lang=LANG_NAME.get(item["lang_original"], item["lang_original"]),
                        period=item.get("period") or "období neuvedeno", form=item.get("form") or "?", translated=translated,
                        chapters=item.get("chapter_count") or 0, topics=hint, material=material)}]

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
            kw = [str(k).strip() for k in (parsed.get("keywords_cs") or []) if str(k).strip()][:10]
            with conn_w.cursor() as cur:
                # name_cs se NIKDY nepřepisuje — jen summary a témata
                cur.execute("""UPDATE works SET summary_short = %s, summary_medium = %s, summary_long = %s, keywords_cs = %s,
                               summary_model = %s, summary_input_sha = %s, summary_at = now(), updated_at = now() WHERE id = %s""",
                            (str(parsed["summary_short"])[:400], str(parsed.get("summary_medium") or "")[:800],
                             str(parsed.get("summary_long") or "")[:2000], kw, model, PROMPT_VERSION + ":" + item["_sha"], item["id"]))
                cur.execute("DELETE FROM work_topics WHERE work_id = %s AND source = 'llm'", (item["id"],))
                for tid, w in tlist[:4]:
                    cur.execute("INSERT INTO work_topics (work_id, topic_id, weight, source) VALUES (%s, %s, %s, 'llm') "
                                "ON CONFLICT (work_id, topic_id) DO UPDATE SET weight = GREATEST(work_topics.weight, EXCLUDED.weight), source = 'llm'",
                                (item["id"], tid, w))
            conn_w.commit()
            return True

        with conn_w.cursor() as cur:
            cur.execute("INSERT INTO enrich_runs (kind, model, note) VALUES ('works', %s, %s) RETURNING id",
                        (args.model, f"priority={args.priority} work={args.work} limit={args.limit} force={args.force}"))
            run_id = cur.fetchone()[0]
        conn_w.commit()
        stats = llm.run(items, messages_for, on_result, label="works", report_every=10)
        with conn_w.cursor() as cur:
            cur.execute("UPDATE enrich_runs SET finished_at = now(), done = %s, failed = %s, rejected_fallback = %s WHERE id = %s",
                        (stats.done, stats.failed, stats.rejected_fallback, run_id))
        conn_w.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())

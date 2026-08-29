#!/usr/bin/env python3
"""Načte výstup ingestu (works.jsonl, chapters.jsonl, books.jsonl) a registr
témat do knihovního Postgresu na JODA. Běží na SPARKu nebo M2 (LAN).

Zásady:
- Kurátorská pole díla (name_cs, autor, jazyk, edice, aliasy) přicházejí
  z registru přes works.jsonl a mají přednost; LLM je později nepřepisuje.
  summary_* a témata se při re-loadu NEmažou (jen chunky a kapitoly).
- Chunky se nahrazují per dílo v transakci. Obohacení (chunk_enrichment)
  přežije tam, kde se chunk_id i text_sha shodují — CASCADE by ho smazal,
  proto se nejdřív odloží stranou a pak vrátí.
- Vše NFC; fold() pro fulltext je tentýž jako v retrieval.py.
- Indexy se staví až po loadu: make pg-index (0003_indexes.sql).

Použití:
    python3 load_pg.py --input books.jsonl --works works.jsonl --chapters chapters.jsonl --replace-all
    python3 load_pg.py --work zh.daodejing --replace-work      # jen jedno dílo
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import psycopg
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from retrieval import fold  # noqa: E402

CJK = ("zh", "lzh")


def nfc(s):
    return unicodedata.normalize("NFC", s) if isinstance(s, str) else s


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def read_jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def bigrams(text: str) -> str:
    """'道可道非常道' → '道可 可道 道非 …' — simple parser čínštinu nedělí."""
    chars = [c for c in text if not c.isspace() and c not in "，。、；：？！「」『』（）《》"]
    return " ".join(chars[i] + chars[i + 1] for i in range(len(chars) - 1))


def upsert_topics(conn, registry: Path) -> int:
    topics = yaml.safe_load((registry / "topics.yaml").read_text(encoding="utf-8")) or []
    with conn.cursor() as cur:
        for i, t in enumerate(topics):
            cur.execute(
                """INSERT INTO topics (id, name_cs, description_cs, parent_id, sort)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET name_cs = EXCLUDED.name_cs,
                     description_cs = EXCLUDED.description_cs, parent_id = EXCLUDED.parent_id, sort = EXCLUDED.sort""",
                (t["id"], t["name_cs"], t["description_cs"], t.get("parent_id"), i),
            )
    return len(topics)


WORK_COLS = ("group", "subgroup", "title", "work_legacy", "name_cs", "author", "author_cs",
             "lang_original", "lang_corpus", "edition", "form", "period", "source_path", "urn",
             "priority", "aliases", "chunk_count", "chapter_count", "char_count")


def upsert_work(cur, w: dict) -> None:
    row = {c: nfc(w.get(c)) for c in WORK_COLS}
    row["id"] = w["id"]
    row["aliases"] = [nfc(a) for a in (w.get("aliases") or [])]
    row["priority"] = int(w.get("priority") or 2)
    for c in ("chunk_count", "chapter_count", "char_count"):
        row[c] = int(w.get(c) or 0)
    cols = ["id"] + list(WORK_COLS)
    sets = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in WORK_COLS)
    cur.execute(
        f'INSERT INTO works ({", ".join(f"\"{c}\"" for c in cols)}) VALUES ({", ".join("%s" for _ in cols)}) '
        f"ON CONFLICT (id) DO UPDATE SET {sets}, updated_at = now()",
        [row[c] for c in cols],
    )


def replace_work(conn, work_id: str, chapters: list[dict], chunks: list[dict]) -> tuple[int, int, int]:
    """Nahradí kapitoly a chunky díla; vrátí (kapitol, chunků, zachovaných obohacení)."""
    with conn.transaction():
        with conn.cursor() as cur:
            # obohacení k zachování: stejné chunk_id i text_sha
            cur.execute(
                """SELECT e.chunk_id, c.text_sha FROM chunk_enrichment e JOIN chunks c ON c.id = e.chunk_id
                   WHERE c.work_id = %s""", (work_id,))
            old_sha = dict(cur.fetchall())
            keep = [c["id"] for c in chunks if old_sha.get(c["id"]) == c["text_sha"]]
            cur.execute("CREATE TEMP TABLE IF NOT EXISTS keep_enrich (LIKE chunk_enrichment INCLUDING ALL) ON COMMIT DROP")
            cur.execute("DELETE FROM keep_enrich")
            if keep:
                cur.execute("INSERT INTO keep_enrich SELECT * FROM chunk_enrichment WHERE chunk_id = ANY(%s)", (keep,))
            # kapitolová summary zachovat podle id, když text kapitoly zůstal (char_count)
            cur.execute("DELETE FROM chunks WHERE work_id = %s", (work_id,))
            cur.execute("DELETE FROM chapters WHERE work_id = %s", (work_id,))

            # kapitoly v pořadí — rodič musí existovat dřív; rodič mimo seznam
            # (front matter vynechané ingestem) → bez rodiče, ne pád
            present = {c["id"] for c in chapters}
            for ch in sorted(chapters, key=lambda c: c["ordinal"]):
                if ch.get("parent_id") and ch["parent_id"] not in present:
                    ch = dict(ch, parent_id=None)
                cur.execute(
                    """INSERT INTO chapters (id, work_id, ordinal, level, parent_id, ref, heading, path, char_count, chunk_count)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (ch["id"], work_id, ch["ordinal"], ch.get("level", 1), ch.get("parent_id"), nfc(ch.get("ref")),
                     nfc(ch.get("heading")), nfc(ch.get("path")), ch.get("char_count", 0), ch.get("chunk_count", 0)),
                )
            with cur.copy(
                "COPY chunks (id, work_id, chapter_id, seq, seq_in_chapter, ref_start, ref_end, lang, text, text_fold, "
                "text_sha, char_count, text_bigrams) FROM STDIN"
            ) as cp:
                for c in chunks:
                    text = nfc(c["text"])
                    cp.write_row((
                        c["id"], work_id, c.get("chapter_id"), c["chunk_index"], c.get("seq_in_chapter", 0),
                        c.get("ref_start"), c.get("ref_end"), c.get("lang") or "en", text, fold(text),
                        c.get("text_sha") or hashlib.sha1(text.encode("utf-8")).hexdigest(), len(text),
                        bigrams(text) if (c.get("lang") in CJK) else None,
                    ))
            if keep:
                cur.execute("INSERT INTO chunk_enrichment SELECT * FROM keep_enrich ON CONFLICT DO NOTHING")
    return len(chapters), len(chunks), len(keep)


def migrate_summaries(conn, summaries_file: Path) -> int:
    """Jednorázově: staré anotace do summary_medium a name_cs jen tam, kde chybí."""
    if not summaries_file.exists():
        return 0
    data = json.loads(summaries_file.read_text(encoding="utf-8"))
    n = 0
    with conn.cursor() as cur:
        for legacy, entry in data.items():
            summary = entry.get("summary") if isinstance(entry, dict) else entry
            name_cs = entry.get("name_cs") if isinstance(entry, dict) else None
            cur.execute(
                """UPDATE works SET
                     summary_medium = COALESCE(summary_medium, %s),
                     summary_model = COALESCE(summary_model, 'translate (summaries.json)'),
                     name_cs = COALESCE(name_cs, %s)
                   WHERE work_legacy = %s""",
                (summary, name_cs, nfc(legacy)),
            )
            n += cur.rowcount
    return n


def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")
    p = argparse.ArgumentParser(description="Load JSONL do knihovního Postgresu")
    p.add_argument("--dsn", default=os.getenv("PG_DSN"))
    p.add_argument("--input", default="books.jsonl")
    p.add_argument("--works", default="works.jsonl")
    p.add_argument("--chapters", default="chapters.jsonl")
    p.add_argument("--registry", default="registry")
    p.add_argument("--summaries", default="summaries.json", help="migrace starých anotací (jen kde chybí)")
    p.add_argument("--work", help="jen tohle work_id")
    p.add_argument("--replace-all", action="store_true")
    p.add_argument("--replace-work", action="store_true")
    args = p.parse_args()
    if not args.dsn:
        print("CHYBA: chybí --dsn / PG_DSN", file=sys.stderr)
        return 2
    if not (args.replace_all or args.replace_work):
        print("CHYBA: zvol --replace-all nebo --replace-work (nic se neděje implicitně)", file=sys.stderr)
        return 2

    t0 = time.time()
    works = {w["id"]: w for w in read_jsonl(Path(args.works))}
    chapters_by = defaultdict(list)
    for ch in read_jsonl(Path(args.chapters)):
        chapters_by[ch["work_id"]].append(ch)
    wanted = {args.work} if args.work else set(works)
    print(f"děl v JSONL: {len(works)}, kapitol: {sum(map(len, chapters_by.values()))}; načítám {len(wanted)} děl")

    with psycopg.connect(args.dsn) as conn:
        n_topics = upsert_topics(conn, Path(args.registry))
        conn.commit()
        with conn.cursor() as cur:
            for wid in wanted:
                upsert_work(cur, works[wid])
        conn.commit()

        # chunky streamem po dílech (soubor je seřazený po dílech)
        buf, cur_wid = [], None
        done = {"chapters": 0, "chunks": 0, "kept": 0, "works": 0}

        def flush():
            if cur_wid is None or cur_wid not in wanted:
                return
            ch, cn, kept = replace_work(conn, cur_wid, chapters_by.get(cur_wid, []), buf)
            done["chapters"] += ch; done["chunks"] += cn; done["kept"] += kept; done["works"] += 1
            if done["works"] % 50 == 0:
                print(f"  {done['works']} děl, {done['chunks']} chunků, {time.time()-t0:.0f} s", flush=True)

        for c in read_jsonl(Path(args.input)):
            if c["work_id"] != cur_wid:
                flush()
                buf, cur_wid = [], c["work_id"]
            buf.append(c)
        flush()

        migrated = migrate_summaries(conn, Path(args.summaries))
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM enrichment_status_v")
            st = dict(zip([d[0] for d in cur.description], cur.fetchone()))

    print(f"\nhotovo za {time.time()-t0:.0f} s: témat {n_topics}, děl {done['works']}, kapitol {done['chapters']}, "
          f"chunků {done['chunks']}, zachovaných obohacení {done['kept']}, migrovaných anotací {migrated}")
    print("stav DB:", st)
    print("teď: make pg-index  (GIN indexy + ANALYZE)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

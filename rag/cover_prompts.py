#!/usr/bin/env python3
"""Prompty k obálkám děl pro Kindlify → works.cover_prompt / cover_palette.

LLM ke každému dílu napíše anglický popis motivu (40–70 slov: scéna nebo
symbol z díla, kompozice, žádný text, žádné portréty skutečných lidí).
Styl edice (dřevoryt) a paletu podle jazyka originálu sem nepíše — ty
skládá až `--dump` do finálního promptu pro obrazový model, takže změna
stylu znamená jen nový dump, ne nový běh LLM. Plán: PLAN-covers.md
v Kindlify.

    python3 cover_prompts.py --llm-url http://localhost:8005/v1 --model qwen36 --priority 3
    python3 cover_prompts.py --dump covers_prompts.jsonl --priority 3   # bez LLM, jen SELECT

Resume přes `cover_input_sha` ("cover-v1:<sha1(vstup)>"): hotová díla se
přeskakují, dílo se změněnou anotací se přegeneruje.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).parent))
from enrich_chunks import LANG_NAME, load_dotenv  # noqa: E402
from export_bundle import slugify  # noqa: E402
from llm_batch import LLMBatch, input_sha  # noqa: E402

PROMPT_VERSION = "cover-v1"

# Styl edice — jedna řada pro celou knihovnu (rozhodnutí Olina 2026-11-24).
STYLE = ("Woodcut book cover illustration, bold carved black linework, visible wood grain texture, "
         "limited flat colors, strong central composition, generous empty margins, "
         "no text, no letters, no lettering, no signature, no border")

# Paleta podle jazyka originálu: dvě až tři tiskové barvy na papíře.
PALETTES = {
    "grc": "black ink with terracotta red and ochre on cream paper",
    "lat": "black ink with imperial purple and gold ochre on parchment",
    "pi": "black ink with saffron orange and deep maroon on palm-leaf beige",
    "sa": "black ink with saffron and indigo on warm paper",
    "zh": "black ink with vermilion red on rice paper",
    "lzh": "black ink with vermilion red on rice paper",
    "ang": "black ink with moss green and rust on oatmeal paper",
    "non": "black ink with slate blue and rust red on oatmeal paper",
    "de": "black ink with dark green and brick red on off-white paper",
    "ae": "black ink with fire orange and lapis blue on sand paper",
    "egy": "black ink with turquoise and sandstone yellow on papyrus",
    "he": "black ink with deep blue and gold on parchment",
    "ar": "black ink with emerald green and gold on cream paper",
}
DEFAULT_PALETTE = "black ink with one muted red on cream paper"

SYSTEM = ("You are an art director for a series of classic-literature book covers. You describe ONE image "
          "for each work: a scene, object or symbol that a reader of the work would recognise. "
          "Never describe text, letters, titles or portraits of real historical people. Return ONLY JSON.")

USER = """Work: "{name}" (original title {title}; {author}; tradition {group}; original language {lang}; {period}; form {form})
Summary (Czech): {summary}
Keywords (Czech): {keywords}

Write the cover motif in English, 40–70 words: what is shown, the composition (foreground/background),
one or two concrete details from the work. No style words (the series style is added later),
no text or lettering, no faces of real historical persons (show figures from behind, small, or as symbols).

Return JSON: {{"motif": "..."}}"""

WORD_RE = re.compile(r"\w+")
# Obrazový model píše text, kdykoli o něm prompt mluví — i v negaci.
TEXT_WORDS = re.compile(r"\b(text|letters?|lettering|inscriptions?|titles?|words?|captions?|writing)\b", re.I)


def palette_for(lang: str | None) -> str:
    return PALETTES.get((lang or "").lower(), DEFAULT_PALETTE)


def clean_motif(raw) -> str | None:
    """Motiv z odpovědi LLM, nebo None, když je k ničemu."""
    if not isinstance(raw, str):
        return None
    motif = " ".join(raw.split()).strip().strip('"')
    n = len(WORD_RE.findall(motif))
    if n < 15 or n > 110:
        return None
    # Zmínka o textu v motivu vede k písmenkům na obálce; styl má vlastní zákaz.
    motif = TEXT_WORDS.sub("", motif)
    return " ".join(motif.split())


def full_prompt(motif: str, palette: str) -> str:
    return f"{STYLE}. Palette: {palette}. {motif}"


def asset_for(work_id: str) -> str:
    """Jméno obálky v Kindlify: assets/covers/<asset>.webp (= jméno bundlu)."""
    return slugify(work_id).replace("-", "_")


def dump(conn, out: Path, priority: int | None) -> int:
    where, params = ["cover_prompt IS NOT NULL"], []
    if priority:
        where.append("priority <= %s"); params.append(priority)
    with conn.cursor() as cur:
        cur.execute(f"SELECT id, cover_prompt, cover_palette FROM works WHERE {' AND '.join(where)} ORDER BY priority, id",
                    params)
        rows = cur.fetchall()
    with out.open("w", encoding="utf-8") as f:
        for wid, motif, palette in rows:
            prompt = full_prompt(motif, palette or DEFAULT_PALETTE)
            f.write(json.dumps({"work_id": wid, "asset": asset_for(wid), "prompt": prompt,
                                "prompt_sha": hashlib.sha1(prompt.encode()).hexdigest()[:12]},
                               ensure_ascii=False) + "\n")
    print(f"{len(rows)} promptů → {out}")
    return 0


def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")
    p = argparse.ArgumentParser(description="Prompty k obálkám děl")
    p.add_argument("--dsn", default=os.getenv("PG_DSN"))
    p.add_argument("--llm-url", default="http://localhost:8005/v1")
    p.add_argument("--model", default="qwen36")
    p.add_argument("--accept-model", action="append")
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--priority", type=int, default=None)
    p.add_argument("--work")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--force", action="store_true", help="přegenerovat i hotová")
    p.add_argument("--dump", type=Path, help="jen zapsat JSONL finálních promptů pro obrazový model")
    args = p.parse_args()
    if not args.dsn:
        print("CHYBA: PG_DSN", file=sys.stderr); return 2

    if args.dump:
        with psycopg.connect(args.dsn) as conn:
            return dump(conn, args.dump, args.priority)

    llm = LLMBatch(args.llm_url, args.model, workers=args.workers, max_tokens=300, temperature=0.7,
                   accept_models=set(args.accept_model or [args.model]))

    with psycopg.connect(args.dsn) as conn_r, psycopg.connect(args.dsn) as conn_w:
        where, params = ["summary_medium IS NOT NULL"], []
        if not args.force:
            where.append("(cover_input_sha IS NULL OR cover_input_sha NOT LIKE %s)"); params.append(PROMPT_VERSION + ":%")
        if args.priority:
            where.append("priority <= %s"); params.append(args.priority)
        if args.work:
            where.append("id = %s"); params.append(args.work)
        cols = ["id", "title", "name_cs", "author", "author_cs", "group", "lang_original", "period", "form",
                "summary_medium", "keywords_cs"]
        with conn_r.cursor() as cur:
            cur.execute(f"""SELECT {', '.join('"group"' if c == 'group' else c for c in cols)} FROM works
                            WHERE {' AND '.join(where)} ORDER BY priority, id {'LIMIT %s' if args.limit else ''}""",
                        params + ([args.limit] if args.limit else []))
            items = [dict(zip(cols, r)) for r in cur.fetchall()]
        print(f"děl k obálce: {len(items)}")

        def messages_for(item):
            body = f"{item['name_cs'] or item['title']}|{item['summary_medium']}|{','.join(item['keywords_cs'] or [])}"
            item["_sha"] = input_sha(PROMPT_VERSION, body)
            return [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": USER.format(
                        name=item["name_cs"] or item["title"], title=item["title"],
                        author=item["author_cs"] or item["author"] or "unknown author", group=item["group"],
                        lang=LANG_NAME.get(item["lang_original"], item["lang_original"]),
                        period=item.get("period") or "period unknown", form=item.get("form") or "?",
                        summary=item["summary_medium"], keywords=", ".join(item["keywords_cs"] or []) or "-")}]

        def on_result(item, parsed, model):
            motif = clean_motif((parsed or {}).get("motif"))
            if not motif:
                return False
            with conn_w.cursor() as cur:
                cur.execute("""UPDATE works SET cover_prompt = %s, cover_palette = %s, cover_model = %s,
                               cover_input_sha = %s, cover_at = now() WHERE id = %s""",
                            (motif, palette_for(item["lang_original"]), model,
                             PROMPT_VERSION + ":" + item["_sha"], item["id"]))
            conn_w.commit()
            return True

        with conn_w.cursor() as cur:
            cur.execute("INSERT INTO enrich_runs (kind, model, note) VALUES ('covers', %s, %s) RETURNING id",
                        (args.model, f"priority={args.priority} work={args.work} limit={args.limit} force={args.force}"))
            run_id = cur.fetchone()[0]
        conn_w.commit()
        stats = llm.run(items, messages_for, on_result, label="covers", report_every=25)
        with conn_w.cursor() as cur:
            cur.execute("UPDATE enrich_runs SET finished_at = now(), done = %s, failed = %s, rejected_fallback = %s WHERE id = %s",
                        (stats.done, stats.failed, stats.rejected_fallback, run_id))
        conn_w.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())

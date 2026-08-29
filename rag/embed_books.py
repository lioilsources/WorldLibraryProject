#!/usr/bin/env python3
"""Embedding do ChromaDB na JODA. Běží na SPARK (GPU).

Dva režimy:

  --mode passages (výchozí)   books.jsonl → kolekce originálů (books_v2)
      Chunk se rozdělí na pasáže ≤ --max-tokens (450) podle tokenizeru
      embedding modelu — multilingual-e5-large má limit 512 a co je nad,
      se tiše uřízne (pálí: 100 % chunků, měřeno). ID pasáže je
      '{chunk_id}#{p}', metadata nesou chunk_id, takže retrieval dedupuje
      zpět na chunk. Embedovaný text má kontextový prefix
      'Dílo › Kapitola' místo dřívějšího '(část 3/19)'.

  --source pg                 chunk_enrichment → kolekce glos (books_gloss)
      Embeduje české glosy + klíčová slova + otázky (čeština × čeština
      obchází cross-lingual slabinu), ID = chunk_id.

Idempotentní: existující ID přeskakuje; --sync navíc smaže z kolekce
pasáže, které v JSONL už nejsou, a přeembeduje ty, jejichž text_sha se
změnil. --reset kolekci smaže a založí znovu (jen pro novou kolekci —
přechod ze staré `books` je nová kolekce, ne reset živé).

Použití (na SPARK):
    python3 embed_books.py --input books.jsonl --collection books_v2
    python3 embed_books.py --source pg --collection books_gloss
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import chromadb

from embeddings import DEFAULT_MODEL, format_passage, make_embedder

# metadata pasáže v Chromě — plochá, jen to, co retrieval filtruje nebo ukazuje
PASSAGE_META = ("chunk_id", "work_id", "work", "group", "subgroup", "lang", "chapter_id",
                "chapter_path", "seq", "text_sha", "source", "path", "title", "chunk_index")


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


def read_jsonl(path: str):
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                print(f"POZOR: řádek {line_no} není validní JSON: {e}", file=sys.stderr)


# --- pasáže -------------------------------------------------------------------

class Splitter:
    """Dělí chunk na pasáže podle tokenů embedding modelu. Tokenizuje
    jednou s offsety a řeže podle nich (binární hledání s opakovanou
    tokenizací bylo hlavní CPU brzda). Řez se zarovná zpět na konec
    věty/řádku, poslední krátký kousek se slije s předchozím."""

    ENDINGS = set(".!?。！？؟।॥…\n;")

    def __init__(self, model_name: str, max_tokens: int, prefix_tokens: int = 40):
        from transformers import AutoTokenizer
        self.tok = AutoTokenizer.from_pretrained(model_name)
        self.budget = max_tokens - prefix_tokens   # místo na prefix 'Dílo › Kapitola'

    def split(self, text: str) -> list[str]:
        enc = self.tok(text, add_special_tokens=False, return_offsets_mapping=True)
        offsets = enc["offset_mapping"]
        n = len(offsets)
        if n <= self.budget:
            return [text]
        pieces, start_tok = [], 0
        while start_tok < n:
            end_tok = min(start_tok + self.budget, n)
            char_start = offsets[start_tok][0]
            char_end = offsets[end_tok - 1][1]
            if end_tok < n:
                # zarovnat na konec věty v poslední třetině okna
                floor = char_start + (char_end - char_start) * 2 // 3
                for i in range(char_end - 1, floor, -1):
                    if text[i] in self.ENDINGS:
                        char_end = i + 1
                        break
                # další start = první token začínající za řezem
                nxt = start_tok + 1
                while nxt < n and offsets[nxt][0] < char_end:
                    nxt += 1
                end_tok = nxt
            piece = text[char_start:char_end].strip()
            if piece:
                pieces.append(piece)
            if end_tok >= n:
                break
            start_tok = end_tok
        if len(pieces) > 1 and len(pieces[-1]) < 120:
            pieces[-2] = pieces[-2] + " " + pieces[-1]
            pieces.pop()
        return pieces or [text]


def passage_text(doc: dict, passage: str, model_name: str) -> str:
    """Kontextový prefix: dílo › kapitola. Krátký (≤ ~40 tokenů), ale stačí,
    aby vektor 'věděl', odkud pasáž je (Anthropic contextual retrieval)."""
    work = nfc(doc.get("name_cs") or doc.get("work") or "")
    chapter = nfc(doc.get("chapter_path") or "")
    head = " › ".join(x for x in (work[:60], chapter[:60]) if x)
    return format_passage(f"{head}\n{passage}" if head else passage, model_name)


def iter_passages(docs, splitter: Splitter, model_name: str, name_cs: dict[str, str]):
    for d in docs:
        d = dict(d)
        d["name_cs"] = name_cs.get(d.get("work_id"), "")
        for p, piece in enumerate(splitter.split(d["text"])):
            yield {
                "id": f"{d['id']}#{p}",
                "embed_text": passage_text(d, piece, model_name),
                "document": piece,
                "meta": {k: d[k] for k in PASSAGE_META if d.get(k) is not None}
                        | {"chunk_id": d["id"], "seq": d.get("chunk_index", 0), "passage": p},
            }


def load_name_cs(works_file: Path) -> dict[str, str]:
    if not works_file.exists():
        return {}
    return {w["id"]: (w.get("name_cs") or "") for w in read_jsonl(str(works_file))}


# --- glosy z PG ----------------------------------------------------------------

def iter_gloss(dsn: str, model_name: str):
    import psycopg
    with psycopg.connect(dsn) as conn, conn.cursor(name="gloss") as cur:
        cur.itersize = 2000
        cur.execute(
            """SELECT e.chunk_id, e.gloss_cs, e.keywords_cs, e.questions_cs, c.work_id, w.title, w.name_cs,
                      w."group", w.subgroup, c.lang, c.chapter_id, ch.path, c.seq, c.text_sha, e.input_sha
               FROM chunk_enrichment e
               JOIN chunks c ON c.id = e.chunk_id
               JOIN works w ON w.id = c.work_id
               LEFT JOIN chapters ch ON ch.id = c.chapter_id
               WHERE coalesce(e.quality, 3) > 0"""
        )
        for (cid, gloss, kw, qs, wid, title, name_cs, group, subgroup, lang, chid, cpath, seq, sha, isha) in cur:
            text = " ".join(x for x in [gloss, " ".join(kw or []), " ".join(qs or [])] if x)
            head = " › ".join(x for x in ((name_cs or title or "")[:60], (cpath or "")[:60]) if x)
            yield {
                "id": cid,
                "embed_text": format_passage(f"{head}\n{text}", model_name),
                "document": text,
                "meta": {"chunk_id": cid, "work_id": wid, "work": title, "group": group, "subgroup": subgroup,
                         "lang": lang, "chapter_id": chid, "chapter_path": cpath, "seq": seq,
                         "text_sha": sha, "input_sha": isha, "kind": "gloss"},
            }


# --- hlavní smyčka -------------------------------------------------------------

def sync_delete_stale(collection, expected_ids: set[str], work_ids: set[str]) -> int:
    """Smaže z kolekce pasáže děl, které v novém vstupu nejsou."""
    removed = 0
    for wid in sorted(work_ids):
        got = collection.get(where={"work_id": wid}, include=[])
        stale = [i for i in got["ids"] if i not in expected_ids]
        if stale:
            collection.delete(ids=stale)
            removed += len(stale)
    return removed


def main() -> int:
    load_dotenv(Path(__file__).parent / ".env")
    p = argparse.ArgumentParser(description="Embedding knih do ChromaDB")
    p.add_argument("--input", default="books.jsonl")
    p.add_argument("--works", default="works.jsonl", help="kvůli name_cs do prefixu")
    p.add_argument("--source", default="jsonl", choices=["jsonl", "pg"])
    p.add_argument("--dsn", default=os.getenv("PG_DSN"))
    p.add_argument("--chroma-url", default=os.getenv("CHROMA_URL", "http://192.168.88.88:8006"))
    p.add_argument("--collection", default="books_v2")
    p.add_argument("--mode", default="passages", choices=["passages", "chunks"])
    p.add_argument("--max-tokens", type=int, default=450)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--embed-url", default=None)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--device", default="auto")
    p.add_argument("--reset", action="store_true", help="smazat a znovu vytvořit kolekci")
    p.add_argument("--sync", action="store_true", help="smazat pasáže, které už nejsou; přeembedovat změněné")
    p.add_argument("--limit", type=int, default=0, help="jen prvních N chunků (test)")
    args = p.parse_args()

    url = urlparse(args.chroma_url)
    client = chromadb.HttpClient(host=url.hostname, port=url.port or 8000)
    if args.reset:
        try:
            client.delete_collection(args.collection)
            print(f"Kolekce {args.collection} smazána.")
        except Exception:
            pass
    collection = client.get_or_create_collection(args.collection, metadata={"hnsw:space": "cosine"})
    print(f"Kolekce {args.collection}: {collection.count()} existujících dokumentů")

    embedder = make_embedder(args.model, device=args.device, url=args.embed_url)

    if args.source == "pg":
        if not args.dsn:
            print("CHYBA: --source pg potřebuje PG_DSN", file=sys.stderr)
            return 2
        items = iter_gloss(args.dsn, args.model)
    else:
        docs = read_jsonl(args.input)
        if args.limit:
            docs = (d for i, d in enumerate(docs) if i < args.limit)
        if args.mode == "passages":
            splitter = Splitter(args.model, args.max_tokens)
            items = iter_passages(docs, splitter, args.model, load_name_cs(Path(args.works)))
        else:
            items = ({"id": d["id"], "embed_text": passage_text(d, d["text"], args.model), "document": d["text"],
                      "meta": {k: d[k] for k in PASSAGE_META if d.get(k) is not None} | {"chunk_id": d["id"]}}
                     for d in docs)

    added = skipped = replaced = 0
    seen_ids: set[str] = set()
    seen_works: set[str] = set()
    batch: list[dict] = []

    def flush():
        nonlocal added, skipped, replaced
        if not batch:
            return
        ids = [b["id"] for b in batch]
        got = collection.get(ids=ids, include=["metadatas"])
        existing = dict(zip(got["ids"], got["metadatas"] or []))
        fresh = []
        for b in batch:
            old = existing.get(b["id"])
            if old is None:
                fresh.append(b)
            elif args.sync and (old.get("text_sha") != b["meta"].get("text_sha")
                                or old.get("input_sha") != b["meta"].get("input_sha")):
                fresh.append(b)
                replaced += 1
            else:
                skipped += 1
        if fresh:
            embeddings = embedder.encode([b["embed_text"] for b in fresh], batch_size=args.batch_size)
            collection.upsert(
                ids=[b["id"] for b in fresh],
                embeddings=embeddings,
                documents=[b["document"] for b in fresh],
                metadatas=[b["meta"] for b in fresh],
            )
            added += len(fresh)
        print(f"  vloženo {added} (z toho nahrazeno {replaced}), přeskočeno {skipped}", end="\r", flush=True)
        batch.clear()

    for item in items:
        seen_ids.add(item["id"])
        if item["meta"].get("work_id"):
            seen_works.add(item["meta"]["work_id"])
        batch.append(item)
        if len(batch) >= args.batch_size:
            flush()
    flush()

    removed = sync_delete_stale(collection, seen_ids, seen_works) if args.sync else 0
    print(f"\nHotovo: {added} nových/nahrazených, {skipped} už existovalo, {removed} smazaných. "
          f"Kolekce {args.collection} má {collection.count()} dokumentů.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

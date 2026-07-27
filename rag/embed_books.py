#!/usr/bin/env python3
"""Embedding books.jsonl do ChromaDB na JODA. Běží na SPARK (GPU).

Čte JSONL z ingest_books.py, embeduje a zapisuje přes HTTP do Chroma
serveru na NAS (AiStack: deploy/docker-compose.swarm.nas.yaml, port
8006) — žádné SQLite přes síťový mount. Idempotentní: už vložená ID
přeskakuje, takže jde spouštět opakovaně / navázat po přerušení.

Embedding buď lokálně (sentence-transformers, výchozí
multilingual-e5-large), nebo přes AiStack swarm-embed API:
    --embed-url http://localhost:8005/v1 --model intfloat/e5-mistral-7b-instruct

Použití (na SPARK):
    python3 embed_books.py --input books.jsonl --chroma-url http://192.168.88.88:8006
"""

import argparse
import json
import sys
from urllib.parse import urlparse

import chromadb

from embeddings import DEFAULT_MODEL, format_passage, make_embedder

METADATA_KEYS = ("source", "lang", "group", "title", "work", "path", "chunk_index")


def prepare_text(doc: dict, model_name: str) -> str:
    text = f"{doc.get('title', '')}\n{doc['text']}".strip()
    return format_passage(text, model_name)


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


def main() -> int:
    p = argparse.ArgumentParser(description="Embedding knih do ChromaDB")
    p.add_argument("--input", default="books.jsonl")
    p.add_argument("--chroma-url", default="http://192.168.88.88:8006",
                   help="Chroma server na JODA (AiStack swarm.nas)")
    p.add_argument("--collection", default="books")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--embed-url", default=None,
                   help="OpenAI-kompatibilní embeddings API (např. swarm-embed); "
                        "prázdné = lokální sentence-transformers")
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--device", default="auto", help="auto|cuda|mps|cpu")
    p.add_argument("--reset", action="store_true", help="smazat a znovu vytvořit kolekci")
    args = p.parse_args()

    url = urlparse(args.chroma_url)
    client = chromadb.HttpClient(host=url.hostname, port=url.port or 8000)
    if args.reset:
        try:
            client.delete_collection(args.collection)
            print(f"Kolekce {args.collection} smazána.")
        except Exception:
            pass
    collection = client.get_or_create_collection(
        args.collection, metadata={"hnsw:space": "cosine"}
    )
    print(f"Kolekce {args.collection}: {collection.count()} existujících dokumentů")

    embedder = make_embedder(args.model, device=args.device, url=args.embed_url)

    added = skipped = 0
    batch = []

    def flush():
        nonlocal added, skipped
        if not batch:
            return
        ids = [d["id"] for d in batch]
        existing = set(collection.get(ids=ids, include=[])["ids"])
        fresh = [d for d in batch if d["id"] not in existing]
        skipped += len(batch) - len(fresh)
        if fresh:
            texts = [prepare_text(d, args.model) for d in fresh]
            embeddings = embedder.encode(texts, batch_size=args.batch_size)
            collection.upsert(
                ids=[d["id"] for d in fresh],
                embeddings=embeddings,
                documents=[d["text"] for d in fresh],
                metadatas=[
                    {k: d[k] for k in METADATA_KEYS if k in d} for d in fresh
                ],
            )
            added += len(fresh)
        print(f"  vloženo {added}, přeskočeno {skipped}", end="\r")
        batch.clear()

    for doc in read_jsonl(args.input):
        batch.append(doc)
        if len(batch) >= args.batch_size:
            flush()
    flush()

    print(f"\nHotovo: {added} nových chunků, {skipped} už existovalo. "
          f"Kolekce má {collection.count()} dokumentů.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Diagnostika: kolik tokenů embedding modelu mají chunky per tradice.

multilingual-e5-large má max_seq_length 512 — co je nad, se při embedování
tiše uřízne. Skript čte chunky z Chromy (nebo z JSONL) a vypíše per skupinu
medián, 90. percentil a podíl chunků nad limitem. Odtud plynou per-tradice
velikosti chunků a hranice pasáží (`--max-tokens`).

Použití (SPARK, kde je tokenizer v cache):
    HF_HUB_OFFLINE=1 python3 eval/diag_tokens.py --sample 400
    python3 eval/diag_tokens.py --jsonl ../books.jsonl --sample 400
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

from embeddings import DEFAULT_MODEL  # noqa: E402

LIMIT = 512


def from_chroma(url: str, collection: str, sample: int):
    import chromadb
    u = urlparse(url)
    col = chromadb.HttpClient(host=u.hostname, port=u.port or 8000).get_collection(collection)
    groups = set()
    limit, offset = 1000, 0
    while True:
        page = col.get(include=["metadatas"], limit=limit, offset=offset)
        metas = page["metadatas"] or []
        groups.update((m or {}).get("group") for m in metas if (m or {}).get("group"))
        if len(metas) < limit:
            break
        offset += limit
    for g in sorted(groups):
        got = col.get(where={"group": g}, limit=sample, include=["documents", "metadatas"])
        for d, m in zip(got["documents"], got["metadatas"]):
            yield g, m.get("lang"), d


def from_jsonl(path: str, sample: int):
    seen = defaultdict(int)
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            g = r.get("group", "?")
            if seen[g] >= sample:
                continue
            seen[g] += 1
            yield g, r.get("lang"), r["text"]


def main() -> int:
    p = argparse.ArgumentParser(description="Histogram tokenů chunků")
    p.add_argument("--chroma-url", default="http://192.168.88.88:8006")
    p.add_argument("--collection", default="books")
    p.add_argument("--jsonl", help="číst z JSONL místo Chromy")
    p.add_argument("--sample", type=int, default=400, help="chunků na skupinu")
    p.add_argument("--model", default=DEFAULT_MODEL)
    args = p.parse_args()

    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(args.model)

    rows = (from_jsonl(args.jsonl, args.sample) if args.jsonl
            else from_chroma(args.chroma_url, args.collection, args.sample))
    by_group: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for g, _lang, text in rows:
        n_tok = len(tok(text, add_special_tokens=True)["input_ids"])
        by_group[g].append((len(text), n_tok))

    print(f"limit modelu: {LIMIT} tokenů ({args.model})\n")
    print(f"{'skupina':<22}{'n':>5}{'znaků med':>11}{'tok med':>9}{'tok p90':>9}{'tok/znak':>10}{'>512':>8}")
    for g, pairs in sorted(by_group.items()):
        chars = [c for c, _ in pairs]
        toks = [t for _, t in pairs]
        toks_sorted = sorted(toks)
        p90 = toks_sorted[int(0.9 * (len(toks_sorted) - 1))]
        ratio = sum(toks) / max(1, sum(chars))
        over = sum(1 for t in toks if t > LIMIT)
        print(f"{g:<22}{len(pairs):>5}{statistics.median(chars):>11.0f}{statistics.median(toks):>9.0f}"
              f"{p90:>9}{ratio:>10.3f}{100*over/len(pairs):>7.0f}%")
    print("\nznaků na 450 tokenů (doporučená hranice pasáže) per skupina:")
    for g, pairs in sorted(by_group.items()):
        ratio = sum(t for _, t in pairs) / max(1, sum(c for c, _ in pairs))
        print(f"  {g:<22} ~{450/ratio:>5.0f} znaků")
    return 0


if __name__ == "__main__":
    sys.exit(main())

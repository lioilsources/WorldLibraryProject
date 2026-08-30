#!/usr/bin/env python3
"""Retrieval eval — měří kvalitu vyhledávání bez LLM (běží v sekundách).

Zlatý standard v golden.jsonl: {"q", "expect_work"?, "expect_group"?,
"diversity"?, "catalog"?}. expect_work/expect_group smí být string nebo
seznam (stačí shoda s kterýmkoli). Otázky s "catalog": true se přeskakují
(odpovídá na ně katalog v promptu, ne retrieval).

Metriky per otázka i agregát:
  work-hit@k      očekávané dílo je mezi top-k
  group-hit@k     očekávaná skupina je mezi top-k
  distinct@k      počet různých děl v top-k (diverzita)
  d1 / spread     vzdálenost top-1 a rozpětí top-k (diskriminace)

Použití (M2 i SPARK, Chroma na JODA):
    .venv/bin/python3 eval/eval_retrieval.py                # uloží výsledek
    .venv/bin/python3 eval/eval_retrieval.py --compare eval/results/baseline.json
    .venv/bin/python3 eval/eval_retrieval.py --retrieve-mode server  # budoucí režimy
"""

import argparse
import json
import os
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))  # rag/ na path

import chromadb

from embeddings import DEFAULT_MODEL, format_query, make_embedder
from retrieval import (build_alias_index, diversify, looks_tabular, route,
                       route_groups)


def norm(s):
    """Chroma drží jména děl v NFD — přišla z názvů souborů na macOS, kde je
    HFS+/APFS rozkládá. Zlatý standard i ruční katalogy se píšou v NFC, takže
    bez sjednocení by „Milindapañhapāḷi" == „Milindapañhapāḷi" tiše neplatilo
    a eval by hlásil miss tam, kde retrieval trefil."""
    return unicodedata.normalize("NFC", s) if isinstance(s, str) else s


def as_list(v):
    if v is None:
        return []
    return [norm(x) for x in (v if isinstance(v, list) else [v])]


def query(collection, embedding, n_results, works=None, groups=None):
    """Vrací i text chunku — filtr na rejstříky se dělá nad ním."""
    where = None
    if works:
        where = {"work": {"$in": works}}
    elif groups:
        where = {"group": {"$in": groups}}
    r = collection.query(
        query_embeddings=[embedding], n_results=n_results, where=where,
        include=["documents", "metadatas", "distances"],
    )
    return [
        {"work": m.get("work"), "group": m.get("group"), "distance": d, "text": t}
        for t, m, d in zip(r["documents"][0], r["metadatas"][0], r["distances"][0])
    ]


HYBRID_MODES = {
    # režim → kanály hybridního retrieveru (PG + books_v2)
    "vec": ("vec",),
    "fts": ("fts",),
    "hybrid": ("vec", "fts"),
    "hybrid+gloss": ("vec", "gloss", "fts", "fts_cs"),
    "all": ("vec", "gloss", "fts", "fts_cs"),
}


def make_hybrid_retriever(mode, collection, gloss, pool, embedder, model_name, index, top_k, factor, max_per_work,
                          legacy_to_id=None):
    """PG režim: totéž, co server (retriever.Retriever). Vrací funkci se
    stejným rozhraním jako make_retriever, ale hity nesou work_id."""
    from retriever import Plan, Retriever
    r = Retriever(orig=collection, gloss=gloss, pool=pool, embedder=embedder, embed_model=model_name,
                  alias_index=index, channels=HYBRID_MODES[mode], candidate_factor=factor,
                  max_per_work=max_per_work, legacy_to_id=legacy_to_id)

    def retrieve(q, emb):
        hits, routed = r.retrieve(q, top_k, Plan())
        out = [{"work": h["meta"]["work_id"], "group": h["meta"]["group"],
                "distance": h["distance"], "text": h["text"]} for h in hits]
        return out, {"works": routed["works"], "groups": routed["groups"]}

    return retrieve


def make_retriever(mode, collection, index, top_k, factor, max_per_work,
                   known_groups=None):
    """Režimy měří jednotlivé zásahy zvlášť, ať je vidět, co pomohlo:
    plain (dnešek) → route (směrování na dílo) → diverse (strop na dílo)
    → full (obojí, tj. server.py)."""
    routing = mode in ("route", "full")
    diverse = mode in ("diverse", "full")

    def retrieve(q, emb):
        works = route(q, index) if routing else []
        groups = [g for g in (route_groups(q) if routing and not works else [])
                  if known_groups is None or g in known_groups]
        pool = max(top_k, top_k * factor) if diverse else top_k
        hits = query(collection, emb, pool, works, groups)
        if diverse:
            # stejné pořadí jako server.retrieve(): nejdřív pryč s rejstříky,
            # pak teprve strop na dílo
            readable = [h for h in hits if not looks_tabular(h["text"])]
            hits = diversify(readable or hits, top_k, max_per_work)
        return hits[:top_k], {"works": works, "groups": groups}

    return retrieve


def load_catalog(collection, summaries_file):
    """({dílo: český název}, {tradice}) — stejný vstup jako má server."""
    import unicodedata
    summaries = json.loads(Path(summaries_file).read_text(encoding="utf-8"))
    summaries = {unicodedata.normalize("NFC", k): v for k, v in summaries.items()}
    catalog, groups, limit, offset = {}, set(), 1000, 0
    while True:
        page = collection.get(include=["metadatas"], limit=limit, offset=offset)
        metas = page["metadatas"] or []
        for m in metas:
            w = (m or {}).get("work")
            if m and m.get("group"):
                groups.add(m["group"])
            if w and w not in catalog:
                entry = summaries.get(unicodedata.normalize("NFC", w))
                catalog[w] = entry.get("name_cs") if isinstance(entry, dict) else None
        if len(metas) < limit:
            break
        offset += limit
    return catalog, groups


def evaluate(questions, retrieve, embedder, model_name):
    rows = []
    for item in questions:
        if item.get("catalog"):
            continue
        text = format_query(item["q"], model_name)
        emb = embedder.encode([text])[0]
        hits, routed = retrieve(item["q"], emb)

        works = [norm(h["work"]) for h in hits]
        groups = [norm(h["group"]) for h in hits]
        dists = [h["distance"] for h in hits]
        expect_w = as_list(item.get("expect_work"))
        expect_g = as_list(item.get("expect_group"))

        rows.append({
            "q": item["q"],
            "work_hit": any(w in works for w in expect_w) if expect_w else None,
            "group_hit": any(g in groups for g in expect_g) if expect_g else None,
            "distinct": len(set(works)),
            "d1": round(dists[0], 4) if dists else None,
            "spread": round(dists[-1] - dists[0], 4) if len(dists) > 1 else None,
            "routed": len(routed["works"]),
            "routed_groups": routed["groups"],
            "top": [f"{w} @{d:.3f}" for w, d in zip(works, dists)],
        })
    return rows


def aggregate(rows):
    def rate(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    return {
        "questions": len(rows),
        "routed": sum(1 for r in rows if r.get("routed")),
        "routed_group": sum(1 for r in rows if r.get("routed_groups")),
        "work_hit_rate": rate("work_hit"),
        "group_hit_rate": rate("group_hit"),
        "mean_distinct": round(sum(r["distinct"] for r in rows) / len(rows), 2),
        "empty": sum(1 for r in rows if r["d1"] is None),   # otázky bez jediného hitu
        "mean_d1": round(sum(r["d1"] for r in rows if r["d1"] is not None)
                         / max(1, sum(1 for r in rows if r["d1"] is not None)), 4),
        "mean_spread": round(
            sum(r["spread"] for r in rows if r["spread"] is not None)
            / max(1, sum(1 for r in rows if r["spread"] is not None)), 4),
    }


def _load_dotenv() -> None:
    """rag/.env: PG_DSN, CHROMA_URL — stejně jako server."""
    env = Path(__file__).parent.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main():
    _load_dotenv()
    p = argparse.ArgumentParser(description="Retrieval eval")
    p.add_argument("--golden", default=str(Path(__file__).parent / "golden.jsonl"))
    p.add_argument("--chroma-url", default=os.environ.get("CHROMA_URL", "http://192.168.88.88:8006"))
    p.add_argument("--collection", default="books")
    p.add_argument("--embed-model", default=DEFAULT_MODEL)
    p.add_argument("--device", default="auto")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--retrieve-mode", default="plain",
                   choices=["plain", "route", "diverse", "full"] + sorted(HYBRID_MODES))
    p.add_argument("--pg-dsn", default=None, help="PG režim (hybridní režimy); výchozí PG_DSN z rag/.env")
    p.add_argument("--gloss-collection", default="books_gloss")
    p.add_argument("--candidate-factor", type=int, default=4)
    p.add_argument("--max-per-work", type=int, default=2)
    p.add_argument("--summaries-file",
                   default=str(Path(__file__).parent.parent / "summaries.json"))
    p.add_argument("--compare", help="baseline JSON pro delta tabulku")
    p.add_argument("--out", help="cesta výstupu (default results/<ts>.json)")
    p.add_argument("--label", default="", help="poznámka do výsledku (co se měří)")
    args = p.parse_args()

    questions = [json.loads(l) for l in open(args.golden, encoding="utf-8") if l.strip()]
    url = urlparse(args.chroma_url)
    collection = chromadb.HttpClient(host=url.hostname, port=url.port or 8000)\
        .get_collection(args.collection)
    embedder = make_embedder(args.embed_model, device=args.device)

    legacy_to_id = {}
    if args.retrieve_mode in HYBRID_MODES:
        if not args.pg_dsn:
            args.pg_dsn = os.environ.get("PG_DSN")
        if not args.pg_dsn:
            print("CHYBA: hybridní režimy potřebují --pg-dsn (nebo PG_DSN v rag/.env)", file=sys.stderr)
            return 2
        from psycopg_pool import ConnectionPool
        pool = ConnectionPool(args.pg_dsn, min_size=1, max_size=4, open=True)
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, coalesce(work_legacy, title), name_cs, aliases, author_cs FROM works")
            rows_w = cur.fetchall()
        # aliasy: kurátorská tabulka přes dnešní jména + registr; výsledek → work_id
        keys = {legacy: name_cs for _id, legacy, name_cs, _a, _ac in rows_w}
        legacy_to_id = {legacy: _id for _id, legacy, _n, _a, _ac in rows_w}
        base = {a: set(w) for a, w in build_alias_index(keys)}
        for _id, legacy, _n, aliases, author_cs in rows_w:
            for a in (aliases or []) + ([author_cs] if author_cs and len(author_cs) >= 4 else []):
                if len(a) >= 3:
                    base.setdefault(norm(a).lower(), set()).add(legacy)
        from retrieval import fold as _fold
        index_h = sorted(((_fold(a), tuple(sorted(w))) for a, w in base.items()), key=lambda kv: -len(kv[0]))
        try:
            gloss = chromadb.HttpClient(host=url.hostname, port=url.port or 8000).get_collection(args.gloss_collection)
        except Exception:
            gloss = None
        retrieve_h = make_hybrid_retriever(args.retrieve_mode, collection, gloss, pool, embedder, args.embed_model,
                                           index_h, args.top_k, args.candidate_factor, args.max_per_work,
                                           legacy_to_id)

        def retrieve(q, emb):
            hits, routed = retrieve_h(q, emb)
            # route() v retrieveru mapuje na dnešní jména → work_id (jako server)
            return hits, routed

        # zlatý standard je psaný dnešními jmény → převést na work_id
        for item in questions:
            ew = item.get("expect_work")
            if ew:
                item["expect_work"] = [legacy_to_id.get(norm(w), w) for w in as_list(ew)]
    else:
        index, groups = (load_catalog(collection, args.summaries_file)
                         if args.retrieve_mode in ("route", "full") else ({}, None))
        retrieve = make_retriever(
            args.retrieve_mode, collection, build_alias_index(index),
            args.top_k, args.candidate_factor, args.max_per_work, groups,
        )
    rows = evaluate(questions, retrieve, embedder, args.embed_model)
    agg = aggregate(rows)

    result = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": args.label,
        "mode": args.retrieve_mode,
        "top_k": args.top_k,
        "embed_model": args.embed_model,
        "aggregate": agg,
        "rows": rows,
    }

    out = Path(args.out) if args.out else (
        Path(__file__).parent / "results"
        / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n== agregát ({len(rows)} otázek, mode={args.retrieve_mode}, "
          f"top_k={args.top_k}) ==")
    for k, v in agg.items():
        print(f"  {k:18s} {v}")
    print(f"\nvýsledek: {out}")

    misses = [r for r in rows if r["work_hit"] is False]
    if misses:
        print(f"\n== work-miss ({len(misses)}) ==")
        for r in misses:
            print(f"  {r['q'][:60]}")
            for t in r["top"][:3]:
                print(f"      {t}")

    if args.compare:
        base = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print(f"\n== delta vs {args.compare} ({base.get('label') or base['ts']}) ==")
        for k in agg:
            b, n = base["aggregate"].get(k), agg[k]
            if isinstance(b, (int, float)) and isinstance(n, (int, float)):
                sign = "+" if n - b >= 0 else ""
                print(f"  {k:18s} {b} → {n}  ({sign}{round(n - b, 4)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Eval Právníka na úrovni paragrafu — bez LLM, v sekundách.

Zlatý standard eval/golden_law.jsonl:
  {"q", "expect_work": "89/2012 Sb.", "expect_ref": "§ 2079", "area": "kupni",
   "intent": "cite"?, "catalog": true?}

Dvě cesty, přesně jako v serveru:
  cite     dotaz jmenuje ustanovení → cite.parse_citation + cite.lookup (PG, bez vektoru);
           metrika: našel správný předpis A správný § (musí být 100 %, cokoli míň je bug)
  content  hybridní retrieval (retriever.Retriever, kanály podle --mode) → hity;
           metriky: work-hit@k, ref-hit@k (očekávaný § mezi kapitolami top-k hitů)
           — ref-hit@k je při jednom očekávaném § totéž co recall@k

Dva režimy spuštění:

1. **přímý** (vlastní embedder + Chroma) — izoluje kanály (`--mode vec|fts|hybrid`),
   ale načítá e5 na GPU. Na SPARKu, když je volná paměť:

       .venv/bin/python3 eval/eval_law.py --mode vec --top-k 8 --max-per-work 6

2. **přes službu** (`--service`) — content otázky jdou na `GET /search` běžícího
   `law-chat`, takže se měří **přesně produkční konfigurace** a nenačítá se druhý
   embedder (SPARK mívá při nočním obohacení volné jen 2 GB). Jde i z M2 přes LAN;
   cite cesta potřebuje jen PG. Kanály tímhle nelze přepínat — jsou ty serverové:

       .venv/bin/python3 eval/eval_law.py --service http://192.168.88.66:8098 --top-k 8
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

from cite import LawRegistry, lookup, parse_citation  # noqa: E402
from embeddings import DEFAULT_MODEL, make_embedder  # noqa: E402
from retrieval import build_alias_index, fold  # noqa: E402

MODES = {"vec": ("vec",), "fts": ("fts",), "hybrid": ("vec", "fts")}

# „§ 2079 odst. 1" → „§ 2079"; „čl. 10 odst. 1" → „čl. 10" (ref kapitoly je bez odstavce)
REF_HEAD = re.compile(r"^\s*(§|čl\.)\s*(\S+)")


def ref_head(ref_start: str | None) -> str | None:
    """§ z `ref_start` úryvku — bez dotazu do PG (služba chapters.ref neposílá)."""
    m = REF_HEAD.match(ref_start or "")
    return f"{m.group(1)} {m.group(2)}" if m else None


def search_service(base: str, q: str, top_k: int, timeout: float = 90.0) -> tuple[list[dict], dict]:
    """GET /search běžícího serveru — hity v produkční konfiguraci (kanály, max-per-work)."""
    url = f"{base.rstrip('/')}/search?" + urllib.parse.urlencode({"q": q, "top_k": top_k})
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = json.load(r)
    return data.get("hits") or [], data.get("routed") or {}


def load_dotenv() -> None:
    env = Path(__file__).parent.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def alias_index_from_pg(conn):
    """Totéž, co dělá server: kurátorské aliasy + jména z katalogu → work_id."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, coalesce(work_legacy, title), name_cs, aliases FROM works")
        rows = cur.fetchall()
    keys = {legacy: name_cs for _id, legacy, name_cs, _a in rows}
    legacy_to_id = {legacy: _id for _id, legacy, _n, _a in rows}
    base = {a: set(w) for a, w in build_alias_index(keys)}
    for _id, legacy, _n, aliases in rows:
        for a in aliases or []:
            if len(a) >= 3:
                base.setdefault(fold(a), set()).add(legacy)
    index = sorted(((fold(a), tuple(sorted(w))) for a, w in base.items()), key=lambda kv: -len(kv[0]))
    return index, legacy_to_id, {v: k for k, v in legacy_to_id.items()}


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="Eval Právníka (paragrafová úroveň)")
    p.add_argument("--golden", default=str(Path(__file__).parent / "golden_law.jsonl"))
    p.add_argument("--chroma-url", default=os.environ.get("CHROMA_URL", "http://127.0.0.1:8007"))
    p.add_argument("--collection", default="law_v1")
    p.add_argument("--pg-dsn", default=os.environ.get("LAW_PG_DSN"))
    p.add_argument("--registry", default=str(Path(__file__).parent.parent / "registry" / "law" / "tier1.yaml"))
    p.add_argument("--embed-model", default=DEFAULT_MODEL)
    p.add_argument("--device", default="auto")
    p.add_argument("--mode", default="hybrid", choices=sorted(MODES))
    p.add_argument("--service", help="URL běžícího law-chat (content otázky přes GET /search, "
                                     "bez vlastního embedderu; kanály jsou serverové)")
    p.add_argument("--top-k", type=int, default=8)
    p.add_argument("--max-per-work", type=int, default=6)
    p.add_argument("--candidate-factor", type=int, default=4)
    p.add_argument("--no-routing", action="store_true")
    p.add_argument("--out", help="výstup JSON (default eval/results/law_<ts>.json)")
    p.add_argument("--label", default="")
    args = p.parse_args()
    if not args.pg_dsn:
        print("CHYBA: chybí --pg-dsn / LAW_PG_DSN v rag/.env", file=sys.stderr)
        return 2

    from psycopg_pool import ConnectionPool

    questions = [json.loads(l) for l in open(args.golden, encoding="utf-8") if l.strip()]
    pool = ConnectionPool(args.pg_dsn, min_size=1, max_size=4, open=True)
    with pool.connection() as conn:
        index, legacy_to_id, id_to_legacy = alias_index_from_pg(conn)
    registry = LawRegistry(Path(args.registry))
    retriever = None
    if not args.service:
        import chromadb
        from retriever import Plan, Retriever

        url = urlparse(args.chroma_url)
        collection = chromadb.HttpClient(host=url.hostname, port=url.port or 8000).get_collection(args.collection)
        embedder = make_embedder(args.embed_model, device=args.device)
        retriever = Retriever(orig=collection, gloss=None, pool=pool, embedder=embedder, embed_model=args.embed_model,
                              alias_index=index, channels=MODES[args.mode], candidate_factor=args.candidate_factor,
                              max_per_work=args.max_per_work, no_routing=args.no_routing, legacy_to_id=legacy_to_id)

    rows = []
    for item in questions:
        if item.get("catalog"):
            continue
        want_work = legacy_to_id.get(item.get("expect_work"), item.get("expect_work"))
        want_ref = item.get("expect_ref")
        area = item.get("area") or "—"
        if item.get("intent") == "cite":
            cit = parse_citation(item["q"], registry)
            with pool.connection() as conn:
                hits = lookup(conn, cit) if cit else []
            ok = bool(cit) and any(h["work_id"] == want_work for h in hits) and cit.ref == want_ref
            rows.append({"q": item["q"], "kind": "cite", "area": area, "parsed": cit.label if cit else None,
                         "act_hint": cit.act_hint if cit else None, "hit": ok,
                         "top": [f"{id_to_legacy.get(h['work_id'], h['work_id'])} {h['ref_start']}" for h in hits[:3]]})
            continue
        if args.service:
            # Produkční cesta: hity nese server, § se bere z ref_start úryvku.
            hits, routed = search_service(args.service, item["q"], args.top_k)
            pairs = [(legacy_to_id.get(h.get("work"), h.get("work")), ref_head(h.get("ref_start"))) for h in hits]
            dists = [h.get("distance") for h in hits]
        else:
            hits, routed = retriever.retrieve(item["q"], args.top_k, Plan())
            chap_ids = [h["meta"].get("chapter_id") for h in hits]
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT id, ref FROM chapters WHERE id = ANY(%s)", ([c for c in chap_ids if c],))
                refs = dict(cur.fetchall())
            pairs = [(h["meta"].get("work_id"), refs.get(h["meta"].get("chapter_id"))) for h in hits]
            dists = [h["distance"] for h in hits]
        work_hit = any(w == want_work for w, _ in pairs)
        ref_hit = any(w == want_work and r == want_ref for w, r in pairs)
        rank = next((i + 1 for i, (w, r) in enumerate(pairs) if w == want_work and r == want_ref), None)
        rows.append({"q": item["q"], "kind": "content", "area": area,
                     "work_hit": work_hit, "ref_hit": ref_hit, "rank": rank,
                     "routed": routed.get("works"),
                     "top": [f"{id_to_legacy.get(w, w)} {r} @{d:.3f}" for (w, r), d in zip(pairs[:5], dists)]})

    cite_rows = [r for r in rows if r["kind"] == "cite"]
    content = [r for r in rows if r["kind"] == "content"]

    def stats(rs: list[dict]) -> dict:
        c = [r for r in rs if r["kind"] == "content"]
        s = [r for r in rs if r["kind"] == "cite"]
        out = {"n": len(rs)}
        if c:
            out |= {"work_hit": round(sum(r["work_hit"] for r in c) / len(c), 3),
                    "ref_hit": round(sum(r["ref_hit"] for r in c) / len(c), 3),
                    "ref_mrr": round(sum(1 / r["rank"] for r in c if r["rank"]) / len(c), 3)}
        if s:
            out |= {"cite_n": len(s), "cite_hit": round(sum(r["hit"] for r in s) / len(s), 3)}
        return out

    areas = {}
    for r in rows:
        areas.setdefault(r.get("area") or "—", []).append(r)
    summary = {
        "label": args.label, "mode": "service" if args.service else args.mode,
        "service": args.service, "top_k": args.top_k, "max_per_work": args.max_per_work,
        "cite_hit_rate": round(sum(r["hit"] for r in cite_rows) / len(cite_rows), 3) if cite_rows else None,
        "work_hit_rate": round(sum(r["work_hit"] for r in content) / len(content), 3) if content else None,
        "ref_hit_rate": round(sum(r["ref_hit"] for r in content) / len(content), 3) if content else None,
        "ref_mrr": round(sum(1 / r["rank"] for r in content if r["rank"]) / len(content), 3) if content else None,
        "questions": len(rows),
        "by_area": {a: stats(rs) for a, rs in sorted(areas.items())},
    }
    for r in rows:
        mark = "✓" if r.get("hit") or r.get("ref_hit") else ("~" if r.get("work_hit") else "✗")
        print(f"{mark} [{r['kind']:<7}] {r.get('area', '—'):<12} {r['q'][:52]:<52} "
              f"{r.get('parsed') or ''} {r.get('rank') or ''}  {' | '.join(r['top'][:3])}")
    print()
    for a, st in summary["by_area"].items():
        print(f"  {a:<20} n={st['n']:<3} ref-hit={st.get('ref_hit', '—'):<6} mrr={st.get('ref_mrr', '—'):<6} "
              f"cite={st.get('cite_hit', '—')}")
    print("\n" + json.dumps({k: v for k, v in summary.items() if k != "by_area"}, ensure_ascii=False))
    out = Path(args.out) if args.out else Path(__file__).parent / "results" / f"law_{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("uloženo:", out)
    return 0 if (summary["cite_hit_rate"] in (None, 1.0)) else 1


if __name__ == "__main__":
    sys.exit(main())

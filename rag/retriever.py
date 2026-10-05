"""Hybridní retrieval: vektor (originál + glosy) + fulltext (Postgres) →
RRF → filtry → diverzita. Sdílí ho server.py a eval — měří se to, co se
posílá do promptu.

Vstupem je dotaz a volitelný plán (přepis z LLM: termíny v jazyce
tradice, HyDE texty, směrování). Bez plánu se termíny pro fulltext
berou přímo z dotazu (jména a termíny: „Milinda", „nibbána", „無為") —
to je přesně to, kde vektor nad pálí selhává a fulltext zabere.

Výstup má stejný tvar jako dřívější server.retrieve(): seznam hitů
{text, meta, distance} + routed. `meta` nese navíc work_id, name_cs,
chapter_id, chapter_path, score, channels.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cite import ref_span
from embeddings import format_query
from hybrid import dedup_passages, rrf
from pg_search import fts_cs, fts_orig, hydrate, known_groups, neighbors, query_terms, sibling_sections
from retrieval import diversify, looks_tabular, route, route_groups


@dataclass
class Plan:
    """Výstup plánovače (LLM) nebo prázdný plán = dnešní chování."""
    works: list[str] = field(default_factory=list)         # work_id
    groups: list[str] = field(default_factory=list)
    terms_orig: dict[str, list[str]] = field(default_factory=dict)  # lang → termíny
    terms_cs: list[str] = field(default_factory=list)
    hyde_cs: str = ""
    hyde_orig: dict[str, str] = field(default_factory=dict)

    def all_orig_terms(self) -> list[str]:
        out = []
        for ts in self.terms_orig.values():
            out.extend(ts)
        return out


class Retriever:
    def __init__(self, *, orig, gloss, pool, embedder, embed_model: str, alias_index,
                 channels=("vec", "gloss", "fts", "fts_cs"), candidate_factor: int = 4,
                 max_per_work: int = 2, rrf_k: int = 60, weights=None, no_routing: bool = False,
                 context_window: int = 0, legacy_to_id: dict[str, str] | None = None,
                 sibling_window: int = 0, sibling_top: int = 3, terms_map=None):
        self.orig = orig            # Chroma kolekce pasáží (books_v2)
        self.gloss = gloss          # Chroma kolekce glos (books_gloss) nebo None
        self.pool = pool            # psycopg_pool.ConnectionPool
        self.embedder = embedder
        self.embed_model = embed_model
        self.alias_index = alias_index
        self.channels = set(channels)
        self.candidate_factor = candidate_factor
        self.max_per_work = max_per_work
        self.rrf_k = rrf_k
        self.weights = weights or {}
        self.no_routing = no_routing
        self.context_window = context_window
        # Právo: přilehlé § prvních `sibling_top` hitů do kontextu (viz sibling_sections).
        # Jen několik prvních, aby prompt nenarostl osminásobkem úryvků.
        self.sibling_window = sibling_window
        self.sibling_top = sibling_top
        # Mapa laických pojmů (law_terms.TermsMap) nebo None = dnešní chování
        self.terms_map = terms_map
        # route() pracuje s dnešními jmény děl (kurátorské aliasy); filtry
        # v Chromě i PG chtějí work_id — bez převodu by směrování vracelo
        # prázdný výsledek (eval to odhalil: 17 z 23 otázek bez hitu)
        self.legacy_to_id = legacy_to_id or {}
        with self.pool.connection() as conn:
            self.known_groups = known_groups(conn)

    # --- kanály ---------------------------------------------------------------

    def _vec(self, collection, embedding, n, where) -> list[str]:
        r = collection.query(query_embeddings=[embedding], n_results=n, where=where, include=["metadatas"])
        hits = [{"id": i, "chunk_id": (m or {}).get("chunk_id")} for i, m in zip(r["ids"][0], r["metadatas"][0])]
        return dedup_passages(hits)

    def _embed(self, text: str):
        return self.embedder.encode([format_query(text, self.embed_model)])[0]

    # --- hlavní vstup ------------------------------------------------------------

    def retrieve(self, query: str, top_k: int, plan: Plan | None = None, works_override=None):
        plan = plan or Plan()
        # Mapa pojmů: termíny do embeddingu vždy, předpis jen jako záloha, když si
        # ho dotaz nejmenuje sám (aliasy jsou spolehlivější než pojmová mapa).
        embed_query, term_works = self.terms_map.expand(query) if self.terms_map else (query, [])
        works = list(works_override or plan.works)
        if not works and not self.no_routing:
            works = [self.legacy_to_id.get(w, w) for w in route(query, self.alias_index)]
            # předpis z mapy musí jít přes katalog — překlep v yaml by jinak
            # zúžil hledání na neexistující work_id, tj. na prázdný výsledek
            if not works and term_works:
                works = [self.legacy_to_id[w] for w in term_works if w in self.legacy_to_id]
        groups = plan.groups if works == [] else []
        if not works and not groups and not self.no_routing:
            groups = [g for g in route_groups(query) if g in self.known_groups]
        where = None
        if works:
            where = {"work_id": {"$in": works}}
        elif groups:
            where = {"group": {"$in": groups}}

        pool_n = max(top_k, top_k * self.candidate_factor)
        rankings: dict[str, list[str]] = {}

        q_emb = self._embed(embed_query)
        if "vec" in self.channels:
            rankings["vec"] = self._vec(self.orig, q_emb, pool_n * 3, where)   # pasáže → chunky, proto ×3
        if "gloss" in self.channels and self.gloss is not None and self.gloss.count() > 0:
            rankings["gloss"] = self._vec(self.gloss, q_emb, pool_n, where)
        if plan.hyde_cs and self.gloss is not None and self.gloss.count() > 0:
            rankings["hyde"] = self._vec(self.gloss, self._embed(plan.hyde_cs), pool_n, where)
        for lang, text in (plan.hyde_orig or {}).items():
            if text:
                rankings[f"hyde_{lang}"] = self._vec(self.orig, self._embed(text), pool_n * 2, where)

        with self.pool.connection() as conn:
            if "fts" in self.channels:
                terms = plan.all_orig_terms() or query_terms(query)
                if terms:
                    rankings["fts"] = [cid for cid, _ in fts_orig(conn, terms, works, groups, pool_n)]
            if "fts_cs" in self.channels:
                terms_cs = plan.terms_cs or query_terms(query)
                if terms_cs:
                    rankings["fts_cs"] = [cid for cid, _ in fts_cs(conn, terms_cs, works, groups, pool_n)]

            fused = rrf(rankings, k=self.rrf_k, weights=self.weights)
            cand_ids = [cid for cid, _ in fused[: pool_n * 2]]
            rows = hydrate(conn, cand_ids)

            hits = []
            for cid, score in fused:
                row = rows.get(cid)
                if not row:
                    continue
                if (row.get("quality") == 0) or looks_tabular(row["text"]):
                    continue
                meta = {
                    "chunk_id": cid,
                    "work": row["title"], "work_id": row["work_id"], "name_cs": row["name_cs"],
                    # Zákon: pozice je paragraf, ne pořadí chunku — appka čte
                    # z konce title „§ 51 odst. 1–3" stejně jako „část 30/488".
                    "title": (f"{row['title']} ({ref_span(row['ref_start'], row['ref_end'])})"
                              if (row.get("ref_start") or "").startswith(("§", "čl."))
                              else f"{row['title']} (část {row['seq'] + 1}/?)"),
                    "group": row["group"], "subgroup": row["subgroup"], "lang": row["lang"],
                    "lang_original": row["lang_original"], "lang_corpus": row["lang_corpus"],
                    "author": row["author"], "author_cs": row["author_cs"], "edition": row["edition"],
                    "path": row["source_path"], "chapter_id": row["chapter_id"], "chapter_path": row["chapter_path"],
                    "ref_start": row["ref_start"], "ref_end": row["ref_end"], "seq": row["seq"],
                    "score": round(score, 5), "channels": [ch for ch, ids in rankings.items() if cid in ids],
                    "gloss_cs": row.get("gloss_cs"),
                }
                hits.append({"text": row["text"], "meta": meta, "distance": round(1.0 - min(score * 20, 1.0), 4)})
                if len(hits) >= pool_n:
                    break

            hits = diversify(hits, top_k, self.max_per_work, key=lambda h: h["meta"]["work_id"])
            if self.context_window:
                for h in hits:
                    h["neighbors"] = neighbors(conn, h["meta"]["chunk_id"], self.context_window)
            if self.sibling_window:
                seen = {h["meta"].get("chapter_id") for h in hits}
                for h in hits[: self.sibling_top]:
                    sibs = [s for s in sibling_sections(conn, h["meta"].get("work_id"),
                                                        h["meta"].get("chapter_id"), self.sibling_window)
                            if s["id"] not in seen]
                    if sibs:
                        h["siblings"] = sibs
                        seen.update(s["id"] for s in sibs)

        routed = {"works": works, "groups": groups, "channels": sorted(rankings)}
        if embed_query != query:
            routed["terms"] = embed_query[len(query):].strip()
        return hits, routed


def context_block(hits: list[dict]) -> str:
    """Úryvky pro prompt: [i] Dílo › Kapitola (ref):\\ntext."""
    lines = []
    for i, h in enumerate(hits, 1):
        m = h["meta"]
        label = m.get("name_cs") or m.get("work") or "neznámý zdroj"
        where = m.get("chapter_path") or ""
        ref = ""
        if m.get("ref_start"):
            ref = f" ({m['ref_start']}" + (f"–{m['ref_end']}" if m.get("ref_end") and m["ref_end"] != m["ref_start"] else "") + ")"
        translated = ""
        if m.get("lang_original") and m.get("lang_corpus") and m["lang_original"] != m["lang_corpus"]:
            translated = f" [v knihovně: překlad, {m.get('edition') or m['lang_corpus']}]"
        # Zákon: datum znění musí být v kontextu, jinak si ho model vymyslí
        # (pozorováno: „účinné od 1. 1. 2026" u znění ZP z 29. 8. 2026).
        elif (m.get("ref_start") or "").startswith(("§", "čl.")) and m.get("edition"):
            translated = f" [{m['edition']}]"
        head = f"[{i}] {label}" + (f" › {where}" if where else "") + ref + translated
        block = f"{head}:\n{h['text']}"
        # Přilehlé paragrafy (sibling_sections): vlastní řádek s vlastním §, ať je
        # z promptu jasné, že to není text úryvku [i] — model to má citovat podle §.
        for s in h.get("siblings") or []:
            title = f"{s['ref']}" + (f" {s['heading']}" if s.get("heading") else "")
            more = " […]" if s.get("truncated") else ""
            block += f"\n↳ k [{i}] přiléhá {title}:\n{s['text']}{more}"
        lines.append(block)
    return "\n\n".join(lines) if lines else "(nic nenalezeno)"

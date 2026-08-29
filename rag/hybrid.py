"""Fúze kanálů retrievalu — čistá logika bez DB a bez modelu, sdílená
serverem a evalem.

Kanály (každý vrací seřazený seznam chunk_id):
  vec    vektor nad originálem (books_v2, pasáže → dedup na chunk_id)
  gloss  vektor nad českými glosami (books_gloss)
  fts    fulltext nad originálem (Postgres, termíny z dotazu/přepisu)
  fts_cs fulltext nad českým obohacením

Reciprocal Rank Fusion: skóre = Σ w_k / (k + rank). Nepotřebuje
kalibraci vzdáleností mezi kanály (vektorové distance a ts_rank jsou
nesouměřitelné), a chunk, který se objeví ve dvou kanálech, vyhraje nad
chunkem, který je první jen v jednom — přesně to, co chceme u dotazu
česky nad korpusem v pálí.
"""

from __future__ import annotations

from collections import defaultdict

DEFAULT_WEIGHTS = {"vec": 1.0, "gloss": 1.0, "fts": 0.8, "fts_cs": 0.8, "hyde": 0.7}


def dedup_passages(hits: list[dict], key: str = "chunk_id") -> list[str]:
    """Pasáže z Chromy → chunk_id v pořadí nejlepší pasáže (min distance)."""
    seen, out = set(), []
    for h in hits:
        cid = h.get(key) or h.get("id", "").split("#")[0]
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def rrf(rankings: dict[str, list[str]], k: int = 60, weights: dict[str, float] | None = None) -> list[tuple[str, float]]:
    """rankings: {kanál: [chunk_id, …]} → [(chunk_id, skóre)] sestupně."""
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    score: dict[str, float] = defaultdict(float)
    for channel, ids in rankings.items():
        w = weights.get(channel, 1.0)
        for rank, cid in enumerate(ids, 1):
            score[cid] += w / (k + rank)
    return sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))


def channels_hit(rankings: dict[str, list[str]], chunk_id: str) -> list[str]:
    return [ch for ch, ids in rankings.items() if chunk_id in ids]


def combine_rerank(rrf_scores: dict[str, float], rerank: dict[str, float], alpha: float = 0.7) -> list[tuple[str, float]]:
    """final = α·rerank/10 + (1−α)·rrf normalizované na max."""
    if not rrf_scores:
        return []
    top = max(rrf_scores.values()) or 1.0
    out = {cid: alpha * (rerank.get(cid, 0.0) / 10.0) + (1 - alpha) * (s / top) for cid, s in rrf_scores.items()}
    return sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))


def _selftest() -> None:
    r = rrf({"vec": ["a", "b", "c"], "fts": ["c", "d"]}, k=1, weights={"vec": 1, "fts": 1})
    # c: 1/(1+3) + 1/(1+1) = 0.75 ; a: 1/2 = 0.5 ; b: 1/3 ; d: 1/3
    assert [x for x, _ in r][:2] == ["c", "a"], r
    assert abs(dict(r)["c"] - 0.75) < 1e-9
    # deterministické pořadí při shodě skóre (b, d) — podle id
    assert [x for x, _ in r][2:] == ["b", "d"]
    assert dedup_passages([{"id": "x:0001:0000#1"}, {"id": "x:0001:0000#0"}, {"chunk_id": "y"}]) == ["x:0001:0000", "y"]
    assert channels_hit({"vec": ["a"], "fts": ["a", "b"]}, "a") == ["vec", "fts"]
    c = combine_rerank({"a": 0.02, "b": 0.01}, {"b": 9.0, "a": 2.0})
    assert c[0][0] == "b"
    print("hybrid.py: selftest ok")


if __name__ == "__main__":
    _selftest()

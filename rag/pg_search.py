"""Fulltext a katalogové dotazy nad knihovním Postgresem (JODA :5433).

Fulltext běží nad `text_fold` (retrieval.fold(): lowercase, bez diakritiky)
v konfiguraci 'simple' — stejný fold se aplikuje na termíny dotazu, takže
„nibbána", „nibbāna" i „NIBBANA" hledají totéž. Termíny ≥ 4 znaky jdou
s prefixem (`nibban:*` chytá nibbānaṃ, nibbānassa…), kratší přesně.
Čínština: 'simple' parser souvislý čínský text nedělí, proto sloupec
bigramů — termín „無為" se hledá jako bigram(y).

Kanály vracejí [(chunk_id, skóre)] a nic víc; texty a metadata načte až
hydrate() pro finální top-k (hybrid.rrf je slije).
"""

from __future__ import annotations

import re
from typing import Iterable

from retrieval import fold

# česká slova, která do fulltextu nepatří (dotaz „co říká Buddha o utrpení")
STOPWORDS_CS = set("""
a aby ako ale anebo ani ano asi az až bez bude budem budes budeš by byl byla byli bylo byt být ci či clanek článek
co coz což cz da dal dalsi další de den deset devet devět do dnes dobre dobré dobry dobrý docela dva dve dvě
ho hodne hodně i jak jake jaké jako je jeho jej jeji její jejich jen jeste ještě jeste ji jí jina jiná jine jiné
jiz již jsem jses jseš jsi jsme jsou jste k kam kde kdo kdy kdyz když ke ktera která ktere které kteri kteří
kterou ktery který ku ma má mate máte me mě mezi mi mit mít mne mně mnou moc mohl moje muj můj muze může my
na nad nam nám nas nás nasi naši ne nebo nebyl nebyla nebyli nebylo nechce nejsi nejsou nemaji nemají nemam
nemám nemate nemáte nemuze nemůže nez než ni nic nich nim ním nove nové novy nový o od ode on ona oni ono
ony pak po pod podle pokud potom pouze prave právě pred před pres přes pri při pro proc proč proti proto protoze
protože prvni první pta ptá rikat říkat rika říká s se si sice sve své svych svých svym svým svymi svými ta
tak take také takze takže tam tato te tě tebe tebou ted teď tedy tema téma ten tento teto této ti tim tím
timto tímto tipy to tohle toho tohoto tom tomto tomu tomuto toto tu tuto tvuj tvůj ty tyto u uz už v vam vám
vas vás vase vaše ve vedle vice více vsak však vsechen všechen vy z za zda zde ze že
mluvi mluví pise píše soudi soudí uci učí uvadi uvádí popisuje vyklada vykládá text texty knihovna knihovne
knihovně kniha knize knizka knihy knih dilo díle dila kapitola kapitoly kapitole kapitolu kapitol oddil oddíl
verš vers verse cast část časti části
""".split())

CJK_RE = re.compile(r"[㐀-䶿一-鿿]")


def query_terms(text: str, min_len: int = 4) -> list[str]:
    """Foldované termíny z volného textu bez stopslov. Krátké (< min_len)
    zůstávají jen tehdy, když jsou to čísla nebo CJK."""
    out = []
    for tok in re.findall(r"[\wͰ-Ͽἀ-῿㐀-鿿]+", text):
        if CJK_RE.search(tok):
            out.append(tok)
            continue
        f = fold(tok)
        if f in STOPWORDS_CS or (len(f) < min_len and not f.isdigit()):
            continue
        out.append(f)
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _tsquery(terms: Iterable[str], prefix_min: int = 4) -> str:
    parts = []
    for t in terms:
        t = re.sub(r"[^\w]", "", t)
        if not t:
            continue
        parts.append(f"{t}:*" if len(t) >= prefix_min else t)
    return " | ".join(parts)


def _bigram_query(term: str) -> str:
    chars = [c for c in term if CJK_RE.match(c)]
    if len(chars) < 2:
        return ""
    return " & ".join(chars[i] + chars[i + 1] for i in range(len(chars) - 1))


def _scope_sql(works, groups) -> tuple[str, list]:
    if works:
        return " AND c.work_id = ANY(%s)", [list(works)]
    if groups:
        return ' AND w."group" = ANY(%s)', [list(groups)]
    return "", []


def fts_orig(conn, terms: list[str], works=None, groups=None, limit: int = 60) -> list[tuple[str, float]]:
    """Fulltext nad originálním textem chunků. Termíny mohou být v jakémkoli
    jazyce — přepis z plánovače dodá pálijské/řecké/čínské tvary."""
    latin = [t for t in terms if not CJK_RE.search(t)]
    cjk = [t for t in terms if CJK_RE.search(t)]
    results: dict[str, float] = {}
    scope, params = _scope_sql(works, groups)
    with conn.cursor() as cur:
        q = _tsquery(latin)
        if q:
            cur.execute(
                f"""SELECT c.id, ts_rank_cd(c.tsv_fold, q) AS r
                    FROM chunks c JOIN works w ON w.id = c.work_id, to_tsquery('simple', %s) q
                    WHERE c.tsv_fold @@ q{scope}
                    ORDER BY r DESC LIMIT %s""",
                [q, *params, limit],
            )
            for cid, r in cur.fetchall():
                results[cid] = max(results.get(cid, 0.0), float(r))
        for term in cjk:
            bq = _bigram_query(term)
            if not bq:
                continue
            cur.execute(
                f"""SELECT c.id, ts_rank_cd(c.tsv_bigrams, q) AS r
                    FROM chunks c JOIN works w ON w.id = c.work_id, to_tsquery('simple', %s) q
                    WHERE c.tsv_bigrams @@ q{scope}
                    ORDER BY r DESC LIMIT %s""",
                [bq, *params, limit],
            )
            for cid, r in cur.fetchall():
                results[cid] = max(results.get(cid, 0.0), float(r))
    return sorted(results.items(), key=lambda kv: -kv[1])[:limit]


def fts_cs(conn, terms_cs: list[str], works=None, groups=None, limit: int = 60) -> list[tuple[str, float]]:
    """Fulltext nad českým obohacením (glosy, klíčová slova, otázky).
    Hrubé stemování: prefix = prvních max(5, len-3) znaků, protože glosy
    jsou česky a 'simple' nestemuje ('utrpení' ~ 'utrpen:*')."""
    stems = []
    for t in terms_cs:
        f = re.sub(r"[^\w]", "", fold(t))
        if len(f) < 4:
            continue
        stems.append(f[:max(5, len(f) - 3)] + ":*")
    if not stems:
        return []
    scope, params = _scope_sql(works, groups)
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT e.chunk_id, ts_rank_cd(e.tsv_cs, q) AS r
                FROM chunk_enrichment e JOIN chunks c ON c.id = e.chunk_id JOIN works w ON w.id = c.work_id,
                     to_tsquery('simple', %s) q
                WHERE e.tsv_cs @@ q AND coalesce(e.quality, 3) > 0{scope}
                ORDER BY r DESC LIMIT %s""",
            [" | ".join(stems), *params, limit],
        )
        return [(cid, float(r)) for cid, r in cur.fetchall()]


HYDRATE_SQL = """
SELECT c.id, c.text, c.work_id, w.title, w.name_cs, w."group", w.subgroup, c.lang, w.lang_original,
       w.lang_corpus, w.author, w.author_cs, w.edition, c.chapter_id, ch.path, ch.heading_cs, c.ref_start, c.ref_end,
       c.seq, c.seq_in_chapter, e.quality, e.gloss_cs, w.source_path
FROM chunks c
JOIN works w ON w.id = c.work_id
LEFT JOIN chapters ch ON ch.id = c.chapter_id
LEFT JOIN chunk_enrichment e ON e.chunk_id = c.id
WHERE c.id = ANY(%s)
"""


def hydrate(conn, chunk_ids: list[str]) -> dict[str, dict]:
    """Text + metadata pro finální výběr; klíč chunk_id."""
    if not chunk_ids:
        return {}
    cols = ["id", "text", "work_id", "title", "name_cs", "group", "subgroup", "lang", "lang_original",
            "lang_corpus", "author", "author_cs", "edition", "chapter_id", "chapter_path", "heading_cs",
            "ref_start", "ref_end", "seq", "seq_in_chapter", "quality", "gloss_cs", "source_path"]
    with conn.cursor() as cur:
        cur.execute(HYDRATE_SQL, [list(chunk_ids)])
        return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


def neighbors(conn, chunk_id: str, window: int = 1) -> list[dict]:
    """Sousední chunky téže kapitoly (±window) — parent–child kontext."""
    with conn.cursor() as cur:
        cur.execute("SELECT chapter_id, seq_in_chapter FROM chunks WHERE id = %s", (chunk_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            return []
        chid, k = row
        cur.execute(
            """SELECT id, text, seq_in_chapter FROM chunks
               WHERE chapter_id = %s AND seq_in_chapter BETWEEN %s AND %s AND id <> %s
               ORDER BY seq_in_chapter""",
            (chid, k - window, k + window, chunk_id),
        )
        return [{"id": i, "text": t, "seq_in_chapter": s} for i, t, s in cur.fetchall()]


def known_groups(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute('SELECT DISTINCT "group" FROM works')
        return {r[0] for r in cur.fetchall()}


def status(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM enrichment_status_v")
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], row))

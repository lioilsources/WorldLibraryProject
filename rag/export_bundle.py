#!/usr/bin/env python3
"""Export díla z knihovního Postgresu do bundlu pro Kindlify.

Kindlify (Flutter čtečka) čte jedno dílo jako strom uzlů: `manifest`
(hierarchie kapitol), `words` (termy pro word cloud, který slouží jako
filtr) a `summaries` (předpočítané souhrny per uzel × locale). Přesně to
už v Postgresu je — `works`/`chapters` nesou souhrny a české nadpisy,
`chunk_enrichment` klíčová slova a entity. Tenhle skript to jen přerovná:
žádný LLM, žádné GPU. Obohacení musí proběhnout dřív (enrich_chunks →
enrich_chapters → enrich_works), jinak vypadne bundle s prázdným cloudem.

    python3 export_bundle.py --work zh.daodejing --out build/bundles
    python3 export_bundle.py --all --priority 1 --out build/bundles
    python3 export_bundle.py --work zh.daodejing --stdout --pretty

Soubor se jmenuje `{slug}.json` s podtržítky (`zh_daodejing.json`), tedy
přesně tak, jak ho hledá `BundleLoader.ensureFresh()` v assetech Kindlify.

Formát je **kontrakt** s Dart modelem `Kindlify/lib/core/models/bundle.dart`
(`BookBundle.fromJson`) — `validate_bundle()` drží jeho povinná pole, aby
se rozchod poznal tady, a ne až pádem importu v telefonu.

Čeho si všimnout na výstupu:

  * `byteStart`/`byteEnd` jsou 0. Postgres drží text v chuncích, ne offsety
    do původního souboru, a odhadovat je z `char_count` by lhalo (bajty ≠
    znaky). Až bude čtenářský mód, přibalí se text chunků, ne offsety.
  * locale je jen `cs`. Knihovna drží originály a česky je až výstup
    (viz CLAUDE.md); Dart si s chybějícím `en` poradí fallbackem.
  * `score` termu je TF-IDF přes uzly díla, normalizované na 0.3–1.0 —
    Kindlify z něj počítá velikost bubliny (`lerp(12, 32, score)`), takže
    nula by znamenala nečitelný term, ne „slabý".

psycopg se importuje až v `main()`, aby čistá logika (a její testy) běžela
i tam, kde ovladač Postgresu není.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from catalog import query_chapters, query_works, summary_for  # noqa: E402

SCHEMA_VERSION = "1.0"
EXPORT_VERSION = "pg-1"      # zvednout při změně formátu; appka podle pipelineVersion reimportuje

# Písmo textu v bundlu — řídí se jazykem KORPUSU (labels a souhrny), ne
# jazykem díla: Avesta je v korpusu anglicky, takže 'latin'. Termy z
# `keywords_orig` můžou být v původním písmu i tak, proto appka potřebuje
# Noto fallback řetěz nezávisle na téhle hodnotě.
SCRIPT_BY_LANG = {
    "lzh": "han", "zh": "han",
    "sa": "devanagari", "hi": "devanagari",
    "he": "hebrew",
    "grc": "greek", "el": "greek",
    "ae": "avestan", "egy": "egyptian",
    "pi": "latin", "lat": "latin", "en": "latin", "de": "latin",
    "ang": "latin", "non": "latin", "cs": "latin",
}

MAX_TERM_LEN = 40
SCORE_FLOOR = 0.3            # nejmenší bublina zůstane čitelná
TOP_TERMS = 50               # kolik termů na uzel (plán Kindlify: top 50)


# --- pomocné čisté funkce -------------------------------------------------------

def slugify(work_id: str) -> str:
    """`zh.daodejing` → `zh-daodejing`. Slug je v Kindlify klíč knihy i
    prefix ID uzlů; tečka a dvojtečka by se pletly s `node://` odkazy."""
    out = "".join(c if c.isalnum() else "-" for c in work_id.lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def asset_name(slug: str) -> str:
    """Jméno souboru pro assety Kindlify (loader mapuje `-` ↔ `_`)."""
    return slug.replace("-", "_") + ".json"


def node_id(ordinal: int) -> str:
    """ID uzlu v bundlu. Ordinal je v PG unikátní v rámci díla a plošný
    přes úrovně, takže stačí on — `chapters.id` nese ještě work_id, který
    by se v bundlu jen opakoval (a loader si slug připojí sám)."""
    return f"c{ordinal:04d}"


def chapter_label(ch: dict) -> str:
    """Český nadpis, když je; jinak originální; jinak aspoň citace edice.
    `ref` se předřazuje jen tehdy, když v nadpisu ještě není — u čínských
    kapitol je číslo součástí nadpisu (學而第一)."""
    heading = (ch.get("heading_cs") or ch.get("heading") or "").strip()
    ref = (ch.get("ref") or "").strip()
    if not heading:
        return f"Kapitola {ref}" if ref else f"Kapitola {ch['ordinal']}"
    if ref and ref not in heading:
        return f"{ref} — {heading}"
    return heading


def node_kind(level: int) -> str:
    """Úroveň PG → `kind` v Kindlify (book | chapter | section | paragraph)."""
    return {1: "chapter", 2: "section"}.get(level, "paragraph")


def norm_term(term: str) -> str | None:
    """NFC, oříznuté, bez balastu. None = term do cloudu nepatří.
    Apostrof se neodřezává — je součástí české transkripce (Lao-c', Čuang-c')."""
    t = unicodedata.normalize("NFC", (term or "").strip().strip(",.;:!?\"“”„()[]"))
    if not t or len(t) > MAX_TERM_LEN or t.isdigit():
        return None
    return t


def build_tree(work: dict, chapters: list[dict]) -> dict:
    """Kořen = dílo, děti = kapitoly zavěšené podle `parent_id`. Kapitola
    s neznámým rodičem (rozbitý ingest) se pověsí na kořen, ať z bundlu
    nevypadne — v Kindlify by pak nešla najít vůbec."""
    by_id = {ch["id"]: ch for ch in chapters}
    nodes = {
        ch["id"]: {
            "id": node_id(ch["ordinal"]),
            "kind": node_kind(ch.get("level") or 1),
            "label": chapter_label(ch),
            "byteStart": 0,
            "byteEnd": 0,
            "children": [],
        }
        for ch in chapters
    }
    root = {
        "id": "root",
        "kind": "book",
        "label": work.get("name_cs") or work.get("title") or work["id"],
        "byteStart": 0,
        "byteEnd": 0,
        "children": [],
    }
    for ch in sorted(chapters, key=lambda c: c["ordinal"]):
        parent_id = ch.get("parent_id")
        parent = nodes[parent_id] if parent_id in by_id and parent_id != ch["id"] else root
        parent["children"].append(nodes[ch["id"]])
    return root


def walk_nodes(node: dict):
    yield node
    for child in node["children"]:
        yield from walk_nodes(child)


# --- termy ----------------------------------------------------------------------

def chunk_terms(row: dict) -> set[tuple[str, str]]:
    """Termy jednoho chunku jako množina (term, kind) — opakování uvnitř
    chunku se nepočítá, `count` v bundlu je počet chunků, ne výskytů.
    `quality == 0` je podle schématu balast (patička, rejstřík) a vyhazuje
    ho i retrieval, takže do cloudu nepatří."""
    if row.get("quality") == 0:
        return set()
    out: set[tuple[str, str]] = set()
    for term in row.get("keywords_cs") or []:
        if (t := norm_term(term)):
            out.add((t, "word"))
    for term in row.get("keywords_orig") or []:
        if (t := norm_term(term)):
            out.add((t, "orig"))
    for ent in row.get("entities") or []:
        name = ent.get("name") if isinstance(ent, dict) else ent
        if (t := norm_term(name or "")):
            out.add((t, "entity"))
    return out


def own_counts(chunk_rows: list[dict]) -> Counter:
    """Chunky jednoho uzlu → Counter[(term, kind)] = v kolika chuncích je."""
    counts: Counter = Counter()
    for row in chunk_rows:
        counts.update(chunk_terms(row))
    return counts


def totals_bottom_up(tree: dict, per_node: dict[str, Counter]) -> dict[str, Counter]:
    """Termy uzlu = jeho vlastní chunky + všechno pod ním. Bez toho by
    vnitřní kapitoly (Mahábhárata: parva → sekce) měly prázdný cloud —
    chunky visí na listech."""
    totals: dict[str, Counter] = {}

    def walk(node: dict) -> Counter:
        acc = Counter(per_node.get(node["id"], Counter()))
        for child in node["children"]:
            acc.update(walk(child))
        totals[node["id"]] = acc
        return acc

    walk(tree)
    return totals


def idf_over_nodes(totals: dict[str, Counter], root_id: str = "root") -> dict[str, float]:
    """IDF přes uzly díla (kořen se nepočítá, obsahuje všechno). Term ve
    všech kapitolách („said", „king") tak spadne pod term, který dělá jednu
    kapitolu zvláštní — to je celý smysl cloudu jako filtru."""
    nodes = [c for nid, c in totals.items() if nid != root_id]
    n = len(nodes)
    if n == 0:
        return {}
    df: Counter = Counter()
    for counts in nodes:
        for term, _kind in counts:
            df[term] += 1
    return {term: math.log(1 + n / (1 + d)) for term, d in df.items()}


def score_terms(counts: Counter, idf: dict[str, float], *, top: int = TOP_TERMS,
                boost: list[str] | None = None) -> list[dict]:
    """Counter → seřazený seznam termů pro bundle. Skóre je podíl vůči
    nejsilnějšímu termu uzlu (ne absolutní TF-IDF) — Kindlify z něj dělá
    velikost bubliny, takže musí být srovnatelné napříč uzly.
    `boost` jsou kurátorská klíčová slova díla/kapitoly: jdou navrch."""
    boosted = {t for t in (norm_term(b) for b in (boost or [])) if t}
    ranked = []
    for (term, kind), count in counts.items():
        weight = count * idf.get(term, 1.0)
        ranked.append((0 if term in boosted else 1, -weight, term, kind, count))
    ranked.sort()

    # Termy z boostu, které v chuncích vůbec nejsou (např. dílo bez obohacení).
    seen = {term for _, _, term, _, _ in ranked}
    for term in boosted - seen:
        ranked.insert(0, (0, 0.0, term, "word", 1))

    ranked = ranked[:top]
    if not ranked:
        return []
    top_weight = max((-w for _, w, _, _, _ in ranked), default=0.0) or 1.0
    out = []
    for is_plain, neg_weight, term, kind, count in ranked:
        raw = (-neg_weight / top_weight) if top_weight else 0.0
        score = 1.0 if is_plain == 0 else max(SCORE_FLOOR, round(raw, 2))
        out.append({"term": term, "score": round(score, 2), "count": int(count), "kind": kind})
    out.sort(key=lambda t: (-t["score"], t["term"]))
    return out


def build_words(tree: dict, per_node: dict[str, Counter], *, work_keywords: list[str],
                chapter_keywords: dict[str, list[str]], top: int = TOP_TERMS) -> dict:
    totals = totals_bottom_up(tree, per_node)
    idf = idf_over_nodes(totals)
    nodes = {}
    for node in walk_nodes(tree):
        nid = node["id"]
        boost = work_keywords if nid == "root" else chapter_keywords.get(nid, [])
        terms = score_terms(totals.get(nid, Counter()), idf, top=top, boost=boost)
        if terms:
            nodes[nid] = {"terms": terms}
    return {"nodes": nodes}


# --- souhrny --------------------------------------------------------------------

def build_summaries(work: dict, chapters: list[dict], *, root_detail: str = "long",
                    chapter_detail: str = "medium") -> dict:
    """`{nodeId: {"cs": text}}`. Uzel bez souhrnu se vynechá — Kindlify si
    vytáhne souhrn nejbližšího předka a řekne to (`_summaryWithFallback`)."""
    out: dict[str, dict[str, str]] = {}
    if (text := summary_for(work, root_detail)):
        out["root"] = {"cs": text}
    for ch in chapters:
        if (text := summary_for(ch, chapter_detail)):
            out[node_id(ch["ordinal"])] = {"cs": text}
    return out


# --- bundle ---------------------------------------------------------------------

def content_digest(tree: dict, words: dict, summaries: dict) -> str:
    """Otisk obsahu → `pipelineVersion`. `BundleLoader.ensureFresh()`
    reimportuje knihu, právě když se pipelineVersion změní, takže se
    doobohacené dílo dostane do appky, a pouhý re-export beze změny ne
    (`generatedAt` se mění pokaždé, proto se do otisku nepočítá)."""
    payload = json.dumps([tree, words, summaries], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]


def build_bundle(work: dict, chapters: list[dict], chunk_rows: dict[int, list[dict]], *,
                 work_keywords: list[str] | None = None,
                 chapter_keywords: dict[int, list[str]] | None = None,
                 top: int = TOP_TERMS, chapter_detail: str = "medium",
                 generated_at: str | None = None) -> dict:
    """Řádky z Postgresu → hotový bundle. `chunk_rows` a `chapter_keywords`
    jsou klíčované ordinálem kapitoly (None = chunk mimo kapitoly)."""
    tree = build_tree(work, chapters)
    per_node = {node_id(o): own_counts(rows) for o, rows in chunk_rows.items() if o is not None}
    orphan = chunk_rows.get(None)
    if orphan:                       # chunky bez kapitoly patří dílu jako celku
        per_node["root"] = own_counts(orphan)
    words = build_words(
        tree, per_node,
        work_keywords=work_keywords or [],
        chapter_keywords={node_id(o): kw for o, kw in (chapter_keywords or {}).items()},
        top=top,
    )
    summaries = build_summaries(work, chapters, chapter_detail=chapter_detail)
    lang = work.get("lang_corpus") or work.get("lang_original") or ""
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "slug": slugify(work["id"]),
        "title": work.get("name_cs") or work.get("title") or work["id"],
        "sourceLanguage": work.get("lang_original") or lang,
        "script": SCRIPT_BY_LANG.get(lang, "latin"),
        "pipelineVersion": f"{EXPORT_VERSION}+{content_digest(tree, words, summaries)}",
        "generatedAt": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tree": tree,
    }
    return {"manifest": manifest, "words": words, "summaries": summaries}


def validate_bundle(bundle: dict) -> None:
    """Kontrakt s `BookBundle.fromJson` (Kindlify/lib/core/models/bundle.dart).
    Dart tam přetypovává natvrdo, takže chybějící klíč nebo špatný typ =
    výjimka při importu v telefonu. Ať to spadne radši tady."""
    def need(cond, msg):
        if not cond:
            raise ValueError(f"bundle nesplňuje kontrakt Kindlify: {msg}")

    for key in ("manifest", "words", "summaries"):
        need(isinstance(bundle.get(key), dict), f"chybí objekt '{key}'")
    m = bundle["manifest"]
    for key in ("schemaVersion", "slug", "title", "sourceLanguage", "script"):
        need(isinstance(m.get(key), str) and m[key], f"manifest.{key} musí být neprázdný text")
    for key in ("pipelineVersion", "generatedAt"):
        need(isinstance(m.get(key), str), f"manifest.{key} musí být text")
    need(isinstance(m.get("tree"), dict), "manifest.tree chybí")

    ids: set[str] = set()
    def check_node(node, path):
        need(isinstance(node, dict), f"{path} není objekt")
        need(isinstance(node.get("id"), str) and node["id"], f"{path}.id musí být neprázdný text")
        need(node["id"] not in ids, f"duplicitní id uzlu '{node['id']}'")
        ids.add(node["id"])
        for key in ("kind", "label"):
            need(isinstance(node.get(key), str) and node[key], f"{path}.{key} musí být neprázdný text")
        for key in ("byteStart", "byteEnd"):
            need(isinstance(node.get(key), int), f"{path}.{key} musí být celé číslo")
        need(isinstance(node.get("children"), list), f"{path}.children musí být seznam")
        for i, child in enumerate(node["children"]):
            check_node(child, f"{path}.children[{i}]")
    check_node(m["tree"], "manifest.tree")

    nodes = bundle["words"].get("nodes")
    need(isinstance(nodes, dict), "words.nodes musí být objekt")
    for nid, entry in nodes.items():
        need(nid in ids, f"words.nodes['{nid}'] ukazuje na neznámý uzel")
        need(isinstance(entry, dict) and isinstance(entry.get("terms"), list),
             f"words.nodes['{nid}'].terms musí být seznam")
        for i, term in enumerate(entry["terms"]):
            where = f"words.nodes['{nid}'].terms[{i}]"
            need(isinstance(term.get("term"), str) and term["term"], f"{where}.term prázdný")
            need(isinstance(term.get("score"), (int, float)) and not isinstance(term.get("score"), bool),
                 f"{where}.score musí být číslo")
            need(isinstance(term.get("count"), int) and not isinstance(term["count"], bool),
                 f"{where}.count musí být celé číslo")
            need(isinstance(term.get("kind"), str) and term["kind"], f"{where}.kind prázdný")
    for nid, locales in bundle["summaries"].items():
        need(nid in ids, f"summaries['{nid}'] ukazuje na neznámý uzel")
        need(isinstance(locales, dict), f"summaries['{nid}'] musí být objekt locale → text")
        for loc, text in locales.items():
            need(isinstance(loc, str) and isinstance(text, str), f"summaries['{nid}']['{loc}'] musí být text")


# --- Postgres -------------------------------------------------------------------

def fetch_keywords(conn, work_id: str) -> tuple[list[str], dict[int, list[str]]]:
    """Kurátorská/LLM klíčová slova díla a kapitol. `catalog.query_*` je
    nevrací — jeho sloupce slouží kontextu pro LLM, ne cloudu."""
    with conn.cursor() as cur:
        cur.execute("SELECT keywords_cs FROM works WHERE id = %s", (work_id,))
        row = cur.fetchone()
        work_kw = list(row[0]) if row and row[0] else []
        cur.execute("SELECT ordinal, keywords_cs FROM chapters WHERE work_id = %s ORDER BY ordinal",
                    (work_id,))
        chapter_kw = {o: list(kw or []) for o, kw in cur.fetchall()}
    return work_kw, chapter_kw


def fetch_chunk_rows(conn, work_id: str) -> dict[int, list[dict]]:
    """Obohacení chunků seskupené podle kapitoly (ordinal; None = chunk
    mimo kapitoly, což u nedetekované struktury není výjimka)."""
    out: dict[int, list[dict]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ch.ordinal, ce.keywords_cs, ce.keywords_orig, ce.entities, ce.quality
            FROM chunks c
            JOIN chunk_enrichment ce ON ce.chunk_id = c.id
            LEFT JOIN chapters ch ON ch.id = c.chapter_id
            WHERE c.work_id = %s
            ORDER BY c.seq
            """,
            (work_id,),
        )
        for ordinal, kw_cs, kw_orig, entities, quality in cur.fetchall():
            out.setdefault(ordinal, []).append({
                "keywords_cs": kw_cs or [], "keywords_orig": kw_orig or [],
                "entities": entities or [], "quality": quality,
            })
    return out


def export_work(conn, work: dict, *, top: int, chapter_detail: str) -> dict:
    chapters = query_chapters(conn, work["id"])
    work_kw, chapter_kw = fetch_keywords(conn, work["id"])
    bundle = build_bundle(
        work, chapters, fetch_chunk_rows(conn, work["id"]),
        work_keywords=work_kw, chapter_keywords=chapter_kw,
        top=top, chapter_detail=chapter_detail,
    )
    validate_bundle(bundle)
    return bundle


def report(bundle: dict) -> str:
    """Jednořádkový přehled — ať je z běhu vidět, kde chybí obohacení."""
    m, nodes = bundle["manifest"], bundle["words"]["nodes"]
    total = sum(1 for _ in walk_nodes(m["tree"]))
    terms = sum(len(n["terms"]) for n in nodes.values())
    gaps = total - len(bundle["summaries"])
    note = f", bez souhrnu {gaps}" if gaps else ""
    empty = total - len(nodes)
    note += f", bez termů {empty}" if empty else ""
    return (f"{m['slug']:28s} uzlů {total:5d}  termů {terms:6d}  "
            f"souhrnů {len(bundle['summaries']):5d}{note}  {m['pipelineVersion']}")


def main() -> int:
    p = argparse.ArgumentParser(description="Export děl z Postgresu do bundlů pro Kindlify")
    p.add_argument("--work", action="append", default=[], help="work_id (lze opakovat)")
    p.add_argument("--all", action="store_true", help="všechna díla do priority --priority")
    p.add_argument("--priority", type=int, default=1, help="s --all: díla s prioritou ≤ N (výchozí 1 = kanonická)")
    p.add_argument("--out", type=Path, help="adresář pro {slug}.json")
    p.add_argument("--stdout", action="store_true", help="vypsat bundle na stdout místo do souboru")
    p.add_argument("--pretty", action="store_true", help="odsazený JSON (čitelný, ale větší)")
    p.add_argument("--top-terms", type=int, default=TOP_TERMS, help=f"termů na uzel (výchozí {TOP_TERMS})")
    p.add_argument("--chapter-detail", default="medium", choices=("short", "medium", "long"),
                   help="délka souhrnu kapitoly (výchozí medium ~50 slov)")
    p.add_argument("--dsn", default=os.getenv("PG_DSN"))
    args = p.parse_args()

    if not args.work and not args.all:
        print("CHYBA: zadej --work ID nebo --all", file=sys.stderr)
        return 2
    if not args.stdout and not args.out:
        print("CHYBA: zadej --out ADRESÁŘ nebo --stdout", file=sys.stderr)
        return 2
    if not args.dsn:
        print("CHYBA: chybí --dsn / PG_DSN", file=sys.stderr)
        return 2

    import psycopg  # až tady: čistá logika i testy běží bez ovladače

    dumps = (lambda b: json.dumps(b, ensure_ascii=False, indent=1)) if args.pretty else \
            (lambda b: json.dumps(b, ensure_ascii=False, separators=(",", ":")))

    with psycopg.connect(args.dsn) as conn:
        works = query_works(conn, work_ids=args.work or None,
                            hide_priority=None if args.work else args.priority + 1)
        if not works:
            print("CHYBA: žádné dílo nesedí na zadání", file=sys.stderr)
            return 1
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
        for work in works:
            bundle = export_work(conn, work, top=args.top_terms, chapter_detail=args.chapter_detail)
            if args.stdout:
                print(dumps(bundle))
            else:
                path = args.out / asset_name(bundle["manifest"]["slug"])
                path.write_text(dumps(bundle), encoding="utf-8")
                size = path.stat().st_size / 1024
                print(f"{report(bundle)}  → {path} ({size:.0f} kB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

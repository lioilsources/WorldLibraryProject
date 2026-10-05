#!/usr/bin/env python3
"""Ingest zákonů z e-Sbírky do JSONL — tentýž kontrakt jako ingest_books.py
(works.jsonl, chapters.jsonl, books.jsonl), takže load_pg.py, embed_books.py
i server.py jedou beze změny. Plán a ověření zdrojů: Ol1nLLM/docs/plan-pravnik.md.

Zdroj je nekeyovaná cache REST e-Sbírky (https://e-sbirka.gov.cz/sbr-cache):
  GET /dokumenty-sbirky/{staleUrl}                    → metadata aktuálního znění
  GET /dokumenty-sbirky/{staleUrl+datum}/fragmenty?cisloStranky=N
                                                       → 1 000 fragmentů/stránka
Stránky jsou **0-based** a jejich surové pořadí napříč stránkami je pořadí
dokumentu (ověřeno 23. 9. 2026 na NOZ: 10 472 fragmentů, 3 106 §, § 1 → § 3081,
nula poklesů čísla §; id fragmentů naopak pořadí nedrží — vložené novelou mají
vyšší id). Každá stažená stránka se uloží na disk (law/cache/…), druhý běh
nesahá na síť.

Struktura fragmentu: `eli` nese celou hierarchii
(…/norma/cast_1/hlava_2/dil_3/oddil_3/pododdil_2/par_311/odst_1/pism_a), takže
strom se staví z ELI (rodič = ELI bez posledního segmentu), ne z `hloubka`.

Mapování na knihovní schéma:
  work     předpis, id cz.sb.{rok}.{cislo}; name_cs = úplná citace z e-Sbírky
  chapter  hierarchie (Část 1 › Hlava 2 › Díl 3 › Oddíl 4 › Pododdíl 5) a každý
           § / čl. jako leaf (level 6); preambule a přílohy jako level 1
  chunk    text § (≤ CHUNK znaků); delší § se dělí po odstavcích a každý kus
           dostane prefix „§ N Nadpis", aby fulltext i výpis v UI věděly,
           odkud je. ref_start/ref_end = odstavce, title = „89/2012 Sb. (§ 2079
           odst. 1–3)" — tvar, ze kterého appka bez změny vyčte pozici.

Použití:
    python3 ingest_law.py --tier 1 --out-dir law
    python3 ingest_law.py --acts 89/2012,262/2006 --stats-only
    python3 ingest_law.py --tier 1 --offline        # jen z cache, bez sítě
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import statistics
import sys
import time
import unicodedata
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from chunking import chunk_text  # noqa: E402
from ingest_books import Writer  # noqa: E402

CACHE_BASE = "https://e-sbirka.gov.cz/sbr-cache"
USER_AGENT = "WorldLibraryProject-ingest-law/1.0 (+https://github.com/lioilsources)"
REQUEST_DELAY = 0.7        # s mezi voláními — cache MV nemá podporu ani zveřejněný limit
RETRY_BACKOFF = (5, 10, 20, 40, 80)

# Chunk pro češtinu: ~0,3 tokenu/znak u e5 → 1 500 znaků ≈ 450 tokenů, jako
# CHUNK_BY_LANG pro en/de. Překryv tu není: hranice jsou odstavce, ne věty.
CHUNK = 1500
MIN_CHUNK = 40             # „§ 123 (zrušen)" je platný, krátký chunk

HIERARCHY = {"Cast": 1, "Hlava": 2, "Dil": 3, "Oddil": 4, "Pododdil": 5}
LEAF = {"Paragraf", "Clanek"}
LEAF_LEVEL = 6
UNIT_START = {"Odstavec_Dc"}
UNIT_TEXT = {"Pismeno_Lb", "Bod_Dd", "Bod_Ld", "Bod_Rd", "Odrazka_Rb", "Pokracovani_Text",
             "Tabulka", "Clanek_text",
             # odrážky a položky ve výčtech (daňové a účetní zákony), poznámky
             # uvnitř §, vazby „vztah k …" — text patří k odstavci, ve kterém stojí
             "Uni_Odrazka", "Pred_Odrazka", "Po_Odrazka", "Polozka", "Poznamka", "Vztah_K", "PPC", "Nadpis",
             } | {f"Bod_Viceurovnovy_L{i}" for i in range(1, 10)}
HEADING_BELOW = {"Nadpis_pod"}
HEADING_ABOVE = {"Nadpis_nad", "Nadpis"}
PREAMBLE = {"Preambule"}
ANNEX_HEAD = {"Hlavicka_priloha"}
# Bez textu, jen obal nebo hlavička dokumentu — projde se skrz, nic se nepíše.
SKIP_PREFIXES = ("Virtual_", "Prefix", "Postfix", "PPC", "DD_", "Eu_", "EU_", "FS")
SKIP_EXACT = {"Block_Priloha", "Block_Priloha_Norma", "Block_Priloha_Rozvolnena", "Block_Priloha_Striktni",
              "Block_Priloha_Strukturovana", "Block_Priloha_Souborova"}

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\r\f\v]+")
ODST_RE = re.compile(r"^\((\d+[a-z]?)\)")


def nfc(s):
    return unicodedata.normalize("NFC", s) if isinstance(s, str) else s


def strip_xhtml(x: str | None) -> str:
    """`<var>(1)</var> Kupní <czechvoc-termin …>smlouvou</czechvoc-termin>…` → prostý
    text. <var> se zahazuje jako tag, ale jeho obsah — „(1)", „a)", „§ 311" —
    zůstává: to jsou markery, podle kterých se čtou odstavce."""
    if not x:
        return ""
    text = html.unescape(TAG_RE.sub("", x))
    return WS_RE.sub(" ", text).replace(" ", " ").strip()


def ref_from_eli(eli: str) -> str | None:
    """`…/par_2079` → „§ 2079", `…/par_2a` → „§ 2a", `…/cl_10` → „čl. 10"."""
    seg = eli.rsplit("/", 1)[-1]
    m = re.match(r"(par|cl)_(\d+[a-z]*)$", seg)
    if not m:
        return None
    return ("§ " if m.group(1) == "par" else "čl. ") + m.group(2)


def is_skipped(kind: str) -> bool:
    return kind in SKIP_EXACT or kind.startswith(SKIP_PREFIXES)


# --- stažení ------------------------------------------------------------------

class EsbirkaCache:
    """Cache REST e-Sbírky s cache na disku: law/cache/{sb}_{rok}_{cislo}/{datum}/pN.json."""

    def __init__(self, cache_dir: Path, offline: bool = False, delay: float = REQUEST_DELAY):
        self.cache_dir = cache_dir
        self.offline = offline
        self.delay = delay
        self._last = 0.0
        self.calls = 0

    def _get(self, path: str) -> dict:
        if self.offline:
            raise RuntimeError(f"offline režim a chybí v cache: {path}")
        import requests  # až tady — --offline a testy requests nepotřebují

        url = f"{CACHE_BASE}/{path}"
        for attempt, wait in enumerate((0,) + RETRY_BACKOFF):
            if wait:
                print(f"    čekám {wait} s a zkouším znovu ({url})", file=sys.stderr)
                time.sleep(wait)
            gap = self.delay - (time.time() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.time()
            self.calls += 1
            r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=90)
            if r.status_code in (403, 429) or r.status_code >= 500:
                continue
            if r.status_code != 200:
                raise RuntimeError(f"{url}: HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if isinstance(data, dict) and data.get("chyby"):
                raise RuntimeError(f"{url}: {data['chyby']}")
            return data
        raise RuntimeError(f"{url}: vzdávám po {len(RETRY_BACKOFF)} opakováních")

    def _cached(self, file: Path, path: str) -> dict:
        if file.exists():
            data = json.loads(file.read_text(encoding="utf-8"))
            # chybová odpověď uložená omylem (ruční stažení) se nesmí tvářit jako stránka
            if not (isinstance(data, dict) and data.get("chyby")):
                return data
        data = self._get(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def document(self, sb: str, rok: int, cislo: int) -> dict:
        """Metadata aktuálního znění (staleUrl s datem, eli, uplnaCitace…)."""
        key = f"{sb}_{rok}_{cislo}"
        stale = urllib.parse.quote(f"/{sb}/{rok}/{cislo}", safe="")
        # metadata se cachují pod jménem předpisu; znění pod jeho datem
        return self._cached(self.cache_dir / key / "meta.json", f"dokumenty-sbirky/{stale}")

    def fragments(self, sb: str, rok: int, cislo: int, stale_url: str) -> list[dict]:
        key = f"{sb}_{rok}_{cislo}"
        version = stale_url.rsplit("/", 1)[-1]
        stale = urllib.parse.quote(stale_url, safe="")
        out, page, pages = [], 0, 1
        while page < pages:
            data = self._cached(self.cache_dir / key / version / f"p{page}.json",
                                f"dokumenty-sbirky/{stale}/fragmenty?cisloStranky={page}")
            out.extend(data.get("seznam") or [])
            pages = int(data.get("pocetStranek") or 1)
            page += 1
        return out


# --- strom fragmentů ------------------------------------------------------------

@dataclass
class Node:
    frag: dict
    kind: str
    eli: str
    text: str
    children: list["Node"] = field(default_factory=list)

    @property
    def id(self) -> int:
        return self.frag.get("id")


def build_tree(fragments: list[dict]) -> Node:
    """Strom z ELI v surovém (dokumentovém) pořadí. Rodič = nejbližší existující
    předek podle ELI; sirotek visí na kořeni, ne v chybě — jeden podivný
    fragment nesmí shodit celý předpis."""
    root = Node({}, "ROOT", "", "")
    by_eli: dict[str, Node] = {}
    for f in fragments:
        eli = f.get("eli") or f"__{f.get('id')}"
        n = Node(f, f.get("kodTypuFragmentu") or "?", eli, strip_xhtml(f.get("xhtml")))
        by_eli[eli] = n
        parent = root
        p = eli
        while "/" in p:
            p = p.rsplit("/", 1)[0]
            if p in by_eli:
                parent = by_eli[p]
                break
        parent.children.append(n)
    return root


# --- průchod → kapitoly a jednotky ----------------------------------------------

@dataclass
class Unit:
    """Odstavec (nebo celý § bez odstavců): nejmenší citovatelná jednotka."""
    ref: str
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(l for l in self.lines if l)


@dataclass
class Section:
    ordinal: int
    level: int
    parent_ordinal: int | None
    ref: str | None
    heading: str
    path: str
    citation: str | None = None
    eli: str | None = None
    stale_url: str | None = None
    units: list[Unit] = field(default_factory=list)   # prázdné u čistě hierarchických uzlů

    @property
    def text(self) -> str:
        return "\n".join(u.text for u in self.units if u.text)


class Walker:
    def __init__(self, unknown: Counter):
        self.sections: list[Section] = []
        self.unknown = unknown
        self.stack: list[Section] = []          # otevřené hierarchické uzly
        self.carry_heading: str | None = None   # Nadpis_nad čekající na další §
        self.loose: Section | None = None       # volný text mimo § (přílohy, preambule)

    # -- pomocníci --
    def _ordinal(self) -> int:
        return len(self.sections) + 1

    def _path(self, own: str) -> str:
        return " › ".join([s.heading for s in self.stack] + [own])

    def _parent(self) -> int | None:
        return self.stack[-1].ordinal if self.stack else None

    def _open(self, level: int, heading: str, ref: str | None = None, **extra) -> Section:
        while self.stack and self.stack[-1].level >= level:
            self.stack.pop()
        sec = Section(self._ordinal(), level, self._parent(), ref, heading, self._path(heading), **extra)
        self.sections.append(sec)
        self.stack.append(sec)
        self.carry_heading = None
        self.loose = None
        return sec

    def _loose_text(self, node: Node, label: str = "(text)") -> None:
        """Text, který nepatří žádnému § — příloha, preambule, hlavička."""
        if not node.text:
            return
        if self.loose is None or self.loose.parent_ordinal != self._parent():
            level = (self.stack[-1].level + 1) if self.stack else 1
            heading = self.carry_heading or label
            self.loose = Section(self._ordinal(), min(level, LEAF_LEVEL), self._parent(), None, heading,
                                 self._path(heading), units=[Unit(heading)])
            self.sections.append(self.loose)
            self.carry_heading = None
        self.loose.units[0].lines.append(node.text)

    # -- průchod --
    def walk(self, node: Node) -> None:
        for child in node.children:
            self.visit(child)

    def visit(self, node: Node) -> None:
        kind = node.kind
        # Část „novela" (…/dokument/novela/…) jsou novelizační body: citace textu
        # vkládaného do JINÝCH zákonů („§ 200o se vkládá…"). V konsolidovaném
        # znění cílového zákona už ten text je; tady by byl duplicitní a bez
        # kontextu — a vnořené „§" v něm mátly strom (změřeno: 32 § v 6 předpisech).
        if node.eli.endswith("/dokument/novela") or "/dokument/novela/" in node.eli:
            return
        if kind in HIERARCHY:
            heading = node.text
            # Název části/hlavy/dílu je samostatný fragment hned pod uzlem —
            # v e-Sbírce Nadpis_pod („ČÁST PRVNÍ" + „OBECNÁ ČÁST"), jinde Nadpis.
            # Sloučí se do jednoho nadpisu, aby v path stálo „ČÁST PRVNÍ OBECNÁ
            # ČÁST", ne dvě kapitoly.
            rest = list(node.children)
            while rest and rest[0].kind in HEADING_ABOVE | HEADING_BELOW and rest[0].text:
                heading = f"{heading} {rest[0].text}".strip()
                rest.pop(0)
            self._open(HIERARCHY[kind], heading or kind)
            for c in rest:
                self.visit(c)
            return
        if kind in ANNEX_HEAD:
            self._open(1, node.text or "Příloha")
            self.walk(node)
            return
        if kind in LEAF:
            self._leaf(node)
            return
        if kind in PREAMBLE:
            self._loose_text(node, "Preambule")
            return
        if kind in HEADING_ABOVE:
            self.carry_heading = node.text or None
            self.loose = None
            self.walk(node)
            return
        if is_skipped(kind) or not node.text:
            # obal bez textu (Block_Prechodne_Ustanoveni_Nov, Block_Zrusovaci_…,
            # Virtual_*) — projde se skrz, potomci jsou normální §/odstavce
            self.walk(node)
            return
        # text mimo § (odstavce přílohy, tabulky, neznámé typy s textem)
        if kind not in UNIT_START | UNIT_TEXT | HEADING_BELOW:
            self.unknown[kind] += 1
        self._loose_text(node)
        self.walk(node)

    def _leaf(self, node: Node) -> None:
        ref = ref_from_eli(node.eli) or node.text or "§"
        heading = ""
        units: list[Unit] = []
        for d in self._descendants(node):
            if d.kind in HEADING_BELOW:
                if not heading:
                    heading = d.text
                continue
            if not d.text:
                continue
            if d.kind in UNIT_START:
                m = ODST_RE.match(d.text)
                units.append(Unit(f"{ref} odst. {m.group(1)}" if m else ref, [d.text]))
                continue
            # mezititulek uvnitř § (§ 232 TZ, § 158f TŘ) je řádek textu, ne kapitola
            if d.kind not in UNIT_TEXT | HEADING_ABOVE:
                self.unknown[d.kind] += 1
            if not units:
                units.append(Unit(ref))
            units[-1].lines.append(d.text)
        if not heading and self.carry_heading:
            heading = self.carry_heading
        if not units:
            units = [Unit(ref, [f"{ref} (bez textu)"])]
        while self.stack and self.stack[-1].level >= LEAF_LEVEL:
            self.stack.pop()
        own = f"{ref} {heading}".strip()
        sec = Section(self._ordinal(), LEAF_LEVEL, self._parent(), ref, heading, self._path(own),
                      citation=node.frag.get("zkracenaCitace"), eli=node.eli,
                      stale_url=node.frag.get("staleUrl"), units=units)
        self.sections.append(sec)
        self.loose = None

    def _descendants(self, node: Node):
        for c in node.children:
            yield c
            yield from self._descendants(c)


# --- chunkování § ------------------------------------------------------------------

def pack_units(sec: Section, chunk: int = CHUNK) -> list[tuple[str, str, str]]:
    """[(text, ref_start, ref_end)] — jednotky se skládají do kusů ≤ chunk.
    Jednotka delší než chunk se rozdělí po řádcích (písmena, body), a řádek
    delší než chunk až větným chunkerem — to je § 3080 NOZ (48 k znaků výčtu
    zrušených předpisů), ne běžný stav."""
    prefix = f"{sec.ref or ''} {sec.heading}".strip()
    pieces: list[tuple[str, str, str]] = []
    cur: list[str] = []
    cur_len = 0
    cur_refs: list[str] = []

    def flush():
        nonlocal cur, cur_len, cur_refs
        if cur:
            pieces.append(("\n".join(cur), cur_refs[0], cur_refs[-1]))
        cur, cur_len, cur_refs = [], 0, []

    def add(text: str, ref: str):
        nonlocal cur_len
        if cur and cur_len + len(text) + 1 > chunk:
            flush()
        cur.append(text)
        cur_len += len(text) + 1
        cur_refs.append(ref)

    for u in sec.units:
        t = u.text
        if len(t) <= chunk:
            add(t, u.ref)
            continue
        flush()
        for line in u.lines:
            if len(line) <= chunk:
                add(line, u.ref)
            else:
                flush()
                for piece in chunk_text(line, chunk_size=chunk, overlap=0, min_chunk_len=MIN_CHUNK):
                    pieces.append((piece, u.ref, u.ref))
        flush()
    flush()
    return [(f"{prefix}\n{body}" if prefix and not body.startswith(prefix) else body, a, b)
            for body, a, b in pieces]


from cite import ref_span  # noqa: E402  — tentýž tvar rozsahu odstavců jako v serveru


# --- registr ---------------------------------------------------------------------

def load_registry(path: Path) -> tuple[dict, list[dict]]:
    reg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    acts = []
    for a in reg.get("acts") or []:
        cislo, rok = str(a["cislo"]).split("/")
        acts.append({**a, "sb": a.get("sb", "sb"), "rok": int(rok), "n": int(cislo),
                     "id": a.get("id") or f"cz.{a.get('sb', 'sb')}.{rok}.{cislo}"})
    return reg.get("groups") or {}, acts


def parse_acts_arg(value: str) -> list[dict]:
    out = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        cislo, rok = item.split("/")
        out.append({"cislo": item, "sb": "sb", "rok": int(rok), "n": int(cislo), "id": f"cz.sb.{rok}.{cislo}",
                    "group": "misc", "priority": 2})
    return out


# --- jeden předpis ---------------------------------------------------------------

def ingest_act(act: dict, meta: dict, fragments: list[dict], writer: Writer, created_at: str,
               unknown: Counter, chunk: int = CHUNK) -> dict:
    wid = act["id"]
    title = f"{act['n']}/{act['rok']} Sb."
    w = Walker(unknown)
    w.walk(build_tree(fragments))
    sections = w.sections

    version = (meta.get("staleUrl") or "").rsplit("/", 1)[-1]
    chapter_rows, chunks = [], []
    for sec in sections:
        pieces = pack_units(sec, chunk) if sec.units else []
        chapter_rows.append({
            "id": f"{wid}:{sec.ordinal:04d}", "work_id": wid, "ordinal": sec.ordinal, "level": sec.level,
            "parent_id": f"{wid}:{sec.parent_ordinal:04d}" if sec.parent_ordinal else None,
            "ref": sec.ref, "heading": nfc(sec.heading), "path": nfc(sec.path),
            "char_count": len(sec.text), "chunk_count": len(pieces),
            # navíc proti knihám (load_pg je ignoruje, dump/eval je má):
            "eli": sec.eli, "stale_url": sec.stale_url, "citation": sec.citation,
        })
        for k, (text, a, b) in enumerate(pieces):
            chunks.append((sec, k, text, a, b))

    n = len(chunks)
    for seq, (sec, k, text, a, b) in enumerate(chunks):
        text = nfc(text)
        writer.write("chunks", {
            "id": f"{wid}:{sec.ordinal:04d}:{k:04d}",
            "source": "e-sbirka",
            "lang": "cs",
            "group": act.get("group") or "misc",
            "title": f"{title} ({ref_span(a, b)})",
            "text": text,
            "created_at": created_at,
            "embedded": 0,
            "work": title,
            "path": meta.get("staleUrl") or "",
            "chunk_index": seq,
            "chunk_count": n,
            "work_id": wid,
            "chapter_id": f"{wid}:{sec.ordinal:04d}",
            "chapter_ref": sec.ref,
            "chapter_path": nfc(sec.path),
            "seq_in_chapter": k,
            "ref_start": a,
            "ref_end": b,
            "text_sha": hashlib.sha1(text.encode("utf-8")).hexdigest(),
            "lang_original": "cs",
            "subgroup": act.get("subgroup") or "ZAKON",
            "author": act.get("author") or "Parlament ČR",
            "citation": sec.citation,
            "stale_url": (sec.stale_url or ""),
            "effective_from": meta.get("datumUcinnostiZneniOd"),
        })
    for row in chapter_rows:
        writer.write("chapters", row)

    name_cs = act.get("name_cs") or meta.get("uplnaCitace") or meta.get("nazev") or title
    char_count = sum(len(s.text) for s in sections)
    writer.write("works", {
        "id": wid, "group": act.get("group") or "misc", "subgroup": act.get("subgroup") or "ZAKON",
        "title": title, "work_legacy": title, "name_cs": nfc(name_cs),
        "author": act.get("author") or "Parlament ČR", "author_cs": act.get("author") or "Parlament ČR",
        "lang_original": "cs", "lang_corpus": "cs", "form": "zakonik",
        "edition": f"úplné znění účinné od {meta.get('datumUcinnostiZneniOd')} (e-Sbírka, staženo {created_at[:10]})",
        "urn": (meta.get("eli") or "").rsplit("/", 1)[0] if version and meta.get("eli", "").endswith(version) else meta.get("eli"),
        "source_path": meta.get("staleUrl"), "priority": int(act.get("priority") or 1),
        "aliases": [nfc(a) for a in act.get("aliases") or []],
        "abbr": act.get("abbr") or [], "short": act.get("short"),
        "effective_from": meta.get("datumUcinnostiZneniOd"), "dokument_base_id": meta.get("dokumentBaseId"),
        "chunk_count": n, "chapter_count": len(chapter_rows), "char_count": char_count,
    })
    leaves = [s for s in sections if s.level == LEAF_LEVEL and s.ref]
    lens = [len(t) for _, _, t, _, _ in chunks]
    return {"paragraphs": len(leaves), "chapters": len(chapter_rows), "chunks": n, "chars": char_count,
            "median": statistics.median(lens) if lens else 0, "over": sum(1 for l in lens if l > chunk),
            "version": meta.get("datumUcinnostiZneniOd")}


# --- CLI -------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Ingest zákonů z e-Sbírky do JSONL (kontrakt knihovny)")
    p.add_argument("--registry", default=str(Path(__file__).parent / "registry" / "law" / "tier1.yaml"))
    p.add_argument("--tier", type=int, default=None, help="1 = celý registr tier1.yaml")
    p.add_argument("--acts", default="", help="čísla předpisů, např. 89/2012,262/2006 (mimo registr = group misc)")
    p.add_argument("--source", default="cache", choices=["cache", "dump"])
    p.add_argument("--cache-dir", default=str(Path(__file__).parent / "law" / "cache"))
    p.add_argument("--out-dir", default=str(Path(__file__).parent / "law"))
    p.add_argument("--offline", action="store_true", help="jen z cache na disku, bez sítě")
    p.add_argument("--stats-only", action="store_true")
    p.add_argument("--strict", action="store_true", help="neznámý typ fragmentu s textem = chyba")
    args = p.parse_args()

    if args.source == "dump":
        print("CHYBA: režim dump (otevřená data e-Sbírky, vrstva 2) zatím není — viz docs/plan-pravnik.md §7",
              file=sys.stderr)
        return 2
    groups, registry_acts = load_registry(Path(args.registry))
    by_cislo = {a["cislo"]: a for a in registry_acts}
    if args.tier:
        acts = registry_acts
    elif args.acts:
        acts = [by_cislo.get(a["cislo"], a) for a in parse_acts_arg(args.acts)]
    else:
        print("CHYBA: zadej --tier 1 nebo --acts 89/2012,…", file=sys.stderr)
        return 2

    src = EsbirkaCache(Path(args.cache_dir), offline=args.offline)
    writer = Writer(Path(args.out_dir), args.stats_only)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    unknown: Counter = Counter()
    totals = Counter()
    t0 = time.time()
    for act in acts:
        try:
            meta = src.document(act["sb"], act["rok"], act["n"])
            frags = src.fragments(act["sb"], act["rok"], act["n"], meta["staleUrl"])
        except Exception as e:  # noqa: BLE001 — jeden předpis nesmí shodit dávku
            print(f"CHYBA {act['cislo']}: {e}", file=sys.stderr)
            totals["failed"] += 1
            continue
        st = ingest_act(act, meta, frags, writer, created_at, unknown)
        for k in ("paragraphs", "chapters", "chunks", "chars", "over"):
            totals[k] += st[k]
        print(f"{act['cislo']:>10}  {act.get('short') or '':<48} znění od {st['version']}  "
              f"§ {st['paragraphs']:>5}  kapitol {st['chapters']:>5}  chunků {st['chunks']:>6}  "
              f"medián {st['median']:>5.0f}  >{CHUNK}: {st['over']}")
    writer.close()
    print(f"\n{len(acts) - totals['failed']} předpisů, § {totals['paragraphs']}, kapitol {totals['chapters']}, "
          f"chunků {totals['chunks']}, znaků {totals['chars']}, chunků nad {CHUNK}: {totals['over']}, "
          f"HTTP volání {src.calls}, {time.time() - t0:.0f} s"
          + (f", selhalo {totals['failed']}" if totals["failed"] else ""))
    if unknown:
        print("neznámé typy fragmentů s textem (zpracované jako volný text):",
              ", ".join(f"{k}×{v}" for k, v in unknown.most_common()))
        if args.strict:
            return 1
    if not args.stats_only:
        print(f"zapsáno do {args.out_dir}/: works.jsonl, chapters.jsonl, books.jsonl")
    return 1 if totals["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())

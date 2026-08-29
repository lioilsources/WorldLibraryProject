#!/usr/bin/env python3
"""Ingest knih z downloads/ do JSONL pro Postgres a embedding. Běží na M2.

Pro každé dílo z kurátorského registru (registry/works.yaml) a z Perseu
(TEI XML, jen originály) vytáhne text, vyčistí ho (clean_text), rozdělí na
kapitoly (chapters / perseus_tei) a kapitoly na chunky — chunk nikdy
nekříží hranici kapitoly a jeho velikost je per jazyk (čínština je hustá,
pálí má dlouhé tokeny; viz eval/diag_tokens.py).

Výstupy:
    books.jsonl     chunky — EduRAG kontrakt {id, source, lang, group, title,
                    text, created_at, embedded} + work, path, chunk_index,
                    chunk_count a nově work_id, chapter_id, chapter_ref,
                    chapter_path, seq_in_chapter, text_sha, lang_original,
                    subgroup, author
    works.jsonl     jedno dílo na řádek (registr + spočtené počty)
    chapters.jsonl  jedna kapitola na řádek

ID jsou slugy nezávislé na cestě: work 'zh.daodejing', kapitola
'zh.daodejing:0008', chunk 'zh.daodejing:0008:0001'. Dnešní Chroma ID
(sha256 cesty) mizí — přechod je nová kolekce, ne --reset (viz plán).

Použití (na M2):
    python3 ingest_books.py --input-dir ../downloads --registry registry --perseus
    python3 ingest_books.py --registry registry --perseus --stats-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml

import chapters as chap
from chunking import DEFAULT_MIN_CHUNK_LEN, chunk_text
from clean_text import clean

LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"

# Velikost chunku ve znacích per jazyk textu v korpusu. Z měření
# (eval/diag_tokens.py, e5 tokenizer, limit 512): pálí 0,40 tok/znak → 1500
# znaků je 590 tokenů (100 % chunků nad limit); čínština 0,86 tok/znak;
# ostatní ~0,27. Cíl ≈ 450 tokenů, pasáže pro embedding to pak už nemusí
# dělit.
CHUNK_BY_LANG = {
    "zh": 500, "lzh": 500,
    "pi": 1100,
    "grc": 1200,
    "lat": 1300,
    "sa": 1500, "de": 1500, "en": 1500,
}
DEFAULT_CHUNK = 1500
OVERLAP_RATIO = 0.1
MIN_CHAPTER_CHUNK = 30   # kratší text než tohle je jen nadpis, ne kapitola

GUTENBERG_START = "*** START OF"
GUTENBERG_END = "*** END OF"
PG_TITLE_RE = re.compile(r"^Title:\s*(.+?)\s*$", re.MULTILINE)

EXCLUDE_NAMES = {"manifest.txt"}
SKIP_DIR_MARKERS = ("__MACOSX", "Tipiṭaka_(Mūla)")

# Nepoužitelné PDF: frakturové OCR, vadná font kódování a obrazová
# faksimile — ověřeno ručně; čekají na pořádné OCR nebo jiný zdroj
EXCLUDE_PATH_MARKERS = (
    "mahabharata/bori_full",     # dévanágarí přes PUA fonty (mojibake); obsah kryje TXT překlad
    "mahabharata/volumes",       # text. vrstvu mají jen předmluvy, zbytek sken
    "european_philosophy/kant",  # frakturové OCR — nečitelné
    "hegel_werke_vol1",          # frakturové OCR; Suhrkamp bandy (band01/03/05) jsou čisté
    "madrid_codex", "grolier_codex", "dresden_kingsborough",  # obrazová faksimile, text jen popisky
    "westminster_leningrad_codex",  # hebrejština přes transliterační font → mojibake
    "full_leningrad_codex",      # totéž, jiný sken
    "avesta_geldner_critical",   # sken bez textové vrstvy (736 stran, 0 znaků)
    "maya/",                     # kodexy = obrazová faksimile
    "sanskrit/ramayana",         # skeny bez použitelné textové vrstvy — čeká na OCR / GRETIL
    "greek_latin/perseus",       # Perseus jde přes TEI parser, ne přes rglob TXT/PDF
)

MAHA_PARVY = {
    1: "Ádiparva", 2: "Sabháparva", 3: "Vanaparva", 4: "Virátaparva",
    5: "Udjógaparva", 6: "Bhíšmaparva", 7: "Drónaparva", 8: "Karnaparva",
    9: "Šaljaparva", 10: "Sauptikaparva", 11: "Stríparva", 12: "Šántiparva",
    13: "Anušásanaparva", 14: "Ášvamédhikaparva", 15: "Ášramavásikaparva",
    16: "Mausalaparva", 17: "Maháprasthánikaparva", 18: "Svargáróhanaparva",
}

MIN_PDF_CHARS = 1000
MAX_LATIN_EXT = 0.35

WORK_FIELDS = ("group", "subgroup", "title", "work_legacy", "name_cs", "author", "author_cs",
               "lang_original", "lang_corpus", "edition", "form", "period", "urn", "priority", "aliases")


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


# --- brány kvality --------------------------------------------------------------

def _script(ch: str) -> str:
    o = ord(ch)
    if o < 128:
        return "ascii"
    if 0x0590 <= o <= 0x05FF:
        return "hebrew"
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
        return "han"
    if 0x0900 <= o <= 0x097F:
        return "devanagari"
    if 0x0370 <= o <= 0x03FF or 0x1F00 <= o <= 0x1FFF:
        return "greek"
    if 0x0600 <= o <= 0x06FF:
        return "arabic"
    return "latin-ext"


def pdf_text_quality_ok(text: str) -> bool:
    """Málo písmen = OCR šum, PUA = custom font bez unicode mapy, záplava
    latinky s diakritikou = transliterační font (Leningradský kodex prošel
    přes obě starší kontroly a 2 461 chunků guláše leželo v korpusu měsíc)."""
    sample = text[:20000]
    if not sample:
        return False
    letters = sum(ch.isalpha() or ch.isspace() for ch in sample) / len(sample)
    pua = sum(0xE000 <= ord(ch) <= 0xF8FF for ch in sample) / len(sample)
    alpha = [ch for ch in sample if ch.isalpha()]
    latin_ext = (sum(1 for ch in alpha if _script(ch) == "latin-ext") / len(alpha)) if alpha else 0.0
    return letters >= 0.6 and pua < 0.02 and latin_ext < MAX_LATIN_EXT


def is_lfs_pointer(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(LFS_POINTER_PREFIX)) == LFS_POINTER_PREFIX
    except OSError:
        return False


def strip_gutenberg_boilerplate(text: str) -> str:
    start = text.find(GUTENBERG_START)
    if start != -1:
        nl = text.find("\n", start)
        if nl != -1:
            text = text[nl + 1:]
    end = text.find(GUTENBERG_END)
    if end != -1:
        text = text[:end]
    return text


def extract_pdf(path: Path) -> tuple[list[str], list]:
    """(text po stránkách, osnova) — stránky zvlášť kvůli pdf_bookmarks."""
    import fitz  # PyMuPDF — lazy import, ať TXT-only běh nepotřebuje závislost

    with fitz.open(path) as doc:
        return [page.get_text("text") for page in doc], doc.get_toc()


def work_title(path: Path, raw_text: str = "") -> str:
    """Fallback pro dílo bez registru (jako dřív)."""
    maha = re.fullmatch(r"maha(\d{2})", path.stem)
    if maha:
        n = int(maha.group(1))
        return f"Mahábhárata {n:02d} — {MAHA_PARVY[n]}"
    if path.stem.startswith("pg") and raw_text:
        m = PG_TITLE_RE.search(raw_text[:2000])
        if m:
            return m.group(1)
    return path.stem.replace("_", " ").replace("-", " ").strip()


# --- registr --------------------------------------------------------------------

def load_registry(registry_dir: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """(díla podle NFC cesty, Perseus overrides podle work_id)."""
    works = yaml.safe_load((registry_dir / "works.yaml").read_text(encoding="utf-8")) or {}
    by_path = {}
    for wid, e in works.items():
        e = dict(e)
        e["id"] = wid
        by_path[nfc(e["path"])] = e
    ov_file = registry_dir / "perseus_overrides.yaml"
    overrides = yaml.safe_load(ov_file.read_text(encoding="utf-8")) if ov_file.exists() else {}
    return by_path, overrides or {}


def legacy_entry(rel: Path, work: str) -> dict:
    """Dílo mimo registr: chování původního ingestu, jen s varováním."""
    group = rel.parts[0] if len(rel.parts) > 1 else "misc"
    lang = {"chinese": "zh", "sanskrit": "sa", "pali": "pi", "european_philosophy": "de"}.get(group, "en")
    slug = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", work).encode("ascii", "ignore").decode().lower()).strip("-")
    return {"id": f"{group}.{slug}", "path": str(rel), "group": group, "title": work, "work_legacy": work,
            "lang_original": lang, "lang_corpus": lang, "priority": 2, "chapters": {"detector": "none"}}


# --- stavba záznamů -------------------------------------------------------------

class Writer:
    def __init__(self, out_dir: Path | None, stats_only: bool):
        self.stats_only = stats_only
        self.files = {}
        if not stats_only and out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            self.files = {
                "chunks": open(out_dir / "books.jsonl", "w", encoding="utf-8"),
                "works": open(out_dir / "works.jsonl", "w", encoding="utf-8"),
                "chapters": open(out_dir / "chapters.jsonl", "w", encoding="utf-8"),
            }
        self.counts = {"works": 0, "chapters": 0, "chunks": 0, "chars": 0}
        self.chunk_lens: list[int] = []

    def write(self, kind: str, rec: dict) -> None:
        self.counts[kind] += 1
        if kind == "chunks":
            self.chunk_lens.append(len(rec["text"]))
            self.counts["chars"] += len(rec["text"])
        if self.files:
            self.files[kind].write(json.dumps(rec, ensure_ascii=False) + "\n")

    def close(self):
        for f in self.files.values():
            f.close()


def emit_work(writer: Writer, entry: dict, chapter_texts: list[dict], created_at: str, path_str: str) -> dict:
    """chapter_texts: [{ordinal, level, parent_ordinal, ref, heading, path, text}]
    v pořadí dokumentu. Chunkuje, píše kapitoly a chunky, vrací statistiku."""
    wid = entry["id"]
    lang = entry.get("lang_corpus") or entry.get("lang_original") or "en"
    size = CHUNK_BY_LANG.get(lang, DEFAULT_CHUNK)
    overlap = int(size * OVERLAP_RATIO)
    title = nfc(entry.get("title") or wid)
    group = entry.get("group") or "misc"

    all_chunks: list[tuple[dict, str]] = []   # (kapitola, text chunku)
    chapter_rows = []
    for ch in chapter_texts:
        # krátká kapitola (čínská kapitola má 60–150 znaků) je celá jeden
        # chunk — min_chunk_len je proti útržkům, ne proti kapitolám
        min_len = min(DEFAULT_MIN_CHUNK_LEN, max(MIN_CHAPTER_CHUNK, len(ch["text"].strip())))
        pieces = chunk_text(ch["text"], chunk_size=size, overlap=overlap, min_chunk_len=min_len)
        chapter_rows.append({
            "id": f"{wid}:{ch['ordinal']:04d}",
            "work_id": wid,
            "ordinal": ch["ordinal"],
            "level": ch["level"],
            "parent_id": f"{wid}:{ch['parent_ordinal']:04d}" if ch.get("parent_ordinal") else None,
            "ref": ch.get("ref"),
            "heading": nfc(ch["heading"]),
            "path": nfc(ch["path"]),
            "char_count": len(ch["text"]),
            "chunk_count": len(pieces),
        })
        for piece in pieces:
            all_chunks.append((ch, piece))

    n = len(all_chunks)
    seq_in_chapter: dict[int, int] = {}
    for seq, (ch, piece) in enumerate(all_chunks):
        k = seq_in_chapter.get(ch["ordinal"], 0)
        seq_in_chapter[ch["ordinal"]] = k + 1
        writer.write("chunks", {
            "id": f"{wid}:{ch['ordinal']:04d}:{k:04d}",
            "source": "book",
            "lang": lang,
            "group": group,
            "title": f"{title} (část {seq + 1}/{n})",
            "text": piece,
            "created_at": created_at,
            "embedded": 0,
            "work": title,
            "path": path_str,
            "chunk_index": seq,
            "chunk_count": n,
            "work_id": wid,
            "chapter_id": f"{wid}:{ch['ordinal']:04d}",
            "chapter_ref": ch.get("ref"),
            "chapter_path": nfc(ch["path"]),
            "seq_in_chapter": k,
            "text_sha": hashlib.sha1(piece.encode("utf-8")).hexdigest(),
            "lang_original": entry.get("lang_original") or lang,
            "subgroup": entry.get("subgroup"),
            "author": entry.get("author"),
        })
    for row in chapter_rows:
        writer.write("chapters", row)

    char_count = sum(len(ch["text"]) for ch in chapter_texts)
    work_row = {"id": wid, "source_path": path_str}
    for f in WORK_FIELDS:
        if entry.get(f) not in (None, "", []):
            work_row[f] = entry[f]
    work_row.setdefault("title", title)
    work_row.setdefault("lang_original", lang)
    work_row.setdefault("lang_corpus", lang)
    work_row.setdefault("priority", 2)
    work_row.update({"chunk_count": n, "chapter_count": len(chapter_rows), "char_count": char_count})
    writer.write("works", work_row)
    return {"chapters": len(chapter_rows), "chunks": n, "chars": char_count,
            "median": statistics.median(len(p) for _, p in all_chunks) if all_chunks else 0}


def chapters_from_lines(lines: list[str], detector: str, toc=None, page_first_line=None) -> list[dict]:
    if detector == "pdf_bookmarks":
        chs = chap.build(lines, chap.marks_from_toc(toc or [], page_first_line or []))
    else:
        chs = chap.detect(lines, detector)
    out = []
    for c in chs:
        if chap.is_front_matter(c.heading):
            continue
        text = c.text(lines)
        out.append({"ordinal": c.ordinal, "level": c.level, "parent_ordinal": c.parent_ordinal,
                    "ref": c.ref, "heading": c.heading, "path": c.path, "text": text})
    return out


def ingest_file(path: Path, rel: Path, entry: dict, writer: Writer, created_at: str, stats: dict, needs_ocr: list):
    lang = entry.get("lang_corpus") or "en"
    detector = (entry.get("chapters") or {}).get("detector", "none")
    toc, page_first_line = None, None
    if path.suffix.lower() == ".txt":
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = clean(strip_gutenberg_boilerplate(raw), lang, "txt")
        kind = "txt"
    else:
        pages, toc = extract_pdf(path)
        # osnova míří na stránky → potřebujeme první řádek každé stránky
        cleaned_pages = [clean(p, lang, "pdf") for p in pages]
        page_first_line, acc = [], 0
        for cp in cleaned_pages:
            page_first_line.append(acc)
            acc += cp.count("\n") + 1
        text = "\n".join(cleaned_pages)
        kind = "pdf"
        if len(text.strip()) < MIN_PDF_CHARS:
            stats["ocr_needed"] += 1
            needs_ocr.append(str(rel))
            return None
        if not pdf_text_quality_ok(text):
            stats["ocr_needed"] += 1
            needs_ocr.append(f"{rel} (vadné OCR/kódování)")
            return None
    lines = text.split("\n")
    chapter_texts = chapters_from_lines(lines, detector, toc, page_first_line)
    stats[kind] += 1
    return emit_work(writer, entry, chapter_texts, created_at, str(rel))


def ingest_perseus(root: Path, overrides: dict, writer: Writer, created_at: str, stats: dict, groups: set[str], verbose: bool):
    import perseus_tei as pt

    works = pt.scan_works(root)
    if not works:
        print("POZOR: Perseus nenalezen v", root, file=sys.stderr)
        return
    for w in works:
        entry = {
            "id": w.work_id, "group": "greek_latin", "subgroup": w.subgroup, "title": w.title,
            "author": w.author, "lang_original": w.lang, "lang_corpus": w.lang, "edition": w.edition_desc,
            "urn": w.urn, "priority": w.priority, "aliases": [w.title_en] if w.title_en else [],
        }
        # overrides: nejdřív autor (author_cs + aliasy pro všechna jeho díla),
        # pak dílo (name_cs, priorita, aliasy, edition_file) — dílo vyhrává
        ov_author = (overrides.get("authors") or {}).get(w.group) or {}
        ov_work = (overrides.get("works") or {}).get(w.work_id) or {}
        for ov in (ov_author, ov_work):
            for k, v in ov.items():
                if k == "aliases":
                    entry["aliases"] = sorted(set(entry["aliases"]) | set(v))
                elif k != "edition_file":
                    entry[k] = v
        file = w.file
        if ov_work.get("edition_file"):
            file = w.file.parent / ov_work["edition_file"]
        tei_chapters, _ = pt.extract(file)
        chapter_texts = [
            {"ordinal": i, "level": 1, "parent_ordinal": None, "ref": c.ref, "heading": c.heading,
             "path": c.heading, "text": clean(c.text, w.lang, "txt")}
            for i, c in enumerate(tei_chapters, 1)
        ]
        rel = file.relative_to(root.parent.parent) if root.parent.parent in file.parents else file
        st = emit_work(writer, entry, chapter_texts, created_at, nfc(str(rel)))
        stats["tei"] += 1
        if verbose:
            print(f"  {w.work_id:<24} {w.author[:20]:<21} kap {st['chapters']:>4} chunků {st['chunks']:>5} med {st['median']:>4}")


def main() -> int:
    p = argparse.ArgumentParser(description="Ingest knih do JSONL (chunky, díla, kapitoly)")
    p.add_argument("--input-dir", default="../downloads", help="kořen korpusu")
    p.add_argument("--output", default="books.jsonl", help="chunky; works.jsonl a chapters.jsonl vedle něj")
    p.add_argument("--registry", default="registry", help="adresář s works.yaml a perseus_overrides.yaml")
    p.add_argument("--perseus", action="store_true", help="zapojit Perseus TEI (greek_latin/perseus)")
    p.add_argument("--no-pdf", action="store_true", help="přeskočit PDF (jen TXT)")
    p.add_argument("--stats-only", action="store_true", help="nic nezapisovat, jen počty per dílo")
    p.add_argument("--groups", default="", help="čárkou oddělený filtr tradic; prázdné = vše")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    root = Path(args.input_dir).resolve()
    if not root.is_dir():
        print(f"CHYBA: {root} není adresář", file=sys.stderr)
        return 1
    groups = {g.strip() for g in args.groups.split(",") if g.strip()}
    by_path, overrides = load_registry(Path(args.registry))

    out = Path(args.output)
    writer = Writer(out.parent if out.parent != Path("") else Path("."), args.stats_only)
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = {"txt": 0, "pdf": 0, "tei": 0, "lfs_skipped": 0, "ocr_needed": 0, "errors": 0, "unregistered": 0}
    needs_ocr, unregistered = [], []

    suffixes = {".txt"} if args.no_pdf else {".txt", ".pdf"}
    files = sorted(f for f in root.rglob("*") if f.is_file() and f.suffix.lower() in suffixes)
    for path in files:
        rel = path.relative_to(root)
        if groups and (len(rel.parts) < 2 or rel.parts[0] not in groups):
            continue
        if path.name in EXCLUDE_NAMES or path.name.startswith("._"):
            continue
        norm_rel = nfc(str(rel))
        if any(m in norm_rel for m in SKIP_DIR_MARKERS + EXCLUDE_PATH_MARKERS):
            continue
        if is_lfs_pointer(path):
            stats["lfs_skipped"] += 1
            continue
        entry = by_path.get(norm_rel)
        if entry is None:
            stats["unregistered"] += 1
            unregistered.append(norm_rel)
            raw = path.read_text(encoding="utf-8", errors="replace") if path.suffix.lower() == ".txt" else ""
            entry = legacy_entry(rel, work_title(path, raw))
        try:
            st = ingest_file(path, rel, entry, writer, created_at, stats, needs_ocr)
        except Exception as e:  # noqa: BLE001 — jedna vadná kniha nesmí shodit celý běh
            stats["errors"] += 1
            print(f"CHYBA {rel}: {e}", file=sys.stderr)
            continue
        if st and not args.quiet:
            print(f"  {entry['id']:<28} kap {st['chapters']:>4} chunků {st['chunks']:>5} med {st['median']:>4}  {norm_rel[-50:]}")

    if args.perseus and (not groups or "greek_latin" in groups):
        ingest_perseus(root / "greek_latin" / "perseus", overrides, writer, created_at, stats, groups, not args.quiet)

    writer.close()
    c = writer.counts
    med = statistics.median(writer.chunk_lens) if writer.chunk_lens else 0
    print(f"\n{'DRY-RUN: ' if args.stats_only else ''}{stats['txt']} TXT + {stats['pdf']} PDF + {stats['tei']} TEI"
          f" → {c['works']} děl, {c['chapters']} kapitol, {c['chunks']} chunků ({c['chars']/1e6:.1f} M znaků, medián {med:.0f})"
          + ("" if args.stats_only else f" → {out}"))
    if stats["lfs_skipped"]:
        print(f"POZOR: {stats['lfs_skipped']} souborů jsou Git LFS pointery bez obsahu — git lfs pull.")
    if needs_ocr:
        print(f"POZOR: {len(needs_ocr)} PDF bez textové vrstvy nebo s vadným kódováním:")
        for r in needs_ocr:
            print(f"  {r}")
    if unregistered:
        print(f"POZOR: {len(unregistered)} souborů bez záznamu v registru (ingestovány s odvozenými metadaty):")
        for r in unregistered:
            print(f"  {r}")
    if stats["errors"]:
        print(f"POZOR: {stats['errors']} souborů selhalo (viz stderr).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

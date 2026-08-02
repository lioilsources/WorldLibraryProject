#!/usr/bin/env python3
"""Ingest knih z downloads/ do JSONL pro embedding. Běží na M2.

Projde strom korpusu (TXT + PDF), vytáhne text, rozseká na chunky a
zapíše JSONL kompatibilní s EduRAG kontraktem:

    {"id","source","lang","group","title","text","created_at","embedded"}

plus knižní metadata navíc: work, path, chunk_index, chunk_count.

Použití (na M2):
    python3 ingest_books.py --input-dir ../downloads --output books.jsonl
"""

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from chunking import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MIN_CHUNK_LEN,
    DEFAULT_OVERLAP,
    chunk_text,
)

LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"

# Jazyk podle tradice (top-level adresář v downloads/). Fallback "en" —
# většina volně dostupných edic jsou anglické překlady.
LANG_BY_GROUP = {
    "chinese": "zh",
    "sanskrit": "sa",
    "pali": "pi",
    "european_philosophy": "de",
    "bible": "he",
    "greek_latin": "grc",
    "islam": "ar",
    "persian": "fa",
}

GUTENBERG_START = "*** START OF"
GUTENBERG_END = "*** END OF"

# "Title:" řádek v Gutenberg hlavičce — zdroj čitelného názvu díla
# (stem souboru typu pg16328 nic neříká)
PG_TITLE_RE = re.compile(r"^Title:\s*(.+?)\s*$", re.MULTILINE)

# Servisní soubory, které nejsou knihy — do korpusu nepatří
EXCLUDE_NAMES = {"manifest.txt"}

# Adresáře k přeskočení (porovnává se po NFC normalizaci — macOS ukládá
# jména v NFD): __MACOSX = balast ze ZIPů, Tipiṭaka_(Mūla) = kompletní
# duplikát samostatných piṭaka adresářů (každá kniha by byla 2×)
SKIP_DIR_MARKERS = ("__MACOSX", "Tipiṭaka_(Mūla)")

# Nepoužitelné PDF: frakturové OCR, vadná font kódování a obrazová
# faksimile — ověřeno ručně 2. 8. 2026; čekají na pořádné OCR
EXCLUDE_PATH_MARKERS = (
    "mahabharata/bori_full",     # dévanágarí přes PUA fonty (mojibake); obsah kryje TXT překlad
    "mahabharata/volumes",       # text. vrstvu mají jen předmluvy, zbytek sken
    "european_philosophy/kant",  # frakturové OCR — nečitelné
    "hegel_werke_vol1",          # frakturové OCR; Suhrkamp bandy (band01/03/05) jsou čisté
    "madrid_codex", "grolier_codex", "dresden_kingsborough",  # obrazová faksimile, text jen popisky
)

# Mahábhárata: maha01–maha18 → názvy parv (Ganguliho členění)
MAHA_PARVY = {
    1: "Ádiparva", 2: "Sabháparva", 3: "Vanaparva", 4: "Virátaparva",
    5: "Udjógaparva", 6: "Bhíšmaparva", 7: "Drónaparva", 8: "Karnaparva",
    9: "Šaljaparva", 10: "Sauptikaparva", 11: "Stríparva", 12: "Šántiparva",
    13: "Anušásanaparva", 14: "Ášvamédhikaparva", 15: "Ášramavásikaparva",
    16: "Mausalaparva", 17: "Maháprasthánikaparva", 18: "Svargáróhanaparva",
}

# PDF, ze kterého vypadne méně textu, je nejspíš sken bez OCR vrstvy.
MIN_PDF_CHARS = 1000


def pdf_text_quality_ok(text: str) -> bool:
    """Brána proti OCR šumu a vadnému kódování: málo písmen = šum
    (interpunkční rozsypaný čaj), PUA znaky = custom font bez unicode mapy."""
    sample = text[:20000]
    if not sample:
        return False
    letters = sum(ch.isalpha() or ch.isspace() for ch in sample) / len(sample)
    pua = sum(0xE000 <= ord(ch) <= 0xF8FF for ch in sample) / len(sample)
    return letters >= 0.6 and pua < 0.02


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
            text = text[nl + 1 :]
    end = text.find(GUTENBERG_END)
    if end != -1:
        text = text[:end]
    return text


def extract_pdf(path: Path) -> str:
    import fitz  # PyMuPDF — lazy import, ať TXT-only běh nepotřebuje závislost

    parts = []
    with fitz.open(path) as doc:
        for page in doc:
            parts.append(page.get_text("text"))
    return "\n".join(parts)


def work_title(path: Path, raw_text: str = "") -> str:
    maha = re.fullmatch(r"maha(\d{2})", path.stem)
    if maha:
        n = int(maha.group(1))
        return f"Mahábhárata {n:02d} — {MAHA_PARVY[n]}"
    if path.stem.startswith("pg") and raw_text:
        # Gutenberg hlavička je před "*** START OF" — hledat jen v úvodu
        m = PG_TITLE_RE.search(raw_text[:2000])
        if m:
            return m.group(1)
    return path.stem.replace("_", " ").replace("-", " ").strip()


def make_records(path: Path, rel: Path, text: str, args, work: str) -> list[dict]:
    chunks = chunk_text(
        text,
        chunk_size=args.chunk_size,
        overlap=args.overlap,
        min_chunk_len=args.min_chunk_len,
    )
    if not chunks:
        return []
    group = rel.parts[0] if len(rel.parts) > 1 else "misc"
    lang = LANG_BY_GROUP.get(group, "en")
    doc_key = hashlib.sha256(str(rel).encode("utf-8")).hexdigest()[:12]
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = []
    for i, chunk in enumerate(chunks):
        records.append(
            {
                "id": f"book_{doc_key}_{i:05d}",
                "source": "book",
                "lang": lang,
                "group": group,
                "title": f"{work} (část {i + 1}/{len(chunks)})",
                "text": chunk,
                "created_at": created_at,
                "embedded": 0,
                "work": work,
                "path": str(rel),
                "chunk_index": i,
                "chunk_count": len(chunks),
            }
        )
    return records


def main() -> int:
    p = argparse.ArgumentParser(description="Ingest knih do JSONL")
    p.add_argument("--input-dir", default="../downloads", help="kořen korpusu")
    p.add_argument("--output", default="books.jsonl")
    p.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    p.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)
    p.add_argument("--min-chunk-len", type=int, default=DEFAULT_MIN_CHUNK_LEN)
    p.add_argument("--no-pdf", action="store_true", help="přeskočit PDF (jen TXT)")
    p.add_argument(
        "--groups",
        default="",
        help="čárkou oddělený filtr tradic (např. chinese,european); prázdné = vše",
    )
    args = p.parse_args()

    root = Path(args.input_dir).resolve()
    if not root.is_dir():
        print(f"CHYBA: {root} není adresář", file=sys.stderr)
        return 1
    groups = {g.strip() for g in args.groups.split(",") if g.strip()}

    stats = {"txt": 0, "pdf": 0, "chunks": 0, "lfs_skipped": 0, "ocr_needed": 0, "errors": 0}
    needs_ocr = []

    suffixes = {".txt"} if args.no_pdf else {".txt", ".pdf"}
    files = sorted(f for f in root.rglob("*") if f.is_file() and f.suffix.lower() in suffixes)

    with open(args.output, "w", encoding="utf-8") as out:
        for path in files:
            rel = path.relative_to(root)
            if groups and (len(rel.parts) < 2 or rel.parts[0] not in groups):
                continue
            if path.name in EXCLUDE_NAMES or path.name.startswith("._"):
                continue
            norm_rel = unicodedata.normalize("NFC", str(rel))
            if any(m in norm_rel for m in SKIP_DIR_MARKERS + EXCLUDE_PATH_MARKERS):
                continue
            if is_lfs_pointer(path):
                stats["lfs_skipped"] += 1
                continue
            try:
                if path.suffix.lower() == ".txt":
                    raw = path.read_text(encoding="utf-8", errors="replace")
                    work = work_title(path, raw)
                    text = strip_gutenberg_boilerplate(raw)
                    kind = "txt"
                else:
                    text = extract_pdf(path)
                    work = work_title(path)
                    kind = "pdf"
                    if len(text.strip()) < MIN_PDF_CHARS:
                        stats["ocr_needed"] += 1
                        needs_ocr.append(str(rel))
                        continue
                    if not pdf_text_quality_ok(text):
                        stats["ocr_needed"] += 1
                        needs_ocr.append(f"{rel} (vadné OCR/kódování)")
                        continue
            except Exception as e:
                stats["errors"] += 1
                print(f"CHYBA {rel}: {e}", file=sys.stderr)
                continue

            records = make_records(path, rel, text, args, work)
            if not records:
                continue
            for r in records:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
            stats[kind] += 1
            stats["chunks"] += len(records)
            print(f"  {rel}: {len(records)} chunků")

    print(
        f"\nHotovo: {stats['txt']} TXT + {stats['pdf']} PDF → {stats['chunks']} chunků "
        f"→ {args.output}"
    )
    if stats["lfs_skipped"]:
        print(
            f"POZOR: {stats['lfs_skipped']} souborů jsou Git LFS pointery bez obsahu "
            f"— spusť `git lfs pull` nebo run_pipeline.sh."
        )
    if needs_ocr:
        print(f"POZOR: {len(needs_ocr)} PDF bez textové vrstvy (skeny, potřebují OCR):")
        for relpath in needs_ocr:
            print(f"  {relpath}")
    if stats["errors"]:
        print(f"POZOR: {stats['errors']} souborů selhalo (viz stderr).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

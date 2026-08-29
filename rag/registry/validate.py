#!/usr/bin/env python3
"""Kontrola registru: každý soubor korpusu má záznam (nebo je vyloučený),
cesty v registru existují, jazyky jsou z povoleného seznamu, work_id jsou
unikátní a detektory kapitol existují. Spouští se před ingestem:

    python3 registry/validate.py --input-dir ../downloads
"""

import argparse
import sys
import unicodedata
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from chapters import DETECTORS  # noqa: E402
from ingest_books import EXCLUDE_NAMES, EXCLUDE_PATH_MARKERS, SKIP_DIR_MARKERS  # noqa: E402

LANGS = {"grc", "lat", "lzh", "zh", "pi", "sa", "ang", "non", "de", "ae", "egy", "en", "he", "ar", "fa"}
FORMS = {"epos", "hymnus", "dialog", "traktat", "sutta", "vinaya", "abhidhamma", "drama", "dopis",
         "epigram", "aforismy", "kronika", "komentar", "zakonik", "jine"}
DETECTOR_NAMES = set(DETECTORS) | {"pdf_bookmarks"}


def nfc(s):
    return unicodedata.normalize("NFC", s)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input-dir", default="../downloads")
    p.add_argument("--registry", default=str(Path(__file__).parent))
    args = p.parse_args()
    root = Path(args.input_dir).resolve()
    reg = Path(args.registry)
    works = yaml.safe_load((reg / "works.yaml").read_text(encoding="utf-8")) or {}
    topics = yaml.safe_load((reg / "topics.yaml").read_text(encoding="utf-8")) or []
    errors, warnings = [], []

    ids = list(works)
    if len(ids) != len(set(ids)):
        errors.append("duplicitní work_id")
    paths = {}
    for wid, e in works.items():
        for f in ("path", "title", "lang_original", "lang_corpus"):
            if not e.get(f):
                errors.append(f"{wid}: chybí {f}")
        if e.get("lang_original") not in LANGS or e.get("lang_corpus") not in LANGS:
            errors.append(f"{wid}: neznámý jazyk {e.get('lang_original')}/{e.get('lang_corpus')}")
        if e.get("form") and e["form"] not in FORMS:
            warnings.append(f"{wid}: neznámá forma {e['form']}")
        det = (e.get("chapters") or {}).get("detector", "none")
        if det not in DETECTOR_NAMES:
            errors.append(f"{wid}: neznámý detektor {det}")
        if not (root / e["path"]).exists() and not (root / unicodedata.normalize("NFD", e["path"])).exists():
            errors.append(f"{wid}: cesta neexistuje: {e['path']}")
        np = nfc(e["path"])
        if np in paths:
            errors.append(f"{wid}: cesta sdílená s {paths[np]}")
        paths[np] = wid
        if not e.get("name_cs"):
            warnings.append(f"{wid}: bez name_cs")

    # soubory korpusu bez záznamu
    missing = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in (".txt", ".pdf"):
            continue
        rel = nfc(str(f.relative_to(root)))
        if f.name in EXCLUDE_NAMES or f.name.startswith("._"):
            continue
        if any(m in rel for m in SKIP_DIR_MARKERS + EXCLUDE_PATH_MARKERS):
            continue
        if rel not in paths:
            missing.append(rel)
    if missing:
        warnings.append(f"{len(missing)} souborů korpusu bez záznamu v registru (ingest je vezme s odvozenými metadaty):")
        warnings.extend(f"    {m}" for m in missing[:40])

    tslugs = [t["id"] for t in topics]
    if len(tslugs) != len(set(tslugs)):
        errors.append("duplicitní slug tématu")
    for t in topics:
        if not t.get("name_cs") or not t.get("description_cs"):
            errors.append(f"téma {t.get('id')}: chybí name_cs/description_cs")

    print(f"děl: {len(works)}, témat: {len(topics)}")
    for w in warnings:
        print("VAROVÁNÍ:", w)
    for e in errors:
        print("CHYBA:", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

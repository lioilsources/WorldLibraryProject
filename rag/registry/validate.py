#!/usr/bin/env python3
"""Kontrola registru: každý soubor korpusu má záznam (nebo je vyloučený),
cesty v registru existují, jazyky, skupiny a tradice jsou z povoleného
seznamu, work_id jsou unikátní (i napříč works.yaml a works.d/*.yaml),
detektory kapitol i adaptéry zdrojů existují. Spouští se před ingestem:

    python3 registry/validate.py --input-dir ../downloads
"""

import argparse
import sys
import unicodedata
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
import sources  # noqa: E402
from chapters import DETECTORS  # noqa: E402
from ingest_books import EXCLUDE_NAMES, EXCLUDE_PATH_MARKERS, SKIP_DIR_MARKERS, adapter_dirs, load_works  # noqa: E402
from retrieval import GROUP_TRADITIONS, TRADITIONS  # noqa: E402

# ISO 639 kódy jazyků korpusu; nové přibývají s fázemi PLAN-corpus-expansion.md
LANGS = {
    "grc", "lat", "lzh", "zh", "pi", "sa", "ang", "non", "de", "ae", "egy", "en", "he", "ar", "fa",
    "bo", "ja", "ta", "pa", "pra",         # Tibet, Japonsko, tamilština, pandžábština (gurmukhí), prákrty
    "arc", "syc", "cop", "gez",            # aramejština, syrština, koptština, ge'ez
    "akk", "sux", "peo",                   # akkadština, sumerština, stará perština
    "quc", "nah", "sga", "cs",             # k'iče', nahuatl, stará irština, čeština
}
# skupina = adresář v downloads/; tradice (pole `tradition`) je jiná osa
GROUPS = set(GROUP_TRADITIONS) | {
    "judaism", "persian", "tibetan", "japanese", "tamil", "sikh", "jain",
    "mesopotamia", "coptic", "syriac", "ethiopic", "americas", "celtic", "czech",
}
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
    errors, warnings = [], []
    try:
        works = load_works(reg)
    except ValueError as e:   # totéž work_id ve dvou souborech registru
        print("CHYBA:", e)
        return 1
    topics = yaml.safe_load((reg / "topics.yaml").read_text(encoding="utf-8")) or []

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
        if e.get("group") and e["group"] not in GROUPS:
            errors.append(f"{wid}: neznámá skupina {e['group']} (doplň GROUPS ve validate.py)")
        trad = e.get("tradition")
        for t in ([trad] if isinstance(trad, str) else (trad or [])):
            if t not in TRADITIONS:
                errors.append(f"{wid}: neznámá tradice {t} (doplň TRADITIONS v retrieval.py)")
        adapter = (e.get("source") or {}).get("adapter")
        if adapter and adapter not in sources.ADAPTERS:
            errors.append(f"{wid}: neznámý adaptér {adapter}")
        if adapter and e.get("chapters"):
            warnings.append(f"{wid}: kapitoly dává adaptér {adapter}, detektor se ignoruje")
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
    in_adapter_dir = adapter_dirs(works.values())
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in (".txt", ".pdf"):
            continue
        rel = nfc(str(f.relative_to(root)))
        if f.name in EXCLUDE_NAMES or f.name.startswith("._"):
            continue
        if any(m in rel for m in SKIP_DIR_MARKERS + EXCLUDE_PATH_MARKERS):
            continue
        if rel.startswith(in_adapter_dir):
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

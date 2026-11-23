"""Korán z Tanzilu (tanzil.net), formát „txt-2": jeden verš na řádek
`súra|ája|text`, na konci souboru licenční blok za `#`.

Kapitola = súra (114), ref = číslo súry. Verše jdou do textu kapitoly každý
na svůj řádek se znakem konce áji a číslem (`… ﴿٧﴾`), jak je tiskne i sám
Tanzil — citace verše tak zůstane v chunku vidět. Text veršů se nemění:
licence (CC BY 3.0) povoluje jen doslovné kopie, proto `CLEAN = False`
a soubor v `downloads/` leží i s licenčním blokem.

Názvy súr bere z `quran-data.xml` (metadata Tanzilu) vedle textu, když tam
je; jinak je nadpis jen „Súra N".
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from sources import Chapter

CLEAN = False
METADATA_FILE = "quran-data.xml"
SURA_COUNT = 114
ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def sura_names(file: Path) -> dict[int, tuple[str, str]]:
    """{súra: (arabský název, přepis)} z metadat Tanzilu, nebo {}."""
    meta = file.parent / METADATA_FILE
    if not meta.exists():
        return {}
    tree = etree.parse(str(meta))
    return {int(s.get("index")): (s.get("name") or "", s.get("tname") or "")
            for s in tree.iter("sura")}


def parse(lines) -> dict[int, list[tuple[int, str]]]:
    """Řádky txt-2 → {súra: [(ája, text)]}; licenční blok a prázdné řádky pryč."""
    suras: dict[int, list[tuple[int, str]]] = {}
    for n, line in enumerate(lines, 1):
        line = line.rstrip("\r\n")
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ValueError(f"řádek {n}: čekám súra|ája|text, je {line[:60]!r}")
        suras.setdefault(int(parts[0]), []).append((int(parts[1]), parts[2]))
    return suras


def extract(file: Path, entry: dict | None = None) -> list[Chapter]:
    with open(file, encoding="utf-8") as f:
        suras = parse(f)
    if sorted(suras) != list(range(1, SURA_COUNT + 1)):
        raise ValueError(f"{file}: čekám súry 1–{SURA_COUNT}, je jich {len(suras)}")
    names = sura_names(file)
    out = []
    for s in sorted(suras):
        ayas = suras[s]
        if [a for a, _ in ayas] != list(range(1, len(ayas) + 1)):
            raise ValueError(f"{file}: súra {s} nemá souvislá čísla áj")
        ar, tr = names.get(s, ("", ""))
        heading = f"Súra {s}" + (f" — {ar} ({tr})" if ar else "")
        text = "\n".join(f"{t} ﴿{str(a).translate(ARABIC_DIGITS)}﴾" for a, t in ayas)
        out.append(Chapter(ref=str(s), heading=heading, text=text))
    return out

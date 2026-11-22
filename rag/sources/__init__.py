"""Adaptéry zdrojů: strukturovaný originál → kapitoly pro ingest.

Strukturované zdroje (TEI, číslované verše) už kapitoly i čísla veršů nesou,
takže je nemá smysl převádět do TXT a hádat zpátky regexem. Adaptér je modul
v tomhle balíku se dvěma funkcemi:

    extract(file: Path, entry: dict) -> list[Chapter]
        jeden soubor (cesta `path` v registru) → kapitoly v pořadí dokumentu
    scan_works(root: Path) -> list           (volitelné)
        zdroje, kde je děl stovky a registr se generuje (Perseus)

a příznakem `CLEAN`: False znamená, že text jde do korpusu tak, jak ho zdroj
dodal (Tanzil licencí zakazuje text měnit), jinak ho ingest prožene
`clean_text.clean`.

Dílo z registru adaptér zapne polem `source: {adapter: <jméno>}` ve
works.yaml (nebo works.d/*.yaml); bez něj jde dílo dál cestou TXT/PDF
a detektoru kapitol.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass


@dataclass
class Chapter:
    ref: str                 # citační klíč kapitoly: '2', '1.3'
    heading: str             # nadpis pro čtenáře
    text: str
    level: int = 1
    parent_ref: str | None = None


# jméno v registru → modul v balíku sources
ADAPTERS = {
    "perseus": "sources.perseus",
    "tanzil": "sources.tanzil",
    "kanripo": "sources.kanripo",
}


def get(name: str):
    """Modul adaptéru podle jména z registru; neznámé jméno je chyba registru."""
    if name not in ADAPTERS:
        raise KeyError(f"neznámý adaptér zdroje: {name} (známé: {', '.join(sorted(ADAPTERS))})")
    return importlib.import_module(ADAPTERS[name])

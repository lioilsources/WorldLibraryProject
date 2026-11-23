"""Perseus TEI jako adaptér. Logika zůstává v `perseus_tei.py` (používá ji
i `verse_refs.py`); tady je jen rozhraní adaptéru, aby ingest volal všechny
strukturované zdroje stejně."""

from __future__ import annotations

from pathlib import Path

import perseus_tei as pt
from sources import Chapter

CLEAN = True
scan_works = pt.scan_works


def extract(file: Path, entry: dict | None = None) -> list[Chapter]:
    chapters, _ = pt.extract(file)
    return [Chapter(ref=c.ref, heading=c.heading, text=c.text, level=c.level) for c in chapters]

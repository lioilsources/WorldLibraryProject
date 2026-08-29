#!/usr/bin/env python3
"""Vyrobí výřezy z korpusu pro tests/fixtures (jednou, výsledek je v gitu).

Každý výřez je dost velký, aby detektor ukázal svůj vzor (obsah + první
kapitoly), a dost malý, aby nezatížil repo (~50–120 kB). Celé soubory se
berou jen u čínských textů (jsou malé) — Tao 81 kapitol, Hovory 20.

    python3 tests/make_fixtures.py --input-dir ../downloads
"""

import argparse
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from ingest_books import extract_pdf, strip_gutenberg_boilerplate  # noqa: E402

OUT = Path(__file__).parent / "fixtures"

# (název fixture, cesta, řádky od, řádky do) — None = celý soubor
TXT = [
    ("dao_de_jing.txt", "chinese/laozi/dao_de_jing.txt", None, None),
    ("analects.txt", "chinese/konfucius/analects.txt", None, None),
    ("mengzi.txt", "chinese/mengzi/mengzi.txt", None, None),
    ("zhuangzi_head.txt", "chinese/zhuangzi/zhuangzi.txt", 0, 1900),
    ("beowulf_hall_head.txt", "european/beowulf/pg16328.txt", 0, 1300),
    ("maha12_head.txt", "sanskrit/mahabharata/maha12.txt", 0, 3400),
    ("maha08_head.txt", "sanskrit/mahabharata/maha08.txt", 0, 700),
    ("edda_poetic_head.txt", "european/edda/poetic/pg73533.txt", 0, 1600),
    ("edda_prose_head.txt", "european/edda/prose/pg18947.txt", 0, 1500),
    ("rigveda_head.txt", None, 0, 900),   # z PDF
]
PDF = [
    ("dhammapada.txt", "pali/tipitaka/Suttapiṭaka/Khuddakanikāyo/Dhammapadapāḷi.pdf", None),
    ("sagathavagga_head.txt", "pali/tipitaka/Suttapiṭaka/Saṃyuttanikāyo/Sagāthāvaggo.pdf", 40),
    ("hegel_band03_head.txt", "european_philosophy/hegel/hegel_werke_band03_phaenomenologie.pdf", 60),
    ("rigveda_head.txt", "sanskrit/vedy/rigveda_complete_sanskrit.pdf", 12),
]


def resolve(root: Path, rel: str) -> Path:
    p = root / rel
    if p.exists():
        return p
    return root / unicodedata.normalize("NFD", rel)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", default="../downloads")
    args = ap.parse_args()
    root = Path(args.input_dir).resolve()
    OUT.mkdir(exist_ok=True)
    for name, rel, a, b in TXT:
        if rel is None:
            continue
        text = strip_gutenberg_boilerplate(resolve(root, rel).read_text(encoding="utf-8", errors="replace"))
        lines = text.split("\n")
        piece = lines if a is None else lines[a:b]
        (OUT / name).write_text("\n".join(piece), encoding="utf-8")
        print(f"{name:<26} {len(piece):>6} řádků")
    for name, rel, pages in PDF:
        pg, _ = extract_pdf(resolve(root, rel))
        piece = pg if pages is None else pg[:pages]
        (OUT / name).write_text("\n".join(piece), encoding="utf-8")
        print(f"{name:<26} {len(piece):>6} stran")
    return 0


if __name__ == "__main__":
    sys.exit(main())

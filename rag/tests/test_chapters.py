"""Detektory kapitol nad výřezy skutečných souborů korpusu (tests/fixtures).

Fixture jsou prvních N řádků + charakteristický výřez každého typu
(vyrobené tests/make_fixtures.py, v gitu), takže test nepotřebuje
downloads/. Očekávané počty pocházejí z běhu nad celými soubory
(29. 8. 2026): Tao 81, Hovory 20, Mencius 14 svazků, Zhuangzi 33,
Beowulf/Hall 43, Mahábhárata 12 = 363 sekcí, Dhammapada 26 vagg.
"""

from pathlib import Path

import pytest

from chapters import detect, is_front_matter, marks_from_toc, build
from clean_text import clean

FIX = Path(__file__).parent / "fixtures"


def lines_of(name: str, lang: str = "en", kind: str = "txt") -> list[str]:
    return clean((FIX / name).read_text(encoding="utf-8"), lang, kind).split("\n")


@pytest.mark.parametrize("name,lang,detector,expected_refs", [
    ("dao_de_jing.txt", "lzh", "zh_zhang", [str(i) for i in range(1, 82)]),
    ("analects.txt", "lzh", "zh_lunyu", [str(i) for i in range(1, 21)]),
])
def test_chinese_full(name, lang, detector, expected_refs):
    ch = detect(lines_of(name, lang), detector)
    assert [c.ref for c in ch if c.level == 1] == expected_refs


def test_mengzi_two_levels():
    ch = detect(lines_of("mengzi.txt", "lzh"), "zh_juan")
    assert sum(c.level == 1 for c in ch) == 14
    assert sum(c.level == 2 for c in ch) > 200
    assert ch[1].path.startswith("卷之一梁惠王上 › ")


def test_zhuangzi_skips_contents():
    ch = detect(lines_of("zhuangzi_head.txt"), "en_chapter_roman")
    refs = [c.ref for c in ch if c.heading != "(úvod)"]
    assert refs[:2] == ["1", "2"]          # fixture je zkrácená na 1 900 řádků
    assert "HAPPY EXCURSIONS" in ch[1].heading or "TRANSCENDENTAL" in ch[1].heading


def test_beowulf_hall_titles():
    ch = detect(lines_of("beowulf_hall_head.txt"), "roman_dot_upper_title")
    body = [c for c in ch if c.heading != "(úvod)"]
    assert body[0].heading.startswith("I. THE LIFE AND DEATH OF SCYLD")
    assert body[1].ref == "2"


def test_mbh_section_ranges_and_crlf():
    ch = detect(lines_of("maha12_head.txt"), "mbh_section")
    refs = [c.ref for c in ch]
    assert refs[:3] == ["1", "2", "3"]
    assert "34" in refs  # 'SECTION XXXIV-XXXV' je dvojsekce


def test_mbh_bare_number_monotonic():
    ch = detect(lines_of("maha08_head.txt"), "mbh_bare_number")
    assert [c.ref for c in ch][:5] == ["1", "2", "3", "4", "5"]


def test_pali_vagga_levels():
    ch = detect(lines_of("dhammapada.txt", "pi", "pdf"), "pali_vagga")
    assert [c.heading for c in ch][:2] == ["1. Yamakavaggo", "2. Appamādavaggo"]
    assert len(ch) == 26
    assert not any("Vipassana" in c.text(lines_of("dhammapada.txt", "pi", "pdf")) for c in ch)


def test_pali_three_levels():
    ch = detect(lines_of("sagathavagga_head.txt", "pi", "pdf"), "pali_vagga")
    levels = {c.level for c in ch}
    assert levels == {1, 2, 3}
    sutta = next(c for c in ch if c.level == 3)
    assert sutta.path.count(" › ") == 2


def test_edda_poetic_pairs_headings():
    ch = detect(lines_of("edda_poetic_head.txt"), "edda_poetic")
    poems = [c for c in ch if c.level == 3]
    assert poems and any("VOLUSPO" in c.heading for c in poems)
    assert any(c.level == 2 and c.heading.startswith("LAYS OF") for c in ch)
    assert not any("INDEX" in c.heading for c in poems)


def test_edda_prose_parts_and_chapters():
    ch = detect(lines_of("edda_prose_head.txt"), "edda_prose")
    parts = [c for c in ch if c.level == 1 and c.heading != "(úvod)"]
    chaps = [c for c in ch if c.level == 2]
    assert parts[0].heading.startswith("THE FOOLING OF GYLFE")
    assert chaps[0].ref == "1" and chaps[0].path.startswith("THE FOOLING OF GYLFE")


def test_hegel_toc_guided():
    ch = detect(lines_of("hegel_band03_head.txt", "de", "pdf"), "hegel")
    tops = [c.heading for c in ch if c.level == 1 and c.heading != "(úvod)"]
    assert tops[:1] == ["Vorrede"]         # Einleitung je až za 60. stranou fixture


def test_rigveda_uppercase_only():
    ch = detect(lines_of("rigveda_head.txt"), "rigveda_hymn")
    assert ch[0].heading.startswith("BOOK THE FIRST")
    assert all(c.heading.upper().startswith(("BOOK", "HYMN", "H YMN")) for c in ch if c.heading != "(úvod)")


def test_toc_marks_and_front_matter():
    marks = marks_from_toc([(1, "Cover", 1), (1, "Fargard I", 2), (2, "Fargard I.1", 2), (1, "Index", 4)], [0, 5, 9, 14])
    ch = build(list(map(str, range(20))), marks)
    assert [c.heading for c in ch] == ["Cover", "Fargard I", "Index"]
    assert [is_front_matter(c.heading) for c in ch] == [True, False, True]

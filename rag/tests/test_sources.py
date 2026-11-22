"""Adaptéry zdrojů (rag/sources) a registr po souborech (works.d).

Fixture tests/fixtures/tanzil je doslovný výřez z Tanzilu: Fátiha celá,
z ostatních súr první dvě áje, licenční blok, metadata prvních tří súr.
"""

import json
import unicodedata
from pathlib import Path

import pytest

import ingest_books as ib
import sources
from sources import tanzil

FIX = Path(__file__).parent / "fixtures" / "tanzil"
QURAN = FIX / "quran-uthmani.txt"


def test_tanzil_chapters_are_suras():
    chs = tanzil.extract(QURAN)
    assert len(chs) == 114
    assert [c.ref for c in chs] == [str(i) for i in range(1, 115)]
    assert chs[0].heading == "Súra 1 — الفاتحة (Al-Faatiha)"
    assert chs[113].heading == "Súra 114"           # bez metadat jen číslo
    assert len(chs[0].text.split("\n")) == 7


def test_tanzil_text_is_verbatim_with_aya_markers():
    chs = tanzil.extract(QURAN)
    src = [line.split("|", 2)[2] for line in QURAN.read_text(encoding="utf-8").splitlines()
           if line[:1].isdigit()]
    lines = [ln for c in chs for ln in c.text.split("\n")]
    assert len(lines) == len(src)
    for out, orig in zip(lines, src):
        body, marker = out.rsplit(" ﴿", 1)
        assert body == orig                        # licence: text se nemění
        assert marker.endswith("﴾")
    assert chs[0].text.split("\n")[6].endswith("﴿٧﴾")
    assert "PLEASE DO NOT REMOVE" not in "".join(c.text for c in chs)


def test_tanzil_rejects_broken_input(tmp_path):
    bad = tmp_path / "q.txt"
    bad.write_text("1|1|a\n1|3|b\n", encoding="utf-8")
    with pytest.raises(ValueError):
        tanzil.extract(bad)
    bad.write_text("1|1|a\nnesmysl\n", encoding="utf-8")
    with pytest.raises(ValueError):
        tanzil.extract(bad)


def test_unknown_adapter():
    with pytest.raises(KeyError):
        sources.get("nic")
    assert sources.get("tanzil").CLEAN is False


def test_ingest_adapter_skips_clean_and_chunks_by_sura(tmp_path):
    entry = {"id": "ar.quran", "group": "islam", "title": "القرآن", "lang_original": "ar",
             "lang_corpus": "ar", "tradition": ["islam"], "source": {"adapter": "tanzil"}}
    writer = ib.Writer(tmp_path, stats_only=False)
    stats = {"adapter": 0}
    st = ib.ingest_adapter(QURAN, Path("islam/quran/quran-uthmani.txt"), entry, writer, "t", stats)
    writer.close()
    assert stats["adapter"] == 1 and st["chapters"] == 114
    chunks = [json.loads(x) for x in (tmp_path / "books.jsonl").read_text(encoding="utf-8").splitlines()]
    work = json.loads((tmp_path / "works.jsonl").read_text(encoding="utf-8"))
    assert work["tradition"] == ["islam"]
    assert chunks[0]["id"] == "ar.quran:0001:0000" and chunks[0]["lang"] == "ar"
    # chunk nekříží súru a nese vokalizaci beze změny
    assert all(c["chapter_id"].startswith("ar.quran:") for c in chunks)
    first = QURAN.read_text(encoding="utf-8").split("\n")[0].split("|", 2)[2]
    assert chunks[0]["text"].startswith(unicodedata.normalize("NFC", first))
    assert unicodedata.is_normalized("NFC", chunks[0]["text"])


def test_works_d_is_merged_and_duplicates_fail(tmp_path):
    (tmp_path / "works.yaml").write_text("a.x:\n  path: a/x.txt\n", encoding="utf-8")
    (tmp_path / "works.d").mkdir()
    (tmp_path / "works.d" / "islam.yaml").write_text("ar.q:\n  path: islam/q.txt\n", encoding="utf-8")
    assert set(ib.load_works(tmp_path)) == {"a.x", "ar.q"}
    (tmp_path / "works.d" / "zz.yaml").write_text("a.x:\n  path: b.txt\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ib.load_works(tmp_path)


def test_registry_has_quran_via_adapter():
    works = ib.load_works(Path(__file__).parent.parent / "registry")
    q = works["ar.quran"]
    assert q["source"]["adapter"] == "tanzil" and q["lang_corpus"] == "ar"

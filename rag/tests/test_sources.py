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



# --- Kanripo ------------------------------------------------------------------

from sources import kanripo  # noqa: E402

KR = Path(__file__).parent / "fixtures" / "kanripo"


def test_kanripo_tls_chapters_from_org_headings():
    chs = kanripo.extract(KR / "tls")
    assert [(c.ref, c.heading) for c in chs] == [("1", "逍遙遊第一"), ("2", "齊物論第二")]
    first = chs[0].text
    assert first.startswith("北冥有魚，其名為鯤。鯤之大，不知其幾千里也。")
    assert "¶" not in first and "<pb:" not in first and "#" not in first
    # řádky odstavce spojené bez mezer, odstavce na řádek
    assert " " not in first and "\n" in first


def test_kanripo_strips_interlinear_notes():
    chs = kanripo.extract(KR / "nanhua")            # bez nadpisů → kapitola na svazek
    assert [c.ref for c in chs] == ["1"]
    text = chs[0].text
    assert "北⬤有魚其名爲鯤鯤之大不知其幾千里" in text
    assert "郭象" in text                           # titulková řádka zůstává, komentář ne
    assert "(" not in text and "音義曰" not in text and "司馬彪" not in text
    kept = kanripo.extract(KR / "nanhua", {"source": {"strip_notes": False}})[0].text
    assert "音義曰" in kept


def test_kanripo_chapter_and_drop_regex():
    src = {"chapter_re": "^　　[^　]+第[一二三四五六七八九十]+$",
           "drop_re": "^(欽定四庫全書|　*墨子卷)"}
    chs = kanripo.extract(KR / "mozi", {"source": src})
    assert [(c.ref, c.heading) for c in chs] == [("1", "親士第一")]
    assert chs[0].text.startswith("入國而不存其士則亡國矣")
    assert "四庫" not in chs[0].text


def test_adapter_dir_files_are_not_ingested_twice():
    entries = [{"id": "zh.x", "path": "chinese/kanripo/KR1", "source": {"adapter": "kanripo"}},
               {"id": "zh.y", "path": "chinese/y.txt"}]
    prefixes = ib.adapter_dirs(entries)
    assert "chinese/kanripo/KR1/KR1_001.txt".startswith(prefixes)
    assert not "chinese/kanripo/KR10/KR10_001.txt".startswith(prefixes)

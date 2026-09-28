"""ingest_law.py nad výřezy skutečných stránek e-Sbírky (tests/fixtures/law).

Fixture jsou stránka 0 z cache REST (23. 9. 2026): Listina celá (219
fragmentů), NOZ prvních 420 fragmentů (Část první, § 1–129), Ústava prvních 160
(preambule, Hlava první–druhá). Očekávané počty pocházejí z běhu nad celými
předpisy téhož dne: NOZ 3 106 § / 3 496 kapitol / 3 166 chunků (medián 312 zn.,
5 chunků nad 1 500 — řádky výčtu v § 3080), ZP 430 § / 608 / 575, Listina
44 čl. / 54 / 46, Ústava 114 čl. / 123 / 118.
"""

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from ingest_books import Writer  # noqa: E402
from ingest_law import (  # noqa: E402
    CHUNK, LEAF_LEVEL, Section, Unit, build_tree, ingest_act, pack_units, ref_from_eli, ref_span, strip_xhtml,
)

FIX = Path(__file__).parent / "fixtures" / "law"


class Mem(Writer):
    """Writer do paměti — testy nepíšou soubory."""

    def __init__(self):
        super().__init__(None, True)
        self.rows = {"chunks": [], "chapters": [], "works": []}

    def write(self, kind, rec):
        super().write(kind, rec)
        self.rows[kind].append(rec)


def run(fixture: str, meta: str, act: dict):
    frags = json.load(open(FIX / fixture, encoding="utf-8"))["seznam"]
    m = json.load(open(FIX / meta, encoding="utf-8"))
    w, unknown = Mem(), Counter()
    st = ingest_act(act, m, frags, w, "2026-09-23T00:00:00+00:00", unknown)
    return st, w.rows, unknown


LISTINA = {"cislo": "2/1993", "sb": "sb", "rok": 1993, "n": 2, "id": "cz.sb.1993.2", "group": "ustavni",
           "aliases": ["listina zakladnich prav", "2/1993"], "abbr": ["LZPS"]}
NOZ = {"cislo": "89/2012", "sb": "sb", "rok": 2012, "n": 89, "id": "cz.sb.2012.89", "group": "obcanske",
       "short": "občanský zákoník"}
USTAVA = {"cislo": "1/1993", "sb": "sb", "rok": 1993, "n": 1, "id": "cz.sb.1993.1", "group": "ustavni"}


# --- čistá logika -----------------------------------------------------------------

def test_strip_xhtml_keeps_markers_drops_tags():
    x = '<var>(1)</var> <czechvoc-termin koncept-id="279703">Kupní smlouvou</czechvoc-termin> se ' \
        'prodávající zavazuje, viz <a data-odkaz-id="1" href="#" class="int_odkaz">odstavec 2</a>.'
    assert strip_xhtml(x) == "(1) Kupní smlouvou se prodávající zavazuje, viz odstavec 2."
    assert strip_xhtml(None) == ""
    assert strip_xhtml("<var>a)</var> název&nbsp;nadace,") == "a) název nadace,"


def test_ref_from_eli():
    assert ref_from_eli(".../norma/cast_4/par_2079") == "§ 2079"
    assert ref_from_eli(".../par_2a") == "§ 2a"
    assert ref_from_eli(".../prilohy/hlava_1/cl_10") == "čl. 10"
    assert ref_from_eli(".../par_314/frag_7978763") is None


def test_ref_span():
    assert ref_span("§ 2079", "§ 2079") == "§ 2079"
    assert ref_span("§ 2079 odst. 1", "§ 2079 odst. 3") == "§ 2079 odst. 1–3"
    assert ref_span("čl. 1 odst. 1", "čl. 1 odst. 2") == "čl. 1 odst. 1–2"
    assert ref_span("§ 1", "§ 2 odst. 1") == "§ 1 – § 2 odst. 1"


def test_pack_units_splits_long_paragraph_by_odstavce_and_keeps_prefix():
    # čtyři odstavce po 600 znacích: do 1 500 se vejdou vždy dva
    units = [Unit(f"§ 9 odst. {i}", [f"({i}) " + ("x" * 596)]) for i in range(1, 5)]
    sec = Section(1, LEAF_LEVEL, None, "§ 9", "Nadpis", "§ 9 Nadpis", units=units)
    pieces = pack_units(sec, CHUNK)
    assert [(a, b) for _, a, b in pieces] == [("§ 9 odst. 1", "§ 9 odst. 2"), ("§ 9 odst. 3", "§ 9 odst. 4")]
    assert all(t.startswith("§ 9 Nadpis\n") for t, _, _ in pieces)
    assert all(len(t) <= CHUNK + len("§ 9 Nadpis\n") for t, _, _ in pieces)


def test_pack_units_one_giant_line_falls_back_to_sentence_chunker():
    # § 3080 NOZ: jeden odstavec = 48 k znaků výčtu — dělí se větným chunkerem,
    # ref zůstává na odstavci, každý kus nese prefix paragrafu
    line = " ".join(f"{i}. zákon č. {i}/1990 Sb., o něčem." for i in range(1, 900))
    sec = Section(1, LEAF_LEVEL, None, "§ 3080", "", "§ 3080", units=[Unit("§ 3080", [line])])
    pieces = pack_units(sec, CHUNK)
    assert len(pieces) > 10
    assert all(a == b == "§ 3080" for _, a, b in pieces)
    assert all(len(t) <= CHUNK + 20 for t, _, _ in pieces)
    assert "".join(t.replace("§ 3080\n", "") for t, _, _ in pieces).replace(" ", "") == line.replace(" ", "")


def test_tree_from_eli_attaches_orphans_to_nearest_ancestor():
    frags = [
        {"id": 1, "eli": "/e/dokument/norma", "kodTypuFragmentu": "Virtual_Norma"},
        {"id": 2, "eli": "/e/dokument/norma/cast_1", "kodTypuFragmentu": "Cast", "xhtml": "<var>ČÁST PRVNÍ</var>"},
        # odstavec, jehož § ve výřezu chybí → visí na části, ne v chybě
        {"id": 3, "eli": "/e/dokument/norma/cast_1/par_5/odst_1", "kodTypuFragmentu": "Odstavec_Dc", "xhtml": "(1) x"},
    ]
    root = build_tree(frags)
    norma = root.children[0]
    assert [c.kind for c in norma.children] == ["Cast"]
    assert [c.kind for c in norma.children[0].children] == ["Odstavec_Dc"]


# --- skutečné výřezy ------------------------------------------------------------------

@pytest.mark.parametrize("fixture,meta,act,paragraphs,chapters,chunks,first_ref", [
    ("listina_2_1993.json", "listina_meta.json", LISTINA, 44, 54, 46, "čl. 1"),
    ("noz_89_2012_head.json", "noz_meta.json", NOZ, 129, 151, 129, "§ 1"),
    ("ustava_1_1993_head.json", "ustava_meta.json", USTAVA, 42, 45, 43, "čl. 1"),
])
def test_counts_and_invariants(fixture, meta, act, paragraphs, chapters, chunks, first_ref):
    st, rows, unknown = run(fixture, meta, act)
    assert (st["paragraphs"], st["chapters"], st["chunks"]) == (paragraphs, chapters, chunks)
    assert not unknown, f"neznámé typy s textem: {unknown}"
    ch, ck = rows["chapters"], rows["chunks"]
    leaves = [c for c in ch if c["level"] == LEAF_LEVEL and c["ref"]]
    assert leaves[0]["ref"] == first_ref
    # § jdou v pořadí dokumentu (surové pořadí stránek = pořadí dokumentu)
    nums = [int("".join(ch for ch in c["ref"] if ch.isdigit())) for c in leaves]
    assert nums == sorted(nums)
    # každý rodič existuje a předchází dítě; každý chunk ukazuje na svou kapitolu
    by_id = {c["id"]: c for c in ch}
    for c in ch:
        if c["parent_id"]:
            assert c["parent_id"] in by_id and by_id[c["parent_id"]]["ordinal"] < c["ordinal"]
    assert all(c["chapter_id"] in by_id for c in ck)
    assert len({c["id"] for c in ck}) == len(ck)
    # chunk nikdy nekříží §: chapter_ref na chunku = ref jeho kapitoly
    assert all(c["chapter_ref"] == by_id[c["chapter_id"]]["ref"] for c in ck)
    assert all(len(c["text"]) <= CHUNK + 60 for c in ck)   # prefix „§ N Nadpis" nad limit
    assert all(c["chunk_index"] == i for i, c in enumerate(ck))


def test_noz_paths_and_titles_read_like_the_statute():
    st, rows, _ = run("noz_89_2012_head.json", "noz_meta.json", NOZ)
    ch = {c["ref"]: c for c in rows["chapters"] if c["ref"]}
    assert ch["§ 1"]["path"] == ("ČÁST PRVNÍ OBECNÁ ČÁST › HLAVA I PŘEDMĚT ÚPRAVY A JEJÍ ZÁKLADNÍ ZÁSADY › "
                                 "Díl 1 Soukromé právo › § 1")
    assert ch["§ 1"]["citation"] == "§ 1 zákona č. 89/2012 Sb."
    assert ch["§ 1"]["stale_url"] == "/sb/2012/89/2026-01-01#par_1"
    # nadpis pod paragrafem se dostane do path i do textu chunku
    p3 = [c for c in rows["chunks"] if c["chapter_ref"] == "§ 3"]
    assert len(p3) == 1 and p3[0]["title"] == "89/2012 Sb. (§ 3 odst. 1–3)"
    assert p3[0]["text"].startswith("§ 3\n(1) Soukromé právo chrání důstojnost")
    assert (p3[0]["ref_start"], p3[0]["ref_end"]) == ("§ 3 odst. 1", "§ 3 odst. 3")
    # § bez odstavců: ref bez „odst.", title bez rozsahu
    p12 = [c for c in rows["chunks"] if c["chapter_ref"] == "§ 12"]
    assert p12[0]["title"] == "89/2012 Sb. (§ 12)" and p12[0]["ref_start"] == "§ 12"
    w = rows["works"][0]
    assert w["name_cs"] == "Zákon č. 89/2012 Sb., občanský zákoník"
    assert w["urn"] == "/eli/cz/sb/2012/89" and w["source_path"] == "/sb/2012/89/2026-01-01"
    assert w["edition"].startswith("úplné znění účinné od 2026-01-01")
    assert w["form"] == "zakonik" and w["lang_original"] == w["lang_corpus"] == "cs"


def test_listina_lives_in_the_annex_and_ustava_keeps_its_preamble():
    _, rows, _ = run("listina_2_1993.json", "listina_meta.json", LISTINA)
    ch = rows["chapters"]
    assert any(c["level"] == 1 and c["heading"] == "LISTINA ZÁKLADNÍCH PRÁV A SVOBOD" for c in ch)
    assert [c["heading"] for c in ch if c["level"] == 2][:2] == [
        "HLAVA PRVNÍ OBECNÁ USTANOVENÍ", "HLAVA DRUHÁ LIDSKÁ PRÁVA A ZÁKLADNÍ SVOBODY"]
    cl3 = [c for c in rows["chunks"] if c["chapter_ref"] == "čl. 3"][0]
    assert cl3["text"].startswith("čl. 3\n(1) Základní práva a svobody se zaručují všem")
    assert rows["works"][0]["aliases"] == ["listina zakladnich prav", "2/1993"]
    assert rows["works"][0]["abbr"] == ["LZPS"]

    _, rows, _ = run("ustava_1_1993_head.json", "ustava_meta.json", USTAVA)
    pre = [c for c in rows["chapters"] if c["heading"] == "Preambule"]
    assert len(pre) == 1 and pre[0]["level"] == 1
    pre_chunk = [c for c in rows["chunks"] if c["chapter_id"] == pre[0]["id"]]
    assert pre_chunk and "My, občané České republiky" in pre_chunk[0]["text"]

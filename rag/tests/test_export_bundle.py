"""Export do Kindlify bundlu nad řádky ve tvaru, v jakém je vrací Postgres.

Fixture je zmenšený Tao te ťing (dvě části, čtyři kapitoly, druhá úroveň)
plus obohacení chunků — dost na to, aby se ukázalo zavěšení stromu,
sčítání termů zdola nahoru a IDF. Živý Postgres tu není a být nemusí:
`build_bundle` dostává hotové řádky, PG vrstva je jen `fetch_*`.
"""

import json

import pytest

from export_bundle import (
    asset_name, build_bundle, build_tree, chapter_label, chunk_terms,
    node_id, score_terms, slugify, validate_bundle, walk_nodes,
)

WORK = {
    "id": "zh.daodejing", "group": "chinese", "title": "道德經", "name_cs": "Tao te ťing",
    "author": "Lao-c'", "lang_original": "lzh", "lang_corpus": "lzh", "priority": 1,
    "summary_short": "Základní text taoismu.",
    "summary_medium": "Osmdesát jedna kapitol o Tau a Te.",
    "summary_long": "Základní text taoistické filosofie, 81 kapitol ve dvou knihách.",
}

# ordinal 1 a 4 jsou části (level 1), 2–3 a 5 kapitoly pod nimi (level 2).
CHAPTERS = [
    {"id": "zh.daodejing:0001", "ordinal": 1, "level": 1, "parent_id": None, "ref": "道",
     "heading": "道經", "heading_cs": "Kniha Taa", "path": "Kniha Taa",
     "summary_short": None, "summary_medium": "První kniha, kapitoly 1–37.", "summary_long": None},
    {"id": "zh.daodejing:0002", "ordinal": 2, "level": 2, "parent_id": "zh.daodejing:0001", "ref": "1",
     "heading": "體道第一", "heading_cs": "Tao, které lze pojmenovat", "path": "Kniha Taa › 1",
     "summary_short": "O nevyslovitelnosti Taa.", "summary_medium": None, "summary_long": None},
    {"id": "zh.daodejing:0003", "ordinal": 3, "level": 2, "parent_id": "zh.daodejing:0001", "ref": "2",
     "heading": "養身第二", "heading_cs": None, "path": "Kniha Taa › 2",
     "summary_short": None, "summary_medium": None, "summary_long": None},
    {"id": "zh.daodejing:0004", "ordinal": 4, "level": 1, "parent_id": None, "ref": "德",
     "heading": "德經", "heading_cs": "Kniha Te", "path": "Kniha Te",
     "summary_short": None, "summary_medium": "Druhá kniha, kapitoly 38–81.", "summary_long": None},
    {"id": "zh.daodejing:0005", "ordinal": 5, "level": 2, "parent_id": "zh.daodejing:0004", "ref": "38",
     "heading": "論德第三十八", "heading_cs": "O ctnosti", "path": "Kniha Te › 38",
     "summary_short": None, "summary_medium": "Te jako přirozený projev Taa.", "summary_long": None},
]

def chunk(cs=(), orig=(), entities=(), quality=2):
    return {"keywords_cs": list(cs), "keywords_orig": list(orig),
            "entities": [{"name": e, "type": "person"} for e in entities], "quality": quality}

# „tao" je všude (nízké IDF), „wu-wei" jen v jedné kapitole (vysoké).
CHUNK_ROWS = {
    2: [chunk(cs=["tao", "nevyslovitelné"], orig=["道"]),
        chunk(cs=["tao", "nevyslovitelné"], orig=["道", "名"])],
    3: [chunk(cs=["tao", "nezasahování"], orig=["無為"], entities=["Lao-c'"])],
    5: [chunk(cs=["tao", "ctnost"], orig=["德"]),
        chunk(cs=["ctnost"], orig=["德"], quality=0)],   # balast se nepočítá
}


def bundle():
    return build_bundle(WORK, CHAPTERS, CHUNK_ROWS,
                        work_keywords=["tao", "te", "wu-wej"],
                        chapter_keywords={2: ["tao"], 5: ["ctnost"]},
                        generated_at="2026-01-01T00:00:00Z")


# --- jména a popisky ------------------------------------------------------------

def test_slug_a_jmeno_assetu():
    assert slugify("zh.daodejing") == "zh-daodejing"
    assert slugify("greek_latin.tlg0012.tlg001") == "greek-latin-tlg0012-tlg001"
    assert asset_name("zh-daodejing") == "zh_daodejing.json"


def test_label_predrazuje_ref_jen_kdyz_v_nadpisu_neni():
    assert chapter_label(CHAPTERS[1]) == "1 — Tao, které lze pojmenovat"          # heading_cs bez čísla
    assert chapter_label(CHAPTERS[2]) == "2 — 養身第二"                            # bez překladu → originál
    assert chapter_label({"ordinal": 9, "ref": "", "heading": "", "heading_cs": ""}) == "Kapitola 9"
    assert chapter_label({"ordinal": 9, "ref": "3", "heading_cs": "Kapitola 3 o vodě"}) == "Kapitola 3 o vodě"


# --- strom ----------------------------------------------------------------------

def test_strom_zavesi_kapitoly_podle_parent_id():
    tree = build_tree(WORK, CHAPTERS)
    assert tree["id"] == "root" and tree["kind"] == "book" and tree["label"] == "Tao te ťing"
    assert [c["id"] for c in tree["children"]] == [node_id(1), node_id(4)]
    kniha_taa = tree["children"][0]
    assert [c["id"] for c in kniha_taa["children"]] == [node_id(2), node_id(3)]
    assert kniha_taa["kind"] == "chapter" and kniha_taa["children"][0]["kind"] == "section"
    assert len(list(walk_nodes(tree))) == 6


def test_kapitola_s_neznamym_rodicem_visi_na_koreni():
    orphan = dict(CHAPTERS[1], parent_id="zh.daodejing:9999")
    tree = build_tree(WORK, [CHAPTERS[0], orphan])
    assert [c["id"] for c in tree["children"]] == [node_id(1), node_id(2)]


# --- termy ----------------------------------------------------------------------

def test_chunk_terms_rozlisi_druh_a_zahodi_balast():
    assert chunk_terms(chunk(cs=["tao"], orig=["道"], entities=["Lao-c'"])) == {
        ("tao", "word"), ("道", "orig"), ("Lao-c'", "entity")}
    assert chunk_terms(chunk(cs=["patička"], quality=0)) == set()
    assert chunk_terms(chunk(cs=["", "  ", "42", "x" * 41])) == set()


def test_vzacny_term_prebije_vsudypritomny():
    words = bundle()["words"]["nodes"]
    kap3 = {t["term"]: t["score"] for t in words[node_id(3)]["terms"]}
    assert kap3["nezasahování"] > kap3["tao"]      # IDF: „tao" je ve všech kapitolách


def test_termy_se_scitaji_zdola_nahoru():
    words = bundle()["words"]["nodes"]
    kniha_taa = {t["term"]: t["count"] for t in words[node_id(1)]["terms"]}
    assert kniha_taa["nevyslovitelné"] == 2        # ze dvou chunků kapitoly 1
    assert kniha_taa["nezasahování"] == 1          # z kapitoly 2, i když sama chunky nemá
    root = {t["term"]: t["count"] for t in words["root"]["terms"]}
    assert root["tao"] == 4                        # čtyři chunky z celého díla (pátý má quality 0)


def test_klicova_slova_dila_jdou_navrch_i_kdyz_v_chuncich_nejsou():
    terms = bundle()["words"]["nodes"]["root"]["terms"]
    assert terms[0]["score"] == 1.0
    assert "wu-wej" in [t["term"] for t in terms if t["score"] == 1.0]


def test_skore_je_v_rozsahu_pro_velikost_bubliny():
    for node in bundle()["words"]["nodes"].values():
        for term in node["terms"]:
            assert 0.3 <= term["score"] <= 1.0


def test_top_terms_orizne():
    counts = {(f"t{i}", "word"): 10 - i % 5 for i in range(30)}
    from collections import Counter
    assert len(score_terms(Counter(counts), {}, top=7)) == 7


# --- souhrny a manifest ---------------------------------------------------------

def test_souhrny_berou_delku_podle_uzlu_a_prazdne_vynechavaji():
    s = bundle()["summaries"]
    assert s["root"]["cs"].startswith("Základní text taoistické")   # dílo = long
    assert s[node_id(1)]["cs"] == "První kniha, kapitoly 1–37."     # kapitola = medium
    assert s[node_id(2)]["cs"] == "O nevyslovitelnosti Taa."        # fallback na short
    assert node_id(3) not in s                                      # kapitola bez souhrnu


def test_manifest():
    m = bundle()["manifest"]
    assert m["slug"] == "zh-daodejing" and m["title"] == "Tao te ťing"
    assert m["sourceLanguage"] == "lzh" and m["script"] == "han"
    assert m["pipelineVersion"].startswith("pg-1+")
    assert m["generatedAt"] == "2026-01-01T00:00:00Z"


def test_pipeline_version_se_meni_jen_s_obsahem():
    stejny = build_bundle(WORK, CHAPTERS, CHUNK_ROWS, work_keywords=["tao", "te", "wu-wej"],
                          chapter_keywords={2: ["tao"], 5: ["ctnost"]},
                          generated_at="2030-12-31T23:59:59Z")   # jiný čas, stejný obsah
    assert stejny["manifest"]["pipelineVersion"] == bundle()["manifest"]["pipelineVersion"]

    jiny = build_bundle(WORK, CHAPTERS, {**CHUNK_ROWS, 3: [chunk(cs=["prázdnota"])]},
                        work_keywords=["tao", "te", "wu-wej"],
                        chapter_keywords={2: ["tao"], 5: ["ctnost"]})
    assert jiny["manifest"]["pipelineVersion"] != bundle()["manifest"]["pipelineVersion"]


def test_dilo_bez_obohaceni_projde_a_da_aspon_kuratorska_slova():
    b = build_bundle(WORK, CHAPTERS, {}, work_keywords=["tao"], chapter_keywords={2: ["ctnost"]})
    validate_bundle(b)
    assert [t["term"] for t in b["words"]["nodes"]["root"]["terms"]] == ["tao"]
    assert [t["term"] for t in b["words"]["nodes"][node_id(2)]["terms"]] == ["ctnost"]
    assert node_id(3) not in b["words"]["nodes"]                  # bez termů se uzel vynechá


# --- kontrakt s Dartem ----------------------------------------------------------

def test_bundle_projde_validaci_a_je_serializovatelny():
    b = bundle()
    validate_bundle(b)
    assert json.loads(json.dumps(b, ensure_ascii=False)) == b


@pytest.mark.parametrize("rozbij,zprava", [
    (lambda b: b["manifest"].pop("script"), "script"),
    (lambda b: b["manifest"]["tree"]["children"].append(dict(b["manifest"]["tree"])), "duplicitní"),
    (lambda b: b["words"]["nodes"].__setitem__("c9999", {"terms": []}), "neznámý uzel"),
    (lambda b: b["words"]["nodes"]["root"]["terms"][0].__setitem__("count", "2"), "count"),
    (lambda b: b["summaries"].__setitem__("c9999", {"cs": "x"}), "neznámý uzel"),
])
def test_validace_chytne_rozchod_formatu(rozbij, zprava):
    b = bundle()
    rozbij(b)
    with pytest.raises(ValueError, match=zprava):
        validate_bundle(b)


# --- vrstva nad Postgresem ------------------------------------------------------

CATALOG_CHAPTER_COLS = ["id", "ordinal", "level", "parent_id", "ref", "heading", "heading_cs", "path",
                        "chunk_count", "summary_short", "summary_medium", "summary_long", "topic_ids"]


class FakeCursor:
    """Rozlišuje dotazy podle tabulky — dost na to, aby se rozbité
    rozbalení řádku poznalo bez běžícího Postgresu (SQL samotné ne)."""

    def __init__(self):
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if "chapters_v" in sql:
            self.rows = [tuple(ch.get(c) for c in CATALOG_CHAPTER_COLS) for ch in CHAPTERS]
        elif "FROM works" in sql:
            self.rows = [(["tao", "te"],)]
        elif "FROM chapters" in sql:
            self.rows = [(2, ["tao"]), (3, None)]
        elif "chunk_enrichment" in sql:
            self.rows = [(2, ["tao"], ["道"], [{"name": "Lao-c'", "type": "person"}], 2),
                         (2, ["tao"], None, None, None),
                         (None, ["mimo kapitolu"], None, None, 1)]
        else:
            raise AssertionError(f"neočekávaný dotaz: {sql}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    def cursor(self):
        return FakeCursor()


def test_fetch_keywords_a_chunk_rows_rozbali_radky():
    from export_bundle import fetch_chunk_rows, fetch_keywords
    work_kw, chapter_kw = fetch_keywords(FakeConn(), "zh.daodejing")
    assert work_kw == ["tao", "te"]
    assert chapter_kw == {2: ["tao"], 3: []}          # NULL keywords_cs → prázdný seznam

    rows = fetch_chunk_rows(FakeConn(), "zh.daodejing")
    assert set(rows) == {2, None}                      # chunk bez kapitoly zůstane pod None
    assert len(rows[2]) == 2
    assert rows[2][1] == {"keywords_cs": ["tao"], "keywords_orig": [], "entities": [], "quality": None}


def test_export_work_slozi_a_zvaliduje_bundle():
    from export_bundle import export_work
    b = export_work(FakeConn(), WORK, top=50, chapter_detail="medium")
    validate_bundle(b)
    assert b["manifest"]["slug"] == "zh-daodejing"
    root = {t["term"] for t in b["words"]["nodes"]["root"]["terms"]}
    assert "mimo kapitolu" in root                     # chunky bez kapitoly patří dílu
    assert {"tao", "te"} <= root

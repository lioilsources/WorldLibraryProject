"""Čistá logika retrievalu a katalogu bez DB a bez modelu."""

from catalog import corpus_note, pick_detail, summary_for
from hybrid import combine_rerank, dedup_passages, rrf
from pg_search import _bigram_query, _tsquery, query_terms
from llm_batch import input_sha, parse_json
from planner import find_chapter, from_json


def test_query_terms_drops_stopwords_and_folds():
    t = query_terms("Co říká Buddha o nibbáně a utrpení v Dhammapadě?")
    assert "nibbane" in t and "utrpeni" in t and "dhammapade" in t
    assert "buddha" in t
    assert not any(x in t for x in ("co", "rika", "říká", "o", "a", "v"))


def test_query_terms_keeps_cjk_and_numbers():
    assert query_terms("kapitola 37 無為") == ["37", "無為"]


def test_tsquery_prefix_for_long_terms():
    assert _tsquery(["nibbana", "sat"]) == "nibbana:* | sat"
    assert _bigram_query("道可道") == "道可 & 可道"
    assert _bigram_query("道") == ""


def test_rrf_prefers_multi_channel_hits():
    r = rrf({"vec": ["a", "b"], "fts": ["b", "c"]}, k=1)
    assert r[0][0] == "b"


def test_dedup_and_rerank():
    assert dedup_passages([{"id": "w:0001:0000#2"}, {"id": "w:0001:0000#0"}]) == ["w:0001:0000"]
    assert combine_rerank({"a": 0.05, "b": 0.04}, {"b": 10.0})[0][0] == "b"


def test_catalog_detail_and_summary():
    assert pick_detail(2, "auto") == "long" and pick_detail(9, "auto") == "medium" and pick_detail(40, "auto") == "short"
    assert pick_detail(40, "long") == "long"
    w = {"summary_medium": "První věta. Druhá věta.", "lang_original": "ang", "lang_corpus": "en", "edition": "Hall 1892"}
    assert summary_for(w, "short") == "První věta."
    assert summary_for(w, "long") == "První věta. Druhá věta."
    assert corpus_note(w) == "angličtina překlad (Hall 1892)"
    assert corpus_note({"lang_original": "pi", "lang_corpus": "pi"}) == "originál"


def test_planner_json_validation():
    plan = from_json({"intent": "catalog", "groups": ["pali", "nope"], "topics": ["smrt_nesmrtelnost", "x"],
                      "terms_orig": {"pi": ["nibbāna"], "zz": []}, "detail": "weird", "group_by": "topic"},
                     known_groups={"pali"}, known_topics={"smrt_nesmrtelnost"})
    assert plan.intent == "catalog" and plan.groups == ["pali"] and plan.topics == ["smrt_nesmrtelnost"]
    assert plan.terms_orig == {"pi": ["nibbāna"]} and plan.detail == "auto" and plan.group_by == "topic"
    assert from_json({"intent": "??"}, set(), set()).intent == "content"
    assert from_json(None, set(), set()) is None


def test_find_chapter_by_number_and_heading():
    chapters = [{"ordinal": 1, "level": 1, "ref": "1", "heading": "第一章"},
                {"ordinal": 8, "level": 1, "ref": "8", "heading": "第八章", "heading_cs": "Nejvyšší dobro je jako voda"},
                {"ordinal": 9, "level": 2, "ref": "1.2", "heading": "Kniha 1 › Oddíl 2"}]
    assert find_chapter(chapters, "kapitola 8")["ref"] == "8"
    assert find_chapter(chapters, "osmá kapitola")["ref"] == "8"
    assert find_chapter(chapters, "jako voda")["ref"] == "8"
    assert find_chapter(chapters, "nic takového") is None


def test_input_sha_carries_prompt_version_prefix():
    """Resume se v SQL ptá `input_sha NOT LIKE 'chunk-v1:%'` — Postgres sha1()
    nemá, takže verze musí být v hodnotě, ne dopočítaná dotazem."""
    a = input_sha("chunk-v1", "text")
    assert a.startswith("chunk-v1:") and len(a) == len("chunk-v1:") + 40
    assert not input_sha("chunk-v2", "text").startswith("chunk-v1:")
    assert input_sha("chunk-v1", "text") != input_sha("chunk-v1", "jiný")
    assert input_sha("chunk-v1", "a", "b") != input_sha("chunk-v1", "ab")


def test_parse_json_repairs_unquoted_value_and_missing_comma():
    """Qwen3 u dlouhé české hodnoty vynechá uvozovky i čárku před dalším
    klíčem — bez opravy padne ~4 % chunků na neparsovatelnou odpověď."""
    d = parse_json('```json\n{\n  "gloss_cs": Úryvek o "čistotě" a démonech.\n'
                   '  "keywords_cs": ["démon"],\n  "quality": 2,\n}\n```')
    assert d == {"gloss_cs": 'Úryvek o "čistotě" a démonech.', "keywords_cs": ["démon"], "quality": 2}


def test_parse_json_leaves_valid_json_alone():
    assert parse_json('{"a": "x", "b": [1, 2], "c": true, "d": null, "e": -3.5}') == \
        {"a": "x", "b": [1, 2], "c": True, "d": None, "e": -3.5}
    assert parse_json("žádný json") is None
    assert parse_json('text před {"a": 1} a za') == {"a": 1}


def test_enrich_keywords_split_and_dedup():
    """Model občas pošle seznam jako jeden řetězec („a, b, c"); bez rozdělení
    by se celá věta uložila jako jedno klíčové slovo a fulltext by ji netrefil."""
    from enrich_chunks import validate
    v = validate({"text": "Grendel a Kain"},
                 {"gloss_cs": "x", "keywords_cs": "Bůh, peklo, duše",
                  "keywords_orig": "Grendel; Kain", "quality": 2, "topics": []}, set())
    assert v["keywords_cs"] == ["Bůh", "peklo", "duše"]
    assert v["keywords_orig"] == ["Grendel", "Kain"]   # obojí je v textu
    assert validate({"text": "x"}, {"gloss_cs": "x", "keywords_cs": ["a", "A", "b"],
                                    "quality": 2, "topics": []}, set())["keywords_cs"] == ["a", "b"]
    assert validate({"text": "x"}, {"gloss_cs": "", "quality": 2}, set()) is None

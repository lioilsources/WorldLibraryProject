"""Text pro /ask musí být holý — hodinky ho zobrazí nebo předčítají.

Knihovník je promptovaný na markdown s citacemi „[1]"; na Apple Watch je
z hvězdiček a čísel jen šum. `to_plain` je poslední pojistka za instrukcí
v promptu, protože model ji občas neuposlechne.

Server tahá chromadb/openai/fastapi — na M2 v čistém pythonu nejsou, tak se
test přeskočí; ve venv na SPARKu proběhne.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
pytest.importorskip("chromadb", reason="server.py potřebuje chromadb")
pytest.importorskip("openai", reason="server.py potřebuje openai")

from server import RAGServer, to_plain  # noqa: E402


def test_strips_think_block():
    assert to_plain("<think>rozvaha\nnadvakrát</think>Odpověď.") == "Odpověď."


def test_strips_markdown_and_citations():
    got = to_plain("**Marcus Aurelius** říká [1], že smrt je *přirozená* [2, 3].")
    assert got == "Marcus Aurelius říká, že smrt je přirozená."


def test_collapses_whitespace_and_quote_blocks():
    got = to_plain("> Smrt je\n> přirozená.\n\n## Shrnutí\n\nNic víc.")
    assert got == "Smrt je přirozená. Shrnutí Nic víc."


def test_truncates_on_sentence_boundary():
    text = "Věta jedna je dost dlouhá. " * 40
    got = to_plain(text, limit=100)
    assert len(got) <= 101          # +1 za výpustku
    assert got.endswith(".…")


def test_short_text_is_untouched_and_has_no_ellipsis():
    assert to_plain("Krátká odpověď.", limit=100) == "Krátká odpověď."


def test_empty_input_survives():
    assert to_plain(None) == ""


def _hit(name_cs=None, title=None, work=None):
    return {"meta": {"name_cs": name_cs, "title": title, "work": work}}


def test_source_line_prefers_czech_name_dedups_and_caps():
    hits = [
        _hit(name_cs="Hovory k sobě"),
        _hit(name_cs="Hovory k sobě"),          # tentýž titul podruhé
        _hit(title="Enneades"),
        _hit(work="dhammapada"),
        _hit(name_cs="Zapomenutá"),             # čtvrtý název už se nevejde
    ]
    assert RAGServer._source_line(hits) == "Hovory k sobě · Enneades · dhammapada"


def test_source_line_empty_hits():
    assert RAGServer._source_line([]) == ""


def test_dangling_sentence_is_dropped():
    from server import drop_dangling_sentence
    got = drop_dangling_sentence("Seneca byl filozof. V úryvcích se zabývá hodnotou")
    assert got == "Seneca byl filozof."


def test_dangling_drop_keeps_text_when_nothing_useful_would_remain():
    from server import drop_dangling_sentence
    text = "Ano. A pak přišla velmi dlouhá věta, která se nedopověděla, protože"
    assert drop_dangling_sentence(text) == text   # „Ano." není odpověď


def test_dangling_drop_without_any_sentence_end():
    from server import drop_dangling_sentence
    assert drop_dangling_sentence("Bez tečky to nejde useknout") == "Bez tečky to nejde useknout"

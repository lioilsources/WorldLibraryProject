"""Testy knihovny šablon: schéma, proměnné, výrazy, render a snapshoty.

Kontrola § proti právnímu indexu je v `docgen/validate.py` a běží v `make
validate-templates` — tady se přeskočí, protože pytest nemá mít po ruce JODU.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from docgen import validate
from docgen.templates import (ChybaSablony, ChybaVstupu, checklist, load, render, vsechny,
                              zaklad_text)

SABLONY = sorted((Path(__file__).resolve().parents[2] / "data" / "templates").glob("*.yaml"))
IDS = [p.stem for p in SABLONY if p.name != "sources.yaml"]
CESTY = [p for p in SABLONY if p.name != "sources.yaml"]


@pytest.fixture(scope="module")
def sablony() -> list[dict]:
    return vsechny()


def test_nejake_sablony_existuji(sablony):
    assert len(sablony) >= 3, "knihovna šablon je prázdná"


@pytest.mark.parametrize("cesta", CESTY, ids=IDS)
def test_schema_a_jmeno(cesta):
    s = load(cesta)
    assert s["typ"] == cesta.stem, "typ musí sedět na jméno souboru"
    assert s["jazyk"] == "cs"


@pytest.mark.parametrize("cesta", CESTY, ids=IDS)
def test_promenne_a_vyrazy(cesta):
    s = load(cesta)
    assert validate.zkontroluj_promenne(s) == []
    assert validate.zkontroluj_vyrazy(s) == []


@pytest.mark.parametrize("cesta", CESTY, ids=IDS)
def test_kazda_klauzule_ma_pravni_zaklad(cesta):
    """Klauzule bez § je tvrzení bez opory — smysl téhle knihovny je opačný."""
    s = load(cesta)
    bez = [k["id"] for k in s["klauzule"] if not k.get("zaklad")]
    assert bez == [], f"klauzule bez zaklad: {bez}"


@pytest.mark.parametrize("cesta", CESTY, ids=IDS)
def test_fixtury_a_snapshoty(cesta):
    s = load(cesta)
    problemy, hotovo = validate.zkontroluj_fixtury(s, update=False)
    assert hotovo >= 1, "šablona bez fixtury se nikdy nevyrenderovala"
    assert problemy == []


def test_render_je_deterministicky():
    s = load(Path(__file__).resolve().parents[2] / "data" / "templates" / "najemni_smlouva_byt.yaml")
    f = validate.FIXTURES / "najemni_smlouva_byt__neurcita_minimum.json"
    vstup = {k: v for k, v in json.loads(f.read_text(encoding="utf-8")).items()
             if not k.startswith("_")}
    a = render(s, vstup, k_datu=date(2026, 9, 28))
    b = render(s, vstup, k_datu=date(2026, 9, 28))
    assert a == b and "{{" not in a
    assert "Nejde o právní službu" in a, "chybí zápatí s upozorněním"


def test_kogentni_limit_jistoty_zastavi_render():
    """§ 2254: jistota + smluvní pokuta nejvýš trojnásobek nájemného."""
    s = load(Path(__file__).resolve().parents[2] / "data" / "templates" / "najemni_smlouva_byt.yaml")
    f = validate.FIXTURES / "najemni_smlouva_byt__neurcita_minimum.json"
    vstup = {k: v for k, v in json.loads(f.read_text(encoding="utf-8")).items()
             if not k.startswith("_")}
    vstup["jistota"] = 30000          # nájemné 12 000 → limit 36 000
    vstup["smluvni_pokuta"] = 12000   # souhrn 42 000 → musí spadnout
    with pytest.raises(ChybaVstupu, match="trojnásobek"):
        render(s, vstup, k_datu=date(2026, 9, 28))
    vstup["smluvni_pokuta"] = 6000    # souhrn 36 000 → přesně na hraně, projde
    assert "Smluvní pokuta" in render(s, vstup, k_datu=date(2026, 9, 28))


def test_dpp_limit_300_hodin():
    s = load(Path(__file__).resolve().parents[2] / "data" / "templates" / "dohoda_o_provedeni_prace.yaml")
    f = validate.FIXTURES / "dohoda_o_provedeni_prace__zakonny_zpusob.json"
    vstup = {k: v for k, v in json.loads(f.read_text(encoding="utf-8")).items()
             if not k.startswith("_")}
    vstup["rozsah_hodin"] = 301
    with pytest.raises(ChybaVstupu, match="300 hodin"):
        render(s, vstup, k_datu=date(2026, 9, 28))


def test_povinnou_klauzuli_nelze_vypnout():
    s = load(Path(__file__).resolve().parents[2] / "data" / "templates" / "najemni_smlouva_byt.yaml")
    f = validate.FIXTURES / "najemni_smlouva_byt__neurcita_minimum.json"
    vstup = {k: v for k, v in json.loads(f.read_text(encoding="utf-8")).items()
             if not k.startswith("_")}
    with pytest.raises(ChybaVstupu, match="nelze vypnout"):
        render(s, vstup, vypnute={"vypoved"}, k_datu=date(2026, 9, 28))


def test_checklist_nese_paragrafy(sablony):
    for s in sablony:
        body = checklist(s)
        assert body, f"{s['typ']}: prázdný checklist"
        s_par = [b for b in body if "zákona č." in b]
        assert s_par, f"{s['typ']}: žádný bod checklistu se neopírá o §"


def test_zaklad_text_formatuje_citaci():
    assert zaklad_text({"zakon": "89/2012 Sb.", "par": "§ 2254"}) == "§ 2254 zákona č. 89/2012 Sb."
    assert zaklad_text(None) == ""


def test_vadna_sablona_spadne_na_schematu(tmp_path):
    zla = tmp_path / "zla.yaml"
    zla.write_text("typ: zla\nnazev: Zlá\nverze: 1\n", encoding="utf-8")
    with pytest.raises(ChybaSablony):
        load(zla)

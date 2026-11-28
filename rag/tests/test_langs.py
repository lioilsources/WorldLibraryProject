"""Tabulky jazyků musí znát každý jazyk, který je v registru. Jinak katalog
píše holý kód („jazyk ar"), bundle dostane latinku a plánovač pro ten jazyk
nepřipraví termíny ani HyDE — a nic z toho nespadne."""

from pathlib import Path

import catalog
import enrich_chunks
import export_bundle
import ingest_books as ib
import planner

REGISTRY = Path(__file__).parent.parent / "registry"


def registry_langs() -> tuple[set[str], set[str]]:
    works = [w for w in ib.load_works(REGISTRY).values() if w.get("lang_original")]
    orig = {w["lang_original"] for w in works}
    corpus = {w.get("lang_corpus") or w["lang_original"] for w in works}
    return orig, corpus


def test_jazyky_registru_maji_ceske_jmeno():
    orig, corpus = registry_langs()
    assert not (orig | corpus) - set(catalog.LANG_CS)
    assert not (orig | corpus) - set(enrich_chunks.LANG_NAME)


def test_jazyky_korpusu_maji_pismo_v_bundlu():
    _orig, corpus = registry_langs()
    assert not corpus - set(export_bundle.SCRIPT_BY_LANG)


def test_planovac_zna_jazyky_ve_kterych_knihovna_drzi_original():
    orig, corpus = registry_langs()
    held = (orig & corpus) | {"grc", "lat"}          # Perseus v registru není
    missing = [lang for lang in sorted(held - {"en"}) if f"{lang}=" not in planner.USER]
    assert not missing, f"planner.USER nezná {missing} — doplň a zvedni PROMPT_VERSION"

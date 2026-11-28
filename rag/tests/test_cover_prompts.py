"""Prompty k obálkám — čistá logika bez LLM a bez Postgresu."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cover_prompts import DEFAULT_PALETTE, STYLE, asset_for, clean_motif, full_prompt, palette_for  # noqa: E402

MOTIF = ("A small bronze-armoured warrior seen from behind on a rocky shore, facing a fleet of black "
         "ships with tall curved prows, smoke rising from a burning camp in the distance, a single "
         "eagle circling above the masts under a low sun")


def test_paleta_podle_jazyka_a_vychozi():
    assert "terracotta" in palette_for("grc")
    assert palette_for("LAT") == palette_for("lat")
    assert palette_for("xx") == DEFAULT_PALETTE
    assert palette_for(None) == DEFAULT_PALETTE


def test_motiv_se_cisti_a_hlida_delku():
    assert clean_motif(MOTIF) == MOTIF
    assert clean_motif("  " + MOTIF.replace(" ", "\n", 3) + "  ") == MOTIF
    assert clean_motif("too short motif") is None
    assert clean_motif(" ".join(["word"] * 120)) is None
    assert clean_motif(None) is None
    assert clean_motif({"x": 1}) is None


def test_zminka_o_textu_z_motivu_zmizi():
    m = clean_motif(MOTIF + ", with an inscription and the title lettering on a scroll")
    assert m is not None
    for w in ("inscription", "title", "lettering"):
        assert w not in m.lower()


def test_finalni_prompt_ma_styl_paletu_a_motiv():
    p = full_prompt(MOTIF, palette_for("grc"))
    assert p.startswith(STYLE)
    assert "terracotta" in p and MOTIF in p
    assert "woodcut" in p.lower() and "no text" in p.lower()


def test_asset_odpovida_jmenu_bundlu():
    assert asset_for("zh.daodejing") == "zh_daodejing"
    assert asset_for("grc/tlg0031/tlg016") == "grc_tlg0031_tlg016"

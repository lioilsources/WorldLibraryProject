"""Testy kroku agenta pro appku (agent/klient.py, POST /agent/chat).

Scénáře se skriptovaným modelem opakují to, co 2026-10-05 dělal qwen36 na
SPARKu (`pravnik-agent` → `openclaw-default`): po `get_template` nezavolá
`save_intake(typ=…)`, v `ask_user` si vymyslí id proměnných a na podmíněně
povinný údaj (`doba_do`) se ptá pod jiným jménem. Bez PG se přeskočí jen
scénáře, převod hodnot a hlášky běží vždycky.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from agent.klient import (HLASKA_MIMO_OKNO, HistorieAgenta, hlaska_modelu, krok_pro_klienta,
                          normalizuj, stav_intake, zprava_s_odpovedmi)
from agent.llm import SkriptovanyKlient
from agent.loop import Pravnik

ROOT = Path(__file__).resolve().parents[2]


def _dsn() -> str | None:
    env = ROOT / "rag" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("LAW_PG_DSN="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("LAW_PG_DSN")


DSN = _dsn()
bez_pg = pytest.mark.skipif(not DSN, reason="chybí LAW_PG_DSN (Postgres `law`)")

# --- převod odpovědí z karet (bez PG) ------------------------------------------------


@pytest.mark.parametrize("typ,vstup,ceka", [
    ("money", "16 500 Kč", 16500),
    ("money", "16 500,-", 16500),
    ("money", "12,5", 12.5),
    ("money", 3200, 3200),
    ("int", "58 m²", 58),
    ("int", "15", 15),
    ("date", "1. 10. 2026", "2026-10-01"),
    ("date", "2027-09-30", "2027-09-30"),
    ("bool", "ano", True),
    ("bool", "Ne", False),
    ("bool", True, True),
    ("string", "  Jana Dvořáková ", "Jana Dvořáková"),
    ("string", "", None),
])
def test_normalizuj(typ, vstup, ceka):
    assert normalizuj({"typ": typ}, vstup) == ceka


def test_normalizuj_enum_bere_hodnotu_ze_sablony():
    p = {"typ": "enum", "hodnoty": ["neurcita", "urcita"]}
    assert normalizuj(p, "Urcita") == "urcita"
    with pytest.raises(ValueError, match="není z nabídky"):
        normalizuj(p, "na rok")


@pytest.mark.parametrize("typ,vstup", [("money", "hodně"), ("int", "2,5"),
                                       ("date", "příští úterý"), ("bool", "možná")])
def test_normalizuj_odmitne_nesmysl_vetou_pro_cloveka(typ, vstup):
    with pytest.raises(ValueError) as e:
        normalizuj({"typ": typ}, vstup)
    assert str(vstup) in str(e.value)


# --- 503 a okno modelu ---------------------------------------------------------------


@pytest.mark.parametrize("hodina", [7, 12, 18])
def test_mimo_okno_rekne_kdy_agent_bezi(hodina):
    h = hlaska_modelu("APIConnectionError: Connection refused", hodina)
    assert h == HLASKA_MIMO_OKNO
    assert "19:00" in h and "01:00" in h and "Connection" not in h


@pytest.mark.parametrize("hodina", [19, 22, 0])
def test_v_okne_je_to_porucha(hodina):
    h = hlaska_modelu("InternalServerError: 500", hodina)
    assert h != HLASKA_MIMO_OKNO and "znovu" in h and "500" in h


# --- historie a text pro model ------------------------------------------------------


def test_historie_drzi_limit_a_reset_ji_smaze():
    h = HistorieAgenta(max_zprav=4)
    for i in range(5):
        h.pridej("s", f"u{i}", f"a{i}")
    assert [z["content"] for z in h.nacti("s")] == ["u3", "a3", "u4", "a4"]
    h.smaz("s")
    assert h.nacti("s") == []


def test_historie_vytlaci_nejstarsi_session():
    h = HistorieAgenta(max_sessions=2)
    for s in ("a", "b", "c"):
        h.pridej(s, "u", "a")
    assert h.nacti("a") == [] and h.nacti("c")


def test_zprava_s_odpovedmi_rekne_modelu_co_se_ulozilo():
    t = zprava_s_odpovedmi("", [{"id": "najemne", "otazka": "Kolik?", "hodnota": "16 500"}],
                           {"ulozeno": ["najemne"], "odmitnuto": [{"id": "x", "duvod": "neznám"}]})
    assert "Kolik? (najemne): 16 500" in t
    assert "Uloženo do dokumentu" in t and "najemne" in t and "x — neznám" in t


# --- scénáře se skriptovaným modelem (PG) -------------------------------------------


@pytest.fixture(scope="module")
def prostredi():
    pytest.importorskip("psycopg_pool")
    from psycopg_pool import ConnectionPool

    from agent.session import Sessions
    from agent.tools import PravniNastroje

    pool = ConnectionPool(DSN, min_size=1, max_size=3, open=True)
    sessions = Sessions(pool)
    yield PravniNastroje("http://127.0.0.1:1", pool, sessions), sessions
    pool.close()


@pytest.fixture()
def sid(prostredi):
    _, sessions = prostredi
    s = f"test-{uuid.uuid4().hex[:10]}"
    yield s
    sessions.smaz(s)


def _krok(prostredi, historie, sid, odpovedi_modelu, zprava="", odpovedi=None):
    nastroje, sessions = prostredi
    llm = SkriptovanyKlient(odpovedi_modelu)
    agent = Pravnik(nastroje, sessions, llm=llm, router="heuristika")
    r = krok_pro_klienta(agent, nastroje, sessions, historie, zprava=zprava, session_id=sid,
                         mode="draft", odpovedi=odpovedi, model="skript")
    return r, llm


ZAKLAD_NAJMU = {
    "pronajimatel_jmeno": "Jana Dvořáková", "pronajimatel_identifikace": "nar. 3. 5. 1971",
    "pronajimatel_adresa": "Krátká 12, 602 00 Brno", "najemce_jmeno": "Petr Novák",
    "najemce_identifikace": "nar. 14. 11. 1994", "najemce_adresa": "Dlouhá 3, 110 00 Praha 1",
    "adresa_bytu": "č. 7, Luční 1480/6, 616 00 Brno", "vymera": "58", "den_predani": "1. 11. 2026",
    "najemne": "16 500 Kč", "splatnost_den": "15", "zaloha_sluzby": "3 200",
    "sluzby_vecet": "voda, teplo", "bankovni_ucet": "123456789/0800",
}


@bez_pg
def test_get_template_priradi_sablonu_a_karty_jsou_ze_sablony(prostredi, sid):
    """qwen36: get_template → ask_user s vymyšlenými id, save_intake(typ) vynechá."""
    _, sessions = prostredi
    r, _ = _krok(prostredi, HistorieAgenta(), sid, [
        {"tool": "get_template", "args": {"typ": "najemni_smlouva_byt"}},
        {"tool": "ask_user", "args": {"otazky": [
            {"id": "name_najemce", "otazka": "Kdo je nájemce?", "typ": "string"}]}},
        "Pojďme na to.",
    ], zprava="Chci sepsat nájemní smlouvu na byt.")

    assert sessions.nacti(sid).typ == "najemni_smlouva_byt"
    assert r["stav"]["typ"] == "najemni_smlouva_byt" and r["stav"]["povinnych_vyplneno"] == 0
    ids = [q["id"] for q in r["otazky"]]
    assert ids == ["pronajimatel_jmeno", "pronajimatel_identifikace", "pronajimatel_adresa"]
    assert r["dokument"] is None and r["odpoved"] == "Pojďme na to."


@bez_pg
def test_karty_od_modelu_se_platnymi_id_zustanou(prostredi, sid):
    r, _ = _krok(prostredi, HistorieAgenta(), sid, [
        {"tool": "get_template", "args": {"typ": "najemni_smlouva_byt"}},
        {"tool": "ask_user", "args": {"otazky": [
            {"id": "jistota", "otazka": "Chcete jistotu (kauci)?", "typ": "money"}]}},
        "Ještě jistota?",
    ], zprava="Sepiš nájemní smlouvu")
    assert [q["id"] for q in r["otazky"]] == ["jistota"]


@bez_pg
def test_odpovedi_z_karet_se_ulozi_s_prevodem_a_model_dostane_historii(prostredi, sid):
    nastroje, sessions = prostredi
    h = HistorieAgenta()
    _krok(prostredi, h, sid, [{"tool": "get_template", "args": {"typ": "najemni_smlouva_byt"}},
                              "Kdo pronajímá?"], zprava="Sepiš nájemní smlouvu na byt")
    r, llm = _krok(prostredi, h, sid, ["Díky, dál."], odpovedi=[
        {"id": "najemne", "otazka": "Nájemné?", "hodnota": "16 500 Kč"},
        {"id": "den_predani", "hodnota": "1. 11. 2026"},
        {"id": "vymera", "hodnota": "hodně"},
        {"id": "vymysleno", "hodnota": "x"},
    ])

    s = sessions.nacti(sid)
    assert s.promenne["najemne"] == 16500 and s.promenne["den_predani"] == "2026-11-01"
    assert r["ulozene_odpovedi"]["ulozeno"] == ["den_predani", "najemne"]
    odmitnuto = {x["id"]: x["duvod"] for x in r["ulozene_odpovedi"]["odmitnuto"]}
    assert set(odmitnuto) == {"vymera", "vymysleno"} and "není číslo" in odmitnuto["vymera"]

    zpravy = llm.volani[0]["zpravy"]
    assert [z["role"] for z in zpravy] == ["system", "user", "assistant", "user"]
    assert "Sepiš nájemní smlouvu" in zpravy[1]["content"]
    assert "najemni_smlouva_byt" in zpravy[0]["content"]          # stav intake v kontextu
    assert "Uloženo do dokumentu" in zpravy[-1]["content"]
    assert "[Karty" not in zpravy[2]["content"]                  # model by je opisoval


@bez_pg
def test_podminene_povinny_udaj_prijde_jako_karta(prostredi, sid):
    """Doba určitá bez doba_do: povinné je vyplněné, render neprojde a karta se
    ptá na doba_do (ne na model vymyšlené doba_ukonceni)."""
    nastroje, sessions = prostredi
    nastroje.save_intake(session_id=sid, typ="najemni_smlouva_byt")
    odpovedi = [{"id": k, "hodnota": v} for k, v in ZAKLAD_NAJMU.items()]
    odpovedi.append({"id": "doba", "hodnota": "urcita"})
    r, _ = _krok(prostredi, HistorieAgenta(), sid, [
        {"tool": "ask_user", "args": {"otazky": [{"id": "doba_ukonceni", "otazka": "Do kdy?"}]}},
        "Do kdy nájem trvá?"], odpovedi=odpovedi)

    assert r["stav"]["chybi_celkem"] == 0 and not r["stav"]["pripraveno_k_renderu"]
    assert r["stav"]["porusene_limity"]
    assert [q["id"] for q in r["otazky"]] == ["doba_do"]
    assert r["otazky"][0]["typ"] == "date" and "určitou" in r["otazky"][0]["napoveda"]


@bez_pg
def test_render_vrati_dokument_checklist_a_upozorneni(prostredi, sid):
    nastroje, sessions = prostredi
    nastroje.save_intake(session_id=sid, typ="najemni_smlouva_byt")
    odpovedi = [{"id": k, "hodnota": v} for k, v in ZAKLAD_NAJMU.items()]
    odpovedi.append({"id": "doba", "hodnota": "neurcita"})
    r, _ = _krok(prostredi, HistorieAgenta(), sid, [
        # model si vymyslí session_id — dokument patří vždycky k session kroku
        {"tool": "render_document", "args": {"session_id": "jina-session"}},
        "Smlouva je hotová."], odpovedi=odpovedi)

    assert r["dokument"] and "NÁJEMNÍ SMLOUVA" in r["dokument"] and "{{" not in r["dokument"]
    assert r["checklist"] and any("§" in b for b in r["checklist"])
    assert r["upozorneni"]
    assert r["otazky"] == []
    assert r["stav"]["stav"] == "hotovo"
    assert stav_intake(nastroje, sessions, sid)["pripraveno_k_renderu"]


@bez_pg
def test_odpovedi_z_dokumentu_se_neukladaji(prostredi, sid):
    """zdroj=dokument (obsah nahraného souboru) nesmí měnit intake ani přes `odpovedi`."""
    nastroje, sessions = prostredi
    nastroje.save_intake(session_id=sid, typ="najemni_smlouva_byt")
    llm = SkriptovanyKlient(["ok"])
    agent = Pravnik(nastroje, sessions, llm=llm, router="heuristika")
    r = krok_pro_klienta(agent, nastroje, sessions, HistorieAgenta(), zprava="text smlouvy",
                         session_id=sid, mode="review", zdroj="dokument",
                         odpovedi=[{"id": "najemne", "hodnota": "1"}])
    assert r["ulozene_odpovedi"] is None
    assert "najemne" not in sessions.nacti(sid).promenne


def test_server_ma_agent_model_pravnik_agent():
    pytest.importorskip("chromadb")
    import server

    args = server.parser().parse_args([])
    assert args.agent_model == "pravnik-agent"
    assert args.llm_model == "translate"          # chat Právníka /chat/stream beze změny

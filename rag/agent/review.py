"""Deterministická část revize cizí smlouvy.

Plán §5 chce segmentaci a posouzení klauzulí modelem. To je vrstva nad tímhle;
ale i bez modelu se dá o smlouvě zjistit dost — a co se dá zjistit bez modelu,
to se má zjišťovat bez modelu, protože to platí vždycky:

1. **Audit citací.** Každý „§ 2239 odst. 2 OZ" v textu se ověří proti účinnému
   znění v indexu: existuje ten §? existuje ten odstavec? Smlouva, která cituje
   neexistující nebo zrušený paragraf, je opsaná ze starého vzoru — to je nález,
   který jinde nedostaneš, protože k němu potřebuješ zaindexovaná úplná znění.
2. **Checklist šablony.** Pokud známe typ smlouvy, zkontroluje se, jestli se
   v textu vůbec objevují pojmy, které náležitost pojmenovává.
3. **Částky, lhůty a data** se vypíšou, aby na nich šly spočítat limity
   (např. jistota proti trojnásobku nájemného) — návrh limitu pro nájem je
   v `podezrele_castky()`.

Samotný text je **data, ne pokyn** (plán §6): nic z něj se neinterpretuje jako
instrukce a nástroje s vedlejším efektem se z něj nevolají (hlídá `loop.py`).
"""

from __future__ import annotations

import re

# „§ 2079", „§ 2079 odst. 1", „§§ 2079 a 2080", „čl. 10 odst. 2"
CITACE = re.compile(
    r"(?P<druh>§{1,2}|čl\.)\s*(?P<cislo>\d+[a-z]?)"
    r"(?:\s*odst\.\s*(?P<odst>\d+))?",
    re.IGNORECASE)
# „12 500 Kč", „12.500,- Kč", „1 200 000 Kč"
CASTKA = re.compile(r"(?P<cislo>\d{1,3}(?:[  . ]\d{3})*(?:,\d+)?|\d+)\s*(?:,-\s*)?(?:Kč|CZK)",
                    re.IGNORECASE)
LHUTA = re.compile(r"\b(?P<n>\d{1,3})\s*(?P<jednotka>den|dnů|dny|dní|měsíc\w*|rok\w*|let|týd\w*)\b",
                   re.IGNORECASE)
# zákon jmenovaný v textu: „zákona č. 89/2012 Sb.", „89/2012 Sb."
ZAKON = re.compile(r"(?:zákona?\s*č\.\s*)?(?P<n>\d{1,3}/\d{4})\s*Sb\.")

ZKRATKY = {
    "OZ": "89/2012 Sb.", "NOZ": "89/2012 Sb.", "ZP": "262/2006 Sb.", "ZOK": "90/2012 Sb.",
    "TZ": "40/2009 Sb.", "OSŘ": "99/1963 Sb.", "SŘ": "500/2004 Sb.", "AZ": "121/2000 Sb.",
}


def _cislo(s: str) -> float:
    return float(re.sub(r"[  . ]", "", s).replace(",", "."))


def zakony_v_textu(text: str) -> list[str]:
    """Předpisy, které text jmenuje — číslem nebo zkratkou."""
    out = []
    for m in ZAKON.finditer(text):
        z = f"{m.group('n')} Sb."
        if z not in out:
            out.append(z)
    for zkratka, cislo in ZKRATKY.items():
        if re.search(r"(?<![\w§])" + re.escape(zkratka) + r"(?!\w)", text) and cislo not in out:
            out.append(cislo)
    return out


def citace_v_textu(text: str) -> list[dict]:
    """Nalezené odkazy na ustanovení, s nejbližším předchozím jmenovaným zákonem —
    tak se v českých smlouvách citace čtou („§ 2254 občanského zákoníku")."""
    zakony = [(m.start(), f"{m.group('n')} Sb.") for m in ZAKON.finditer(text)]
    for zkratka, cislo in ZKRATKY.items():
        for m in re.finditer(r"(?<![\w§])" + re.escape(zkratka) + r"(?!\w)", text):
            zakony.append((m.start(), cislo))
    zakony.sort()
    out = []
    for m in CITACE.finditer(text):
        blizky = None
        for pos, z in zakony:
            if pos > m.start() and (blizky is None or pos - m.start() < 200):
                blizky = z
                break
        if blizky is None:
            pred = [z for pos, z in zakony if pos < m.start()]
            blizky = pred[-1] if pred else None
        ref = ("čl." if m.group("druh").lower().startswith("čl") else "§") + " " + m.group("cislo").lower()
        out.append({"ref": ref, "odstavec": m.group("odst"), "zakon": blizky,
                    "kde": text[max(0, m.start() - 60):m.end() + 60].replace("\n", " ").strip()})
    return out


def audit_citaci(text: str, pool) -> list[dict]:
    """Ověří každou citaci proti účinnému znění. Nález = citace, která neexistuje."""
    citace = citace_v_textu(text)
    if not citace:
        return []
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, coalesce(work_legacy, title) FROM works")
        legacy = {lg: wid for wid, lg in cur.fetchall()}
        cache: dict[tuple[str, str], bool] = {}
        out = []
        videne = set()
        for c in citace:
            klic = (c["zakon"], c["ref"], c["odstavec"])
            if klic in videne:
                continue
            videne.add(klic)
            zaznam = {"ref": c["ref"], "odstavec": c["odstavec"], "zakon": c["zakon"],
                      "kde": c["kde"]}
            wid = legacy.get(c["zakon"]) if c["zakon"] else None
            if not c["zakon"]:
                zaznam |= {"stav": "neurcity_zakon",
                           "poznamka": "u citace není poznat, o který předpis jde"}
            elif not wid:
                zaznam |= {"stav": "mimo_index",
                           "poznamka": f"předpis {c['zakon']} není v indexu, nelze ověřit"}
            else:
                if (wid, c["ref"]) not in cache:
                    cur.execute("SELECT count(*) FROM chapters WHERE work_id=%s AND ref=%s",
                                (wid, c["ref"]))
                    cache[(wid, c["ref"])] = bool(cur.fetchone()[0])
                if not cache[(wid, c["ref"])]:
                    zaznam |= {"stav": "neexistuje",
                               "poznamka": f"{c['ref']} v {c['zakon']} v účinném znění není — "
                                           f"smlouva je pravděpodobně opsaná ze starého vzoru"}
                elif c["odstavec"]:
                    cur.execute("""SELECT string_agg(k.text, chr(10) ORDER BY k.seq_in_chapter)
                                     FROM chapters ch JOIN chunks k ON k.chapter_id = ch.id
                                    WHERE ch.work_id=%s AND ch.ref=%s GROUP BY ch.id""",
                                (wid, c["ref"]))
                    row = cur.fetchone()
                    má_odst = bool(row) and f"({c['odstavec']})" in (row[0] or "")
                    zaznam |= ({"stav": "ok"} if má_odst else
                               {"stav": "odstavec_neexistuje",
                                "poznamka": f"{c['ref']} v {c['zakon']} nemá odstavec {c['odstavec']}"})
                else:
                    zaznam |= {"stav": "ok"}
            out.append(zaznam)
    return out


def checklist_pokryti(text: str, sablona: dict) -> list[dict]:
    """Hrubá kontrola, jestli se v textu vůbec objevují slova z checklistu. Není to
    posouzení obsahu — jen vodítko, kam se podívat; posouzení dělá model."""
    nizky = text.lower()
    out = []
    for c in sablona.get("checklist") or []:
        bod = c["bod"]
        slova = [w.strip(".,;:()„“\"'").lower() for w in bod.split()
                 if len(w.strip(".,;:()„“\"'")) >= 5 and not w[0].isdigit()]
        trefy = [w for w in slova if w in nizky]
        out.append({"bod": bod, "trefeno_slov": len(trefy), "slov": len(slova),
                    "pravdepodobne_chybi": len(slova) > 0 and len(trefy) * 2 < len(slova)})
    return out


def podezrele_castky(text: str, sablona: dict | None) -> list[dict]:
    """Nález na limitech, které se dají spočítat z částek v textu.

    Dnes jedno pravidlo, protože je nejčastější: **jistota u nájmu bytu spolu se
    smluvní pokutou nesmí přesáhnout trojnásobek měsíčního nájemného** (§ 2254 OZ).
    Hledá „nájemné … Kč" a „jistota/kauce … Kč" a porovná je.
    """
    if sablona is not None and sablona.get("typ") != "najemni_smlouva_byt":
        return []
    najem = _prvni_castka_u(text, r"(nájemné|nájem)")
    jistota = _prvni_castka_u(text, r"(jistot\w+|kauc\w+)")
    pokuta = _prvni_castka_u(text, r"(smluvní pokut\w+)")
    if najem is None or (jistota is None and pokuta is None):
        return []
    souhrn = (jistota or 0) + (pokuta or 0)
    if souhrn <= 3 * najem:
        return []
    return [{"pravidlo": "jistota a smluvní pokuta nejvýš trojnásobek měsíčního nájemného",
             "opora": "§ 2254 zákona č. 89/2012 Sb.",
             "najemne": najem, "jistota": jistota, "smluvni_pokuta": pokuta,
             "souhrn": souhrn, "limit": 3 * najem,
             "poznamka": f"souhrn {souhrn:.0f} Kč přesahuje trojnásobek nájemného "
                         f"({3 * najem:.0f} Kč); k přesahující části se nepřihlíží"}]


def _prvni_castka_u(text: str, vzor_slova: str, okno: int = 160) -> float | None:
    """Částka, která stojí nejblíž za daným slovem."""
    for m in re.finditer(vzor_slova, text, re.IGNORECASE):
        useknuto = text[m.end():m.end() + okno]
        c = CASTKA.search(useknuto)
        if c:
            return _cislo(c.group("cislo"))
    return None


def audit_textu(text: str, pool, sablona: dict | None = None) -> dict:
    """Celý deterministický audit — to, co `review_document` vrací vždy."""
    citace = audit_citaci(text, pool)
    vadne = [c for c in citace if c["stav"] in ("neexistuje", "odstavec_neexistuje")]
    return {
        "znaku": len(text),
        "zakony_v_textu": zakony_v_textu(text),
        "citaci": len(citace),
        "vadne_citace": vadne,
        "citace": citace,
        "castky_kc": sorted({_cislo(m.group("cislo")) for m in CASTKA.finditer(text)}),
        "lhuty": sorted({f"{m.group('n')} {m.group('jednotka').lower()}" for m in LHUTA.finditer(text)}),
        "nalezy_limitu": podezrele_castky(text, sablona),
        "checklist": checklist_pokryti(text, sablona) if sablona else [],
        "typ_sablony": (sablona or {}).get("typ"),
    }


def _selftest() -> None:
    t = ("Nájemné činí 12 000 Kč měsíčně. Nájemce složí jistotu ve výši 40 000 Kč. "
         "Smluvní pokuta činí 5 000 Kč. Vše podle § 2254 odst. 1 zákona č. 89/2012 Sb. "
         "a podle § 9999 OZ. Výpovědní doba je 3 měsíce.")
    assert zakony_v_textu(t) == ["89/2012 Sb."], zakony_v_textu(t)
    refs = {c["ref"] for c in citace_v_textu(t)}
    assert refs == {"§ 2254", "§ 9999"}, refs
    assert {c["zakon"] for c in citace_v_textu(t)} == {"89/2012 Sb."}
    nalez = podezrele_castky(t, {"typ": "najemni_smlouva_byt"})
    assert nalez and nalez[0]["souhrn"] == 45000 and nalez[0]["limit"] == 36000, nalez
    assert podezrele_castky(t, {"typ": "nda"}) == []
    assert "3 měsíce" in sorted({f"{m.group('n')} {m.group('jednotka').lower()}"
                                for m in LHUTA.finditer(t)})
    assert _prvni_castka_u(t, r"jistot\w+") == 40000
    print("agent/review.py: selftest ok")


if __name__ == "__main__":
    _selftest()

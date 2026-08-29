#!/usr/bin/env python3
"""Směrování dotazu na dílo, diverzita výsledků a filtry na úryvky.

Čistá logika bez Chromy a bez modelu — používá ji server.py i
eval/eval_retrieval.py, takže se měří přesně to, co appka posílá.

Proč to vůbec je: dotaz česky nad korpusem v pálí, sanskrtu, čínštině
a hebrejštině má u multilingual-e5 mizivý rozptyl vzdáleností (měřeno:
top-5 se u dotazu liší o ~0,007), takže pořadí rozhoduje spíš podíl
tradice v korpusu než téma otázky — a pálijská Tipitaka je 60 z 93 děl.
Dvě protiopatření:

  1. **Směrování** — když otázka dílo jmenuje („v Tao te ťingu"), hledá
     se rovnou v něm (`where={"work": {"$in": [...]}}`).
  2. **Diverzita** — z většího výběru se bere nejvýš pár úryvků z jednoho
     díla, aby top-k nebylo pětkrát tentýž Šántiparva.
"""

import re
import unicodedata
from difflib import SequenceMatcher

# --- porovnávání bez diakritiky a velikosti písmen -------------------------


def fold(text: str) -> str:
    """Malá písmena bez diakritiky: „Tao te ťing" i „TAO TE TING" → totéž.
    Dotazy se píšou ledabyle a názvy děl jsou plné háčků a délek."""
    nfd = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


# --- kurátorská tabulka aliasů --------------------------------------------
#
# Alias → díla, na která ukazuje. Zapisuje se ve zjednodušené („folded")
# podobě a jako **kmen**, ne celé slovo — čeština dílo skloňuje
# („v Therígáthě", „Milindovy otázky"), takže „therigath" trefí obojí.
# Alias se hledá od hranice slova, takže kmen nechytá vnitřek jiného slova.

ALIASES: dict[str, tuple[str, ...]] = {
    # --- čínská tradice ---
    "tao te ting": ("dao de jing",),
    "tao-te-ting": ("dao de jing",),
    "taoteting": ("dao de jing",),
    "dao de jing": ("dao de jing",),
    "daodejing": ("dao de jing",),
    "lao-c": ("dao de jing",),
    "laoc": ("dao de jing",),
    "laozi": ("dao de jing",),
    "hovory": ("analects",),
    "hovorech": ("analects",),
    "lun-ju": ("analects",),
    "lunju": ("analects",),
    "analekt": ("analects",),
    "konfuci": ("analects",),
    "confuci": ("analects",),
    "mencius": ("mengzi",),
    "menci": ("mengzi",),
    "meng-c": ("mengzi",),
    "mengzi": ("mengzi",),
    "zhuangzi": ("zhuangzi",),
    "cuang-c": ("zhuangzi",),
    "cuangc": ("zhuangzi",),
    "chuang tzu": ("zhuangzi",),
    # --- pálijský kánon: jednotlivá díla ---
    "dhammapad": ("Dhammapadapāḷi",),
    "milind": ("Milindapañhapāḷi",),
    "nagasen": ("Milindapañhapāḷi",),
    "therigath": ("Therīgāthāpāḷi",),
    "theragath": ("Theragāthāpāḷi",),
    "sutta-nipat": ("Suttanipātapāḷi",),
    "suttanipat": ("Suttanipātapāḷi",),
    "itivuttak": ("Itivuttakapāḷi",),
    "jatak": ("Jātakapāḷi 1", "Jātakapāḷi 2"),
    "dzatak": ("Jātakapāḷi 1", "Jātakapāḷi 2"),
    "buddhavans": ("Buddhavaṃsapāḷi",),
    "buddhavams": ("Buddhavaṃsapāḷi",),
    "apadan": ("Therāpadānapāḷi 1", "Therāpadānapāḷi 2"),
    "khuddakapath": ("Khuddakapāṭhapāḷi",),
    "petavatthu": ("Petavatthupāḷi",),
    "vimanavatthu": ("Vimānavatthupāḷi",),
    # --- pálijský kánon: celé sbírky (koše a nikáje) ---
    "vinaj": ("Pārājikapāḷi", "Pācittiyapāḷi", "Cūḷavaggapāḷi", "Parivārapāḷi"),
    "vinay": ("Pārājikapāḷi", "Pācittiyapāḷi", "Cūḷavaggapāḷi", "Parivārapāḷi"),
    "digha": ("Sīlakkhandhavaggapāḷi", "Mahāvaggapāḷi", "Pāthikavaggapāḷi"),
    "dighanikaj": ("Sīlakkhandhavaggapāḷi", "Mahāvaggapāḷi", "Pāthikavaggapāḷi"),
    "majjhim": ("Mūlapaṇṇāsapāḷi", "Majjhimapaṇṇāsapāḷi", "Uparipaṇṇāsapāḷi"),
    "madzdzhim": ("Mūlapaṇṇāsapāḷi", "Majjhimapaṇṇāsapāḷi", "Uparipaṇṇāsapāḷi"),
    "samjutt": ("Sagāthāvaggo", "Nidānavaggo", "Khandhavaggo",
                "Saḷāyatanavaggo", "Mahāvaggo"),
    "samyutt": ("Sagāthāvaggo", "Nidānavaggo", "Khandhavaggo",
                "Saḷāyatanavaggo", "Mahāvaggo"),
    "anguttar": ("Ekakanipātapāḷi", "Dukanipātapāḷi", "Tikanipātapāḷi",
                 "Catukkanipātapāḷi", "Pañcakanipātapāḷi", "Chakkanipātapāḷi",
                 "Sattakanipātapāḷi", "Aṭṭhakanipātapāḷi", "Navakanipātapāḷi",
                 "Dasakanipātapāḷi", "Ekādasakanipātapāḷi"),
    "abhidham": ("Dhammasaṅgaṇīpāḷi", "Vibhaṅgapāḷi", "Dhātukathāpāḷi",
                 "Puggalapaññattipāḷi", "Kathāvatthupāḷi",
                 "Yamakapāḷi 1", "Yamakapāḷi 2", "Yamakapāḷi 3",
                 "Paṭṭhānapāḷi 1", "Paṭṭhānapāḷi 2", "Paṭṭhānapāḷi 3",
                 "Paṭṭhānapāḷi 4", "Paṭṭhānapāḷi 5"),
    # --- sanskrt ---
    "mahabharat": tuple(
        f"Mahábhárata {i:02d} — {name}" for i, name in enumerate(
            ["Ádiparva", "Sabháparva", "Vanaparva", "Virátaparva",
             "Udjógaparva", "Bhíšmaparva", "Drónaparva", "Karnaparva",
             "Šaljaparva", "Sauptikaparva", "Stríparva", "Šántiparva",
             "Anušásanaparva", "Ášvamédhikaparva", "Ášramavásikaparva",
             "Mausalaparva", "Maháprasthánikaparva", "Svargáróhanaparva"], 1)
    ),
    "adiparv": ("Mahábhárata 01 — Ádiparva",),
    "sabhaparv": ("Mahábhárata 02 — Sabháparva",),
    "vanaparv": ("Mahábhárata 03 — Vanaparva",),
    "virataparv": ("Mahábhárata 04 — Virátaparva",),
    "udjogaparv": ("Mahábhárata 05 — Udjógaparva",),
    "bhismaparv": ("Mahábhárata 06 — Bhíšmaparva",),
    "dronaparv": ("Mahábhárata 07 — Drónaparva",),
    "karnaparv": ("Mahábhárata 08 — Karnaparva",),
    "saljaparv": ("Mahábhárata 09 — Šaljaparva",),
    "sauptikaparv": ("Mahábhárata 10 — Sauptikaparva",),
    "striparv": ("Mahábhárata 11 — Stríparva",),
    "santiparv": ("Mahábhárata 12 — Šántiparva",),
    "anusasanaparv": ("Mahábhárata 13 — Anušásanaparva",),
    "asvamedhikaparv": ("Mahábhárata 14 — Ášvamédhikaparva",),
    "asramavasikaparv": ("Mahábhárata 15 — Ášramavásikaparva",),
    "mausalaparv": ("Mahábhárata 16 — Mausalaparva",),
    "mahaprasthanikaparv": ("Mahábhárata 17 — Maháprasthánikaparva",),
    "svargarohanaparv": ("Mahábhárata 18 — Svargáróhanaparva",),
    # Bhagavadgíta je zpěv uvnitř Bhíšmaparvy — vlastní dílo v korpusu není
    "bhagavadgit": ("Mahábhárata 06 — Bhíšmaparva",),
    "bhagavad git": ("Mahábhárata 06 — Bhíšmaparva",),
    "rigved": ("rigveda complete sanskrit",),
    "rgved": ("rigveda complete sanskrit",),
    # --- evropská tradice ---
    "beowulf": ("Beowulf", "Beowulf: An Anglo-Saxon Epic Poem"),
    "edd": ("The poetic Edda",
            "The Younger Edda; Also called Snorre's Edda, or The Prose Edda"),
    "poeticka edd": ("The poetic Edda",),
    "starsi edd": ("The poetic Edda",),
    "volusp": ("The poetic Edda",),
    "vedmin": ("The poetic Edda",),
    "snorri": ("The Younger Edda; Also called Snorre's Edda, or The Prose Edda",),
    "mladsi edd": (
        "The Younger Edda; Also called Snorre's Edda, or The Prose Edda",),
    "prozaicka edd": (
        "The Younger Edda; Also called Snorre's Edda, or The Prose Edda",),
    # --- německá filosofie ---
    "hegel": ("hegel werke band01 fruehe schriften",
              "hegel werke band03 phaenomenologie",
              "hegel werke band05 wissenschaft logik"),
    "fenomenologi": ("hegel werke band03 phaenomenologie",),
    "phanomenologi": ("hegel werke band03 phaenomenologie",),
    "veda o logice": ("hegel werke band05 wissenschaft logik",),
    "vede o logice": ("hegel werke band05 wissenschaft logik",),
    "wissenschaft der logik": ("hegel werke band05 wissenschaft logik",),
    # --- ostatní ---
    "bibl": ("westminster leningrad codex",),
    "tanach": ("westminster leningrad codex",),
    "tora": ("westminster leningrad codex",),
    "tory": ("westminster leningrad codex",),
    "leningradsk": ("westminster leningrad codex",),
    "genesis": ("westminster leningrad codex",),
    "pyramid": ("pyramid texts allen",),
    "avest": ("avesta darmesteter complete",),
    "ahura mazd": ("avesta darmesteter complete",),
    "zarathustr": ("avesta darmesteter complete",),
    "zoroastr": ("avesta darmesteter complete",),
}

# --- tradice (skupiny) ------------------------------------------------------
#
# Otázka nemusí jmenovat dílo, ale tradici („co učí hinduistické texty").
# Pak nezbývá než filtr na skupinu — bez něj rozhoduje podíl v korpusu
# a pálijská Tipitaka (60 z 93 děl) přebije všechno ostatní: měřeno,
# „hinduistické texty o karmě a dharmě" vrátí pět pálijských Abhidhamm.

GROUP_ALIASES: dict[str, tuple[str, ...]] = {
    "buddhis": ("pali",),
    "buddhov": ("pali",),
    "theravad": ("pali",),
    "tipitak": ("pali",),
    "palijsk": ("pali",),
    "hinduis": ("sanskrit",),
    "vedsk": ("sanskrit",),
    "vedy": ("sanskrit",),
    "vedach": ("sanskrit",),
    "indick epos": ("sanskrit",),
    "cinsk": ("chinese",),
    "taois": ("chinese",),
    "daois": ("chinese",),
    "konfucian": ("chinese",),
    "seversk": ("european",),
    "germansk": ("european",),
    "vikings": ("european",),
    "staroanglick": ("european",),
    "nemeck filoso": ("european_philosophy",),
    "nemeck idealis": ("european_philosophy",),
    "idealis": ("european_philosophy",),
    "hegelian": ("european_philosophy",),
    "egyptsk": ("egypt",),
    "hebrejsk": ("bible",),
    "zidovsk": ("bible",),
    "stary zakon": ("bible",),
    "zoroastri": ("avesta",),
    "perssk": ("avesta",),
}

_GROUP_INDEX = sorted(
    ((fold(a), g) for a, g in GROUP_ALIASES.items()), key=lambda kv: -len(kv[0])
)


def route_groups(query: str) -> list[str]:
    """Tradice, na které otázka ukazuje. Na rozdíl od děl se **sjednocuje**:
    „srovnej buddhismus a hinduismus" má sáhnout do obou."""
    folded = fold(query)
    hit = {g for alias, groups in _GROUP_INDEX
           if re.search(r"\b" + re.escape(alias), folded) for g in groups}
    return sorted(hit)


# alias odvozený z názvu díla musí být aspoň takhle dlouhý, jinak by
# „Rozbory" nebo „Dvojice 1" chytaly běžnou řeč
MIN_DERIVED_ALIAS = 8


def build_alias_index(catalog_keys) -> list[tuple[str, tuple[str, ...]]]:
    """Kurátorské aliasy + názvy děl z katalogu (a jejich české varianty).

    `catalog_keys` je mapa {jméno díla v katalogu: název_cs nebo None};
    jména se porovnávají v NFC, protože Chroma je drží v NFD (přišla
    z názvů souborů na macOS) a ruční tabulka se píše v NFC.
    """
    by_nfc = {unicodedata.normalize("NFC", k): k for k in catalog_keys}
    index: dict[str, set[str]] = {}

    def add(alias: str, works) -> None:
        real = [by_nfc[unicodedata.normalize("NFC", w)] for w in works
                if unicodedata.normalize("NFC", w) in by_nfc]
        if real:
            index.setdefault(fold(alias), set()).update(real)

    for alias, works in ALIASES.items():
        add(alias, works)

    for key, name_cs in catalog_keys.items():
        add(key, [key])
        if not name_cs:
            continue
        add(name_cs, [key])
        # „Khuddaka-nikája — Otázky krále Milindy" → i samotné „Otázky krále
        # Milindy"; uživatel nikáju nejmenuje
        for part in re.split(r"\s+—\s+|[()]", name_cs):
            part = part.strip(" ,")
            if len(part) >= MIN_DERIVED_ALIAS:
                add(part, [key])

    # delší alias napřed — „poeticka edda" má přebít samotné „edda"
    return sorted(
        ((a, tuple(sorted(w))) for a, w in index.items()),
        key=lambda kv: -len(kv[0]),
    )


def route(query: str, index, max_works: int = 24) -> list[str]:
    """Díla, na která otázka ukazuje. Prázdný seznam = hledej všude.

    Když sedí víc aliasů, platí jejich **průnik**, pokud je neprázdný:
    „Bhagavadgíta z Mahábháraty" tak zamíří na Bhíšmaparvu, kdežto
    „Tao te ťing a Zhuangzi" na obě díla (průnik prázdný → sjednocení).
    """
    folded = fold(query)
    matched = [
        works for alias, works in index
        if re.search(r"\b" + re.escape(alias), folded)
    ]
    if not matched:
        return []
    common = set(matched[0]).intersection(*[set(m) for m in matched[1:]])
    works = common or set().union(*[set(m) for m in matched])
    # příliš široké směrování (celá Tipitaka) už není směrování
    return sorted(works) if len(works) <= max_works else []


def diversify(hits, top_k: int, max_per_work: int = 2, key=None):
    """Z většího výběru vybere top_k tak, aby jedno dílo nezabralo všechno.

    Pořadí podle vzdálenosti se zachovává; co se kvůli stropu vynechá,
    doplní se nakonec, aby se výsledek nikdy nezkrátil pod top_k.
    """
    key = key or (lambda h: h.get("work"))
    picked, spare, counts = [], [], {}
    for h in hits:
        if len(picked) >= top_k:
            break
        work = key(h)
        if counts.get(work, 0) < max_per_work:
            picked.append(h)
            counts[work] = counts.get(work, 0) + 1
        else:
            spare.append(h)
    if len(picked) < top_k:
        picked.extend(spare[: top_k - len(picked)])
    return picked


def looks_tabular(text: str) -> bool:
    """Přepis tabulky (rejstřík, obsah, konkordance), ne souvislý text.

    Allenovy Texty pyramid mají vzadu konkordanci zaříkání — „PT 160 /
    W 122, T 128, P 179, M 170, N 226, Nt 161", tedy číslo zaříkání a jeho
    pozice v šesti pyramidách. PyMuPDF tabulku vytáhne buňku po buňce
    a chunker z ní udělá chunk jako z každého jiného textu; jako citace je
    to k ničemu a překladač z toho dělá nesmysl.

    Měřeno na korpusu (vzorek začátek/střed/konec každého díla): chytí
    zadní matérii Textů pyramid (27 %), obsah Čuang-c' (10 %) a Eddy
    (9 %), pod 2 % u Avesty a Rgvédu — a **nic** u zbylých 87 děl.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 5:
        return False
    # obsah knihy: „Spells for Proceeding toward the Sky ......... 297"
    dotted = sum(1 for line in lines if re.search(r"\.{6,}", line)) / len(lines)
    if dotted > 0.3:
        return True
    short = sum(1 for line in lines if len(line) < 40) / len(lines)
    digits = sum(c.isdigit() for c in text) / max(1, len(text))
    return short > 0.8 and digits > 0.12


def is_echo(original: str, translation: str, threshold: float = 0.7) -> bool:
    """Přeložil model úryvek, nebo ho jen opsal?

    U pálijských veršů se to stává běžně — model si neví rady, tak vrátí
    originál. Vydávat to za překlad je horší než nic: appka pak ukáže
    dvakrát totéž, jednou jako překlad a jednou jako originál.

    Práh 0,7 je schválně vysoko: skutečný český překlad sdílí se zdrojem
    nanejvýš jména a čísla, kdežto opsaný text sedí skoro znak na znak.
    """
    a = " ".join(original.split())
    b = " ".join(translation.split())
    if not b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold


def _selftest() -> None:
    catalog = {
        "Milindapañhapāḷi": "Khuddaka-nikája — Otázky krále Milindy",
        "Dhammapadapāḷi": "Khuddaka-nikája — Dhammapadam",
        "dao de jing": "Tao te ťing — Lao-c'",
        "zhuangzi": "Čuang-c' (Zhuangzi, překlad H. A. Giles)",
        "The poetic Edda": "Starší (Poetická) Edda (překlad H. A. Bellows)",
        "The Younger Edda; Also called Snorre's Edda, or The Prose Edda":
            "Mladší (Snorriho) Edda",
        "Mahábhárata 06 — Bhíšmaparva": None,
        "Mahábhárata 02 — Sabháparva": None,
    }
    # katalog v NFD, jako ho vrací Chroma
    catalog = {unicodedata.normalize("NFD", k): v for k, v in catalog.items()}
    idx = build_alias_index(catalog)
    nfd = lambda s: unicodedata.normalize("NFD", s)  # noqa: E731

    assert route("Co říká Tao te ťing o nečinnosti?", idx) == ["dao de jing"]
    assert route("co rika TAOTETING o wu-wej", idx) == ["dao de jing"]
    assert route("Na co se ptá král Milinda?", idx) == [nfd("Milindapañhapāḷi")]
    assert route("Otázky krále Milindy", idx) == [nfd("Milindapañhapāḷi")]
    # průnik: širší alias + užší → užší
    assert route("Bhagavadgíta z Mahábháraty", idx) == [nfd("Mahábhárata 06 — Bhíšmaparva")]
    # prázdný průnik → sjednocení
    assert route("Srovnej Tao te ťing a Zhuangziho", idx) == ["dao de jing", "zhuangzi"]
    # obecná otázka nesměruje nikam
    assert route("Co učí buddhismus o utrpení?", idx) == []
    # „edda" míří na obě, „Poetická Edda" jen na jednu
    assert len(route("Co se píše v Eddě?", idx)) == 2
    assert route("Poetická Edda a Völuspá", idx) == ["The poetic Edda"]
    # kmen nechytá vnitřek jiného slova
    assert route("Vypravuj o zeleninovem gulasi", idx) == []

    # tradice: sjednocení, a jmenované dílo má přednost (řeší volající)
    assert route_groups("Jak hinduistické texty vykládají karmu?") == ["sanskrit"]
    assert route_groups("Srovnej duši v buddhismu a v hinduismu.") == ["pali", "sanskrit"]
    assert route_groups("Jak čínská filosofie chápe ctnost?") == ["chinese"]
    assert route_groups("Jak německý idealismus pracuje s duchem?") == ["european_philosophy"]
    assert route_groups("Co říká severská mytologie o ragnaröku?") == ["european"]
    assert route_groups("Vyprávěj mi o motýlovi.") == []

    hits = [{"work": "a", "d": 1}, {"work": "a", "d": 2}, {"work": "a", "d": 3},
            {"work": "b", "d": 4}, {"work": "c", "d": 5}]
    assert [h["d"] for h in diversify(hits, 3, 2)] == [1, 2, 4]
    assert [h["d"] for h in diversify(hits, 5, 2)] == [1, 2, 4, 5, 3]
    assert [h["d"] for h in diversify(hits, 2, 1)] == [1, 4]
    # konkordance z Textů pyramid vs. souvislá próza
    tabulka = ("225, Nt 160\nPT 160\nW 122, T 128, P 179, M\n170, N 226, Nt 161\n"
               "PT 161\nW 123, T 129, P 180, M\n171, N 227, Nt 162\nPT 162\n")
    proza = ("Nečinnost není pasivita, ale nezásah. Když vládce nechá věci "
             "plynout, samy se uspořádají.\nProto se říká: cesta nic nedělá "
             "a přesto není nic, co by nebylo uděláno.\nTak zní čtyřicátá "
             "kapitola.\nA dál se praví, že měkké přemáhá tvrdé.\n"
             "Voda je toho příkladem, protože ustupuje a přece hloubí kámen.")
    obsah = ("CONTENTS\n"
             "Spells for Proceeding toward the Sky ................. 297\n"
             "Spells for Joining the Gods ......................... 312\n"
             "Fragments ........................................... 331\n"
             "A Note on Translation ............................... 8\n")
    assert looks_tabular(tabulka)
    assert looks_tabular(obsah)
    assert not looks_tabular(proza)
    assert not looks_tabular("krátký úryvek\nna dva řádky")
    # verše s číslováním ještě tabulka nejsou
    assert not looks_tabular(
        "469. Tassa puṭṭho viyākāsi, mātali devasārathi;\n"
        "Vipākaṃ pāpakammānaṃ, jānaṃ akkhāsijānato.\n"
        "470. Yo ve pubbe katvā pāpakammaṃ, taṃ pacchā anutappati;\n"
        "Assumukho rodamāno, vipākaṃ phussate pāpaṃ.\n"
        "471. Idha socati pecca socati, pāpakārī ubhayattha socati.")

    pali = "Soṇḍova pitvā visamissapānaṃ, teneva so hoti dukkhī parattha."
    assert is_echo(pali, pali)
    assert is_echo(pali, " ".join(pali.split()) + " ")
    assert is_echo(pali, "")
    assert not is_echo(pali, "Kdo pije nápoj smíšený s jedem, tím se pak trápí.")
    assert not is_echo("道可道，非常道。", "Cesta, kterou lze vyslovit, není věčná cesta.")

    print("retrieval.py: selftest ok")


if __name__ == "__main__":
    _selftest()

"""Krok agenta pro klienta (Ol1nLLM) — co `POST /agent/chat` přidává ke smyčce.

`loop.Pravnik.krok` je jeden průchod model ↔ nástroje a o konverzaci nic neví.
Naměřeno na SPARKu 2026-10-05 s qwen36 (`pravnik-agent` → `openclaw-default`):
bez historie model ve druhém kroku zapomněl, že jde o nájemní smlouvu, a ptal
se na vymyšlené proměnné (`name_najemce`). Tady je proto to, co drží konverzaci
a intake pohromadě nezávisle na modelu:

* **historie v RAM** serveru, per `session_id` (jako `/chat/stream`), smaže ji
  `/reset`. Obsah dokumentu je v Postgresu (`lawyer_sessions`), takže restart
  serveru ztratí jen rozhovor, ne odpovědi;
* **stav intake do kontextu** — šablona, co je vyplněné a co chybí, z PG, ať
  model navazuje i po restartu;
* **odpovědi z karet se ukládají deterministicky** (`odpovedi` v požadavku →
  `save_intake` s převodem typů), ne až když si na to model vzpomene;
* **karty jsou z šablony** — otázky `ask_user` s id, které šablona nezná, se
  nahradí dalšími chybějícími povinnými údaji ze šablony.

Kontrakt (`docs/lawyer/AGENT.md`, sekce „Kontrakt pro appku“) je jeden JSON na
krok, ne stream: krok trvá 10–60 s a jeho výsledkem jsou strukturovaná data
(karty, dokument), ne text, který by stálo za to vykreslovat po tokenech.
"""

from __future__ import annotations

import re
from collections import deque
from datetime import date

# agent.tools (a s ním docgen) se importuje až ve funkcích: HistorieAgenta
# zakládá i knihovní instance serveru, která šablony nemá
# Okno qwen36 na SPARKu (AiStack PLAN-spark-scheduler.md, okno llm). Gemma pod
# aliasem `pravnik-agent` běží jen v profilu gemma na vyžádání; mimo okna alias
# skončí chybou a klient dostane tohle místo stacktrace z LiteLLM.
HLASKA_MIMO_OKNO = (
    "Právník teď smlouvy nesepisuje: model, který agenta pohání, běží na serveru "
    "jen večer od 19:00 do 01:00. Zkus to v tom čase — co už máš vyplněné, zůstává "
    "uložené a naváže se na to. Dotazy na zákon v běžném chatu Právníka fungují dál.")

HLASKA_V_OKNE = (
    "Model Právníka teď neodpovídá, ačkoli by v tuhle dobu běžet měl — zkus to "
    "za pár minut znovu. Co už máš vyplněné, zůstává uložené. (Technicky: {chyba})")

OKNO_OD, OKNO_DO = 19, 1     # hodiny, místní čas SPARKu; okno přes půlnoc

MAX_HISTORIE = 24            # zpráv (user + assistant) na session, starší odpadnou


def v_okne(hodina: int) -> bool:
    return hodina >= OKNO_OD or hodina < OKNO_DO


def hlaska_modelu(chyba: str, hodina: int) -> str:
    """Chyba modelu → věta pro člověka do 503. Mimo okno je to očekávaný stav
    (model neběží), v okně porucha — to má uživatel poznat."""
    if v_okne(hodina):
        return HLASKA_V_OKNE.format(chyba=(chyba or "")[:200])
    return HLASKA_MIMO_OKNO


# --- historie ---------------------------------------------------------------------

class HistorieAgenta:
    """Rozhovor per session_id v RAM. Jen text — volání nástrojů se do historie
    nedávají, jejich výsledek je ve stavu intake, který jde do kontextu zvlášť."""

    def __init__(self, max_zprav: int = MAX_HISTORIE, max_sessions: int = 500):
        self.max_zprav = max_zprav
        self.max_sessions = max_sessions
        self._d: dict[str, deque] = {}

    def nacti(self, session_id: str) -> list[dict]:
        return list(self._d.get(session_id) or [])

    def pridej(self, session_id: str, user: str, assistant: str) -> None:
        if session_id not in self._d and len(self._d) >= self.max_sessions:
            self._d.pop(next(iter(self._d)))          # nejstarší session ven
        h = self._d.setdefault(session_id, deque(maxlen=self.max_zprav))
        h.append({"role": "user", "content": user})
        h.append({"role": "assistant", "content": assistant})

    def smaz(self, session_id: str) -> None:
        self._d.pop(session_id, None)


# --- převod odpovědí z karet ------------------------------------------------------

_ANO = {"ano", "a", "true", "1", "yes", "y", "jo", "chci"}
_NE = {"ne", "n", "false", "0", "no", "nechci"}


def _cislo(v) -> float:
    if isinstance(v, bool):
        raise ValueError("čekám číslo")
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower()
    s = re.sub(r"(kč|czk|,-|m²|m2|hod(in)?|dn[ůíu]|měsíc[ůeí]?)", "", s)
    s = s.replace(" ", "").replace(" ", "")
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    s = s.replace(",", "")
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        raise ValueError(f"„{v}“ není číslo")
    return float(s)


def _datum(v) -> str:
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return date.fromisoformat(s).isoformat()
    m = re.fullmatch(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", s)
    if m:
        d, mes, r = (int(x) for x in m.groups())
        return date(r, mes, d).isoformat()
    raise ValueError(f"„{v}“ není datum (čekám 1. 10. 2026 nebo 2026-10-01)")


def normalizuj(promenna: dict, hodnota):
    """Hodnota z karty → hodnota, kterou šablona umí (render i kogentní limity).
    Vyhodí ValueError s větou pro člověka."""
    typ = promenna.get("typ") or "string"
    if hodnota is None or (isinstance(hodnota, str) and not hodnota.strip()):
        return None                                   # odvolaná / prázdná odpověď
    if typ in ("money", "int"):
        n = _cislo(hodnota)
        if typ == "int" and n != int(n):
            raise ValueError(f"„{hodnota}“ má být celé číslo")
        return int(n) if n == int(n) else n
    if typ == "date":
        return _datum(hodnota)
    if typ == "bool":
        if isinstance(hodnota, bool):
            return hodnota
        s = str(hodnota).strip().lower()
        if s in _ANO:
            return True
        if s in _NE:
            return False
        raise ValueError(f"„{hodnota}“ — odpověz ano, nebo ne")
    if typ == "enum":
        povolene = [str(x) for x in promenna.get("hodnoty") or []]
        s = str(hodnota).strip()
        for p in povolene:
            if s.lower() == p.lower():
                return p
        raise ValueError(f"„{hodnota}“ není z nabídky ({', '.join(povolene)})")
    return str(hodnota).strip()


# --- stav intake -------------------------------------------------------------------

def stav_intake(nastroje, sessions, session_id: str) -> dict | None:
    """Kde je rozpracovaný dokument — pro progress bar v klientovi a pro kontext
    modelu. None, dokud session nemá zvolenou šablonu."""
    if sessions is None:
        return None
    s = sessions.nacti(session_id)
    if s is None or not s.typ:
        return None
    from docgen.templates import stav_vstupu

    sab = nastroje._sablona(s.typ)
    st = stav_vstupu(sab, s.promenne)
    povinne = [p for p in sab["promenne"] if p.get("povinna")]
    return {"typ": s.typ, "nazev": sab["nazev"], "stav": s.stav,
            "vyplneno": len(s.promenne),
            "povinnych": len(povinne),
            "povinnych_vyplneno": len(povinne) - len(st["chybi"]),
            "chybi_celkem": len(st["chybi"]),
            "dalsi_otazky": dalsi_otazky(sab, s.promenne, st),
            "porusene_limity": [{"zprava": p["zprava"], "zaklad": p["zaklad"]} for p in st["porusene"]],
            "pripraveno_k_renderu": st["ok"]}


def dalsi_otazky(sab: dict, promenne: dict, st: dict, max_otazek: int = 3) -> list[dict]:
    """Na co se zeptat dál, ze šablony, ne od modelu. Nejdřív chybějící povinné
    údaje; když žádný nechybí a vstup přesto neprojde, tak proměnné z porušené
    kontroly — typicky podmíněně povinné („doba == 'neurcita' or doba_do != null“:
    doba_do není povinná, dokud není nájem na dobu určitou). qwen36 si na ně
    vymýšlí id (`doba_ukonceni`, SPARK 2026-10-05), takže karta musí přijít odsud."""
    if st["chybi"]:
        return st["chybi"][:max_otazek]
    from docgen.templates import jmena_ve_vyrazu, promenne_indexu, zaklad_text

    prom = promenne_indexu(sab)
    out: list[dict] = []
    for k in sab.get("kontroly") or []:
        if not any(p["vyraz"] == k["vyraz"] for p in st["porusene"]):
            continue
        jmena = [j for j in sorted(jmena_ve_vyrazu(k["vyraz"])) if j in prom]
        # prázdné napřed (doplnit), vyplněné jen když prázdné nejsou (opravit)
        prazdne = [j for j in jmena if promenne.get(j) in (None, "")]
        for j in prazdne or jmena:
            if any(q["id"] == j for q in out):
                continue
            p = prom[j]
            z = zaklad_text(k.get("zaklad"))
            out.append({"id": j, "otazka": p.get("otazka") or "", "typ": p["typ"],
                        "hodnoty": p.get("hodnoty") or None,
                        "napoveda": k["zprava"] + (f" ({z})" if z else ""),
                        "hodnota": promenne.get(j)})
    return out[:max_otazek]


def kontext_intake(stav: dict | None, promenne: dict | None = None) -> str:
    """Věta do systémové zprávy: na co model navazuje."""
    if not stav:
        return ("Šablona zatím není zvolená. Až ji vybereš, načti ji přes get_template "
                "— tím se k rozpracovanému dokumentu přiřadí.")
    t = (f"Rozpracovaný dokument: šablona {stav['typ']} ({stav['nazev']}), "
         f"vyplněno {stav['povinnych_vyplneno']}/{stav['povinnych']} povinných údajů.")
    if promenne:
        t += " Vyplněné proměnné: " + ", ".join(sorted(promenne)) + "."
    if stav["dalsi_otazky"]:
        t += " Další chybějící: " + ", ".join(q["id"] for q in stav["dalsi_otazky"]) + "."
    if stav["porusene_limity"]:
        t += " Porušené zákonné limity: " + "; ".join(p["zprava"] for p in stav["porusene_limity"]) + "."
    if stav["pripraveno_k_renderu"] and stav["stav"] == "intake":
        t += " Vstup je kompletní — můžeš zavolat render_document."
    t += (" Při ask_user používej id proměnných ze šablony; v textu pro uživatele"
          " id nepiš, jen lidské otázky.")
    return t


def uloz_odpovedi(nastroje, sessions, session_id: str, odpovedi: list[dict]) -> dict:
    """Odpovědi z karet → `save_intake`. Neznámé id nebo nepřevoditelná hodnota
    neshodí ostatní: vrátí se v `odmitnuto` a model se na ně zeptá znovu."""
    out = {"ulozeno": [], "odmitnuto": []}
    s = sessions.nacti(session_id) if sessions is not None else None
    if s is None or not s.typ:
        out["odmitnuto"] = [{"id": o.get("id"), "duvod": "dokument ještě nemá zvolenou šablonu"}
                            for o in odpovedi]
        return out
    prom = {p["id"]: p for p in nastroje._sablona(s.typ)["promenne"]}
    k_ulozeni: dict = {}
    for o in odpovedi:
        pid = o.get("id")
        if pid not in prom:
            out["odmitnuto"].append({"id": pid, "duvod": f"šablona {s.typ} takový údaj nemá"})
            continue
        try:
            k_ulozeni[pid] = normalizuj(prom[pid], o.get("hodnota"))
        except ValueError as e:
            out["odmitnuto"].append({"id": pid, "duvod": str(e)})
    if k_ulozeni:
        from agent.tools import zavolej

        _, chyba = zavolej(nastroje.registr(), "save_intake",
                           {"session_id": session_id, "promenne": k_ulozeni},
                           sessions=sessions, session_id=session_id)
        if chyba:
            out["odmitnuto"] += [{"id": k, "duvod": chyba} for k in k_ulozeni]
        else:
            out["ulozeno"] = sorted(k_ulozeni)
    return out


def zprava_s_odpovedmi(zprava: str, odpovedi: list[dict], vysledek: dict | None) -> str:
    """Text pro model: co uživatel napsal + co server s odpověďmi udělal."""
    radky = [zprava.strip()] if (zprava or "").strip() else []
    if odpovedi:
        radky.append("Odpovědi na otázky:")
        radky += [f"- {o.get('otazka') or o.get('id')} ({o.get('id')}): {o.get('hodnota')}"
                  for o in odpovedi]
    if vysledek:
        if vysledek["ulozeno"]:
            radky.append("[Systém] Uloženo do dokumentu (save_intake už proběhl): "
                         + ", ".join(vysledek["ulozeno"]) + ".")
        if vysledek["odmitnuto"]:
            radky.append("[Systém] Neuloženo: " + "; ".join(
                f"{x['id']} — {x['duvod']}" for x in vysledek["odmitnuto"]) + ".")
    return "\n".join(radky)


def _otazky_ze_sablony(otazky: list[dict], stav: dict | None, zname_id: set[str]) -> list[dict]:
    """Karty od modelu nechá, jen když všechna id zná šablona (i nepovinná —
    model nabízí volitelné klauzule); jinak dá další chybějící povinné údaje.
    Bez zvolené šablony nechá, co model poslal."""
    if not stav:
        return otazky
    if otazky and all(q.get("id") in zname_id for q in otazky):
        return otazky
    if stav["pripraveno_k_renderu"] or stav["stav"] != "intake":
        return []
    return stav["dalsi_otazky"]


# --- jeden krok pro klienta -------------------------------------------------------

def krok_pro_klienta(agent, nastroje, sessions, historie: HistorieAgenta, *, zprava: str,
                     session_id: str, mode: str | None = None, zdroj: str = "uzivatel",
                     ma_prilohu: bool = False, odpovedi: list[dict] | None = None,
                     model: str = "") -> dict:
    """Jeden krok agenta tak, jak ho vrací `POST /agent/chat`. Výjimku
    `ChybaModelu` nechává projít — převod na 503 je věc serveru."""
    vysledek = None
    if odpovedi and zdroj == "uzivatel":
        vysledek = uloz_odpovedi(nastroje, sessions, session_id, odpovedi)
    text = zprava_s_odpovedmi(zprava, odpovedi or [], vysledek)

    pred = stav_intake(nastroje, sessions, session_id)
    s = sessions.nacti(session_id) if sessions is not None else None
    krok = agent.krok(text, session_id=session_id, historie=historie.nacti(session_id),
                      zdroj=zdroj, mode=mode, ma_prilohu=ma_prilohu,
                      kontext=kontext_intake(pred, s.promenne if s else None))

    from agent.tools import ChybaNastroje

    stav = stav_intake(nastroje, sessions, session_id)
    zname_id: set[str] = set()
    if stav:
        try:
            zname_id = {p["id"] for p in nastroje._sablona(stav["typ"])["promenne"]}
        except ChybaNastroje:
            pass
    otazky = [] if krok.dokument else _otazky_ze_sablony(krok.otazky, stav, zname_id)

    # do historie jde i to, na co se karty ptaly — v dalším kroku model ví, k čemu
    # patří odpovědi
    # Do historie jen text odpovědi. Karty se do ní nepřipisují: qwen36 pak
    # značky „[Karty …]“ opisoval do vlastních odpovědí. K čemu odpovědi patří,
    # ví model z dalšího kroku (otázka + id u každé odpovědi, zprava_s_odpovedmi).
    historie.pridej(session_id, text, krok.odpoved or "")

    return {"odpoved": krok.odpoved, "mode": krok.mode, "session_id": session_id,
            "otazky": otazky, "dokument": krok.dokument,
            "checklist": krok.checklist, "upozorneni": krok.upozorneni,
            "stav": stav, "ulozene_odpovedi": vysledek,
            "volani": krok.volani, "ms_modelu": krok.ms_modelu, "model": model}

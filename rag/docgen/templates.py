"""Šablony smluv a dokumentů: načtení, kontroly, render.

Šablona je **data, ne prompt** — YAML v `data/templates/{typ}.yaml`, renderuje
kód, ne model (plán `LAWYER_TEMPLATES_PLAN.md` §0). Model se uplatní jinde:
při psaní šablony (s § v ruce) a v agentovi, který sbírá odpovědi uživatele.

Tři věci, které tenhle modul dělá:
  1. `load()` — YAML → dict, s kontrolou struktury proti `schema.json`.
  2. `Evaluator` — bezpečné vyhodnocení výrazů v `podminka` a `kontroly`
     (`jistota + smluvni_pokuta <= 3 * najemne`). Žádný eval(): AST se prochází
     a povolené jsou jen jména, čísla, aritmetika a srovnání.
  3. `render()` — dokument v Markdownu: strany, číslované články, zápatí
     s verzí šablony a datem.

Čistá logika bez DB: `python3 -m docgen.templates` spustí selftest. Existenci
paragrafů proti právnímu indexu kontroluje `docgen/validate.py`.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]          # WorldLibraryProject/
TEMPLATE_DIR = ROOT / "data" / "templates"
SCHEMA = Path(__file__).parent / "schema.json"

NBSP = " "
MESICE = ["ledna", "února", "března", "dubna", "května", "června", "července",
          "srpna", "září", "října", "listopadu", "prosince"]
RIMSKE = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII",
          "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX"]
PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*(?:\|\s*([a-z]+)\s*)?\}\}")
# role strany → proměnné, které pro ni šablona musí deklarovat (kontroluje validate.py)
STRANA_POLOZKY = ("jmeno", "identifikace", "adresa")


class ChybaSablony(Exception):
    """Šablona je vadná (struktura, neznámá proměnná, nevyhodnotitelný výraz)."""


class ChybaVstupu(Exception):
    """Vstupní hodnoty nesplňují šablonu (chybí povinná, porušen kogentní limit)."""


# --- formátování ---------------------------------------------------------------

def kc(value) -> str:
    """12500 → „12 500 Kč" (nezlomitelné mezery, jak se to v češtině sází)."""
    if value is None:
        return ""
    n = int(round(float(value)))
    groups = f"{n:,}".replace(",", NBSP)
    return f"{groups}{NBSP}Kč"


def datum(value) -> str:
    """„2026-10-01" → „1. října 2026"."""
    if value is None:
        return ""
    d = value if isinstance(value, date) else date.fromisoformat(str(value))
    return f"{d.day}.{NBSP}{MESICE[d.month - 1]}{NBSP}{d.year}"


def cislo(value) -> str:
    if value is None:
        return ""
    n = float(value)
    return f"{int(n):,}".replace(",", NBSP) if n == int(n) else f"{n}".replace(".", ",")

FILTRY = {"kc": kc, "datum": datum, "cislo": cislo, "text": lambda v: "" if v is None else str(v)}


# --- výrazy --------------------------------------------------------------------

@dataclass
class Evaluator:
    """Vyhodnocení výrazu nad proměnnými bez eval().

    Povoleno: jména proměnných, `null`, `true`/`false`, čísla, text v uvozovkách,
    `+ - * /`, srovnání, `and/or/not`, `min/max`. Chybějící číslo je v aritmetice
    nula, takže `jistota + smluvni_pokuta <= 3 * najemne` projde i bez jistoty.
    """
    hodnoty: dict = field(default_factory=dict)

    FUNKCE = {"min": min, "max": max, "abs": abs}

    def eval(self, vyraz: str):
        try:
            tree = ast.parse(vyraz, mode="eval")
        except SyntaxError as e:
            raise ChybaSablony(f"nevyhodnotitelný výraz {vyraz!r}: {e}") from None
        return self._node(tree.body, vyraz)

    def _node(self, n, src: str):
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            if n.id == "null":
                return None
            if n.id in ("true", "false"):
                return n.id == "true"
            if n.id not in self.hodnoty:
                raise ChybaSablony(f"výraz {src!r} používá neznámou proměnnou {n.id!r}")
            return self.hodnoty[n.id]
        if isinstance(n, ast.BoolOp):
            vals = [self._node(v, src) for v in n.values]
            return all(vals) if isinstance(n.op, ast.And) else any(vals)
        if isinstance(n, ast.UnaryOp):
            v = self._node(n.operand, src)
            if isinstance(n.op, ast.Not):
                return not v
            if isinstance(n.op, ast.USub):
                return -self._cislo(v)
            raise ChybaSablony(f"výraz {src!r}: nepodporovaný unární operátor")
        if isinstance(n, ast.BinOp):
            a, b = self._cislo(self._node(n.left, src)), self._cislo(self._node(n.right, src))
            for typ, fn in ((ast.Add, lambda: a + b), (ast.Sub, lambda: a - b),
                            (ast.Mult, lambda: a * b), (ast.Div, lambda: a / b if b else 0.0)):
                if isinstance(n.op, typ):
                    return fn()
            raise ChybaSablony(f"výraz {src!r}: nepodporovaný operátor")
        if isinstance(n, ast.Compare):
            left = self._node(n.left, src)
            for op, comp in zip(n.ops, n.comparators):
                right = self._node(comp, src)
                if not self._srovnani(op, left, right, src):
                    return False
                left = right
            return True
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name) or n.func.id not in self.FUNKCE:
                raise ChybaSablony(f"výraz {src!r}: povolené funkce jsou {sorted(self.FUNKCE)}")
            return self.FUNKCE[n.func.id](*[self._cislo(self._node(a, src)) for a in n.args])
        raise ChybaSablony(f"výraz {src!r}: nepovolená konstrukce {type(n).__name__}")

    @staticmethod
    def _cislo(v) -> float:
        if v is None or v is False:
            return 0.0
        if v is True:
            return 1.0
        try:
            return float(v)
        except (TypeError, ValueError):
            raise ChybaSablony(f"v aritmetice nelze použít {v!r}") from None

    def _srovnani(self, op, a, b, src: str) -> bool:
        if isinstance(op, ast.Eq):
            return a == b
        if isinstance(op, ast.NotEq):
            return a != b
        a_, b_ = self._cislo(a), self._cislo(b)
        for typ, fn in ((ast.Lt, lambda: a_ < b_), (ast.LtE, lambda: a_ <= b_),
                        (ast.Gt, lambda: a_ > b_), (ast.GtE, lambda: a_ >= b_)):
            if isinstance(op, typ):
                return fn()
        raise ChybaSablony(f"výraz {src!r}: nepodporované srovnání")


# --- načtení -------------------------------------------------------------------

def load(path: str | Path) -> dict:
    """YAML šablona + kontrola proti JSON schématu."""
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ChybaSablony(f"{path.name}: šablona musí být mapping")
    validuj_schema(data, path.name)
    return data


def validuj_schema(data: dict, jmeno: str = "") -> None:
    import jsonschema

    try:
        jsonschema.validate(data, json.loads(SCHEMA.read_text(encoding="utf-8")))
    except jsonschema.ValidationError as e:
        kde = "/".join(str(p) for p in e.absolute_path) or "(kořen)"
        raise ChybaSablony(f"{jmeno}: {kde}: {e.message}") from None


def vsechny(dir_: str | Path = TEMPLATE_DIR) -> list[dict]:
    return [load(p) for p in sorted(Path(dir_).glob("*.yaml")) if p.name != "sources.yaml"]


# --- render --------------------------------------------------------------------

def promenne_indexu(sablona: dict) -> dict[str, dict]:
    return {p["id"]: p for p in sablona.get("promenne") or []}


def zkontroluj_vstup(sablona: dict, vstup: dict) -> dict:
    """Doplní chybějící nepovinné na None, ohlásí chybějící povinné, špatné enum
    hodnoty a porušené kogentní limity (`kontroly`). Vrací normalizované hodnoty."""
    prom = promenne_indexu(sablona)
    nezname = set(vstup) - set(prom)
    if nezname:
        raise ChybaVstupu(f"neznámé proměnné: {', '.join(sorted(nezname))}")
    hodnoty: dict = {}
    chybi = []
    for pid, p in prom.items():
        v = vstup.get(pid)
        if v in (None, ""):
            if p.get("povinna"):
                chybi.append(f"{pid} ({p.get('otazka') or ''})".strip())
            hodnoty[pid] = None
            continue
        if p["typ"] == "enum" and v not in (p.get("hodnoty") or []):
            raise ChybaVstupu(f"{pid}: {v!r} není z {p.get('hodnoty')}")
        hodnoty[pid] = v
    if chybi:
        raise ChybaVstupu("chybí povinné údaje: " + "; ".join(chybi))

    ev = Evaluator(hodnoty)
    porusene = [k for k in sablona.get("kontroly") or [] if not ev.eval(k["vyraz"])]
    if porusene:
        raise ChybaVstupu("porušené zákonné limity: " + " | ".join(
            f"{k['zprava']} ({zaklad_text(k.get('zaklad'))})" for k in porusene))
    return hodnoty


def zaklad_text(zaklad) -> str:
    """{zakon: 89/2012 Sb., par: § 2254} → „§ 2254 zákona č. 89/2012 Sb."."""
    if not zaklad:
        return ""
    if isinstance(zaklad, list):
        return "; ".join(zaklad_text(z) for z in zaklad)
    par = zaklad.get("par") or zaklad.get("paragrafy") or ""
    return f"{par} zákona č. {zaklad['zakon']}".strip()


def _dosad(text: str, hodnoty: dict, kde: str) -> str:
    def repl(m: re.Match) -> str:
        pid, filtr = m.group(1), m.group(2) or "text"
        if pid not in hodnoty:
            raise ChybaSablony(f"{kde}: neznámá proměnná {{{{{pid}}}}}")
        if filtr not in FILTRY:
            raise ChybaSablony(f"{kde}: neznámý filtr |{filtr} (mám {sorted(FILTRY)})")
        return FILTRY[filtr](hodnoty[pid])

    return PLACEHOLDER.sub(repl, text)


def vyber_klauzule(sablona: dict, hodnoty: dict, vypnute: set[str] | None = None) -> list[dict]:
    """Klauzule, které se do dokumentu dostanou: povinné vždy, nepovinné podle
    `podminka` a podle toho, co uživatel nevypnul."""
    vypnute = vypnute or set()
    ev = Evaluator(hodnoty)
    out = []
    for k in sablona["klauzule"]:
        if k["id"] in vypnute:
            if k.get("povinna"):
                raise ChybaVstupu(f"klauzuli {k['id']} nelze vypnout, je povinná "
                                  f"({zaklad_text(k.get('zaklad'))})")
            continue
        if not k.get("povinna") and k.get("podminka") and not ev.eval(k["podminka"]):
            continue
        out.append(k)
    return out


def text_klauzule(k: dict, hodnoty: dict) -> str:
    if "varianty" in k:
        podle = k["varianty"]["podle"]
        hodnota = hodnoty.get(podle)
        varianty = k["varianty"]["hodnoty"]
        if hodnota not in varianty:
            raise ChybaVstupu(f"klauzule {k['id']}: pro {podle}={hodnota!r} není varianta "
                              f"(mám {sorted(varianty)})")
        return _dosad(varianty[hodnota], hodnoty, f"klauzule {k['id']}/{hodnota}")
    return _dosad(k["text"], hodnoty, f"klauzule {k['id']}")


def podpisy(sablona: dict, hodnoty: dict) -> list[str]:
    """Podpisová pole: u smlouvy obě strany, u jednostranného dokumentu odesílatel."""
    strany = sablona.get("strany") or []
    if not strany:
        return []
    radky = ["V ……………………………… dne ………………………………", ""]
    if sablona["druh"] == "smlouva":
        podepisuji = strany
    else:
        # jednostranný dokument podepisuje odesílatel; šablona může přidat druhou
        # stranu přes `podpisuje` (plná moc — zmocněnec zmocnění přijímá)
        podepisuji = [s for i, s in enumerate(strany) if i == 0 or s.get("podpisuje")]
    for s in podepisuji:
        jmeno = hodnoty.get(s["role"] + "_jmeno") or ""
        radky += ["………………………………………………………………",
                  f"{s['nazev']}: {jmeno}" if len(podepisuji) > 1 else jmeno, ""]
    return radky


def render(sablona: dict, vstup: dict, vypnute: set[str] | None = None,
           k_datu: date | None = None) -> str:
    """Dokument v Markdownu. Deterministický — stejný vstup dá stejný výstup
    (kromě data v zápatí, které jde zadat přes `k_datu`, aby šly dělat snapshoty)."""
    hodnoty = zkontroluj_vstup(sablona, vstup)
    klauzule = vyber_klauzule(sablona, hodnoty, vypnute)
    if len(klauzule) > len(RIMSKE):
        raise ChybaSablony(f"{sablona['typ']}: víc klauzulí než římských číslic")

    smlouva = sablona["druh"] == "smlouva"
    radky = [f"# {sablona['nazev'].upper()}", ""]
    zaklad = sablona.get("pravni_zaklad") or []
    if zaklad:
        z = zaklad[0]
        radky += [f"*{'Uzavřena podle' if smlouva else 'Podle'} "
                  f"{z.get('paragrafy') or z.get('par')} zákona č. {z['zakon']}*", ""]

    strany = sablona.get("strany") or []
    for i, s in enumerate(strany):
        role = s["role"]
        jmeno = hodnoty.get(f"{role}_jmeno") or ""
        ident = hodnoty.get(f"{role}_identifikace") or ""
        adresa = hodnoty.get(f"{role}_adresa") or ""
        detail = ", ".join(x for x in (ident, adresa) if x)
        # u jednostranného dokumentu není „smluvní strana", ale kdo komu píše;
        # šablona to může přepsat vlastním `oznaceni` (plná moc: udílí / přijímá)
        oznaceni = s.get("oznaceni") or ("" if smlouva else ("odesílatel" if i == 0 else "adresát"))
        label = f"{s['nazev']} ({oznaceni})" if oznaceni else s["nazev"]
        radky += [f"**{label}:** {jmeno}" + (f", {detail}" if detail else ""), ""]
    if strany and smlouva:
        radky += ["(dále jen " + " a ".join(f"„{s['nazev']}“" for s in strany) + ")", ""]

    for i, k in enumerate(klauzule):
        nadpis = f"Článek {RIMSKE[i]}. {k['nazev']}" if smlouva else k["nazev"]
        radky += [f"## {nadpis}", "", text_klauzule(k, hodnoty).strip(), ""]

    radky += podpisy(sablona, hodnoty) + ["---", ""]
    den = k_datu or date.today()
    radky += [f"*Návrh vygenerovaný Ol1nLLM, šablona `{sablona['typ']}` verze "
              f"{sablona['verze']}, k datu {datum(den)}. Nejde o právní službu; "
              f"u věci s reálnými následky doporučujeme kontrolu advokátem.*"]
    return "\n".join(radky) + "\n"


def checklist(sablona: dict) -> list[str]:
    """Kontrolní seznam k dokumentu — s § u bodů, které vycházejí ze zákona."""
    out = []
    for c in sablona.get("checklist") or []:
        z = zaklad_text(c.get("zaklad"))
        out.append(f"{c['bod']}" + (f" ({z})" if z else ""))
    return out


# --- selftest ------------------------------------------------------------------

def _selftest() -> None:
    assert kc(12500) == f"12{NBSP}500{NBSP}Kč", kc(12500)
    assert kc(None) == "" and kc(0) == f"0{NBSP}Kč"
    assert datum("2026-10-01") == f"1.{NBSP}října{NBSP}2026", datum("2026-10-01")

    ev = Evaluator({"jistota": 20000, "smluvni_pokuta": None, "najemne": 12000, "doba": "urcita"})
    assert ev.eval("jistota + smluvni_pokuta <= 3 * najemne") is True     # 20000 <= 36000
    assert ev.eval("jistota > 3 * najemne") is False
    assert ev.eval("smluvni_pokuta != null") is False                     # chybějící = None
    assert ev.eval("jistota != null and doba == 'urcita'") is True
    assert ev.eval("not (jistota > 100)") is False
    assert ev.eval("max(jistota, najemne) == 20000") is True
    for zly in ("__import__('os')", "open('x')", "neznama > 1"):
        try:
            ev.eval(zly)
            raise AssertionError(f"mělo spadnout: {zly}")
        except ChybaSablony:
            pass

    sablona = {
        "typ": "test", "nazev": "Testovací dokument", "verze": 1, "jazyk": "cs",
        "druh": "smlouva",
        "pravni_zaklad": [{"zakon": "89/2012 Sb.", "paragrafy": "§ 1"}],
        "strany": [{"role": "a", "nazev": "Strana A", "typ": ["fyzicka"]},
                   {"role": "b", "nazev": "Strana B", "typ": ["fyzicka"]}],
        "promenne": [
            {"id": "a_jmeno", "typ": "string", "povinna": True, "otazka": "Kdo?"},
            {"id": "a_identifikace", "typ": "string", "povinna": False, "otazka": "RČ?"},
            {"id": "a_adresa", "typ": "string", "povinna": False, "otazka": "Kde?"},
            {"id": "b_jmeno", "typ": "string", "povinna": True, "otazka": "Kdo?"},
            {"id": "b_identifikace", "typ": "string", "povinna": False, "otazka": "IČO?"},
            {"id": "b_adresa", "typ": "string", "povinna": False, "otazka": "Kde?"},
            {"id": "najemne", "typ": "money", "povinna": True, "otazka": "Kolik?"},
            {"id": "jistota", "typ": "money", "povinna": False, "otazka": "Jistota?"},
        ],
        "kontroly": [{"vyraz": "jistota <= 3 * najemne", "zprava": "Jistota je moc vysoká.",
                      "zaklad": {"zakon": "89/2012 Sb.", "par": "§ 2254"}}],
        "klauzule": [
            {"id": "zaklad", "nazev": "Základ", "povinna": True,
             "zaklad": [{"zakon": "89/2012 Sb.", "par": "§ 1"}],
             "text": "Nájemné činí {{ najemne|kc }}."},
            {"id": "jistota", "nazev": "Jistota", "povinna": False, "podminka": "jistota != null",
             "zaklad": [{"zakon": "89/2012 Sb.", "par": "§ 2254"}],
             "text": "Jistota činí {{ jistota|kc }}."},
        ],
        "checklist": [{"bod": "Nájemné je uvedeno", "zaklad": {"zakon": "89/2012 Sb.", "par": "§ 2246"}}],
    }
    validuj_schema(sablona, "selftest")

    doc = render(sablona, {"a_jmeno": "Jan Novák", "b_jmeno": "Petr Svoboda", "najemne": 12000},
                 k_datu=date(2026, 9, 28))
    assert "Nájemné činí 12" in doc and "Jistota" not in doc.split("---")[0], doc
    assert "{{" not in doc
    assert "šablona `test` verze 1" in doc

    doc2 = render(sablona, {"a_jmeno": "A", "b_jmeno": "B", "najemne": 12000, "jistota": 20000},
                  k_datu=date(2026, 9, 28))
    assert "Jistota činí 20" in doc2 and "Článek II." in doc2

    for vstup, cekej in (({"b_jmeno": "B", "najemne": 1}, "chybí povinné"),
                         ({"a_jmeno": "A", "b_jmeno": "B", "najemne": 10000, "jistota": 40000},
                          "porušené zákonné limity"),
                         ({"a_jmeno": "A", "b_jmeno": "B", "najemne": 1, "neco": 2}, "neznámé")):
        try:
            render(sablona, vstup)
            raise AssertionError(f"mělo spadnout: {vstup}")
        except ChybaVstupu as e:
            assert cekej in str(e), (cekej, str(e))

    assert checklist(sablona) == ["Nájemné je uvedeno (§ 2246 zákona č. 89/2012 Sb.)"]
    print("docgen/templates.py: selftest ok")


if __name__ == "__main__":
    _selftest()

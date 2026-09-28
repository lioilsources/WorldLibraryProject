"""Validace šablon — `make validate-templates`.

Pět vrstev, od nejlevnější k nejdražší:

1. **schéma** (`schema.json`) a shoda `typ` s jménem souboru;
2. **proměnné**: každé `{{x}}` v textu je deklarované, každá deklarovaná
   proměnná se někde použije (text, podmínka, kontrola, varianty) a každá strana
   má svoje `<role>_jmeno / _identifikace / _adresa`;
3. **výrazy**: `podminka` i `kontroly` se vyhodnotí (na fixtuře, jinak na
   prázdných hodnotách) — překlep ve jménu proměnné spadne tady;
4. **§ existují v právním indexu**: zákon je v katalogu a `zaklad.par` je
   skutečný `chapters.ref` toho předpisu. Tohle je ta podstatná kontrola —
   šablona bez opory v účinném znění je horší než žádná. Potřebuje `LAW_PG_DSN`
   (bez něj se vrstva přeskočí a nahlásí to);
5. **fixtury a snapshoty**: každá fixtura se vyrenderuje (žádné zbylé `{{`)
   a porovná se snapshotem, ať se změna textu nedostane do produkce nepozorovaně.

    .venv/bin/python3 -m docgen.validate
    .venv/bin/python3 -m docgen.validate --update-snapshots   # po zamýšlené změně textu
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from docgen.templates import (PLACEHOLDER, STRANA_POLOZKY, TEMPLATE_DIR, ChybaSablony,
                              ChybaVstupu, Evaluator, load, promenne_indexu, render)

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOTS = FIXTURES / "snapshots"
META_KLICE = ("_popis", "_vypnute", "_k_datu")


def load_dotenv() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# --- vrstvy --------------------------------------------------------------------

def texty(sablona: dict):
    """(kde, text) pro všechna místa, kde mohou být {{proměnné}}."""
    for k in sablona["klauzule"]:
        if "text" in k:
            yield f"klauzule {k['id']}", k["text"]
        for h, t in (k.get("varianty", {}).get("hodnoty") or {}).items():
            yield f"klauzule {k['id']}/{h}", t


def zkontroluj_promenne(sablona: dict) -> list[str]:
    prom = promenne_indexu(sablona)
    problemy = []
    pouzite: set[str] = set()
    for kde, text in texty(sablona):
        for m in PLACEHOLDER.finditer(text):
            pid = m.group(1)
            pouzite.add(pid)
            if pid not in prom:
                problemy.append(f"{kde}: {{{{{pid}}}}} není deklarovaná proměnná")

    # proměnné zmíněné ve výrazech a variantách se taky počítají za použité
    for k in sablona["klauzule"]:
        if k.get("podminka"):
            pouzite |= set(re.findall(r"[a-z_][a-z0-9_]*", k["podminka"]))
        if "varianty" in k:
            pouzite.add(k["varianty"]["podle"])
    for kontrola in sablona.get("kontroly") or []:
        pouzite |= set(re.findall(r"[a-z_][a-z0-9_]*", kontrola["vyraz"]))

    # hlavičku s identifikací stran skládá render() z konvence <role>_jmeno/_identifikace/
    # _adresa, takže tyhle proměnné jsou použité, i když v textu klauzule nejsou
    for s in sablona.get("strany") or []:
        for polozka in STRANA_POLOZKY:
            pid = f"{s['role']}_{polozka}"
            pouzite.add(pid)
            if pid not in prom:
                problemy.append(f"strana {s['role']}: chybí proměnná {pid}")

    for pid in prom:
        if pid not in pouzite:
            problemy.append(f"proměnná {pid} se nikde nepoužije (mrtvá otázka pro uživatele)")

    for k in sablona["klauzule"]:
        if "varianty" not in k:
            continue
        podle = k["varianty"]["podle"]
        p = prom.get(podle)
        if not p:
            problemy.append(f"klauzule {k['id']}: varianty podle neznámé proměnné {podle}")
        elif p["typ"] != "enum":
            problemy.append(f"klauzule {k['id']}: varianty podle {podle}, což není enum")
        else:
            chybi = set(p.get("hodnoty") or []) - set(k["varianty"]["hodnoty"])
            if chybi:
                problemy.append(f"klauzule {k['id']}: chybí varianta pro {sorted(chybi)}")
    return problemy


def zkontroluj_vyrazy(sablona: dict, hodnoty: dict | None = None) -> list[str]:
    prom = promenne_indexu(sablona)
    zaklad = {pid: (hodnoty or {}).get(pid) for pid in prom}
    # enum proměnné dostanou první hodnotu, ať `doba == 'urcita'` nespadne na None
    for pid, p in prom.items():
        if zaklad.get(pid) is None and p["typ"] == "enum" and p.get("hodnoty"):
            zaklad[pid] = p["hodnoty"][0]
    ev = Evaluator(zaklad)
    problemy = []
    for k in sablona["klauzule"]:
        if k.get("podminka"):
            try:
                ev.eval(k["podminka"])
            except ChybaSablony as e:
                problemy.append(f"klauzule {k['id']}: {e}")
    for kontrola in sablona.get("kontroly") or []:
        try:
            ev.eval(kontrola["vyraz"])
        except ChybaSablony as e:
            problemy.append(f"kontrola {kontrola['vyraz']!r}: {e}")
    return problemy


def zaklady(sablona: dict):
    """(kde, zaklad) pro všechna místa, kde šablona tvrdí oporu v zákoně."""
    for z in sablona.get("pravni_zaklad") or []:
        yield "pravni_zaklad", z
    if sablona.get("forma", {}).get("zaklad"):
        yield "forma", sablona["forma"]["zaklad"]
    for p in sablona.get("promenne") or []:
        if p.get("zaklad"):
            yield f"promenna {p['id']}", p["zaklad"]
    for kontrola in sablona.get("kontroly") or []:
        if kontrola.get("zaklad"):      # kontrola bez § je formální (úplnost dokumentu)
            yield f"kontrola {kontrola['vyraz']!r}", kontrola["zaklad"]
    for k in sablona["klauzule"]:
        for z in k.get("zaklad") or []:
            yield f"klauzule {k['id']}", z
    for c in sablona.get("checklist") or []:
        if c.get("zaklad"):
            yield f"checklist {c['bod'][:28]!r}", c["zaklad"]
    for u in sablona.get("upozorneni") or []:
        yield f"upozorneni {u['text'][:28]!r}", u["zaklad"]


def zkontroluj_paragrafy(sablony: list[dict], dsn: str) -> tuple[list[str], int]:
    """Každý zákon je v katalogu a každý `par` je skutečný § toho zákona."""
    import psycopg

    problemy, overeno = [], 0
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, coalesce(work_legacy, title) FROM works")
        legacy = {lg: wid for wid, lg in cur.fetchall()}
        cache: dict[tuple[str, str], bool] = {}
        for sablona in sablony:
            for kde, z in zaklady(sablona):
                wid = legacy.get(z["zakon"])
                if not wid:
                    problemy.append(f"{sablona['typ']}: {kde}: zákon {z['zakon']} není "
                                    f"v právním indexu (vrstva 1 = 53 předpisů)")
                    continue
                par = z.get("par")
                if not par:
                    continue
                key = (wid, par)
                if key not in cache:
                    cur.execute("SELECT count(*) FROM chapters WHERE work_id=%s AND ref=%s",
                                (wid, par))
                    cache[key] = bool(cur.fetchone()[0])
                overeno += 1
                if not cache[key]:
                    problemy.append(f"{sablona['typ']}: {kde}: {par} v {z['zakon']} "
                                    f"v účinném znění neexistuje")
    return problemy, overeno


def fixtury_sablony(typ: str) -> list[Path]:
    return sorted(FIXTURES.glob(f"{typ}__*.json"))


def zkontroluj_fixtury(sablona: dict, update: bool = False) -> tuple[list[str], int]:
    problemy, hotovo = [], 0
    for f in fixtury_sablony(sablona["typ"]):
        vstup = json.loads(f.read_text(encoding="utf-8"))
        meta = {k: vstup.pop(k) for k in META_KLICE if k in vstup}
        try:
            doc = render(sablona, vstup, vypnute=set(meta.get("_vypnute") or []),
                         k_datu=date.fromisoformat(meta.get("_k_datu", "2026-09-28")))
        except (ChybaSablony, ChybaVstupu) as e:
            problemy.append(f"{f.name}: render spadl: {e}")
            continue
        if "{{" in doc:
            problemy.append(f"{f.name}: v dokumentu zůstal nevyplněný {{{{…}}}}")
        problemy += zkontroluj_vyrazy(sablona, vstup)
        snap = SNAPSHOTS / f"{f.stem}.md"
        if update:
            snap.parent.mkdir(parents=True, exist_ok=True)
            snap.write_text(doc, encoding="utf-8")
        elif not snap.exists():
            problemy.append(f"{f.name}: chybí snapshot {snap.name} "
                            f"(vyrob ho: --update-snapshots)")
        elif snap.read_text(encoding="utf-8") != doc:
            problemy.append(f"{f.name}: render se rozešel se snapshotem {snap.name}")
        hotovo += 1
    if not hotovo:
        problemy.append(f"{sablona['typ']}: žádná fixtura — render se nikdy nezkusil")
    return problemy, hotovo


# --- CLI -----------------------------------------------------------------------

def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="Validace šablon smluv")
    p.add_argument("--templates", default=str(TEMPLATE_DIR))
    p.add_argument("--pg-dsn", default=os.environ.get("LAW_PG_DSN"))
    p.add_argument("--update-snapshots", action="store_true")
    p.add_argument("--skip-pg", action="store_true", help="bez kontroly § v indexu")
    args = p.parse_args()

    soubory = [f for f in sorted(Path(args.templates).glob("*.yaml")) if f.name != "sources.yaml"]
    if not soubory:
        print(f"CHYBA: v {args.templates} nejsou žádné šablony", file=sys.stderr)
        return 2

    sablony, problemy = [], []
    for f in soubory:
        try:
            s = load(f)
        except ChybaSablony as e:
            problemy.append(str(e))
            continue
        if s["typ"] != f.stem:
            problemy.append(f"{f.name}: typ {s['typ']!r} nesedí na jméno souboru")
        sablony.append(s)
        problemy += [f"{s['typ']}: {x}" for x in zkontroluj_promenne(s)]
        problemy += [f"{s['typ']}: {x}" for x in zkontroluj_vyrazy(s)]

    renderu = 0
    for s in sablony:
        pr, n = zkontroluj_fixtury(s, update=args.update_snapshots)
        problemy += pr
        renderu += n

    overeno = 0
    if args.skip_pg or not args.pg_dsn:
        print("POZOR: přeskakuji kontrolu § v právním indexu (chybí LAW_PG_DSN)")
    else:
        pr, overeno = zkontroluj_paragrafy(sablony, args.pg_dsn)
        problemy += pr

    print(f"šablon: {len(sablony)} | klauzulí: {sum(len(s['klauzule']) for s in sablony)} "
          f"| ověřených § proti indexu: {overeno} | renderů: {renderu}")
    for s in sablony:
        print(f"  {s['typ']:<34} v{s['verze']} {len(s['klauzule'])} klauzulí, "
              f"{len(s['promenne'])} otázek, {len(s.get('kontroly') or [])} kontrol")
    for x in problemy:
        print(f"✗ {x}")
    print("problémy:", len(problemy) or "žádné")
    return 1 if problemy else 0


if __name__ == "__main__":
    sys.exit(main())

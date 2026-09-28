"""Vyrenderuj dokument ze šablony a odpovědí — `make render-doc`.

    .venv/bin/python3 -m docgen.render najemni_smlouva_byt --vstup odpovedi.json
    .venv/bin/python3 -m docgen.render odstoupeni_od_smlouvy_na_dalku --otazky

`--otazky` vypíše, na co se šablona ptá (tohle bude vstup pro agenta
z LAWYER_AGENT_PLAN.md), `--checklist` kontrolní seznam s §, `--upozorneni`
co zákon zakazuje nebo omezuje.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from docgen.templates import (TEMPLATE_DIR, ChybaSablony, ChybaVstupu, checklist, load, render,
                              zaklad_text)
from docgen.validate import META_KLICE


def vypis_otazky(sablona: dict) -> None:
    print(f"{sablona['nazev']} (šablona {sablona['typ']}, verze {sablona['verze']})")
    for p in sablona["promenne"]:
        znak = "*" if p["povinna"] else " "
        typ = p["typ"] + (f" {p['hodnoty']}" if p["typ"] == "enum" else "")
        print(f" {znak} {p['id']:<26} {typ:<22} {p['otazka']}")
        if p.get("napoveda"):
            print(f"     └ {p['napoveda']}")
        if p.get("zaklad"):
            print(f"     opora: {zaklad_text(p['zaklad'])}")
    print("\n* = povinné")


def main() -> int:
    p = argparse.ArgumentParser(description="Render dokumentu ze šablony")
    p.add_argument("typ", help="id šablony, např. najemni_smlouva_byt")
    p.add_argument("--templates", default=str(TEMPLATE_DIR))
    p.add_argument("--vstup", help="JSON s odpověďmi (bez něj jen --otazky)")
    p.add_argument("--out", help="kam zapsat dokument (default stdout)")
    p.add_argument("--otazky", action="store_true", help="vypiš otázky šablony")
    p.add_argument("--checklist", action="store_true")
    p.add_argument("--upozorneni", action="store_true")
    p.add_argument("--vypnout", default="", help="id klauzulí k vynechání, oddělené čárkou")
    args = p.parse_args()

    cesta = Path(args.templates) / f"{args.typ}.yaml"
    if not cesta.exists():
        k_dispozici = ", ".join(sorted(f.stem for f in Path(args.templates).glob("*.yaml")
                                       if f.name != "sources.yaml"))
        print(f"CHYBA: šablona {args.typ!r} není; mám: {k_dispozici}", file=sys.stderr)
        return 2
    try:
        sablona = load(cesta)
    except ChybaSablony as e:
        print(f"CHYBA: {e}", file=sys.stderr)
        return 2

    if args.otazky:
        vypis_otazky(sablona)
        return 0
    if args.checklist:
        for b in checklist(sablona):
            print(f"- [ ] {b}")
        return 0
    if args.upozorneni:
        for u in sablona.get("upozorneni") or []:
            print(f"! {' '.join(u['text'].split())}\n  opora: {zaklad_text(u['zaklad'])}")
        return 0

    if not args.vstup:
        print("CHYBA: bez --vstup umím jen --otazky / --checklist / --upozorneni", file=sys.stderr)
        return 2
    vstup = json.loads(Path(args.vstup).read_text(encoding="utf-8"))
    meta = {k: vstup.pop(k) for k in META_KLICE if k in vstup}
    vypnute = set(x for x in args.vypnout.split(",") if x) | set(meta.get("_vypnute") or [])
    try:
        doc = render(sablona, vstup, vypnute=vypnute,
                     k_datu=date.fromisoformat(meta["_k_datu"]) if "_k_datu" in meta else None)
    except (ChybaVstupu, ChybaSablony) as e:
        print(f"CHYBA: {e}", file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(doc, encoding="utf-8")
        print(f"uloženo: {args.out}")
    else:
        sys.stdout.write(doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())

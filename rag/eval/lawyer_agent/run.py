#!/usr/bin/env python3
"""Eval agenta Právník — `make eval-agent`.

Dvě sady, obě **bez LLM**, protože obojí má platit bez ohledu na model:

* **draft** (`scenarios_draft.jsonl`): skriptovaný uživatel vysype odpovědi po
  trojicích tak, jak by je agent dostával z `ask_user`, a eval kontroluje, že
  (a) intake postupuje (počet chybějících klesá), (b) dokument se vyrenderuje
  jen když má, (c) v hotovém dokumentu nezůstal `{{…}}`, (d) scénář s porušeným
  kogentním limitem render **nedostane** a hláška cituje správné pravidlo.
* **review** (`scenarios_review.jsonl`): podstrčené vady v textu cizí smlouvy —
  jistota 5× nájemné, citace neexistujícího §, chybějící cena díla — a kontrola,
  že deterministický audit je najde a na čisté smlouvě nehlásí falešný poplach.

Latenci a počet volání modelu měřit nejde, dokud v parku neběží chat model; ta
část plánu §7 zůstává otevřená a je v docs/lawyer/AGENT.md popsaná jako to, co se
má proměřit v modelovém okně.

    .venv/bin/python3 eval/lawyer_agent/run.py            # obě sady
    .venv/bin/python3 eval/lawyer_agent/run.py --only draft
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

KDE = Path(__file__).parent


def load_dotenv() -> None:
    env = Path(__file__).resolve().parents[2] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def scenare(jmeno: str) -> list[dict]:
    return [json.loads(l) for l in (KDE / jmeno).read_text(encoding="utf-8").splitlines() if l.strip()]


def po_trojicich(d: dict) -> list[dict]:
    """Odpovědi rozdělené po třech — simuluje, jak je uživatel dává na karty."""
    items = list(d.items())
    return [dict(items[i:i + 3]) for i in range(0, len(items), 3)]


def eval_draft(nastroje, sessions) -> tuple[int, int, list[str]]:
    ok, celkem, problemy = 0, 0, []
    for sc in scenare("scenarios_draft.jsonl"):
        celkem += 1
        sid = f"eval-{sc['id']}-{uuid.uuid4().hex[:8]}"
        chyby: list[str] = []
        try:
            st = nastroje.save_intake(session_id=sid, typ=sc["typ"])
            predchozi = st["chybi_celkem"]
            for davka in po_trojicich(sc["odpovedi"]):
                st = nastroje.save_intake(session_id=sid, promenne=davka)
                if st["chybi_celkem"] > predchozi:
                    chyby.append(f"intake se vrací: {predchozi} → {st['chybi_celkem']} chybějících")
                predchozi = st["chybi_celkem"]

            r = nastroje.render_document(session_id=sid)
            ceka_hotovo = sc["ceka"].get("hotovo", True)
            if ceka_hotovo and not r.get("vyrenderovano"):
                chyby.append(f"nevyrenderovalo se: chybí {[c['id'] for c in r.get('chybi', [])]}, "
                             f"limity {[p['zprava'][:40] for p in r.get('porusene_limity', [])]}")
            if not ceka_hotovo:
                if r.get("vyrenderovano"):
                    chyby.append("vyrenderovalo se, i když scénář porušuje zákon")
                else:
                    text = json.dumps(r, ensure_ascii=False)
                    if sc["ceka"].get("porusuje") and sc["ceka"]["porusuje"] not in text:
                        chyby.append(f"hláška necituje {sc['ceka']['porusuje']!r}: "
                                     f"{[p['zprava'][:60] for p in r.get('porusene_limity', [])]}"
                                     f"{[c['id'] for c in r.get('chybi', [])]}")
            if r.get("vyrenderovano"):
                if "{{" in (r.get("markdown") or ""):
                    chyby.append("v dokumentu zůstal nevyplněný {{…}}")
                if "Nejde o právní službu" not in (r.get("markdown") or ""):
                    chyby.append("v dokumentu chybí upozornění v zápatí")
                if not r.get("checklist"):
                    chyby.append("render nevrátil checklist")
        except Exception as e:
            chyby.append(f"{type(e).__name__}: {e}")
        finally:
            try:
                sessions.smaz(sid)
            except Exception:
                pass
        if chyby:
            problemy += [f"[draft/{sc['id']}] {c}" for c in chyby]
        else:
            ok += 1
        print(f"{'✓' if not chyby else '✗'} draft  {sc['id']:<34} {sc['popis'][:58]}")
    return ok, celkem, problemy


def eval_review(nastroje) -> tuple[int, int, list[str]]:
    ok, celkem, problemy = 0, 0, []
    for sc in scenare("scenarios_review.jsonl"):
        celkem += 1
        chyby: list[str] = []
        try:
            r = nastroje.review_document(text=sc["text"], typ=sc.get("typ"))
            c = sc["ceka"]
            vadne = len(r["vadne_citace"])
            if "vadne_citace" in c and vadne != c["vadne_citace"]:
                chyby.append(f"vadných citací {vadne}, čekáno {c['vadne_citace']}: "
                             f"{[x['ref'] for x in r['vadne_citace']]}")
            if "vadne_citace_min" in c and vadne < c["vadne_citace_min"]:
                chyby.append(f"nenašlo vadné citace (čekáno ≥ {c['vadne_citace_min']}), "
                             f"audit: {[(x['ref'], x['stav']) for x in r['citace']]}")
            if "nalez_limitu" in c and bool(r["nalezy_limitu"]) != c["nalez_limitu"]:
                chyby.append(f"nálezy limitu {r['nalezy_limitu']}, čekáno {c['nalez_limitu']}")
            if "checklist_chybi_min" in c:
                chybi = [x for x in r["checklist"] if x["pravdepodobne_chybi"]]
                if len(chybi) < c["checklist_chybi_min"]:
                    chyby.append(f"checklist neukázal mezeru (čekáno ≥ {c['checklist_chybi_min']})")
            if "neurcity_zakon_min" in c:
                n = len([x for x in r["citace"] if x["stav"] == "neurcity_zakon"])
                if n < c["neurcity_zakon_min"]:
                    chyby.append(f"neoznačilo citace bez předpisu (našlo {n})")
            for z in c.get("zakony") or []:
                if z not in r["zakony_v_textu"]:
                    chyby.append(f"nerozpoznalo zákon {z} (našlo {r['zakony_v_textu']})")
            if c.get("lhuty_obsahuji") and c["lhuty_obsahuji"] not in r["lhuty"]:
                chyby.append(f"nevypsalo lhůtu {c['lhuty_obsahuji']!r} (našlo {r['lhuty']})")
            if c.get("castky_obsahuji") and c["castky_obsahuji"] not in r["castky_kc"]:
                chyby.append(f"nevypsalo částku {c['castky_obsahuji']} (našlo {r['castky_kc']})")
        except Exception as e:
            chyby.append(f"{type(e).__name__}: {e}")
        if chyby:
            problemy += [f"[review/{sc['id']}] {x}" for x in chyby]
        else:
            ok += 1
        print(f"{'✓' if not chyby else '✗'} review {sc['id']:<34} {sc['popis'][:58]}")
    return ok, celkem, problemy


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="Eval agenta Právník (bez LLM)")
    p.add_argument("--pg-dsn", default=os.environ.get("LAW_PG_DSN"))
    p.add_argument("--law-url", default=os.environ.get("LAW_URL", "http://192.168.88.66:8098"))
    p.add_argument("--only", choices=["draft", "review"])
    args = p.parse_args()
    if not args.pg_dsn:
        print("CHYBA: chybí LAW_PG_DSN", file=sys.stderr)
        return 2

    from psycopg_pool import ConnectionPool

    from agent.session import Sessions
    from agent.tools import PravniNastroje

    pool = ConnectionPool(args.pg_dsn, min_size=1, max_size=4, open=True)
    sessions = Sessions(pool)
    nastroje = PravniNastroje(args.law_url, pool, sessions)

    problemy: list[str] = []
    souhrn = {}
    if args.only in (None, "draft"):
        ok, celkem, pr = eval_draft(nastroje, sessions)
        souhrn["draft"] = f"{ok}/{celkem}"
        problemy += pr
    if args.only in (None, "review"):
        print()
        ok, celkem, pr = eval_review(nastroje)
        souhrn["review"] = f"{ok}/{celkem}"
        problemy += pr

    print()
    for k, v in souhrn.items():
        print(f"  {k:<8} {v} scénářů prošlo")
    for x in problemy:
        print(f"✗ {x}")
    print("problémy:", len(problemy) or "žádné")
    return 1 if problemy else 0


if __name__ == "__main__":
    sys.exit(main())

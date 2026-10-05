#!/usr/bin/env python3
"""Kontrola zlatého standardu Právníka proti korpusu — aby v evalu nebyl §,
který v účinném znění neexistuje (a eval pak neměřil vlastní překlep).

Pro každý řádek `golden_law.jsonl` ověří, že `expect_work` je v katalogu a že
`expect_ref` je skutečný `chapters.ref` toho předpisu. Vypíše i rozpad po
oblastech, ať je vidět, kam zlatý standard míří.

    .venv/bin/python3 eval/check_golden_law.py          # PG z LAW_PG_DSN v rag/.env
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def load_dotenv() -> None:
    env = Path(__file__).parent.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="Kontrola golden_law.jsonl proti korpusu")
    p.add_argument("--golden", default=str(Path(__file__).parent / "golden_law.jsonl"))
    p.add_argument("--pg-dsn", default=os.environ.get("LAW_PG_DSN"))
    args = p.parse_args()
    if not args.pg_dsn:
        print("CHYBA: chybí --pg-dsn / LAW_PG_DSN v rag/.env", file=sys.stderr)
        return 2

    import psycopg

    rows = [json.loads(l) for l in open(args.golden, encoding="utf-8") if l.strip()]
    problems = []
    with psycopg.connect(args.pg_dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, coalesce(work_legacy, title) FROM works")
        legacy = {legacy: wid for wid, legacy in cur.fetchall()}
        for r in rows:
            if r.get("catalog"):
                continue
            wid = legacy.get(r.get("expect_work"))
            if not wid:
                problems.append((r["q"], f"neznámý předpis {r.get('expect_work')!r}"))
                continue
            cur.execute("SELECT count(*) FROM chapters WHERE work_id = %s AND ref = %s",
                        (wid, r.get("expect_ref")))
            if not cur.fetchone()[0]:
                problems.append((r["q"], f"{r['expect_work']} {r.get('expect_ref')} není v korpusu"))

    measurable = [r for r in rows if not r.get("catalog")]
    print(f"otázek: {len(rows)} (měřitelných {len(measurable)}, katalogových {len(rows) - len(measurable)})")
    print("oblasti:", ", ".join(f"{a}={n}" for a, n in sorted(Counter(r.get("area") or "—" for r in rows).items())))
    print("bez area:", sum(1 for r in rows if not r.get("area")) or "žádná")
    for q, why in problems:
        print(f"✗ {q}  →  {why}")
    print("problémy:", len(problems) or "žádné")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

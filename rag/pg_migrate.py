#!/usr/bin/env python3
"""Aplikuje SQL migrace z rag/sql/ na knihovní Postgres (JODA :5433).

Eviduje se v tabulce schema_migrations (název souboru), takže opakované
spuštění nic nedělá. Soubory s "indexes" v názvu se přeskakují, dokud se
nezavolá --only — GIN indexy se staví až po načtení dat (make pg-index).

Použití (kdekoli v LAN):
    python3 pg_migrate.py --dsn "$PG_DSN"              # vše kromě indexů
    python3 pg_migrate.py --dsn "$PG_DSN" --only 0003  # indexy po loadu
    python3 pg_migrate.py --dsn "$PG_DSN" --status
"""

import argparse
import os
import re
import sys
from pathlib import Path

import psycopg

SQL_DIR = Path(__file__).parent / "sql"
DEFERRED_MARKER = "indexes"


def load_dotenv(path: Path) -> None:
    """Minimální .env: KEY=VALUE, bez exportu, bez uvozovek kolem hodnot."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def default_dsn() -> str | None:
    load_dotenv(Path(__file__).parent / ".env")
    return os.getenv("PG_DSN")


def applied(conn) -> set[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    return {r[0] for r in conn.execute("SELECT filename FROM schema_migrations")}


def main() -> int:
    p = argparse.ArgumentParser(description="Migrace knihovního Postgresu")
    p.add_argument("--dsn", default=default_dsn(),
                   help="postgresql://library:…@192.168.88.88:5433/library (nebo PG_DSN v rag/.env)")
    p.add_argument("--only", help="prefix souboru, který aplikovat (např. 0003) — i odložené indexy")
    p.add_argument("--status", action="store_true", help="jen vypsat, co je a není aplikované")
    p.add_argument("--force", action="store_true", help="aplikovat i už zapsané (IF NOT EXISTS to snese)")
    args = p.parse_args()
    if not args.dsn:
        print("CHYBA: chybí --dsn (nebo PG_DSN v rag/.env)", file=sys.stderr)
        return 2

    files = sorted(f for f in SQL_DIR.glob("*.sql") if re.match(r"^\d{4}_", f.name))
    with psycopg.connect(args.dsn) as conn:
        done = applied(conn)
        if args.status:
            for f in files:
                mark = "✓" if f.name in done else ("·" if DEFERRED_MARKER in f.name else "✗")
                print(f"  {mark} {f.name}")
            return 0

        for f in files:
            if args.only:
                if not f.name.startswith(args.only):
                    continue
            elif DEFERRED_MARKER in f.name:
                continue  # indexy až po loadu
            if f.name in done and not args.force:
                continue
            sql = f.read_text(encoding="utf-8")
            print(f"aplikuji {f.name} …", end=" ", flush=True)
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s) "
                    "ON CONFLICT (filename) DO NOTHING",
                    (f.name,),
                )
            print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

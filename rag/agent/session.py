"""Stav rozpracovaného dokumentu (intake) v Postgresu — tabulka `lawyer_sessions`.

Proč v DB a ne v RAM: konverzaci si uživatel může nechat spadnout, ale patnáct
odpovědí o nájemní smlouvě chce najít, když se vrátí (plán §4 „uložené sessiony").
Konverzační historie chatu zůstává v RAM serveru, tady je jen obsah dokumentu.

Merge je idempotentní: `uloz(session_id, promenne)` sloučí nové odpovědi do
existujících, takže opakované volání nástroje `save_intake` nic nerozbije.
Hodnota `None` klíč smaže (uživatel odpověď odvolal).

Osobní údaje: každá session má `expires_at` (default 30 dnů) a `uklid_expirovane()`
je maže. Retenci si nastav podle toho, co slíbíš v UI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Session:
    session_id: str
    typ: str | None = None
    promenne: dict = field(default_factory=dict)
    vypnute_klauzule: list[str] = field(default_factory=list)
    stav: str = "intake"
    verze_sablony: int | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None

    @property
    def rozpracovana(self) -> bool:
        return self.stav == "intake"


class Sessions:
    """Tenká vrstva nad psycopg poolem; žádné ORM, ať je vidět SQL."""

    def __init__(self, pool, retence_dnu: int = 30):
        self.pool = pool
        self.retence_dnu = retence_dnu

    # --- čtení ---------------------------------------------------------------

    def nacti(self, session_id: str) -> Session | None:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""SELECT session_id, typ, promenne, vypnute_klauzule, stav,
                                  verze_sablony, updated_at, expires_at
                             FROM lawyer_sessions WHERE session_id = %s""", (session_id,))
            row = cur.fetchone()
        if not row:
            return None
        return Session(session_id=row[0], typ=row[1], promenne=row[2] or {},
                       vypnute_klauzule=row[3] or [], stav=row[4], verze_sablony=row[5],
                       updated_at=row[6], expires_at=row[7])

    def seznam(self, limit: int = 20) -> list[Session]:
        """Rozpracované sessiony, nejnovější napřed — pro „vrať se a dokonči“."""
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""SELECT session_id, typ, promenne, vypnute_klauzule, stav,
                                  verze_sablony, updated_at, expires_at
                             FROM lawyer_sessions
                            WHERE stav = 'intake' AND expires_at > now()
                            ORDER BY updated_at DESC LIMIT %s""", (limit,))
            rows = cur.fetchall()
        return [Session(session_id=r[0], typ=r[1], promenne=r[2] or {}, vypnute_klauzule=r[3] or [],
                        stav=r[4], verze_sablony=r[5], updated_at=r[6], expires_at=r[7])
                for r in rows]

    # --- zápis ---------------------------------------------------------------

    def zaloz(self, session_id: str, typ: str, verze: int | None = None) -> Session:
        """Založí session pro daný typ šablony; při změně typu vyprázdní odpovědi,
        protože proměnné jiné šablony nemají stejný význam."""
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO lawyer_sessions (session_id, typ, verze_sablony, expires_at)
                VALUES (%s, %s, %s, now() + INTERVAL '{int(self.retence_dnu)} days')
                ON CONFLICT (session_id) DO UPDATE
                   SET typ = EXCLUDED.typ,
                       verze_sablony = EXCLUDED.verze_sablony,
                       promenne = CASE WHEN lawyer_sessions.typ = EXCLUDED.typ
                                       THEN lawyer_sessions.promenne ELSE '{{}}'::jsonb END,
                       vypnute_klauzule = CASE WHEN lawyer_sessions.typ = EXCLUDED.typ
                                               THEN lawyer_sessions.vypnute_klauzule ELSE '[]'::jsonb END,
                       stav = 'intake',
                       updated_at = now()
            """, (session_id, typ, verze))
        return self.nacti(session_id)

    def uloz(self, session_id: str, promenne: dict) -> Session:
        """Idempotentní merge odpovědí. None hodnota klíč odstraní."""
        s = self.nacti(session_id)
        if s is None:
            raise KeyError(f"session {session_id} neexistuje — nejdřív zaloz()")
        merged = dict(s.promenne)
        for k, v in (promenne or {}).items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE lawyer_sessions
                              SET promenne = %s::jsonb, updated_at = now()
                            WHERE session_id = %s""", (json.dumps(merged, ensure_ascii=False), session_id))
        return self.nacti(session_id)

    def nastav_klauzule(self, session_id: str, vypnute: list[str]) -> Session:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE lawyer_sessions
                              SET vypnute_klauzule = %s::jsonb, updated_at = now()
                            WHERE session_id = %s""",
                        (json.dumps(sorted(set(vypnute)), ensure_ascii=False), session_id))
        return self.nacti(session_id)

    def nastav_stav(self, session_id: str, stav: str) -> Session:
        if stav not in ("intake", "hotovo", "zruseno"):
            raise ValueError(f"neznámý stav {stav!r}")
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE lawyer_sessions SET stav = %s, updated_at = now() WHERE session_id = %s",
                        (stav, session_id))
        return self.nacti(session_id)

    def smaz(self, session_id: str) -> None:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM lawyer_tool_calls WHERE session_id = %s", (session_id,))
            cur.execute("DELETE FROM lawyer_sessions WHERE session_id = %s", (session_id,))

    def uklid_expirovane(self) -> int:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""DELETE FROM lawyer_tool_calls WHERE session_id IN
                           (SELECT session_id FROM lawyer_sessions WHERE expires_at <= now())""")
            cur.execute("DELETE FROM lawyer_sessions WHERE expires_at <= now()")
            return cur.rowcount

    # --- log nástrojů --------------------------------------------------------

    def zaloguj(self, session_id: str, nastroj: str, vstup: dict, vystup: str,
                ms: int, chyba: str | None = None) -> None:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO lawyer_tool_calls (session_id, nastroj, vstup, vystup, ms, chyba)
                           VALUES (%s, %s, %s::jsonb, %s, %s, %s)""",
                        (session_id, nastroj, json.dumps(vstup, ensure_ascii=False, default=str),
                         (vystup or "")[:2000], ms, chyba))

    def log(self, session_id: str, limit: int = 50) -> list[dict]:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""SELECT ts, nastroj, vstup, vystup, ms, chyba FROM lawyer_tool_calls
                            WHERE session_id = %s ORDER BY ts DESC LIMIT %s""", (session_id, limit))
            return [{"ts": r[0], "nastroj": r[1], "vstup": r[2], "vystup": r[3], "ms": r[4], "chyba": r[5]}
                    for r in cur.fetchall()]

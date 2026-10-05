-- Právník jako agent: stav rozpracovaného dokumentu (intake) a log volání nástrojů.
-- Aplikuje se na databázi `law` (make pg-migrate-law). Konverzační paměť chatu
-- zůstává v RAM serveru; tady je jen to, co musí přežít restart: co už uživatel
-- nadiktoval do smlouvy.
--
-- Retence: nahrané a nadiktované údaje jsou osobní údaje, takže session má
-- `expires_at` a maže se (agent/session.py: uklid_expirovane). Default 30 dnů.

CREATE TABLE IF NOT EXISTS lawyer_sessions (
    session_id        TEXT PRIMARY KEY,
    typ               TEXT,                                  -- id šablony (data/templates)
    promenne          JSONB NOT NULL DEFAULT '{}'::jsonb,    -- odpovědi uživatele
    vypnute_klauzule  JSONB NOT NULL DEFAULT '[]'::jsonb,    -- nepovinné klauzule, které uživatel nechce
    stav              TEXT NOT NULL DEFAULT 'intake',        -- intake | hotovo | zruseno
    verze_sablony     INTEGER,                               -- s jakou verzí se sbíralo
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '30 days',
    CONSTRAINT lawyer_sessions_stav CHECK (stav IN ('intake', 'hotovo', 'zruseno'))
);

CREATE INDEX IF NOT EXISTS lawyer_sessions_expires ON lawyer_sessions (expires_at);

-- Log volání nástrojů. Plán §2 chce logovat každé volání; slouží k evalu
-- (počet volání na úkol) i k dohledání, proč agent udělal, co udělal.
CREATE TABLE IF NOT EXISTS lawyer_tool_calls (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    nastroj     TEXT NOT NULL,
    vstup       JSONB,
    vystup      TEXT,            -- shrnutí, ne celý výstup (chunky mohou být velké)
    ms          INTEGER,
    chyba       TEXT
);

CREATE INDEX IF NOT EXISTS lawyer_tool_calls_session ON lawyer_tool_calls (session_id, ts);

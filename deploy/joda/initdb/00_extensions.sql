-- Běží jednou při založení databáze (docker-entrypoint-initdb.d).
-- Rozšíření potřebují superusera, kterého má aplikační role `library`
-- jen tady (initdb běží jako POSTGRES_USER = vlastník db). Schéma tabulek
-- je v rag/sql/ a aplikuje ho rag/pg_migrate.py — ne initdb — aby šlo
-- měnit bez zakládání databáze znovu.
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- ILIKE '%…%' přes GIN pro CJK a řečtinu
CREATE EXTENSION IF NOT EXISTS unaccent;  -- ad-hoc SQL; retrieval používá vlastní fold()

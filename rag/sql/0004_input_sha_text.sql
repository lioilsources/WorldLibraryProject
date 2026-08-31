-- input_sha nese od této migrace prefix s verzí promptu ("chunk-v1:<sha1>"),
-- aby se „změnil se prompt?" dalo v SQL zeptat prefixem místo hashovací
-- funkce — Postgres sha1() nemá (jen sha224+ nad bytea, a ty by potřebovaly
-- pgcrypto). char(40) na prefix nestačí a navíc by hodnotu doplňoval mezerami.
-- Změnu vlastního textu řeší load_pg.replace_work(), který obohacení zahodí,
-- když nesedí text_sha.

ALTER TABLE chunk_enrichment ALTER COLUMN input_sha TYPE text;

-- Řádky z běhů před prefixem (holý hexdigest) se tímhle stanou „starým
-- promptem" a přeberou se znovu — což je správně, protože jejich vstup
-- odpovídá dřívější verzi promptu.

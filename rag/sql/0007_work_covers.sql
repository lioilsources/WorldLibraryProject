-- Obálky děl pro Kindlify (PLAN-covers.md v Kindlify).
-- cover_prompt = anglický popis motivu (bez stylu, bez textu); styl edice
-- (dřevoryt) a paletu skládá až generátor obrázků, takže změna stylu
-- nevyžaduje nový běh LLM. cover_palette = paleta podle jazyka originálu.
-- cover_input_sha = "cover-v1:<sha1(vstup)>" → idempotence jako u summary.

ALTER TABLE works ADD COLUMN IF NOT EXISTS cover_prompt text;
ALTER TABLE works ADD COLUMN IF NOT EXISTS cover_palette text;
ALTER TABLE works ADD COLUMN IF NOT EXISTS cover_model text;
ALTER TABLE works ADD COLUMN IF NOT EXISTS cover_input_sha text;
ALTER TABLE works ADD COLUMN IF NOT EXISTS cover_at timestamptz;

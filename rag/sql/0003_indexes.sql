-- Indexy se staví až po COPY dat (make pg-index) — GIN nad 200 MB textu
-- se plní řádově rychleji najednou než po řádcích, a maintenance_work_mem
-- je jen 192 MB. pg_migrate.py tenhle soubor přeskakuje, dokud se nezavolá
-- s --only 0003 (viz Makefile), a díky IF NOT EXISTS ho jde pouštět znovu.

CREATE INDEX IF NOT EXISTS works_group_idx        ON works ("group", subgroup);
CREATE INDEX IF NOT EXISTS works_priority_idx     ON works (priority);
CREATE INDEX IF NOT EXISTS works_author_trgm_idx  ON works USING gin (author gin_trgm_ops);
CREATE INDEX IF NOT EXISTS works_name_cs_trgm_idx ON works USING gin (name_cs gin_trgm_ops);

CREATE INDEX IF NOT EXISTS chapters_work_idx      ON chapters (work_id, ordinal);

CREATE INDEX IF NOT EXISTS chunks_tsv_fold_idx    ON chunks USING gin (tsv_fold);
CREATE INDEX IF NOT EXISTS chunks_tsv_bigrams_idx ON chunks USING gin (tsv_bigrams);
CREATE INDEX IF NOT EXISTS chunks_chapter_idx     ON chunks (chapter_id, seq_in_chapter);
CREATE INDEX IF NOT EXISTS chunks_work_seq_idx    ON chunks (work_id, seq);

CREATE INDEX IF NOT EXISTS chunk_enrichment_tsv_idx    ON chunk_enrichment USING gin (tsv_cs);
CREATE INDEX IF NOT EXISTS chunk_enrichment_topics_idx ON chunk_enrichment USING gin (topics);
CREATE INDEX IF NOT EXISTS chunk_enrichment_quality_idx ON chunk_enrichment (quality);

CREATE INDEX IF NOT EXISTS work_topics_topic_idx    ON work_topics (topic_id);
CREATE INDEX IF NOT EXISTS chapter_topics_topic_idx ON chapter_topics (topic_id);

-- Trigram index nad textem chunků je největší (~400–600 MB) a slouží jen
-- ILIKE '%…%' u krátkých termínů a CJK/řečtiny bez mezer. Je poslední,
-- aby šel vynechat (make pg-index NO_TRGM=1 → sed ho vystřihne).
CREATE INDEX IF NOT EXISTS chunks_text_trgm_idx ON chunks USING gin (text_fold gin_trgm_ops);

ANALYZE works; ANALYZE chapters; ANALYZE chunks; ANALYZE chunk_enrichment;

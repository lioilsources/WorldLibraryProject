-- Pohledy pro katalog. Témata díla agregují všechny zdroje (llm/curated/
-- aggregated) — katalog podle témat tak funguje hned po obohacení chunků,
-- ještě než director dopíše summary.

CREATE OR REPLACE VIEW catalog_v AS
SELECT
  w.*,
  coalesce(t.topic_ids, '{}')     AS topic_ids,
  coalesce(t.topic_weights, '{}') AS topic_weights
FROM works w
LEFT JOIN (
  SELECT work_id,
         array_agg(topic_id ORDER BY weight DESC) AS topic_ids,
         array_agg(weight   ORDER BY weight DESC) AS topic_weights
  FROM work_topics
  GROUP BY work_id
) t ON t.work_id = w.id;

CREATE OR REPLACE VIEW chapters_v AS
SELECT
  c.*,
  coalesce(t.topic_ids, '{}')     AS topic_ids,
  coalesce(t.topic_weights, '{}') AS topic_weights
FROM chapters c
LEFT JOIN (
  SELECT chapter_id,
         array_agg(topic_id ORDER BY weight DESC) AS topic_ids,
         array_agg(weight   ORDER BY weight DESC) AS topic_weights
  FROM chapter_topics
  GROUP BY chapter_id
) t ON t.chapter_id = c.id;

-- Stav obohacení pro /status.
CREATE OR REPLACE VIEW enrichment_status_v AS
SELECT
  (SELECT count(*) FROM works)                                   AS works,
  (SELECT count(*) FROM chapters)                                AS chapters,
  (SELECT count(*) FROM chunks)                                  AS chunks,
  (SELECT count(*) FROM chunk_enrichment)                        AS enriched,
  (SELECT count(*) FROM works    WHERE summary_short IS NOT NULL) AS works_summarized,
  (SELECT count(*) FROM chapters WHERE summary_short IS NOT NULL) AS chapters_summarized;

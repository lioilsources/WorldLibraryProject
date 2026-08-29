-- Knihovní katalog + fulltext. Aplikuje rag/pg_migrate.py (podle
-- schema_migrations), rozšíření zakládá deploy/joda/initdb/00_extensions.sql.
--
-- Zásady:
-- - ID jsou ASCII slugy nezávislé na cestě souboru: work 'zh.daodejing',
--   kapitola 'zh.daodejing:0008', chunk 'zh.daodejing:0008:0001'.
--   Řadí se textově a přežijí přejmenování souboru (dnešní Chroma ID je
--   sha256(cesty), takže nový text na staré cestě se tiše přeskočil).
-- - Všechny texty jsou NFC. Chroma drží jména děl v NFD (názvy souborů
--   z macOS) — normalizuje loader, ne databáze.
-- - Fulltext běží nad `*_fold` sloupci (retrieval.fold(): lowercase, NFD,
--   bez diakritiky) v konfiguraci 'simple'. Postgres nemá českou konfiguraci
--   a pro pálí/řečtinu by stemming stejně neexistoval; fold je deterministický
--   napříč jazyky a stejnou funkci používá i dotaz.
-- - GIN indexy jsou v 0003_indexes.sql — staví se až po COPY dat.
-- - Text chunku je vždy ORIGINÁL díla; obohacení je metadata vedle něj.

CREATE TABLE IF NOT EXISTS topics (
  id             text PRIMARY KEY,          -- slug: 'smrt_nesmrtelnost'
  name_cs        text NOT NULL,
  description_cs text NOT NULL,             -- 2 věty pro LLM: kdy použít, kdy ne
  parent_id      text REFERENCES topics(id),
  sort           int  NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS works (
  id              text PRIMARY KEY,
  "group"         text NOT NULL,            -- tradice = top-level adresář korpusu
  subgroup        text,                     -- greek|latin|sutta|vinaya|abhidhamma|…
  title           text NOT NULL,            -- původní/katalogový název (NFC)
  work_legacy     text,                     -- dnešní metadata `work` z Chromy (NFC) — most k summaries.json a aliasům
  name_cs         text,                     -- kurátorské; LLM ho NIKDY nepřepisuje
  author          text,
  author_cs       text,
  lang_original   text NOT NULL,            -- jazyk DÍLA (ISO 639-3): grc, lat, lzh, pi, sa, ang, non, de, ae, egy
  lang_corpus     text NOT NULL,            -- jazyk TEXTU v korpusu: shodný, nebo 'en' u překladu
  is_translation  boolean GENERATED ALWAYS AS (lang_original <> lang_corpus) STORED,
  edition         text,                     -- 'Monro & Allen, OCT 1908–1920' / 'K. M. Ganguli, 1883–96'
  form            text,                     -- epos|hymnus|dialog|traktat|sutta|vinaya|abhidhamma|drama|dopis|epigram|aforismy|kronika|komentar|zakonik|jine
  period          text,                     -- volný text: '8. stol. př. n. l.'
  source_path     text,                     -- rel. cesta v downloads (NFC)
  urn             text,                     -- CTS URN u Perseu
  priority        smallint NOT NULL DEFAULT 2,  -- 1 kanonické (director, kapitoly), 2 běžné, 3 fragmenty/scholia
  chunk_count     int    NOT NULL DEFAULT 0,
  chapter_count   int    NOT NULL DEFAULT 0,
  char_count      bigint NOT NULL DEFAULT 0,
  summary_short   text,                     -- 1 věta
  summary_medium  text,                     -- ~50 slov
  summary_long    text,                     -- ~150 slov
  keywords_cs     text[] NOT NULL DEFAULT '{}',
  summary_model   text,                     -- skutečný response.model — audit, že to není fallback
  summary_input_sha text,                   -- sha1(PROMPT_VERSION + vstup) → idempotence
  summary_at      timestamptz,
  aliases         text[] NOT NULL DEFAULT '{}',  -- kmeny pro směrování (route())
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chapters (
  id              text PRIMARY KEY,         -- '{work_id}:{ordinal:04d}'
  work_id         text NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  ordinal         int  NOT NULL,            -- pořadí v díle 1..n, plošně přes úrovně
  level           smallint NOT NULL DEFAULT 1,
  parent_id       text REFERENCES chapters(id),
  ref             text,                     -- citace edice: '1', '1.3', 'SECTION XII', 'Vagga 3'
  heading         text,                     -- nadpis, jak stojí v textu (NFC)
  heading_cs      text,                     -- LLM překlad nadpisu
  path            text NOT NULL,            -- 'Kniha 1 › Kapitola 3' — do promptu i UI
  char_count      int  NOT NULL DEFAULT 0,
  chunk_count     int  NOT NULL DEFAULT 0,
  summary_short   text,
  summary_medium  text,
  summary_long    text,
  keywords_cs     text[] NOT NULL DEFAULT '{}',
  summary_model   text,
  summary_input_sha text,
  summary_at      timestamptz,
  UNIQUE (work_id, ordinal)
);

CREATE TABLE IF NOT EXISTS chunks (
  id              text PRIMARY KEY,         -- '{work_id}:{chapter_ordinal:04d}:{seq_in_chapter:04d}'
  work_id         text NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  chapter_id      text REFERENCES chapters(id) ON DELETE SET NULL,
  seq             int  NOT NULL,            -- pořadí v díle 0..n-1 (= dnešní chunk_index)
  seq_in_chapter  int  NOT NULL,
  ref_start       text,                     -- CTS/verš na začátku chunku ('1.1', '469')
  ref_end         text,
  lang            text NOT NULL,
  text            text NOT NULL,            -- ORIGINÁL, nikdy překlad
  text_fold       text NOT NULL,            -- retrieval.fold(text)
  text_sha        char(40) NOT NULL,        -- sha1(text) — detekce změn při sync
  char_count      int  NOT NULL,
  tsv_fold        tsvector GENERATED ALWAYS AS (to_tsvector('simple', text_fold)) STORED,
  text_bigrams    text,                     -- jen CJK: '道可 可道 道非 …' — 'simple' parser čínštinu nedělí
  tsv_bigrams     tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text_bigrams, ''))) STORED,
  UNIQUE (work_id, seq)
);

CREATE TABLE IF NOT EXISTS chunk_enrichment (
  chunk_id        text PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
  gloss_cs        text NOT NULL,            -- 1–2 věty česky, o čem chunk je (ne překlad)
  keywords_cs     text[] NOT NULL DEFAULT '{}',
  keywords_en     text[] NOT NULL DEFAULT '{}',
  keywords_orig   text[] NOT NULL DEFAULT '{}',  -- termíny v písmu originálu (無為, nibbāna, ἀρετή)
  questions_cs    text[] NOT NULL DEFAULT '{}',  -- 2–3 otázky, na které chunk odpovídá
  entities        jsonb  NOT NULL DEFAULT '[]',  -- [{"name":"Arjuna","type":"person"}]
  topics          text[] NOT NULL DEFAULT '{}',  -- 0–3 slugy z topics
  quality         smallint,                 -- LLM 0–3; 0 = patička/rejstřík/balast → retrieval vyřadí
  input_sha       char(40) NOT NULL,        -- sha1(PROMPT_VERSION + text) → idempotence
  model           text NOT NULL,            -- skutečný response.model; fallback se neukládá
  created_at      timestamptz NOT NULL DEFAULT now(),
  enrich_fold     text NOT NULL,            -- fold(gloss + keywords + questions) — vstup FTS
  tsv_cs          tsvector GENERATED ALWAYS AS (to_tsvector('simple', enrich_fold)) STORED
);

CREATE TABLE IF NOT EXISTS work_topics (
  work_id   text REFERENCES works(id) ON DELETE CASCADE,
  topic_id  text REFERENCES topics(id),
  weight    real NOT NULL DEFAULT 1.0,      -- 0–1 z LLM; 1.0 kurátorské
  source    text NOT NULL DEFAULT 'llm',    -- llm | curated | aggregated (z četnosti v chuncích)
  PRIMARY KEY (work_id, topic_id)
);

CREATE TABLE IF NOT EXISTS chapter_topics (
  chapter_id text REFERENCES chapters(id) ON DELETE CASCADE,
  topic_id   text REFERENCES topics(id),
  weight     real NOT NULL DEFAULT 1.0,
  source     text NOT NULL DEFAULT 'llm',
  PRIMARY KEY (chapter_id, topic_id)
);

-- Audit dlouhých běhů obohacení: resume, throughput, kolik výstupů přišlo
-- z fallback modelu a bylo zahozeno.
CREATE TABLE IF NOT EXISTS enrich_runs (
  id                 serial PRIMARY KEY,
  kind               text NOT NULL,         -- chunks | chapters | works
  model              text NOT NULL,
  started_at         timestamptz NOT NULL DEFAULT now(),
  finished_at        timestamptz,
  done               int NOT NULL DEFAULT 0,
  failed             int NOT NULL DEFAULT 0,
  rejected_fallback  int NOT NULL DEFAULT 0,
  note               text
);

-- Plánovač dotazu (intent + přepis) — stejná otázka nepotřebuje druhý roundtrip.
CREATE TABLE IF NOT EXISTS query_cache (
  key        text PRIMARY KEY,              -- sha1(fold(otázka) + PROMPT_VERSION)
  plan       jsonb NOT NULL,
  model      text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

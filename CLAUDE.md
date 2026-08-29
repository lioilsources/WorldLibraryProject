# WorldLibraryProject

Multijazyčný korpus filozofických a posvátných textů (download pipeline
v kořeni, RAG chatbot v `rag/`). Dokumentace a komentáře česky.

**Knihovna drží originály.** Smysl projektu je zjistit, jak si LLM poradí
s exotickými jazyky — pálí, sanskrtem, klasickou čínštinou, hebrejštinou —
takže se do korpusu **nepřidávají české ani anglické překlady děl**.
Nepoužitelné dílo (rozbitá textová vrstva, mojibake) se opravuje lepším
zdrojem v původním jazyce, nebo se vyhodí. Do češtiny se překládá až
výstup: odpověď knihovníka a pole `excerpt_cs` u zdrojů.

## Infrastruktura — jména strojů

| Jméno | Co to je | Role |
|---|---|---|
| **M2** | Mac Mini M2 | orchestrátor: stahování korpusu (`run_pipeline.sh`), ingest (`rag/ingest_books.py` → `books.jsonl`), rsync na SPARK |
| **JODA** | Ubuntu server s Dockerem, `192.168.88.88` (LAN only, žádné sdílené disky; 3,8 GB RAM, 2 CPU) | **`deploy/joda/`** tohoto repa: `library_postgres` :5433 (katalog, kapitoly, fulltext, obohacení; data na /media) a `library_chroma` :8007 (books_v2 + books_gloss; data na SSD /home — /media je plotnový disk, 2 upserty/s). AiStack `swarm-chromadb` :8006 drží jen legacy kolekci `books` |
| **SPARK** | DGX Spark (GB10 Grace Blackwell, 128 GB UMA, aarch64) | AiStack LLM park za LiteLLM :4000, veřejně https://llm.ol1n.com; embedding + chatbot `rag/server.py` :8090 (chystá se https://chat.ol1n.com) |

Data mezi stroji tečou přes ssh/rsync (ssh alias `spark`, JODA
dosažitelná jako `joda`).

## Související repa

- **AiStack** — provoz LLM na SPARKu (NIM/vLLM/LiteLLM/cloudflared); řídit se jeho `SKILL.md`
- **EduRAG** — předloha RAG architektury; `rag/` sdílí jeho JSONL kontrakt

## Klíčové soubory

- `PLAN-spark-chatbot.md` — nasazovací plán chatbota (fáze, verifikace, rollback)
- `rag/README.md` — architektura a zprovoznění chatbota
- `rag/registry/` — kurátorský registr děl (works.yaml: název_cs, autor, `lang_original` vs
  `lang_corpus`, detektor kapitol), témata (topics.yaml), výjimky Perseu; `validate.py`
- `rag/retrieval.py` — směrování dotazu na dílo/tradici a diverzita výsledků
  (kurátorská tabulka aliasů; `python3 retrieval.py` spustí selftest)
- `rag/chapters.py`, `rag/clean_text.py`, `rag/perseus_tei.py` — kapitoly per tradice, čištění, TEI
- `rag/retriever.py` + `rag/pg_search.py` + `rag/hybrid.py` — hybridní retrieval (vektor + fulltext → RRF)
- `rag/planner.py` + `rag/catalog.py` — intent dotazu a katalogové odpovědi z Postgresu
- `rag/enrich_*.py` + `rag/llm_batch.py` — obohacení korpusu LLM (přímo na TRT-LLM :8004, fallback se zahazuje)
- `rag/sql/` + `rag/pg_migrate.py` — schéma Postgresu; `rag/.env` (mimo git): `PG_DSN`, `CHROMA_URL`, `COLLECTION`
- `rag/eval/` — měření retrievalu bez LLM proti zlatému standardu; baseline
  a výsledky režimů v `rag/eval/results/`
- `downloads/` — korpus v Git LFS (bez `git lfs pull` jsou to jen pointery!)

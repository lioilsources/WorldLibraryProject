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
| **JODA** | Ubuntu server s Dockerem, `192.168.88.88` (LAN only, žádné sdílené disky; 3,8 GB RAM, 2 CPU) | **`deploy/joda/`** tohoto repa: jen `library_postgres` :5433 (katalog, kapitoly, fulltext, obohacení; data na /media). Chroma se odsud 2026-09-12 přesunula na SPARK (viz `PLAN-spark-library-storage.md`) — JODĚ na 3,8 GB RAM docházela paměť, index se stránkoval ze swapu |
| **SPARK** | DGX Spark (GB10 Grace Blackwell, 128 GB UMA, aarch64) | AiStack LLM park za LiteLLM :4000, veřejně https://llm.ol1n.com; embedding + chatbot `rag/server.py` :8090 (chystá se https://chat.ol1n.com); **`deploy/spark/`** tohoto repa: `library_chroma` :8007 (books_v2 + books_gloss, na NVMe) |

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
- `rag/export_bundle.py` — export díla z Postgresu do bundlu pro Kindlify (čtečka); kontrakt hlídá `validate_bundle()`
- `rag/eval/` — měření retrievalu bez LLM proti zlatému standardu; baseline
  a výsledky režimů v `rag/eval/results/`
- **Právník** (zákony ČR, persona v Ol1nLLM): `rag/ingest_law.py` (e-Sbírka →
  tytéž JSONL), `rag/cite.py` (intent `cite` = lookup paragrafu bez vektoru),
  `rag/registry/law/` (53 zákonů vrstvy 1, aliasy, zkratky, odvětví),
  `rag/prompts/pravnik_cs.md`, `deploy/spark/law-chat.service` (**port 8098**,
  kolekce `law_v1`, databáze `law` na JODA), `make eval-law`
  (`rag/eval/eval_law.py` + `golden_law.jsonl`, `check_golden_law.py`); sekce
  „Právník" a „Eval Právníka" v `rag/README.md`, stav a nálezy
  `docs/lawyer/CURRENT_STATE.md`, plán `Ol1nLLM/docs/plan-pravnik.md`
- **Šablony smluv** (Právník generuje dokumenty): `data/templates/*.yaml` (data,
  ne prompt) + `rag/docgen/` (schéma, render, validace). Každá klauzule nese §
  a `make validate-templates` ověří, že ten § v účinném znění existuje. Jak se
  šablona píše: `docs/lawyer/TEMPLATES.md`
- **Agent Právníka**: `rag/agent/` — osm nástrojů (search_law, get_paragraph,
  šablony, intake, render, revize, ask_user), intake v Postgresu
  (`lawyer_sessions`), deterministická revize cizích smluv. Model je injektovaný,
  takže `make eval-agent` (22 scénářů) běží bez LLM; `POST /agent/chat` jede na
  aliasu `pravnik-agent` (`--agent-model`; dnes qwen36, okno 19–01) a jeho kontrakt
  pro appku Ol1nLLM (persona „Právník – smlouvy 📝") je v `rag/agent/klient.py`.
  Stav, kontrakt a nasazení: `docs/lawyer/AGENT.md`
- `downloads/` — korpus v Git LFS (bez `git lfs pull` jsou to jen pointery!)

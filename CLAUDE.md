# WorldLibraryProject

Multijazyčný korpus filozofických a posvátných textů (download pipeline
v kořeni, RAG chatbot v `rag/`). Dokumentace a komentáře česky.

## Infrastruktura — jména strojů

| Jméno | Co to je | Role |
|---|---|---|
| **M2** | Mac Mini M2 | orchestrátor: stahování korpusu (`run_pipeline.sh`), ingest (`rag/ingest_books.py` → `books.jsonl`), rsync na SPARK |
| **JODA** | Ubuntu server s Dockerem, `192.168.88.88` (LAN only, žádné sdílené disky) | ChromaDB v server režimu na :8006 (AiStack `deploy/docker-compose.swarm.nas.yaml`); kolekce `books` patří tomuto projektu |
| **SPARK** | DGX Spark (GB10 Grace Blackwell, 128 GB UMA, aarch64) | AiStack LLM park za LiteLLM :4000, veřejně https://llm.ol1n.com; embedding + chatbot `rag/server.py` :8090 (chystá se https://chat.ol1n.com) |

Data mezi stroji tečou přes ssh/rsync (ssh alias `spark`, JODA
dosažitelná jako `joda`).

## Související repa

- **AiStack** — provoz LLM na SPARKu (NIM/vLLM/LiteLLM/cloudflared); řídit se jeho `SKILL.md`
- **EduRAG** — předloha RAG architektury; `rag/` sdílí jeho JSONL kontrakt

## Klíčové soubory

- `PLAN-spark-chatbot.md` — nasazovací plán chatbota (fáze, verifikace, rollback)
- `rag/README.md` — architektura a zprovoznění chatbota
- `rag/retrieval.py` — směrování dotazu na dílo/tradici a diverzita výsledků
  (kurátorská tabulka aliasů; `python3 retrieval.py` spustí selftest)
- `rag/eval/` — měření retrievalu bez LLM proti zlatému standardu; baseline
  a výsledky režimů v `rag/eval/results/`
- `downloads/` — korpus v Git LFS (bez `git lfs pull` jsou to jen pointery!)

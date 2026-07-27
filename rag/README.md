# RAG chatbot nad světovou knihovnou

Personalizovaný chatbot pro povídání o knihách z `downloads/`. Návrh
vychází z [EduRAG](https://github.com/lioilsources/EduRAG) (sdílí jeho
JSONL kontrakt a embedding model), LLM a vektorovou DB zajišťuje
[AiStack](https://github.com/lioilsources/AiStack).

## Architektura

```
M2 (orchestrátor)         JODA (NAS)                     SPARK (AiStack)
─────────────────         ──────────                     ───────────────
run_pipeline.sh           ChromaDB :8006                 LiteLLM gateway :4000
ingest_books.py      ──▶  (AiStack swarm.nas)      ◀──  ├─ translate (Qwen3-32B-AWQ)
  → books.jsonl                                         ├─ swarm-director (on-demand)
                                                        └─ ...
                                                        embed_books.py ──▶ Chroma
                                                        server.py :8090  ◀── klient
```

- **M2**: stáhne korpus, vytáhne text z TXT/PDF, rozseká na chunky,
  zapíše `books.jsonl` na NAS mount.
- **JODA**: Chroma v server režimu — nasazuje AiStack
  (`deploy/docker-compose.swarm.nas.yaml`, port **8006**). Knihy jdou do
  vlastní kolekce `books`, se SwarmBattle daty se nemíchají.
- **SPARK**: AiStack drží LLM park za LiteLLM (:4000); tady běží i
  `embed_books.py` a chatbot `server.py` (:8090 — 8080 má Go gateway).

## Výběr modelu (z AiStack parku)

| LiteLLM role | Model | Kdy použít |
|---|---|---|
| **`translate`** | Qwen3-32B-AWQ | **výchozí** — nejlepší čeština + multilingvální (sanskrt, pálí, čínština), ~20 GB |
| `swarm-director` | Nemotron-3-Super-120B-A12B-NVFP4 | hluboké filozofické rozbory; on-demand (`make up-swarm-director`, +60 GB) |
| `lab` | gpt-oss-120b | silné reasoning, slabší čeština |
| `swarm-nano` | Nemotron-3-Nano-30B-A3B | rychlé odpovědi, slabší humanitní hloubka |

Model jde přepnout za běhu per dotaz (`"model": "swarm-director"` v
requestu) nebo flagem `--llm-model`. Thinking mód vypíná LiteLLM config.

**Embeddingy**: výchozí je lokální `multilingual-e5-large` (nejlepší
cross-lingual čeština↔angličtina, nutný pro dotazy česky nad anglickými
překlady). Alternativně AiStack `swarm-embed` (e5-mistral-7b) přes
`--embed-url http://localhost:8005/v1 --embed-model intfloat/e5-mistral-7b-instruct`
— ale indexovat a dotazovat se musí stejným modelem, takže volbu udělej
před `make embed`.

## Kde může chatbot běžet

AiStack je vystavený přes Cloudflare tunel na **https://llm.ol1n.com**
(→ Go gateway :8080 → LiteLLM :4000), takže `server.py` nemusí běžet na
SPARKu:

- **Na SPARKu** (výchozí): `--llm-url http://localhost:4000/v1` — přímo
  na LiteLLM, bez auth.
- **Na M2 / jinde**: `LLM_URL=https://llm.ol1n.com/v1 make serve` +
  Cloudflare Access service token v env:
  `CF_ACCESS_CLIENT_ID` a `CF_ACCESS_CLIENT_SECRET` (server je pošle
  jako `CF-Access-Client-Id/Secret` hlavičky).

Pozor: ChromaDB na JODA (`192.168.88.88:8006`) je jen v LAN — retrieval
tedy vyžaduje, aby server běžel v domácí síti (M2 stačí; embedding
jednoho dotazu zvládne i MPS/CPU). Přes internet by musel i Chroma port
do tunelu.

## Zprovoznění

### 0. Data (M2)

Korpus je v gitu jako Git LFS pointery — nejdřív `git lfs pull`, nebo
`./run_pipeline.sh` pro čerstvé stažení.

### 1. JODA — Chroma (přes AiStack)

```bash
# na NASu, z AiStack repa:
docker compose -f deploy/docker-compose.swarm.nas.yaml up -d   # :8006
```

### 2. M2 — ingest

```bash
cd rag && pip install pymupdf
make ingest            # TXT + PDF → books.jsonl
make ingest-txt        # jen TXT (rychlý start, čistá data)
```

Skript hlásí LFS pointery bez obsahu a PDF bez textové vrstvy (skeny
z archive.org — potřebují OCR, viz TODO).

### 3. SPARK — embedding

```bash
cd rag && pip install -r requirements.txt
make embed             # CHROMA_URL má výchozí http://192.168.88.88:8006
```

Idempotentní — už vložené chunky přeskakuje, jde navázat po přerušení.

### 4. SPARK — chatbot

AiStack musí mít nahozený gateway a translate: `make up-llm up-translate`.

```bash
make serve             # chatbot na :8090
```

### 5. Povídání

```bash
curl -s localhost:8090/chat -H 'Content-Type: application/json' \
  -d '{"message": "Co říká Tao te ťing o nečinnosti?"}' | jq

# hluboký rozbor přes director (musí běžet: make up-swarm-director)
curl -s localhost:8090/chat -H 'Content-Type: application/json' \
  -d '{"message": "Srovnej wu-wej s Kantovou autonomií vůle.",
       "model": "swarm-director", "session_id": "..."}' | jq
```

Odpověď obsahuje `session_id` — pošli ho v dalším dotazu a chatbot si
pamatuje kontext konverzace. `POST /reset` paměť smaže. Swagger UI na
`/docs`, stav na `/status`.

## Personalizace

- **Osobnost**: `prompts/librarian_cs.md`, přepínatelná přes
  `--prompt-file`. Výchozí je český „sečtělý knihovník" citující zdroje.
- **Paměť**: posledních `--history-turns` (výchozí 10) párů
  otázka/odpověď per session, drží se v RAM serveru.
- **Model**: per dotaz nebo `--llm-model` (viz tabulka výše).

## TODO / známá omezení

- **OCR**: většina PDF (zejm. Tipiṭaka, BORI Mahábhárata) jsou skeny bez
  textové vrstvy — ingest je zatím přeskakuje a vypíše seznam. AiStack
  OCR stack (TrOCR Kurrent) je na ručně psané matriky, na knižní skeny
  bude potřeba jiná cesta (např. `ocrmypdf`).
- Paměť konverzací je jen v RAM — restart serveru ji smaže.
- Perseus submoduly (řečtina/latina, TEI XML) zatím nejsou napojené.
- Chatbot jde případně zaregistrovat do AiStack `litellm_config.yaml`
  / Go gatewaye, aby byl dostupný přes Cloudflare tunel.

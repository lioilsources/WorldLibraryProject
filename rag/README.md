# RAG chatbot nad světovou knihovnou

Personalizovaný chatbot pro povídání o knihách z `downloads/`. Návrh
vychází z [EduRAG](https://github.com/lioilsources/EduRAG) — sdílí jeho
JSONL kontrakt (`{id, source, lang, group, title, text, created_at,
embedded}`), embedding model i celkovou pipeline, ale je přizpůsobený
knižnímu korpusu a rozložený na tři stroje.

## Architektura

```
M2 (orchestrátor)            JODA (NAS, Docker)            SPARK (GPU)
─────────────────            ──────────────────            ───────────
run_pipeline.sh              ChromaDB server :8000         vLLM :8000 (Qwen3-32B)
ingest_books.py         ──▶  books.jsonl (mount)      ──▶  embed_books.py ──▶ Chroma
                                                           server.py :8080  ◀── klient
```

- **M2**: stáhne korpus, vytáhne text z TXT/PDF, rozseká na chunky,
  zapíše `books.jsonl` na NAS mount.
- **JODA**: drží korpus, JSONL a vektorovou DB. Chroma běží v server
  režimu v Dockeru — žádné SQLite přes NFS/SMB.
- **SPARK**: embeduje chunky (GPU), servíruje vLLM a chatbot API.

## Zprovoznění

### 0. Data (M2)

Korpus je v gitu jako Git LFS pointery — nejdřív `git lfs pull`, nebo
`./run_pipeline.sh` pro čerstvé stažení.

### 1. JODA — vektorová DB

```bash
cd rag/deploy/joda && docker compose up -d   # Chroma na :8000
```

### 2. M2 — ingest

```bash
cd rag && pip install pymupdf
make ingest            # TXT + PDF → books.jsonl
make ingest-txt        # jen TXT (rychlý start, čistá data)
```

Skript hlásí LFS pointery bez obsahu a PDF bez textové vrstvy (skeny
z archive.org — ty potřebují OCR, viz TODO níže).

### 3. SPARK — embedding

```bash
cd rag && pip install -r requirements.txt
make embed CHROMA_URL=http://<IP-JODA>:8000
```

Idempotentní — už vložené chunky přeskakuje, jde navázat po přerušení.

### 4. SPARK — LLM + chatbot

```bash
make vllm                                   # terminál 1: vLLM na :8000
make serve CHROMA_URL=http://<IP-JODA>:8000 # terminál 2: chatbot na :8080
```

### 5. Povídání

```bash
curl -s localhost:8080/chat -H 'Content-Type: application/json' \
  -d '{"message": "Co říká Tao te ťing o nečinnosti?"}' | jq
```

Odpověď obsahuje `session_id` — pošli ho v dalším dotazu a chatbot si
pamatuje kontext konverzace. `POST /reset` paměť smaže. Swagger UI na
`/docs`, stav na `/status`.

## Personalizace

- **Osobnost**: `prompts/librarian_cs.md`, přepínatelná přes
  `--prompt-file`. Výchozí je český „sečtělý knihovník" citující zdroje.
- **Paměť**: posledních `--history-turns` (výchozí 10) párů
  otázka/odpověď per session, drží se v RAM serveru.
- **Model**: cokoli, co vLLM utáhne — `--llm-model`.

## TODO / známá omezení

- **OCR**: většina PDF (zejm. Tipiṭaka, BORI Mahábhárata) jsou skeny bez
  textové vrstvy — ingest je zatím přeskakuje a vypíše seznam. Další
  krok: OCR pipeline (např. `ocrmypdf` s jazykovými balíky).
- Paměť konverzací je jen v RAM — restart serveru ji smaže.
- Perseus submoduly (řečtina/latina, TEI XML) zatím nejsou napojené.

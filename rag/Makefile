# RAG chatbot — cíle pro jednotlivé stroje.
# Přepiš CHROMA_URL / LLM_MODEL podle svého prostředí, např.:
#   make embed CHROMA_URL=http://192.168.1.20:8000

CHROMA_URL ?= http://joda:8000
LLM_URL    ?= http://localhost:8000/v1
LLM_MODEL  ?= Qwen/Qwen3-32B
INPUT_DIR  ?= ../downloads
JSONL      ?= books.jsonl
COLLECTION ?= books

.PHONY: deps ingest embed serve chroma-up chroma-down

deps:
	pip install -r requirements.txt

# --- M2 (orchestrátor) ---
ingest:
	python3 ingest_books.py --input-dir $(INPUT_DIR) --output $(JSONL)

ingest-txt:
	python3 ingest_books.py --input-dir $(INPUT_DIR) --output $(JSONL) --no-pdf

# --- JODA (NAS, Docker) ---
chroma-up:
	cd deploy/joda && docker compose up -d

chroma-down:
	cd deploy/joda && docker compose down

# --- SPARK (GPU) ---
embed:
	python3 embed_books.py --input $(JSONL) --chroma-url $(CHROMA_URL) \
		--collection $(COLLECTION)

serve:
	python3 server.py --chroma-url $(CHROMA_URL) --collection $(COLLECTION) \
		--llm-url $(LLM_URL) --llm-model $(LLM_MODEL) --no-think

# vLLM na SPARK (spustit zvlášť, běží dlouho):
vllm:
	vllm serve $(LLM_MODEL) --port 8000 --max-model-len 16384

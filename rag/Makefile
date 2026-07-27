# RAG chatbot — cíle pro jednotlivé stroje.
# LLM a Chroma zajišťuje AiStack (https://github.com/lioilsources/AiStack):
#   - JODA:  docker compose -f deploy/docker-compose.swarm.nas.yaml up -d  → Chroma :8006
#   - SPARK: make up-llm + make up-translate                               → LiteLLM :4000
# Přepiš proměnné podle prostředí, např.: make embed CHROMA_URL=http://192.168.88.88:8006

CHROMA_URL ?= http://192.168.88.88:8006
LLM_URL    ?= http://localhost:4000/v1
LLM_MODEL  ?= translate
EMBED_URL  ?=
INPUT_DIR  ?= ../downloads
JSONL      ?= books.jsonl
COLLECTION ?= books

EMBED_FLAGS = $(if $(EMBED_URL),--embed-url $(EMBED_URL),)

.PHONY: deps ingest ingest-txt embed serve

deps:
	pip install -r requirements.txt

# --- M2 (orchestrátor) ---
ingest:
	python3 ingest_books.py --input-dir $(INPUT_DIR) --output $(JSONL)

ingest-txt:
	python3 ingest_books.py --input-dir $(INPUT_DIR) --output $(JSONL) --no-pdf

# --- SPARK (GPU; předpokládá běžící AiStack) ---
# Mimo SPARK: make serve LLM_URL=https://llm.ol1n.com/v1
#   (+ export CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET)
embed:
	python3 embed_books.py --input $(JSONL) --chroma-url $(CHROMA_URL) \
		--collection $(COLLECTION) $(EMBED_FLAGS)

serve:
	python3 server.py --chroma-url $(CHROMA_URL) --collection $(COLLECTION) \
		--llm-url $(LLM_URL) --llm-model $(LLM_MODEL) $(EMBED_FLAGS)

# RAG chatbot — cíle pro jednotlivé stroje.
#
#   M2    ingest (registry + kapitoly + Perseus) → JSONL → rsync na SPARK; migrace PG; deploy
#   JODA  Postgres :5433 + Chroma knihovny :8007 (deploy/joda); AiStack Chroma :8006 drží legacy `books`
#   SPARK load do PG, embedding, obohacení (LLM), server
#
# Tajemství jsou v rag/.env (PG_DSN) a deploy/joda/.env (heslo) — mimo git.
# Přepiš proměnné podle prostředí, např.: make embed COLLECTION=books_v2

-include .env
export PG_DSN

CHROMA_URL  ?= http://192.168.88.88:8006
LLM_URL     ?= http://localhost:4000/v1
GATEWAY_URL ?= http://localhost:8080/v1
TRT_URL     ?= http://localhost:8004/v1
LLM_MODEL   ?= translate
EMBED_URL   ?=
INPUT_DIR   ?= ../downloads
JSONL       ?= books.jsonl
COLLECTION  ?= books
GLOSS_COLLECTION ?= books_gloss
REGISTRY    ?= registry
PY          ?= .venv/bin/python3
SPARK_DIR   ?= /home/ol1n/deploy/WorldLibraryProject
JODA_DIR    ?= ~/deploy/WorldLibraryProject/deploy/joda
MODE        ?= full
SERVER_URL  ?= http://localhost:8090
PRIORITY    ?= 1
NO_TRGM     ?=

EMBED_FLAGS = $(if $(EMBED_URL),--embed-url $(EMBED_URL),)

.PHONY: deps test ingest ingest-txt ingest-stats ingest-perseus sync-spark \
        pg-up pg-down pg-logs pg-status pg-migrate pg-index deploy-joda \
        load-pg embed embed-gloss chroma-drop \
        enrich-benchmark enrich-chunks enrich-chapters enrich-works \
        eval eval-catalog serve restart-chat deploy-spark

deps:
	$(PY) -m pip install -r requirements.txt

# selftesty čisté logiky + pytest (bez GPU, bez LLM; PG testy se přeskočí bez PG_DSN)
test:
	$(PY) retrieval.py
	$(PY) chapters.py
	$(PY) clean_text.py
	$(PY) hybrid.py
	$(PY) -m pytest -q tests

# eval plánovače a katalogu přes běžící server (PG režim)
eval-catalog:
	$(PY) eval/eval_catalog.py --url $(SERVER_URL)

# --- M2 (orchestrátor) ---
ingest:
	$(PY) ingest_books.py --input-dir $(INPUT_DIR) --output $(JSONL) --registry $(REGISTRY) --perseus

ingest-txt:
	$(PY) ingest_books.py --input-dir $(INPUT_DIR) --output $(JSONL) --registry $(REGISTRY) --no-pdf

# dry-run: per dílo kapitoly a chunky, délky — nic se nezapisuje
ingest-stats:
	$(PY) ingest_books.py --input-dir $(INPUT_DIR) --registry $(REGISTRY) --perseus --stats-only

ingest-perseus:
	$(PY) ingest_books.py --input-dir $(INPUT_DIR) --output $(JSONL) --registry $(REGISTRY) --perseus --groups greek_latin

sync-spark:
	rsync -avz --progress $(JSONL) works.jsonl chapters.jsonl spark:$(SPARK_DIR)/rag/
	rsync -a $(REGISTRY)/ spark:$(SPARK_DIR)/rag/$(REGISTRY)/

# --- JODA (Postgres + Chroma knihovny; deploy/joda) ---
deploy-joda:
	ssh joda "mkdir -p $(JODA_DIR)"
	rsync -a --exclude .env ../deploy/joda/ joda:$(JODA_DIR)/
	ssh joda "cd $(JODA_DIR) && cp -n .env.example .env; docker compose -f docker-compose.library.yaml --env-file .env up -d"

pg-up:
	ssh joda "cd $(JODA_DIR) && docker compose -f docker-compose.library.yaml --env-file .env up -d"

pg-down:
	ssh joda "cd $(JODA_DIR) && docker compose -f docker-compose.library.yaml --env-file .env down"

pg-logs:
	ssh joda "docker logs --tail 100 library_postgres"

pg-status:
	$(PY) pg_migrate.py --status
	ssh joda "docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}' library_postgres library_backup"

pg-migrate:
	$(PY) pg_migrate.py

# GIN indexy až po loadu; NO_TRGM=1 vynechá trigram index nad textem chunků (~0,5 GB)
pg-index:
ifeq ($(NO_TRGM),)
	$(PY) pg_migrate.py --only 0003 --force
else
	sed '/chunks_text_trgm_idx/d' sql/0003_indexes.sql > /tmp/0003_no_trgm.sql && \
	  $(PY) -c "import psycopg,os; c=psycopg.connect(os.environ['PG_DSN']); c.execute(open('/tmp/0003_no_trgm.sql').read()); c.commit()"
endif

# --- SPARK (GPU; předpokládá běžící AiStack) ---
load-pg:
	$(PY) load_pg.py --input $(JSONL) --works works.jsonl --chapters chapters.jsonl --registry $(REGISTRY) --replace-all

embed:
	$(PY) embed_books.py --input $(JSONL) --chroma-url $(CHROMA_URL) \
		--collection $(COLLECTION) --mode passages $(EMBED_FLAGS)

embed-gloss:
	$(PY) embed_books.py --source pg --chroma-url $(CHROMA_URL) \
		--collection $(GLOSS_COLLECTION) $(EMBED_FLAGS)

chroma-drop:
	$(PY) -c "import chromadb,urllib.parse as u; p=u.urlparse('$(CHROMA_URL)'); c=chromadb.HttpClient(host=p.hostname,port=p.port); c.delete_collection('$(COLLECTION)'); print('smazána', '$(COLLECTION)', 'na', '$(CHROMA_URL)')"

# obohacení jde přímo na TRT-LLM (:8004), ne přes gateway — LiteLLM by při pádu
# translate tiše přepnul na Qwen3-4B a otrávil data
enrich-benchmark:
	$(PY) enrich_chunks.py --llm-url $(TRT_URL) --benchmark 200

enrich-chunks:
	mkdir -p logs && nohup $(PY) enrich_chunks.py --llm-url $(TRT_URL) --priority $(PRIORITY) > logs/enrich_chunks.log 2>&1 &
	@echo "běží na pozadí: tail -f logs/enrich_chunks.log"

enrich-chapters:
	mkdir -p logs && nohup $(PY) enrich_chapters.py --llm-url $(GATEWAY_URL) --model swarm-director --priority $(PRIORITY) > logs/enrich_chapters.log 2>&1 &
	@echo "běží na pozadí: tail -f logs/enrich_chapters.log"

enrich-works:
	mkdir -p logs && nohup $(PY) enrich_works.py --llm-url $(GATEWAY_URL) --model swarm-director > logs/enrich_works.log 2>&1 &
	@echo "běží na pozadí: tail -f logs/enrich_works.log"

eval:
	$(PY) eval/eval_retrieval.py --retrieve-mode $(MODE) --collection $(COLLECTION) --compare eval/results/baseline.json

# Mimo SPARK: make serve LLM_URL=https://llm.ol1n.com/v1
#   (+ export CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET)
serve:
	$(PY) server.py --chroma-url $(CHROMA_URL) --collection $(COLLECTION) \
		--llm-url $(LLM_URL) --llm-model $(LLM_MODEL) $(EMBED_FLAGS)

# systemd unit library-chat má Restart=on-failure — kill stačí, sudo netřeba
restart-chat:
	ssh spark "kill \$$(pgrep -f 'server.py' | head -1) && sleep 8 && curl -s localhost:8090/status"

deploy-spark:
	ssh spark "cd $(SPARK_DIR) && git pull --ff-only && rag/.venv/bin/pip install -q -r rag/requirements.txt"
	$(MAKE) restart-chat

# RAG chatbot — cíle pro jednotlivé stroje.
#
#   M2    ingest (registry + kapitoly + Perseus) → JSONL → rsync na SPARK; migrace PG; deploy
#   JODA  Postgres :5433 (deploy/joda)
#   SPARK load do PG, embedding, obohacení (LLM), server, Chroma knihovny :8007
#         (deploy/spark; přesunuto z JODY 2026-09-12, viz PLAN-spark-library-storage.md)
#
# Tajemství jsou v rag/.env (PG_DSN) a deploy/joda/.env (heslo) — mimo git.
# Přepiš proměnné podle prostředí, např.: make embed COLLECTION=books_v2

-include .env
export PG_DSN

CHROMA_URL  ?= http://127.0.0.1:8007
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
BUNDLE_OUT  ?= build/bundles
BUNDLE_ARGS ?= --all --priority $(PRIORITY)

EMBED_FLAGS = $(if $(EMBED_URL),--embed-url $(EMBED_URL),)

.PHONY: deps test ingest ingest-txt ingest-stats ingest-perseus sync-spark \
        pg-up pg-down pg-logs pg-status pg-migrate pg-index deploy-joda \
        load-pg embed embed-gloss chroma-drop \
        enrich-benchmark enrich-chunks enrich-chapters enrich-works \
        eval eval-catalog eval-law eval-agent agent-uklid validate-templates update-template-snapshots render-doc \
        bundles kindlify-sync serve restart-chat install-unit mode deploy-spark

deps:
	$(PY) -m pip install -r requirements.txt

# selftesty čisté logiky + pytest (bez GPU, bez LLM; PG testy se přeskočí bez PG_DSN)
test:
	$(PY) retrieval.py
	$(PY) chapters.py
	$(PY) clean_text.py
	$(PY) hybrid.py
	$(PY) law_terms.py
	$(PY) cite.py
	$(PY) -m docgen.templates
	$(PY) -m agent.review
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

# --- Právník (zákony ČR z e-Sbírky; Ol1nLLM/docs/plan-pravnik.md) ---
# Tentýž řetěz jako knihy, jen jiný vstup, jiná PG databáze a jiná kolekce:
#   ingest-law → sync-law → (SPARK) load-pg-law → pg-index-law → embed-law → law-chat
# LAW_PG_DSN je v rag/.env (databáze `law` na tomtéž Postgresu :5433).
LAW_DIR        ?= law
LAW_COLLECTION ?= law_v1
LAW_TIER       ?= 1
LAW_TOP_K      ?= 8
export LAW_PG_DSN

ingest-law:
	$(PY) ingest_law.py --tier $(LAW_TIER) --out-dir $(LAW_DIR)

ingest-law-stats:
	$(PY) ingest_law.py --tier $(LAW_TIER) --stats-only

sync-law:
	rsync -avz --progress $(LAW_DIR)/works.jsonl $(LAW_DIR)/chapters.jsonl $(LAW_DIR)/books.jsonl spark:$(SPARK_DIR)/rag/$(LAW_DIR)/
	rsync -a registry/law/ spark:$(SPARK_DIR)/rag/registry/law/

pg-migrate-law:
	PG_DSN=$(LAW_PG_DSN) $(PY) pg_migrate.py

load-pg-law:
	$(PY) load_pg.py --dsn $(LAW_PG_DSN) --input $(LAW_DIR)/books.jsonl --works $(LAW_DIR)/works.jsonl \
		--chapters $(LAW_DIR)/chapters.jsonl --registry registry/law --summaries /dev/null --replace-all

pg-index-law:
	PG_DSN=$(LAW_PG_DSN) $(PY) pg_migrate.py --only 0003 --force

embed-law:
	$(PY) embed_books.py --input $(LAW_DIR)/books.jsonl --works $(LAW_DIR)/works.jsonl \
		--chroma-url $(CHROMA_URL) --collection $(LAW_COLLECTION) --mode passages $(EMBED_FLAGS)

# Lokálně (M2, proti Chromě a PG v LAN): make serve-law LLM_URL=https://llm.ol1n.com/v1
serve-law:
	$(PY) server.py --port 8098 --prompt-file prompts/pravnik_cs.md \
		--chroma-url $(CHROMA_URL) --collection $(LAW_COLLECTION) --gloss-collection law_gloss_none \
		--channels vec --pg-dsn $(LAW_PG_DSN) --cite-registry registry/law/tier1.yaml \
		--planner off --rewrite off --law-terms registry/law/legal_terms.yaml \
		--sibling-window 1 --sibling-top 3 \
		--max-per-work 6 --no-translate-excerpts --summaries-file $(LAW_DIR)/summaries.json \
		--llm-url $(LLM_URL) --llm-model $(LLM_MODEL) $(EMBED_FLAGS)

restart-law-chat:
	ssh spark "systemctl --user restart law-chat && sleep 20 && curl -s localhost:8098/status"

# Eval Právníka proti běžící službě (produkční konfigurace, bez druhého embedderu
# v paměti SPARKu — jde i z M2 přes LAN). Přímý režim s vlastním embedderem:
#   make eval-law EVAL_LAW_ARGS="--mode vec"   (na SPARKu, když je volná paměť)
LAW_URL       ?= http://192.168.88.66:8098
EVAL_LAW_ARGS ?= --service $(LAW_URL)
eval-law:
	$(PY) eval/eval_law.py $(EVAL_LAW_ARGS) --top-k $(LAW_TOP_K)

# --- Šablony smluv (docgen; data v ../data/templates) -----------------------------
# Validace = schéma + proměnné + výrazy + existence § v právním indexu + snapshoty
# renderů. Kontrola § potřebuje LAW_PG_DSN; bez něj se přeskočí a řekne to.
validate-templates:
	$(PY) -m docgen.validate

update-template-snapshots:
	$(PY) -m docgen.validate --update-snapshots

# make render-doc TYP=najemni_smlouva_byt VSTUP=docgen/fixtures/najemni_smlouva_byt__neurcita_minimum.json
TYP   ?= najemni_smlouva_byt
VSTUP ?=
render-doc:
	$(PY) -m docgen.render $(TYP) $(if $(VSTUP),--vstup $(VSTUP),--otazky)

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

# Bundly pro Kindlify (čtečka) — jen přerovnání Postgresu, žádný LLM.
# Jedno dílo: make bundles BUNDLE_ARGS="--work zh.daodejing"
# Rovnou do assetů appky: make bundles BUNDLE_OUT=../../Kindlify/assets/bundles
bundles:
	$(PY) export_bundle.py $(BUNDLE_ARGS) --out $(BUNDLE_OUT)

# Průběžné plnění Kindlify: hotová díla (obohacené chunky + souhrny kapitol)
# → assets/bundles + index.json, přepíše jen změněné. Nanečisto bez SYNC_ARGS.
#   make kindlify-sync SYNC_ARGS="--write --commit"
KINDLIFY_BUNDLES ?= ../../Kindlify/assets/bundles
kindlify-sync:
	$(PY) kindlify_sync.py --out $(KINDLIFY_BUNDLES) --priority $(PRIORITY) $(SYNC_ARGS)

eval:
	$(PY) eval/eval_retrieval.py --retrieve-mode $(MODE) --collection $(COLLECTION) --compare eval/results/baseline.json

# Mimo SPARK: make serve LLM_URL=https://llm.ol1n.com/v1
#   (+ export CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET)
serve:
	$(PY) server.py --chroma-url $(CHROMA_URL) --collection $(COLLECTION) \
		--llm-url $(LLM_URL) --llm-model $(LLM_MODEL) $(EMBED_FLAGS)

# Uživatelská systemd služba (deploy/spark/library-chat.service) — bez sudo
# a s Restart=always, takže SIGTERM už produkci nenechá dole jako dřív.
restart-chat:
	ssh spark "systemctl --user restart library-chat && sleep 15 && curl -s localhost:8090/status"

# Instalace/aktualizace uživatelských unitů (poprvé i po změně souboru).
# library-chat  chat knihovny, běží pořád
# comfyui       Image Studio, přes den
# library-enrich  obohacení korpusu, v noci
# rag-schedule.timer  přepínač 08:00 / 22:00
install-unit:
	scp ../deploy/spark/*.service ../deploy/spark/*.timer spark:.config/systemd/user/
	ssh spark "systemctl --user daemon-reload && systemctl --user enable --now library-chat rag-schedule.timer"
	@echo
	@echo "jednorázově se sudem, ať po rebootu nekoliduje systémový unit:"
	@echo "  ssh spark 'sudo systemctl disable --now library-chat'"

# Ruční přepnutí režimu mimo rozvrh (MODE=day|night|auto)
mode:
	ssh spark "deploy/WorldLibraryProject/deploy/spark/rag-schedule.sh $(or $(MODE),auto)"

deploy-spark:
	ssh spark "cd $(SPARK_DIR) && git pull --ff-only && rag/.venv/bin/pip install -q -r rag/requirements.txt"
	$(MAKE) restart-chat

# --- Právník jako agent (nástroje, intake, revize; docs/lawyer/AGENT.md) ----------
# Eval jede bez LLM: draft scénáře přes skriptovaného uživatele, review na
# podstrčených vadách. Latenci a tool calling lze měřit teprve v okně s modelem.
eval-agent:
	$(PY) eval/lawyer_agent/run.py $(EVAL_AGENT_ARGS)

agent-uklid:
	$(PY) -c "import os,sys; sys.path.insert(0,'.'); \
	from psycopg_pool import ConnectionPool; from agent.session import Sessions; \
	from docgen.validate import load_dotenv; load_dotenv(); \
	p=ConnectionPool(os.environ['LAW_PG_DSN'], min_size=1, max_size=2, open=True); \
	print('smazáno expirovaných sessionů:', Sessions(p).uklid_expirovane())"

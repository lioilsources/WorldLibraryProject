# PLAN — nasazení knihovního RAG chatbota na SPARK

Plán pro Claude Code běžící na DGX Spark (`/home/ol1n/deploy/AiStack/`).
Cíl: zprovoznit chatbot nad korpusem WorldLibraryProject, dostupný na
https://chat.ol1n.com, s LLM z AiStack parku a vektory na JODA.

## Kontext — co už existuje

| Repo | Větev | Obsah |
|---|---|---|
| WorldLibraryProject | `claude/personalized-chatbot-edurag-5x5hoz` | `rag/`: ingest → JSONL → embed → chatbot (`server.py`, port **8090**) |
| AiStack | `claude/chat-tunnel-ingress` | `cloudflared/config.yml`: ingress `chat.ol1n.com` → `host.docker.internal:8090` |

Architektura: M2 dělá ingest (`books.jsonl`), JODA (192.168.88.88) je
samostatný Ubuntu server s Dockerem — žádný síťový mount — a drží jen
ChromaDB :8006 (AiStack `deploy/docker-compose.swarm.nas.yaml`), SPARK
embeduje a servíruje chatbot. Data mezi stroji tečou přes ssh/rsync. LLM: LiteLLM role **`translate`**
(Qwen3-32B-AWQ, ~20 GB); eskalace `swarm-director` (on-demand, +60 GB).

Pravidla AiStacku platí (viz `SKILL.md`): pracovat z
`/home/ol1n/deploy/AiStack/`, před nasazením zkontrolovat paměť
(`make ps`, `nvidia-smi`, `free -g`), žádný `--kv-cache-dtype fp8`.

---

## Fáze 1 — AiStack: merge tunelové větve

```bash
cd /home/ol1n/deploy/AiStack
git fetch origin claude/chat-tunnel-ingress
git checkout main && git merge origin/claude/chat-tunnel-ingress
git push origin main   # nebo PR, podle preference
```

Ověřit: `git log -1 cloudflared/config.yml` obsahuje pravidlo `chat.ol1n.com`.

## Fáze 2 — Cloudflare: DNS + Access

```bash
# DNS route (jednorázově; credentials už jsou v cloudflared/)
docker run --rm -v $PWD/cloudflared:/etc/cloudflared cloudflare/cloudflared:latest \
  tunnel --config /etc/cloudflared/config.yml route dns \
  f3cb3ac1-d9fa-4c78-9d87-ff3cab6c7051 chat.ol1n.com
```

**[ČLOVĚK]** Zero Trust dashboard → vytvořit Access App pro
`chat.ol1n.com` (self-hosted, service token policy — stejný token jako
llm.ol1n.com, nebo nový). Zkopírovat **AUD tag**.

Pak v `cloudflared/config.yml` odkomentovat `access` blok u
`chat.ol1n.com`, doplnit AUD tag, commitnout a:

```bash
docker compose -f deploy/docker-compose.yml restart cloudflared-1 cloudflared-2
```

⚠️ Dokud Access blok není aktivní, je chatbot na internetu bez ochrany —
pořadí dodržet, případně restart cloudflared odložit až za tento krok.

## Fáze 3 — závislosti chatbota

```bash
# 3a. Chroma na JODA běží?
curl -sf http://192.168.88.88:8006/api/v2/heartbeat
# Pokud spadne — JODA je ubuntu server s Dockerem; při nastaveném ssh klíči jde i ze SPARKu:
#   scp deploy/docker-compose.swarm.nas.yaml joda:~/chromadb/ && \
#   ssh joda 'cd ~/chromadb && docker compose -f docker-compose.swarm.nas.yaml up -d'
# [ČLOVĚK, pokud ssh na JODA není] spustit totéž na JODA ručně

# 3b. translate běží? (LiteLLM přes gateway)
curl -sf http://localhost:8080/v1/models | grep -q translate || make up-translate
# paměťová kontrola PŘED startem: free -g, nvidia-smi (translate ~20 GB)

# 3c. WorldLibraryProject na SPARK
cd /home/ol1n/deploy
git clone -b claude/personalized-chatbot-edurag-5x5hoz \
  https://github.com/lioilsources/WorldLibraryProject.git
cd WorldLibraryProject/rag
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # torch/CUDA: na DGX OS použít systémový torch, viz pozn. níže
```

Pozn. torch: `sentence-transformers` potáhne torch z PyPI — na aarch64
DGX OS ověřit, že jde o CUDA build (`python3 -c "import torch; print(torch.cuda.is_available())"`).
Pokud ne, použít NGC PyTorch kontejner nebo pip index NVIDIA.

## Fáze 4 — data + embedding

`books.jsonl` vzniká na M2 a na SPARK se pošle přes rsync (stejná cesta
jako pro `dataset.jsonl` v kořenovém README; přes LAN nebo Tailscale):

```bash
# [ČLOVĚK/M2] na M2:
cd WorldLibraryProject/rag && make ingest-txt
rsync -avz --progress books.jsonl spark:/home/ol1n/deploy/WorldLibraryProject/rag/

# pak na SPARKu:
cd /home/ol1n/deploy/WorldLibraryProject/rag
make embed   # JSONL default ./books.jsonl, CHROMA_URL default http://192.168.88.88:8006
# ověřit: výpis "Kolekce má N dokumentů", N > 0
```

Idempotentní — při přerušení spustit znovu. Embedding model
(`multilingual-e5-large`) se stahuje z HF při prvním běhu (~2,2 GB).

## Fáze 5 — chatbot server

```bash
cd /home/ol1n/deploy/WorldLibraryProject/rag
source .venv/bin/activate
make serve   # :8090, LLM localhost:4000/v1, model translate
```

Pro trvalý běh vytvořit systemd unit (doporučeno, ať přežije reboot):

```ini
# /etc/systemd/system/library-chat.service
[Unit]
Description=Library RAG chatbot
After=docker.service network-online.target
[Service]
User=ol1n
WorkingDirectory=/home/ol1n/deploy/WorldLibraryProject/rag
ExecStart=/home/ol1n/deploy/WorldLibraryProject/rag/.venv/bin/python3 server.py
Restart=on-failure
[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now library-chat
```

## Fáze 6 — verifikace end-to-end

```bash
# lokálně
curl -s localhost:8090/status | python3 -m json.tool          # documents > 0
curl -s localhost:8090/chat -H 'Content-Type: application/json' \
  -d '{"message":"Co říká Tao te ťing o nečinnosti?"}' | python3 -m json.tool
# → answer česky, sources z group "chinese", session_id přítomné

# multi-turn: druhý dotaz se session_id z první odpovědi
# → odpověď navazuje na kontext

# přes tunel (se service tokenem)
curl -s https://chat.ol1n.com/health \
  -H "CF-Access-Client-Id: <id>" -H "CF-Access-Client-Secret: <secret>"

# eskalace na director (jen pokud běží: make up-swarm-director; +60 GB!)
curl -s localhost:8090/chat -H 'Content-Type: application/json' \
  -d '{"message":"Srovnej wu-wej s Kantovou autonomií vůle.","model":"swarm-director"}'
```

## Rollback

- chatbot: `sudo systemctl stop library-chat` (nebo Ctrl-C u make serve)
- tunel: revert commitu v `cloudflared/config.yml` + restart cloudflared
- Chroma: kolekce `books` je oddělená — `embed_books.py --reset` ji smaže,
  SwarmBattle data nedotčena

## Známé pasti

1. Port **8090**, ne 8080 (tam sedí AiStack Go gateway).
2. Embedding model indexu a dotazů musí být týž — `--embed-model` neměnit
   po `make embed` (jinak reindex s `--reset`).
3. `translate` má thinking off v LiteLLM configu; `--no-think` je potřeba
   jen při obcházení LiteLLM (přímé vLLM).
4. TXT v `downloads/` jsou Git LFS pointery — ingest na M2 vyžaduje
   `git lfs pull` (skript to sám ohlásí).
5. PDF skeny bez OCR vrstvy ingest přeskakuje a vypíše — to je očekávané,
   OCR je samostatná budoucí fáze.

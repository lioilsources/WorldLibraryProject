# HOWTO — M2: orchestrace knihovního chatbota

M2 je dirigent celé pipeline: stahuje korpus, dělá ingest a rozváží data.
Tenhle návod je pro ruční provoz z terminálu na M2.

## Celkový tok — kdy co spustit

```
1. JODA   PLAN-joda-chroma.md      (jednou; ChromaDB :8006)
2. M2     tento návod, kroky 1–3   (korpus → books.jsonl → rsync na SPARK)
3. SPARK  PLAN-spark-chatbot.md    (Claude Code; embed + chatbot + tunel)
```

## Prerekvizity (jednou)

```bash
brew install aria2 go git-lfs
git lfs install

git clone -b claude/personalized-chatbot-edurag-5x5hoz \
  https://github.com/lioilsources/WorldLibraryProject.git
cd WorldLibraryProject

# python prostředí pro ingest
python3 -m venv rag/.venv && source rag/.venv/bin/activate
pip install pymupdf
```

Ssh alias `spark` v `~/.ssh/config` (pokud ještě není):

```
Host spark
  HostName <IP-SPARKu-v-LAN>   # nebo Tailscale jméno
  User ol1n
```

## Krok 1 — korpus

```bash
git lfs pull          # stáhne ~2,8 GB skutečného obsahu místo pointerů
# NEBO čerstvé stažení ze zdrojů (přes noc):
./run_pipeline.sh
```

Kontrola, že soubory nejsou pointery:

```bash
head -c 60 downloads/chinese/laozi/dao_de_jing.txt
# špatně: "version https://git-lfs.github.com/spec/v1"
# dobře:  text knihy
```

## Krok 2 — ingest

```bash
cd rag && source .venv/bin/activate
make ingest-txt       # jen TXT — rychlý start, čistá data (~28 knih)
# make ingest         # + PDF s textovou vrstvou (skeny bez OCR přeskočí a vypíše)
```

Výstup: `rag/books.jsonl` + souhrn (počet chunků, přeskočené LFS
pointery, PDF čekající na OCR).

## Krok 3 — přenos na SPARK

```bash
rsync -avz --progress books.jsonl spark:/home/ol1n/deploy/WorldLibraryProject/rag/
```

## Krok 4 — zbytek řídí SPARK

```bash
ssh spark
# tam: Claude Code + PLAN-spark-chatbot.md (embed, chatbot, tunel)
# nebo ručně: cd ~/deploy/WorldLibraryProject/rag && make embed && make serve
```

## Aktualizace korpusu (opakovaný cyklus)

Po přidání nových knih (nové URL v `urls.txt`):

```bash
./run_pipeline.sh                 # stáhne jen nové soubory (resume-friendly)
cd rag && make ingest             # přegeneruje books.jsonl
rsync -avz books.jsonl spark:/home/ol1n/deploy/WorldLibraryProject/rag/
ssh spark 'cd ~/deploy/WorldLibraryProject/rag && source .venv/bin/activate && make embed'
```

`make embed` je idempotentní — vloží jen nové chunky, běžící chatbot
nové knihy vidí okamžitě (čte z Chromy per dotaz, restart netřeba).

## Volitelné — chatbot přímo na M2

Server může běžet i tady (LLM přes tunel, Chroma po LAN):

```bash
cd rag && pip install -r requirements.txt
export CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=...
make serve LLM_URL=https://llm.ol1n.com/v1
```

## Diagnostika

```bash
curl -sf http://192.168.88.88:8006/api/v2/heartbeat        # JODA Chroma
curl -sf https://llm.ol1n.com/health \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET"   # SPARK gateway
ssh spark 'curl -s localhost:8090/status'                  # chatbot + počet dokumentů
```

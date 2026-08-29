# RAG chatbot nad světovou knihovnou

Personalizovaný chatbot pro povídání o knihách z `downloads/`. Návrh
vychází z [EduRAG](https://github.com/lioilsources/EduRAG) (sdílí jeho
JSONL kontrakt a embedding model), LLM a vektorovou DB zajišťuje
[AiStack](https://github.com/lioilsources/AiStack).

## Architektura

```
M2 (orchestrátor)         JODA (Ubuntu + Docker)         SPARK (AiStack)
─────────────────         ──────────────────────         ───────────────
run_pipeline.sh           ChromaDB :8006                 LiteLLM gateway :4000
ingest_books.py           (AiStack swarm.nas)      ◀──   ├─ translate (Qwen3-32B-AWQ)
  → books.jsonl ──rsync──────────────────────────▶      ├─ swarm-director (on-demand)
                                                         └─ ...
                                                         embed_books.py ──▶ Chroma (JODA)
                                                         server.py :8090 ◀── klient
```

- **M2**: stáhne korpus, vytáhne text z TXT/PDF, rozseká na chunky a
  `books.jsonl` pošle rsyncem na SPARK.
- **JODA**: samostatný Ubuntu server s Dockerem (žádné sdílené disky) —
  jediná role je Chroma v server režimu, nasazuje AiStack
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
# na JODA (ubuntu server), z AiStack repa:
docker compose -f deploy/docker-compose.swarm.nas.yaml up -d   # :8006
```

### 2. M2 — ingest

```bash
cd rag && pip install pymupdf
make ingest            # TXT + PDF → books.jsonl
make ingest-txt        # jen TXT (rychlý start, čistá data)
rsync -avz --progress books.jsonl spark:/home/ol1n/deploy/WorldLibraryProject/rag/
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

### 5. Povídání — webové UI (mobil i desktop)

Otevři v prohlížeči **`http://<IP-SPARKu>:8090/`**. Na telefonu v domácí
síti to funguje hned, bez tunelu a bez appky — stránku jde přidat na
plochu jako webovou aplikaci.

UI umí: streamované odpovědi (token po tokenu), citace `[1]` provázané
se sbalitelnou sekcí zdrojů, seznam děl (tlačítko „Díla" = `GET /works`),
přepnutí modelu (`rychlejší` = translate, `hluboký rozbor` =
swarm-director) a počtu úryvků, historii konverzace přes `session_id`
v `localStorage` a tlačítko „Nová" pro reset. Zdroj je
`static/chat.html` — čte se při každém requestu, takže úpravy vzhledu
nevyžadují restart serveru.

Pozn.: generování trvá jednotky minut (viz pasti v
`PLAN-ol1nllm-integration.md`), proto UI ukazuje běžící stopky. Méně
úryvků = rychlejší odpověď.

### 5b. Povídání přes API

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

## Kvalita vyhledávání

Dotaz je česky, korpus v pálí, sanskrtu, čínštině a hebrejštině — a
multilingual-e5 v téhle situaci skoro nerozlišuje: **top-5 se u dotazu
liší o ~0,007 vzdálenosti**, takže o pořadí rozhoduje spíš podíl tradice
v korpusu než téma otázky. A pálijská Tipitaka je 60 z 92 děl, takže
„Co říká Tao te ťing o wu-wej?" vracelo pět pálijských svazků.

Proto `retrieval.py`:

- **Směrování na dílo** — když otázka dílo jmenuje, hledá se rovnou v něm
  (`where={"work": {"$in": [...]}}`). Aliasy jsou **kmeny bez diakritiky**
  („therigath" trefí „v Therígáthě" i „Therígáthá"), hledají se od hranice
  slova. Když sedí víc aliasů, platí jejich **průnik**, je-li neprázdný:
  „Bhagavadgíta z Mahábháraty" → Bhíšmaparva, kdežto „Tao te ťing
  a Zhuangzi" → obě díla.
- **Směrování na tradici** — otázka bez názvu díla („co učí hinduistické
  texty") aspoň zúží na skupinu. Tady se naopak **sjednocuje**, aby
  „srovnej buddhismus a hinduismus" sáhlo do obou.
- **Diverzita** — hledá se `top_k × --candidate-factor` kandidátů a z nich
  se bere top_k s nejvýš `--max-per-work` úryvky z jednoho díla, aby
  kontext nebyl pětkrát tentýž Šántiparva.

Vypnout: `--no-routing`, `--max-per-work 99`.

### Eval

`eval/eval_retrieval.py` měří retrieval **bez LLM** (běží v sekundách)
proti zlatému standardu `eval/golden.jsonl` (otázky se známým dílem
a tradicí):

```bash
.venv/bin/python3 eval/eval_retrieval.py --retrieve-mode full \
    --compare eval/results/baseline.json
```

Režimy `plain | route | diverse | full` izolují jednotlivé zásahy, takže
je vidět, co doopravdy pomohlo. Naměřeno (23 otázek, top-5, srpen 2026):

| režim | work-hit@5 | group-hit@5 | různých děl v top-5 |
|---|---|---|---|
| plain (stav před) | 0,61 | 0,67 | 2,33 |
| route | 1,00 | 0,96 | 1,50 |
| diverse | 0,61 | 0,67 | 2,79 |
| **full** | **1,00** | **1,00** | 1,62 |

Druhá sada `eval/golden_v2.jsonl` je psaná až po aliasech a jinými slovy
(skloňování, jiné varianty názvů) — kontrola, že tabulka není ušitá na
míru první sadě: 0,40 → 1,00 work-hit, 0,64 → 1,00 group-hit.

**Kandidáti se před výběrem čistí** (`looks_tabular`): rejstříky, obsahy
a konkordance vypadnou, protože jako citace neříkají nic a překladač z nich
dělá nesmysl. U dotazu na egyptské texty jich bylo 16 z 20 kandidátů.
Když by po filtru nezbylo nic, tabulka se vrátí — prázdný kontext je horší.

Pozor: **hit@5 měří jen to, že se trefí správná kniha**, ne že je
odpověď dobrá. Rozptyl vzdáleností (`mean_spread`) zůstává mizivý —
model pořád nerozlišuje, jen se ho na to teď tolik neptáme.

## Personalizace

- **Osobnost**: `prompts/librarian_cs.md`, přepínatelná přes
  `--prompt-file`. Výchozí je český „sečtělý knihovník" citující zdroje.
- **Katalog v promptu**: seznam děl se skládá ze `summaries.json` a jde
  do **každého** dotazu, takže se každý znak platí prefillem — anotace
  se krátí na první větu a u velkých skupin (Tipitaka, parvy) se vypouští
  úplně. Plné anotace zůstávají v `GET /works`.
- **České názvy**: `summaries.json` má u díla `name_cs` (kurátorské, ručně
  psané — `gen_summaries.py` je při `--force` nepřepíše). Používají se
  v katalogu i v hlavičkách úryvků, takže model cituje „Otázky krále
  Milindy", ne „Milindapañhapāḷi". Pálijské názvy začínají nikájí, takže
  se seznam sám seskupí po sbírkách.
- **Překlad úryvků**: ke každé odpovědi se `sources[].excerpt` překládá do
  češtiny (`excerpt_cs`) — pálijský nebo čínský doklad je jinak pro
  čtenáře nečitelný. Běží ve vlákně souběžně s generováním odpovědi
  (`--excerpt-model`, výchozí `translate`), takže odpověď nezdrží;
  vypíná `--no-translate-excerpts`. Když model úryvek jen opíše místo
  překladu (u pálijských veršů běžné, ověřeno i na angličtině), pozná to
  `is_echo()` a pole se neposílá — v appce je pak jen originál.

**Korpus jsou originály, ne překlady** — na tom projekt stojí, jde právě
o to, jak si model poradí s cizojazyčným úryvkem. Nepoužitelné dílo se
proto opravuje lepším zdrojem v původním jazyce, nebo vyhazuje; české
překlady do knihovny nepatří. Překládá se až výstup (odpověď a `excerpt_cs`).
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

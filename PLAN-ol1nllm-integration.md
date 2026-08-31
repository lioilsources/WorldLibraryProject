# PLAN — integrace knihovního chatbota do Ol1nLLM + katalog a kapitoly

Plán rozšíření RAG chatbota (`rag/server.py` na SPARK :8090) o schopnosti,
které dnes nemá, a napojení na mobilní appku **Ol1nLLM** (Flutter,
`/Volumes/YOTTA/Dev/Ol1nLLM`). Navazuje na PLAN-spark-chatbot.md
(nasazeno 31. 7. 2026, kolekce `books` = 14 497 chunků z 29 knih).

---

## Co chatbot umí dnes — poctivá inventura

Odpovědi na tři klíčové otázky (stav k dnešku, `rag/server.py`):

### 1. „Zná všechny knihy z knihovny?"

**Částečně.** Všech 29 knih je zaindexováno a *dosažitelných* — ale na
každý dotaz se k modelu dostane jen **top-5 chunků** (~2 000 znaků každý)
podle vektorové podobnosti (`server.py: retrieve()`, `top_k=5`). Chatbot
tedy umí odpovědět z kterékoli knihy, pokud je k dotazu relevantní,
ale nemá „přehled o celku" — syntéza napříč celou knihovnou („srovnej
pojetí ctnosti ve všech tradicích") je omezená tím, co se vejde do 5
úryvků.

### 2. „Ví, jaké knihy zná?"

**Ne — a dnes na to odpoví špatně.** Otázka „jaké knihy znáš?" se
embedne jako každá jiná, vytáhne 5 víceméně náhodných chunků a model
z nich odpověď *odhadne* (= halucinace nebo neúplný výčet). V systémovém
promptu (`prompts/librarian_cs.md`) žádný katalog není a server nemá
endpoint na seznam děl. → **Fáze 1** to řeší za pár desítek řádků.

### 3. „Umí listovat kapitolami?"

**Ne.** Chunky jsou fixní okna textu (`chunking.py`), metadata nesou jen
`work/path/chunk_index/chunk_count` — kapitoly se při ingestu
neextrahují, přestože zdroje strukturu mají (Gutenberg čínské edice mají
nadpisy typu 學而第一, Mahábhárata má parvy a sekce). *Sekvenční*
listování po chuncích je implementovatelné hned (Chroma `get` s
`where={"work": ..., "chunk_index": ...}`); skutečné kapitoly potřebují
chapter-aware ingest → **Fáze 4**.

---

## Cílová architektura

```
Ol1nLLM (Flutter, iOS/Android)
  │  CF Access service token (--dart-define, stejný vzor jako llm.ol1n.com)
  ▼
https://chat.ol1n.com  (cloudflared tunel → SPARK :8090)
  │
  ├─ POST /chat          RAG odpověď + sources + session_id   (existuje)
  ├─ POST /chat/stream   SSE streaming varianta               (fáze 1)
  ├─ GET  /works         katalog knihovny                     (fáze 1)
  ├─ GET  /works/{work}/chunks?from=&n=   listování textem    (fáze 3)
  └─ GET  /works/{work}/chapters          kapitoly            (fáze 4)
```

---

## Fáze 1 — server: katalog + streaming (SPARK)

**1a. Katalog děl.** Při startu serveru jednorázově projít metadata
kolekce (stránkovaný `collection.get(include=["metadatas"])`) a postavit
`self.catalog: dict[work, {group, lang, path, chunk_count}]` (29 položek).

- Nový endpoint `GET /works` → JSON katalog.
- Katalog (jen `work` + `group`) přibalit do systémového promptu:
  „Knihovna obsahuje tato díla: …" — otázka „jaké knihy znáš?" pak má
  oporu v kontextu, ne v halucinaci.
- Pozor: katalog je ~30 řádků, prompt to nafoukne zanedbatelně.

**1b. SSE streaming `POST /chat/stream`.** Ol1nLLM UX stojí na
token-po-tokenu streamu (`VllmService.chat()` čte SSE deltas). Přidat
variantu, která streamuje `completion` z LiteLLM (OpenAI SDK
`stream=True`) a na konci pošle event `sources` + `session_id`.
Non-streaming `/chat` zůstává (curl, testy, jiní klienti).

**Verifikace:** `curl /works` vrací 29 děl; „Jaké knihy znáš?" vyjmenuje
skupiny a stěžejní díla bez vymyšlených titulů; `/chat/stream` streamuje.

## Fáze 2 — expozice chat.ol1n.com (blokováno na [ČLOVĚK])

Už rozpracováno (PLAN-spark-chatbot.md fáze 2): Access App v Zero Trust
dashboardu → AUD tag → odkomentovat `access` blok v
`AiStack/cloudflared/config.yml` → restart cloudflared. Appka použije
service token přes `--dart-define` (stejný mechanismus jako pro
llm.ol1n.com — může být tentýž token, pokud Access App sdílí policy).

## Fáze 3 — Ol1nLLM: režim „Knihovna" (Flutter)

**3a. `LibraryChatService`** (`lib/services/library_chat_service.dart`)
podle vzoru `VllmService`:

- `_baseUrl = 'https://chat.ol1n.com'`, CF hlavičky z `--dart-define`
  (`String.fromEnvironment` — stejné konstanty, žádná nová konfigurace).
- `chat(message, {sessionId, model})` → POST `/chat` (fáze 1b pak
  `/chat/stream` se `ChatDelta`/`ChatDone` eventy, které už appka zná).
- Odpověď nese `sources` — nový typ `LibrarySource(work, group, lang,
  distance, excerpt)`.

**3b. UI vstup.** Nejmenší zásah: **persona „Knihovník" 📚** (persona
registr už existuje — `models/persona.dart`), která místo `VllmService`
routuje na `LibraryChatService`. `Conversation.personaId` se už
persistuje, takže mapování `conversation.id ↔ session_id` chatbota stačí
držet v `Conversation` (nové pole `remoteSessionId`).

**3c. Zdroje v UI.** Pod odpovědí sbalitelná sekce „📖 Zdroje" — work,
tradice (group), vzdálenost; tap → celý excerpt. Bez toho je RAG
netransparentní.

**3d. Přepínač modelu.** `/chat` bere `model` per request — v UI volba
„hluboký rozbor" → `swarm-director` (pozor: on-demand, +60 GB na SPARKu,
`make up-swarm-director`; jinak požadavek spadne — appka má fallback na
`translate` + hlášku).

**Verifikace:** konverzace s Knihovníkem na fyzickém zařízení, zdroje
vidět, multi-turn drží kontext (session_id), přepnutí modelu funguje.

## Fáze 4 — kapitoly (ingest + server + UI)

**4a. Chapter-aware ingest.** `ingest_books.py`: per-tradice regexy
nadpisů (čínské edice: `^\S+第[一二三四五六七八九十]+$` apod.; Gutenberg
EN: `^(BOOK|CHAPTER|PART) [IVXLC0-9]+`; Mahábhárata: `SECTION [IVXLC]+`).
Chunk dostane metadata `chapter` (název) a `chapter_index`. Fallback:
bez rozpoznané struktury zůstává jen chunk_index (dnešní stav).

**4b. Server:** `GET /works/{work}/chapters` (z katalogu),
`GET /works/{work}/chunks?from=N&n=3` (sekvenční čtení — jde i bez 4a).
Volitelně: intent routing v `/chat` — dotazy typu „přečti mi druhou
kapitolu Tao te ťingu" obsloužit lookupem místo vektorového retrievalu
(jednoduchá heuristika, nebo tool-calling přes LiteLLM).

**4c. UI listování:** v detailu zdroje „číst dál" → sekvenční chunky;
seznam kapitol jako navigace.

**Náklad:** re-ingest + re-embed celého korpusu (ID chunků se změní —
`--reset` kolekce, ~25 min na SPARKu; viz pasti níže).

---

## FAQ

**Zná chatbot celé knihy, nebo jen úryvky?**
Celé knihy jsou v indexu, ale do odpovědi vstupuje vždy jen top-5
nejrelevantnějších úryvků. Na cílené otázky („co říká X o Y") to stačí;
na globální syntézu ne — tam pomůže vyšší `top_k` (parametr `/chat`)
nebo `swarm-director` s větším kontextem.

**Proč v odpovědi na otázku o Tao te ťingu cituje Zhuangzi?**
Retrieval je čistě vektorový napříč celou knihovnou — Gilesův překlad
Zhuangzi obsahuje dlouhé pasáže *o* Tao a wu-wej, které se k dotazu
podobají víc než 6 hutných chunků čínského originálu Tao te ťingu.
Nápravy: per-work filtr (`where={"work": ...}`) při dotazu na konkrétní
dílo (fáze 4b intent routing), jemnější chunking čínských originálů.

**V jakém jazyce se ptát?**
V libovolném — embedding (multilingual-e5-large) i LLM (`translate` =
Qwen3-32B) jsou multilingvální; odpovědi řídí český systémový prompt.
Korpus je směs zh/sa/en/de/grc/he originálů a překladů.

**Jak přidat novou knihu?**
`urls.txt` → `run_pipeline.sh` → `make ingest` → rsync → `make embed`
(HOWTO-m2.md). Embed je idempotentní, běžící server novou knihu vidí
okamžitě (čte Chromu per dotaz). Katalog ve fázi 1 se staví při startu
serveru → po přidání knihy `systemctl restart library-chat`.

**Jaká je latence odpovědi?**
Retrieval (embedding dotazu na GPU + Chroma po LAN) ~0,5 s; generování
`translate` ~5–15 s podle délky. Streaming (fáze 1b) dělá čekání
snesitelným. `swarm-director` je znatelně pomalejší a musí být předem
nahozený.

**Proč nejde appka přímo na LiteLLM jako u chatu s `lab`?**
Šlo by to, ale přišla by o retrieval — RAG server dělá embedding dotazu,
vektorové hledání v Chromě a skládání kontextu. Proto samostatný
endpoint chat.ol1n.com.

**Je to bezpečné vystavit na internet?**
Za Cloudflare Access se service tokenem (stejně jako llm.ol1n.com).
Dokud Access App neexistuje, tunel na chatbota nezapínat — pořadí kroků
viz PLAN-spark-chatbot.md fáze 2.

**Funguje appka offline / mimo LAN?**
Chatbot ano (přes tunel). Pozn.: Chroma na JODA je jen v LAN, ale to
klientovi nevadí — s Chromou mluví jen server na SPARKu.

---

## Známé pasti

1. `embed_books.py` **přeskakuje existující ID** — změna textu knihy se
   bez smazání starých chunků (per `path`) do Chromy nepropíše. Fáze 4a
   mění ID globálně → jedině `--reset` + plný re-embed.
2. Katalog v system promptu se staví při startu — po změně korpusu
   restartovat server.
3. `swarm-director` je on-demand; UI musí počítat s tím, že neběží.
4. SSE přes cloudflared: `keepAliveTimeout` v ingressu už je 300 s
   (nastaveno pro chat.ol1n.com), delší generování by ho mohlo přesáhnout.
5. Zhuangzi je v korpusu anglicky (Giles 1889), metadata `lang: zh` —
   kosmetická nepřesnost z per-group mapování jazyků (`LANG_BY_GROUP`);
   případná oprava = per-file výjimka v ingestu.
6. ~~Gateway mrší velké requesty~~ **OPRAVENO 2. 8. 2026** (AiStack
   `af71bf8`): příčinou byl LiteLLM `router_settings.timeout: 120` —
   dlouhé RAG dotazy (~2–4 min na translate) stínal a tiše přepadal na
   fallback. Timeout zvednut na 300 s a fallback vyměněn za
   Qwen3-4B-AWQ (česky mluvící nouzovka místo anglického LFM 350M).
   Server je zpět na gateway `:8080` (centrální routing + fallback).
7. translate potřebuje `--max_seq_len` i `--max_num_tokens` ≥ 16384
   (AiStack `docker-compose.translate.yaml`, commit 3b3aa2e) — RAG
   prompt s katalogem a top-5 chunky má 9–12k tokenů; pálí diakritika
   tokenizuje ~1 token/znak.
8. **Jména děl z Chromy jsou v NFD**, protože vznikla z názvů souborů na
   macOS (`Milindapañhapāḷi` = `n` + `~`, `l` + tečka…). Ručně psaný
   katalog i zlatý standard jsou NFC, takže `==` tiše selže a vypadá to
   jako chyba retrievalu — první baseline eval kvůli tomu hlásil o tři
   „miss" víc, než jich doopravdy bylo. Kdekoli se jméno díla porovnává
   nebo dává do `where={"work": ...}`, musí se sjednotit
   (`unicodedata.normalize("NFC", …)` na obou stranách).
9. **Embedding česky × korpus v pálí/sanskrtu je skoro slepý**: měřeno,
   top-5 se u dotazu liší o ~0,007 vzdálenosti, takže pořadí rozhoduje
   podíl tradice v korpusu (Tipitaka = 60 z 92 děl) víc než téma otázky.
   Bez směrování na dílo/tradici (`rag/retrieval.py`) vracel dotaz na Tao
   te ťing pálijské svazky. Kdyby se to mělo řešit v základu, znamená to
   jiný embedding model a re-embed celého korpusu.
10. **Brána kvality PDF nechytí transliterační fonty.** Westminster
    Leningrad Codex prošel ingestem, ačkoli jeho „hebrejština" byla
    mojibake z nestandardního fontu — 2 461 chunků s **nula** hebrejskými
    písmeny leželo v korpusu měsíc. `pdf_text_quality_ok()` hlídalo jen
    podíl písmen a PUA znaky, jenže ten guláš je složený z obyčejné
    latinky s diakritikou. Dílo je od 29. 8. 2026 smazané z kolekce
    i z `summaries.json`, cesta je v `EXCLUDE_PATH_MARKERS` a brána nově
    hlídá i podíl latinky s diakritikou (`MAX_LATIN_EXT`). Korpus: 92 děl,
    41 232 chunků. Pozor při případném re-ingestu — ID chunku se počítá
    ze `sha256(cesty)`, takže nový text na staré cestě by `embed_books.py`
    přeskočil jako „už tam je"; staré chunky musí padnout první.
11. **SwarmBattle Chroma na JODA zapisuje na plotnový disk** (`/media`, 16 TB,
    93 % plno): 128 upsertů za 24–69 s = 2 vektory/s při 0 % CPU. Původních
    41 k chunků prošlo, ale 250 k pasáží 2. vlny by trvalo dva dny. Proto
    má knihovna vlastní `library_chroma` :8007 s daty na SSD (`deploy/joda`,
    439 upsertů/s). Kolekce `books` na :8006 je jen legacy pro režim bez PG.
12. **`multilingual-e5-large` má `max_seq_length` 512** a co je nad, tiše
    uřízne: 100 % pálijských chunků (medián 592 tokenů), polovina čínských.
    Řeší se pasážemi ≤ 450 tokenů v `embed_books.py` a per-jazyk velikostí
    chunku v ingestu (pálí 1 100, čínština 500 znaků).
13. **fp16 embedding** na GB10: 28 → 95 pasáží/s, shoda s fp32 min cos 0,9998;
    bf16 je 165/s, ale min cos 0,998 — už se to hne. Index i dotaz musí jet
    stejně (`embeddings.LocalEmbedder`, `EMBED_DTYPE`).
14. **`pkill -f` přes ssh zabije i vlastní shell**, když vzor sedí na
    příkazovou řádku `bash -c` (obsahuje tentýž text). Vzor `"[e]mbed_books"`
    pomůže jen tehdy, když start nového procesu není v témže ssh volání.
15. ~~**Restart `library-chat` bez sudo jen přes `kill -9`.**~~ **VYŘEŠENO
    31. 8. 2026** přesunem pod **uživatelský** systemd
    (`deploy/spark/library-chat.service`, `make install-unit`). Původně:
    systémový unit měl `Restart=on-failure`, takže SIGTERM ukončil uvicorn
    čistě, systemd ho **nenastartoval znovu** a `systemctl start` chtěl
    heslo — produkce 30. 8. zůstala dole a doběhla ručně přes `nohup` mimo
    systemd. Nový unit má `Restart=always` (ověřeno: `kill` MainPID →
    `NRestarts=1` a služba je zpět) a `systemctl --user restart` nechce
    sudo. Systémový unit zůstal `enabled` — jednorázově
    `sudo systemctl disable --now library-chat`, jinak po rebootu sáhnou
    oba na port 8090.
16. **Uvažování se u obohacení musí vypnout.** Qwen3 napíše na značkovací
    úlohu dlouhý `<think>` a JSON pak useknou `max_tokens` uprostřed →
    chybí závorka → neparsovatelné. První ostrý benchmark kvůli tomu vrátil
    „0 hotovo, 40 chyb". `chat_template_kwargs: {enable_thinking: false}`
    TRT-LLM umí; na „vrať jen `{"a":1}`" spadne ze 123 tokenů na 9.
    Zbytek chyb ošetřuje tolerantní `parse_json` (holá hodnota bez uvozovek,
    chybějící čárka) a `max_tokens` 900 — po obojím 0 chyb z 50.
17. **Postgres nemá `sha1()`** (built-in jsou jen `sha224`+ nad `bytea`).
    Idempotence obohacení proto nese verzi promptu v hodnotě
    (`chunk-v1:<sha1>`) a SQL se ptá prefixem `NOT LIKE 'chunk-v1:%'`.
18. **`translate` a `swarm-director` se na GB10 nevejdou rozumně vedle
    sebe** (měřeno 31. 8. 2026, 121,7 GiB unified):

    | | GiB | poznámka |
    |---|---|---|
    | ComfyUI (`run.sh`, `--cache-lru 2`) | 52 | mimo docker, `~/Code/ComfyUI` |
    | `translate` produkční (batch 16, KV 0,5) | 57 | váhy 19,8 + KV 28,7 |
    | `translate` úsporný (batch 8, KV 0,12) | 31 | váhy 19,8 + KV ~7 |
    | `swarm-director` (util 0,65, len 32k, eager) | 84 | váhy ~74 (75 GB na disku) |
    | `fallback` (Qwen3-4B) | 9 | |
    | server knihovny (e5 embedder) | 1,2 | |

    Vedle sebe naběhnou jen takhle: `translate` úsporný + `fallback` dole +
    director `--gpu-memory-utilization 0.65 --max-model-len 32768
    --enforce-eager` → 117 ze 121,7 GiB, aktivní swap, nulová page cache.
    Rezidentní director přitom **propustnost nebere** (9,8 vs 9,7 chunku/min),
    daň se platí předem na `translate`: úsporný 9,7/min vs plný 13,6/min,
    tedy −29 %. Pořadí startu je dané: **director poslední**, protože tahle
    verze vLLM odmítne start, když `volná paměť < util × total` (chybová
    hláška to říká přesně) — kdežto `kv_cache_free_gpu_memory_fraction`
    u TRT-LLM se volnému místu přizpůsobí sama.
    Doporučení: nespouštět spolu. Nejdřív chunky na plném `translate`,
    pak ho shodit a pustit directora samotného na kapitoly a díla.
19. **Dávkový timeout musí počítat s čekáním na dávku, ne na sebe.** Při
    16 vláknech a 7,7 chunku/min trvá jeden request ~125 s, takže původních
    180 s v `LLMBatch` padalo na „Request timed out" při každém zaškobrtnutí
    (na translate s 18,9/min ≈ 50 s to nikdy nevyplavalo). Výchozích 600 s.
20. **`kill $(pgrep … | head -1)` zabije obálku, ne program.** `pgrep -f`
    najde nejdřív `bash -c`, který proces spustil; `head -1` pak zabije jen
    ji a Python běží dál. Starý běh obohacení takhle přežil, mířil na
    mezitím shozený `translate` a půl hodiny bral GPU čas tomu novému
    (7,7 → 6,7 chunku/min). Ověřovat `pgrep -af` bez `head`.
21. **Obohacení v šířku, ne do hloubky.** `ORDER BY work_id, seq` znamená,
    že po první noci je hotová Avesta a nic jiného — katalog ani témata
    napříč knihovnou nefungují ještě dva týdny. `--order breadth` bere n-tý
    chunk každého díla: po 204 uzlech se dotklo 107 děl.
22. Latence: plný dotaz (top_k=5, max_tokens=1024) ~4 min na GB10;
   katalogový (top_k=1–2) ~40–80 s. Mitigace: `/chat/stream` (první
   tokeny po prefillu), snížit top_k/max_tokens, případně zkrátit
   chunky v kontextu.

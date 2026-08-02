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
8. Latence: plný dotaz (top_k=5, max_tokens=1024) ~4 min na GB10;
   katalogový (top_k=1–2) ~40–80 s. Mitigace: `/chat/stream` (první
   tokeny po prefillu), snížit top_k/max_tokens, případně zkrátit
   chunky v kontextu.

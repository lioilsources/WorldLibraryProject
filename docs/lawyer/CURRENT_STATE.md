# Právník — současný stav RAG nad zákony ČR (průzkum 2026-09-28)

Bod 0 plánu `LAWYER_RAG_FIX_PLAN.md`: co reálně běží, jak je to postavené, co
je změřené a co z plánu je hotové, co je změřeně škodlivé a co chybí. Vše
v tomhle dokumentu je buď přečtené v kódu, nebo dotažené z běžící služby /
Postgresu 28. 9. 2026 — čísla z třetí ruky tu nejsou.

Klíčové zjištění pro plán: **role Právník není greenfield.** Běží od
2026-09-23 (53 předpisů, 18 331 chunků, 19 031 pasáží) a tři věci, které plán
navrhuje jako řešení, jsou tam už změřené — dvě z nich s opačným výsledkem,
než plán čeká (hybridní fulltext a LLM rewriting, viz §7).

---

## 1. Kde to je

| Co | Kde |
|---|---|
| RAG server i data | **`WorldLibraryProject/rag/`** — ne AiStack, ne backend Ol1nLLM. Na SPARKu nasazené v `~/deploy/WorldLibraryProject` |
| Služba | uživatelský unit **`law-chat.service`** na SPARKu, **port 8098** (8091 drží gen-queue), definice v `deploy/spark/law-chat.service` |
| Serverový kód | `rag/server.py` — **tentýž soubor jako Knihovník** (:8090), jen jiná kolekce, databáze, prompt a přepínače |
| Vektorová DB | **Chroma** na SPARKu `:8007`, kolekce **`law_v1`** (NVMe). pgvector se nepoužívá |
| Relační DB | Postgres na **JODĚ `:5433`, databáze `law`** (schéma `rag/sql/`, `LAW_PG_DSN` v `rag/.env`) — katalog, fulltext, deterministický lookup § |
| Embedding | `intfloat/multilingual-e5-large`, prefixy `query:`/`passage:`, fp16 na GB10 (`rag/embeddings.py`) |
| LLM | přes `--llm-url http://localhost:8080/v1`, model `translate`; akceptuje i `swarm-director` |
| Appka | `Ol1nLLM`, persona ⚖️ s `"backend": "law"`, třída `LibraryChatService.law()`, `LAW_CHAT_URL` (default `https://pravnik.ol1n.com`), `top_k` 8 |
| Plán, podle kterého se to stavělo | `Ol1nLLM/docs/plan-pravnik.md` (50 kB, ověřené zdroje, stav fází A–D) |

Živý `/status` 28. 9. 2026: `collection law_v1, documents 19031, works 53,
chapters 15850, chunks 18331, embed_model multilingual-e5-large, mode pg`.

## 2. Ingest pipeline

```
e-Sbírka (sbr-cache REST, stránky 0-based, pořadí stránek = pořadí dokumentu)
   │ rag/ingest_law.py --tier 1          registry: rag/registry/law/tier1.yaml (53 zákonů)
   │   cache na disku: rag/law/cache/{sb}_{rok}_{cislo}/{datum}/pN.json
   ▼
rag/law/{works,chapters,books}.jsonl     tentýž JSONL kontrakt jako knihovní korpus
   │ load_pg.py --dsn $LAW_PG_DSN --replace-all      → Postgres `law` na JODĚ
   │ embed_books.py --collection law_v1              → Chroma na SPARKu :8007
   ▼
server.py --port 8098 ... (law-chat.service)
```

Makefile: `make ingest-law` (M2) → `make sync-law` → na SPARKu `make load-pg-law
pg-index-law embed-law` → `make install-unit` → `make restart-law-chat`.
Registr zákonů je kurátorský: `cislo`, `short`, `name_cs`, `group` (odvětví),
`aliases` (kmeny bez diakritiky pro směrování, každý pád zvlášť), `abbr`
(zkratky jako celé tokeny pro parser citací).

**Co se zahazuje a proč** (ověřeno v `ingest_law.py`): část `novela` (text
vkládaný do *jiných* zákonů; v konsolidovaném znění cíle už je), poznámky pod
čarou, prefix, podpisy. Přílohy a preambule zůstávají (Listina je celá
v příloze usnesení).

**Verzování je jednoznění, ne časová osa.** Ingest stahuje *jedno* úplné znění
(účinné k datu stažení) a uloží `effective_from`; `ucinnost_do` ani historické
verze v datech nejsou. `works.edition` nese text „úplné znění účinné od
2026-09-01 (e-Sbírka, staženo 2026-09-23)" — a znění se mezi předpisy liší
(141/1961 → 2026-01-01, 99/1963 → 2026-09-01, 262/2006 → 2026-08-29).

## 3. Schéma indexu

Chunk = **§ (respektive čl.)**, u dlouhých § dělený po odstavcích na ~`CHUNK`
znaků; nadpis dílu/oddílu a název § jsou součástí `chapter_path` a text chunku
začíná „§ 2079 Kupní smlouva …". Kapitola = § se svou cestou ve struktuře.

Postgres `law` (reálné sloupce, `information_schema` 28. 9.):

- `works`: id, group, subgroup, title (`89/2012 Sb.`), work_legacy, name_cs,
  author, lang_*, **edition** (nese datum účinnosti jako text), form, period,
  source_path, urn (ELI), priority, chunk_count, chapter_count, char_count,
  summary_*, keywords_cs, aliases
- `chapters`: id, work_id, ordinal, level, parent_id, **ref** (`§ 2079`),
  heading, path, char_count, chunk_count, summary_*
- `chunks`: id, work_id, chapter_id, seq, seq_in_chapter, **ref_start**,
  **ref_end**, lang, text, text_fold, text_sha, char_count, **tsv_fold**,
  text_bigrams, tsv_bigrams
- `topics`: 8 právních odvětví (`registry/law/topics.yaml`)

**Pozor:** ingest emituje pole, která `load_pg` zahazuje, protože schéma je
společné s knihovnou — `effective_from`, `citation`, `stale_url`, `eli`
u kapitol. V Postgresu tedy datum účinnosti per předpis existuje jen jako text
v `works.edition`; per chunk je `effective_from` v **metadatech Chromy** (odtud
se dostane do odpovědi). Filtr „jen účinné k datu" se dnes nedá napsat jako
SQL podmínka.

Metadata chunku v Chromě: work, work_id, name_cs, title (`262/2006 Sb. (§ 51
odst. 1–3)`), group, subgroup, lang, chapter_id, chapter_ref, chapter_path,
ref_start, ref_end, citation, stale_url, path, effective_from, text_sha.
**Křížové odkazy mezi paragrafy (`paragraf_odkazy`) neexistují** — ani tabulka,
ani extrakce.

## 4. Dotazovací cesta

Produkční konfigurace (`law-chat.service`), každý přepínač je výsledek měření
z 2026-09-23:

```
--channels vec          jen vektor (bez fulltextu!)
--planner off --rewrite off
--max-per-work 6        (knihovna má 2)
--no-translate-excerpts
--cite-registry registry/law/tier1.yaml
--prompt-file prompts/pravnik_cs.md
```

Cesty dotazu:

1. **`cite`** (`rag/cite.py`) — dotaz jmenuje ustanovení („§ 51 odst. 1 ZP",
   „čl. 10 Listiny", „paragraf 204 OSŘ"): regex vytáhne §/čl., odstavec,
   písmeno, bod; předpis se hledá číslem → kmenem aliasu → zkratkou jako celým
   tokenem. Pak deterministický SELECT z Postgresu, **bez vektoru**. Měřeno
   100 %.
2. **content** — `retriever.Retriever`: vektorové hledání v Chromě (`vec`),
   směrování na předpis/odvětví podle aliasů (`retrieval.route`), RRF (přes
   jediný kanál = jen přerovnání), filtr tabulkových pasáží, diverzita
   `max_per_work`, hydratace textu z PG.
3. **katalog** („které zákony znáš") — `catalog.py` + plánovač; **s
   `--planner off` se dnes katalogové otázky chovají jako obyčejné hledání**
   (známé, pojmenované v README).

HTTP: `POST /chat`, `POST /chat/stream` (SSE), `GET /search` (retrieval bez
LLM — používá ho eval), `GET /works`, `/works/{id}/chapters`, `/works/{id}/chunks`,
`POST /reset`, `GET /status`, `/health`.

## 5. Prompt a odpověď do appky

`rag/prompts/pravnik_cs.md` (2 529 znaků) už dělá to, co plán žádá v §5:
odpovídat výhradně z dodaných úryvků, citovat `[n]` + slovně („§ 2079 odst. 1
občanského zákoníku"), přiznat, když to v úryvcích není, uvést znění, ze
kterého se vychází, oddělit doslovný text / důsledek / nejistotu, disclaimer
jednou na konci.

Do appky jde per úryvek: `work`, `name_cs`, `title` („262/2006 Sb. (§ 51 odst.
1–3)"), `group`, `path`, `excerpt`, `distance`, plus struktura, kterou appka
zatím ignoruje (`work_id`, `chapter_id`, `chapter_path`, `ref_start`,
`ref_end`, `score`). **URL na e-Sbírku se neposílá** — `stale_url`
(`/sb/2012/89/2026-01-01#par_2079`) zůstává v Chromě, `_sources()` ho
nepřepošle a `LibrarySource` v appce žádné pole pro odkaz nemá. Citace tedy
nejsou klikací.

## 6. Eval a baseline

`rag/eval/eval_law.py` + `rag/eval/golden_law.jsonl`. Dnes rozšířeno:

- zlatý standard **30 → 67 otázek** (65 měřitelných + 2 katalogové), každá má
  `area`; nové otázky míří na smluvní oblasti, které potřebují navazující plány
  (nájem 7, pracovní 13, kupní/spotřebitel 8, dílo 2, závazky 6, korporace 3,
  autorské 4, procesní 5, daňové 4…). **Každý očekávaný § je ověřen proti
  korpusu** (existuje v daném předpisu) — `eval/check_golden_law.py` to ověří
  znovu a hlásí i rozpad po oblastech.
- eval umí režim **`--service URL`**: content otázky jdou na `GET /search`
  běžícího `law-chat`, takže se měří přesně produkční konfigurace a nenačítá se
  druhý embedder (SPARK mívá při obohacení knihovny volné 2 GB ze 121). Jde to
  z M2 přes LAN. Přímý režim (`--mode vec|fts|hybrid`) zůstal pro izolaci kanálů.
- `make eval-law` (volitelně `LAW_TOP_K=5`, `EVAL_LAW_ARGS="--mode vec"`).

**Baseline 28. 9. 2026** (65 otázek, produkční konfig, `eval/results/law_20260928-*.json`):

| metrika | @8 (produkční top_k) | @5 (metrika plánu) |
|---|---|---|
| cite-hit | 1,00 (8 otázek) | 1,00 |
| work-hit | 1,00 | 0,965 |
| **ref-hit = recall** | **0,860** | **0,825** |
| ref-MRR | 0,618 | 0,613 |

Cíl plánu je recall@5 ≥ 0,85 — chybí 2,5 bodu a je jasně adresný. Rozpad
po oblastech (ref-hit@8 / MRR): pracovní 1,00/0,86 · trestní 1,00/1,00 ·
ústavní 1,00/1,00 · závazky 1,00/0,90 · kupní 1,00/0,61 · správní 1,00/0,75 ·
dílo 1,00/0,75 · korporace 1,00/0,58 · **autorské 1,00/0,20** ·
**spotřebitel 0,75/0,53** · **nájem 0,714/0,32** · **daňové 0,667/0,14** ·
**procesní 0,25/0,25** · **insolvence 0,00**.

Starší měření (2026-09-23, 28 otázek) zůstává platné pro volbu kanálů:
`vec` ref-hit@8 91 % / MRR 0,75 · `vec+fts` 74 % · `fts` 35 % (výsledek je
v `eval/results/law_20260923-131029.json` na SPARKu).

---

## 7. `LAWYER_RAG_FIX_PLAN.md` proti realitě

| Bod plánu | Stav |
|---|---|
| §1 zdroj e-Sbírka, ne zakonyprolidi | **hotovo** — `ingest_law.py` proti sbr-cache REST, s diskovou cache; zakonyprolidi i CzCDC (CC BY-NC) byly prověřeny a zamítnuty (`plan-pravnik.md` §2.2) |
| §1 `cmd/law-ingest` v Go (sqlc, golang-migrate, Postgres) | **konflikt** — ingest existuje v Pythonu a je odladěný na zvláštnosti e-Sbírky (0-based stránky, pořadí fragmentů, Listina v příloze, zahazování části `novela`). Přepis do Go by to zahodil bez měřitelného přínosu; schéma jede na `rag/sql/` + `pg_migrate.py` |
| §1 startovní sada předpisů | **hotovo, až na dvě položky**: tier1 má 53 zákonů včetně 89/2012, 262/2006, 90/2012, 634/1992, 110/2019, 235/2004, 99/1963, 121/2000. **Chybí 216/1994 (rozhodčí řízení)** a **nařízení GDPR** (není v e-Sbírce — jen Cellar/EUR-Lex, v plánu Právníka je to vrstva 5) |
| §1 týdenní cron na novely | **chybí** — postup je navržený (`plan-pravnik.md` §10), timer neexistuje |
| §2 chunk = § / odstavec, nadpisy v textu | **hotovo** |
| §2 metadata (zákon, část/hlava/díl, §, odstavec, oblast) | **hotovo** v Chromě i PG (`chapter_path`, `ref_start/end`, `group`) |
| §2 `ucinnost_od` / `ucinnost_do`, historická znění | **částečně**: `effective_from` ano (Chroma + text v `works.edition`), `ucinnost_do` a verze ne → dotaz „k datu" nejde |
| §2 tabulka `paragraf_odkazy` | **chybí** celá |
| §3 BM25 + dense → RRF | **změřeno jako horší**: fulltext v Postgresu jede na konfiguraci `simple` bez českého stemmingu a tahá do RRF šum (74 % vs. 91 %). Buď český slovník/stemmer, nebo to nechat vypnuté — samo přidání kanálu kvalitu srazí |
| §3 reranker (`bge-reranker-v2-m3`) | **nezkoušeno, smysluplné.** Fúzní funkce `combine_rerank()` v `hybrid.py` existuje (α·rerank + (1−α)·RRF) a je otestovaná, ale do `Retriever` není zapojená a žádná rerank služba neběží. MRR 0,62 při recall 0,86 říká, že se má co přerovnávat |
| §3 embedding bge-m3 / multilingual-e5-large | běží **multilingual-e5-large** (jedna ze dvou variant plánu); bge-m3 by znamenal reindex celé kolekce a dal by se změřit až proti tomuhle baseline |
| §3 expanze na sousední odstavce | **poloviční**: `pg_search.neighbors()` + `Retriever(context_window=…)` + CLI `--context-window` existují, ale (a) expandují jen odstavce **téhož §**, ne sousední §§, (b) `h["neighbors"]` **nikdo nečte** — do promptu se to nedostane. Dnes je to no-op |
| §3 expanze po křížových odkazech | **chybí** (není z čeho — viz `paragraf_odkazy`) |
| §3 filtr `ucinnost_do IS NULL`, parametr `k_datu` | **nejde** bez verzování dat |
| §4 LLM query rewriting jako největší přínos | **změřeno jako škodlivé v dnešní podobě**: knihovní plánovač překládá právní termíny do jazyků tradic (vyšlo „Vertragsende", „Arbeitsverhältnis") a stojí ~10 s na první token → `--planner off --rewrite off`. Právní plánovač je otevřený follow-up, ne hotová výhra — a měřit se musí proti `vec`-only baseline |
| §4 statická mapa pojmů `legal_terms.yaml` | **chybí** — a data z §6 ukazují, kde by pomohla (procesní 0,25) |
| §5 odpovídat jen z chunků, citace, disclaimer | **hotovo** v promptu |
| §5 klikací citace na e-Sbírku | **chybí** poslední kus: `stale_url` se do appky neposílá a `LibrarySource` nemá pole pro URL |
| §5 „confidence" z reranker score | **nejde z dnešních čísel** — viz nález N1 |
| §6 `make eval-lawyer`, recall + MRR + rozpad | **hotovo dnes** jako `make eval-law` (+ `by_area` v reportu) |
| §6 logovat reálné dotazy a rozšiřovat eval | **chybí** |

## 8. Nálezy z dnešního průzkumu

**N1 — `distance` i `score` v odpovědi jsou funkcí ranku, ne podobnosti.**
`retriever.py:145` počítá `distance = 1 − min(score·20, 1)` z RRF skóre, takže
každý dotaz dostane tutéž řadu 0,6721 / 0,6774 / 0,6825… (ověřeno na dvou
nesouvisejících dotazech). Surová kosinová vzdálenost z Chromy se zahodí.
Důsledek: „confidence" z §5 plánu se z dnešních dat spočítat nedá a `distance`
v appce nic neříká. Oprava je malá (nést `min` surové vzdálenosti v metadatech),
ale mění i knihovní instanci.

**N2 — 3 z 8 minutých otázek jsou přímý sousední §.** Očekáváno § 2235, v top-5
je § 2236; očekáváno § 21 ZDPH, v top-5 §§ 20a a 22; očekáváno § 389 IZ, v top-5
§§ 390 a 391. Expanze na sousední **kapitoly** (±1 `chapters.ordinal` v témže
předpisu), ne na odstavce téhož §, je nejlevnější cesta k recall@5 ≥ 0,85.
Další případ (§ 2288, v top-5 § 2285 z téhož dílu „Skončení nájmu") by vyřešila
expanze na úroveň dílu/oddílu.

**N3 — procesní oblast selhává na slovníku, ne na ranku.** „Kdo platí náklady
soudního řízení?" vrátí 292/2013 (ZŘS) a 150/2002 (SŘS) místo 99/1963 (OSŘ);
„který soud bude spor rozhodovat" vrátí 91/2012 (ZMPS). Vektor nerozliší tři
procesní kodexy — sem patří mapa pojmů (§4 plánu) nebo routing na `group`
s preferencí obecného kodexu, a je to měřitelné na 5 otázkách.

**N4 — `§ 29 odst. 1 zákona o DPH` nerozpoznal předpis** (235/2004 mělo jen
zkratku `ZDPH`) → `cite` spadl na 0,875. **Opraveno dnes** (`abbr: [ZDPH, DPH]`),
cite zpět na 1,00. Živý server to vezme až po `make sync-law` + `make
restart-law-chat` (restart maže konverzace v RAM).

**N5 — katalogový intent je vypnutý spolu s plánovačem.** „Které trestní
předpisy znáš" jde do vektoru. V evalu jsou dvě katalogové otázky, ale
`eval_law.py` je přeskakuje — katalog se dnes neměří vůbec.

**N7 — kurátorský registr zákonů tiše nebyl ve gitu.** `rag/.gitignore` měl
vzor `law/` bez úvodního lomítka, takže kromě zamýšleného `rag/law/` (cache
e-Sbírky + výstup ingestu) chytal i **`rag/registry/law/`** (tier1.yaml —
53 zákonů, aliasy, zkratky, odvětví; ručně dělaný, nereprodukovatelný
z e-Sbírky) a `rag/tests/fixtures/law/` (880 kB fixtur pro
`test_ingest_law.py`). Jediné další kopie registru jsou na SPARKu, kam ho
rozkopíruje `make sync-law`. **Opraveno dnes** na `/law/`; registr i fixtury
jsou teď vidět jako untracked a patří do commitu. Celá práce na Právníkovi
z 23. 9. je mimochodem **necommitnutá na větvi `pravnik`** (untracked
`ingest_law.py`, `cite.py`, `prompts/pravnik_cs.md`, `eval_law.py`,
`golden_law.jsonl`, `deploy/spark/law-chat.service`, `tests/test_ingest_law.py`
+ nezastagované změny `server.py`, `retriever.py`, `Makefile`).

**N6 — chybí 216/1994 Sb. a GDPR.** Bez zákona o rozhodčím řízení nelze
podložit rozhodčí doložku, bez GDPR (jen 110/2019, což je adaptační zákon)
nelze podložit klauzule o zpracování osobních údajů. Pro generování smluv jsou
to obě potřebné.

## 9. Odpovědi na otevřené otázky z plánu

1. **Zůstává RAG v AiStack, nebo v backendu Ol1nLLM?** Ani jedno — je
   a zůstane ve `WorldLibraryProject/rag`, sdílený kód s Knihovníkem, na SPARKu
   jako `law-chat.service`. AiStack drží LLM park, ne RAG.
2. **Jaká vektorová DB dnes?** Chroma `:8007`, kolekce `law_v1`; Postgres
   (JODA `:5433`, db `law`) na katalog, fulltext a `cite`. pgvector by znamenal
   migraci kolekce a reindex — proti dnešnímu baseline měřitelné, ale samo to
   kvalitu nezvedne.
3. **Judikatura jako druhá kolekce hned?** Zdroj je prověřený a strojově
   čitelný (MSp open data, `rozhodnuti.justice.cz/api/opendata/…`, 605 tis.
   rozhodnutí, ECLI + `regulations[]` s §) a v plánu Právníka je to vrstva 4,
   tj. po vrstvě 2. Pozor na jména soudců v `header` (osobní údaje). Dokud
   recall na zákonech není nad cílem, druhá kolekce jen zvětší prostor k chybě.

## 10. Co se změnilo 28. 9. 2026

- `rag/eval/golden_law.jsonl`: 30 → 67 otázek, přidané `area`, důraz na smluvní
  oblasti; všechny očekávané § ověřené proti korpusu.
- `rag/eval/eval_law.py`: režim `--service` (měří produkční konfiguraci bez
  druhého embedderu), rozpad `by_area`, § se bere z `ref_start`.
- `rag/eval/check_golden_law.py`: nový — kontrola zlatého standardu proti korpusu.
- `rag/Makefile`: cíl `eval-law` (`LAW_URL`, `LAW_TOP_K`, `EVAL_LAW_ARGS`).
- `rag/registry/law/tier1.yaml`: zkratka `DPH` u 235/2004 (nález N4).
- `rag/.gitignore`: `law/` → `/law/` (nález N7).
- `rag/README.md` sekce „Eval Právníka", `CLAUDE.md` odkazy.
- `rag/eval/results/law_20260928-baseline-k8.json`, `-k5.json` (před opravou)
  a `law_20260928-dph-fix-k8.json` (po ní).
- `make test` prochází (76 testů).

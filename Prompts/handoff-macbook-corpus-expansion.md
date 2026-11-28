# Handoff — rozšíření korpusu, pokračování na MacBooku

**Zadání pro Opus:** implementuj podle `PLAN-corpus-expansion.md`. Tenhle
soubor říká, v jakém stavu práci přebíráš a čím se MacBook liší od M2.
Stav k 2026-11-22.

## Co je hotové

- Plán: inventura korpusu, ověřené zdroje originálů, fáze 0–7.
- Rozhodnutí vlastníka jsou v plánu v sekci **Rozhodnuto** — neptej se na ně
  znovu.
- Z plánu zatím není implementováno nic, co by bylo v gitu.

## Co leží necommitnuté na M2

Jiná session tam na větvi `feat/nt-reading` rozpracovala:

- Rámájanu z GRETIL: sedm kánd jako TXT v `downloads/sanskrit/ramayana/gretil/`,
  záznamy `sa.ram1`–`sa.ram7` v `rag/registry/works.yaml`, úprava
  `EXCLUDE_PATH_MARKERS` v `rag/ingest_books.py`
- stažené zdroje bez záznamu v registru: `downloads/bible/hebrew/morphhb/`
  (hebrejská Bible, WLC), `downloads/greek_latin/first1k/` (First1KGreek),
  `downloads/european_philosophy/latin/`

Dokud to M2 nepushne, na MacBooku to není. Na začátku udělej `git fetch`
a podívej se, jestli už ta práce někde je (`git log --all --oneline -15`).
Když není, **nezačínej tatáž díla znovu** — vezmi nejdřív to, co se
nepřekrývá (viz První kroky).

`works.yaml` a `ingest_books.py` upravují obě strany. Větev zakládej z `main`,
záznamy přidávej na konec a po pushi z M2 hned rebasuj. Jestli ve fázi 0
budeš sahat na načítání registru, rozděl ho po skupinách do více souborů —
konflikty tím zmizí.

## Příprava MacBooku (jednou)

1. `git lfs install`, pak LFS jen pro to, co potřebuješ, např.
   `git lfs pull -I "downloads/chinese/**"`. Celé `downloads/` má 3,9 GB.
2. `rag/.env` podle `rag/.env.example` — `PG_DSN`, `CHROMA_URL`, `COLLECTION`.
   Hodnoty nejsou v gitu a do gitu nepatří.
3. `cd rag && make deps && make test`
4. Spojení na Postgres: `.venv/bin/python3 pg_migrate.py --status`.
   (`make pg-status` navíc volá `ssh joda`.)
5. Zjisti, jestli MacBook dosáhne na SPARK: `ssh spark true`.

## Co jde z MacBooku

| Práce | Jde? |
|---|---|
| registr, adaptéry, detektory kapitol, testy | ano |
| `make ingest-stats`, ingest nových děl | ano |
| nahrání do Postgresu, katalogové dotazy, mazání děl | ano |
| export bundlů pro Kindlify (jen SELECT) | ano |
| embedding do Chromy, chatbot | jen přes SPARK a `spark-broker` |
| obohacení LLM | SPARK přes `spark-broker`, nebo výplň na L40S (řeší session L40S; klient potřebuje Postgres a tunel na vLLM) |

Když MacBook na SPARK nedosáhne, skonči u nahrání do Postgresu a do PR
napiš, co zbývá spustit na SPARKu (`make embed`, obohacení). Kolekce glos
`books_gloss` se schválně nestaví (`PLAN-spark-library-storage.md`).

## Data bez plného korpusu

`books.jsonl`, `works.jsonl` a `chapters.jsonl` v gitu nejsou. Plný ingest
potřebuje celé `downloads/` z LFS a navíc Perseus, který se klonuje přes
`./run_git_wget.sh` (v LFS není). Pro přidávání děl ho nepotřebuješ:

```bash
cd rag
# příklad: jen jedna skupina, do vlastního souboru (works a chapters vzniknou
# vedle něj); skupina i work_id jsou ilustrační
.venv/bin/python3 ingest_books.py --input-dir ../downloads --registry registry \
    --groups islam --output build/new/books.jsonl
# nahrát po jednom díle
.venv/bin/python3 load_pg.py --input build/new/books.jsonl \
    --works build/new/works.jsonl --chapters build/new/chapters.jsonl \
    --work ar.quran --replace-work
```

**Nenahrávej znovu díla, která už v Postgresu jsou.** Obohacení chunku
přežije jen tam, kde sedí `chunk_id` i otisk textu; ingest z jiné verze
kódu nebo dat ho zahodí a jsou to hodiny práce LLM. `make load-pg`
(`--replace-all`) proto pouštěj jen nad JSONL, ve kterém jsou výhradně
nová díla.

## Mazání z databáze

Schválené je smazání 26 překladů, vždy až po nahrazení originálem; seznam,
podmínky a stav jsou v plánu (Rozhodnuto č. 3). Běžný load ani
`embed_books.py --sync` dílo, které zmizelo ze vstupu, nesmažou. Postup:

1. záznam díla pryč z registru a zdrojový soubor pryč z `downloads/`
   (soubor bez záznamu by ingest vrátil s odvozenými metadaty)
2. `load_pg.py --drop-work <id> --dry-run`, počty do hlášení
3. `load_pg.py --drop-work <id>` — zálohu obohacení zapíše do `rag/run/`
   (mimo git) a smaže dílo z Postgresu i z Chromy (`books_v2`)
4. aliasy a zlaté otázky přepojit na originál, chat restartovat

## První kroky

1. **Fáze 0** — adaptéry, pole `tradition`, jazyky ve `validate.py`. Čistě
   kód, nezávisí na datech z M2.
2. **Korán** z Tanzilu (1,4 MB) jako první dílo celou cestou až do
   Postgresu. Na M2 se nepřekrývá s ničím a ověří, že MacBook umí celý řetěz.
3. **Čína** z Kanripa: Čuang-c' v originále a klasika.
4. Hebrejská Bible, First1KGreek a latina — až M2 pushne, co má stažené.

Jedna fáze = jeden PR, merge rozhoduje Olin.

## Úklid na M2

- Session, které patří rozdělaná práce na `feat/nt-reading`, ji musí
  commitnout a pushnout.
- Plán a tento handoff leží na M2 i jako neverzované kopie. Před `git pull`
  hlavní větve je smaž, jinak git odmítne soubory přepsat.

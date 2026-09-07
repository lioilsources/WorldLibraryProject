# PLAN — bundly pro Kindlify z knihovního Postgresu

Plán pro Claude Code (nebo ruční provedení) na stroji, který **dosáhne na
Postgres na JODĚ** (`library_postgres` :5433) — tedy M2 nebo SPARK, kde je
`rag/.env` s `PG_DSN`. Cíl: z korpusu vypadnou skutečné bundly pro
Kindlify a nahradí dvě ručně psaná demo díla v assetech appky.

Kontext: `rag/export_bundle.py` je hotový a otestovaný **bez databáze** —
čistá logika (strom, termy, skóre, souhrny) i rozbalení řádků mají testy
(`rag/tests/test_export_bundle.py`, 22 případů) a `validate_bundle()`
prošel proti oběma demo bundlům, které Kindlify dnes opravdu importuje.
Co ověřené **není**: znění SQL proti živému schématu a to, co z korpusu
skutečně vypadne. To je práce tohohle plánu.

Export je **read-only** vůči Postgresu (jen SELECT) a Chromu nepotřebuje
vůbec. Jediné, co něco přepisuje, je zápis JSON souborů do `--out`.

---

## Fáze 0 — prerekvizity

```bash
cd WorldLibraryProject/rag
cat .env | grep -o '^PG_DSN=.\{0,20\}'      # PG_DSN musí existovat (heslo neechovat celé)
.venv/bin/python3 -c "import psycopg; print('psycopg ok')"
.venv/bin/python3 -m pytest -q tests/test_export_bundle.py   # 22 passed, bez DB
```

Když `.venv` chybí: `make deps`.

## Fáze 1 — stav obohacení (rozhoduje, co má smysl exportovat)

Bundle je jen přerovnání toho, co v PG je. Bez `enrich_chunks` vypadne
prázdný word cloud, bez `enrich_chapters` chybí souhrny kapitol —
a Kindlify je pak k ničemu, i když export „projde".

```bash
psql "$PG_DSN" -c "SELECT * FROM enrichment_status_v;"

# pokrytí po dílech, kandidáti na export = vysoké enriched i summarized
psql "$PG_DSN" -c "
SELECT w.id, w.priority, w.chapter_count,
       count(*) FILTER (WHERE ch.summary_medium IS NOT NULL) AS kapitol_se_souhrnem,
       (SELECT count(*) FROM chunks c JOIN chunk_enrichment e ON e.chunk_id = c.id
         WHERE c.work_id = w.id) AS obohacenych_chunku,
       w.summary_long IS NOT NULL AS ma_souhrn_dila
FROM works w LEFT JOIN chapters ch ON ch.work_id = w.id
WHERE w.priority = 1
GROUP BY w.id ORDER BY 4 DESC LIMIT 20;"
```

**Rozhodnutí:** exportovat jen díla, kde `kapitol_se_souhrnem` je aspoň
polovina `chapter_count`. Dílo bez obohacení do appky posílat nemá smysl —
zbudou kurátorská klíčová slova a prázdné panely.

Kandidáti na první běh podle dnešní znalosti korpusu: `zh.daodejing`
(81 kapitol, malé, kontrolovatelné okem) a `zh.lunyu` (Hovory) — obě
existují i jako ruční demo bundle, takže jde porovnat kvalita.

## Fáze 2 — první export a čtení výsledku

```bash
make bundles BUNDLE_ARGS="--work zh.daodejing" BUNDLE_OUT=build/bundles
```

Report na stderr (`slug`, uzlů, termů, souhrnů, `bez souhrnu N`,
`pipelineVersion`) je hlavní diagnostika — přečíst ho, ne přeskočit.

Verifikace obsahu:

```bash
cd build/bundles
python3 -m json.tool zh_daodejing.json | head -40
jq '.manifest.tree.children | length' zh_daodejing.json          # části/kapitoly
jq '[.words.nodes[].terms | length] | add' zh_daodejing.json     # termů celkem
jq '.words.nodes.root.terms[:15]' zh_daodejing.json              # dávají smysl?
jq -r '.summaries | to_entries[:3][] | .value.cs' zh_daodejing.json
```

Na co se dívat (tohle metriky neřeknou):

- **Termy v cloudu jsou pojmy, ne balast.** Když nahoře plavou „kapitola",
  „text", „autor", je vadné obohacení, ne export — oprava patří do
  `enrich_chunks.py`, ne sem.
- **Labely kapitol jsou čitelné česky.** Prázdné `heading_cs` znamená
  neproběhlý `enrich_chapters`; label pak spadne na originál (學而第一),
  což je pravdivé, ale pro českého čtenáře slepé.
- **Poměr `orig` termů.** Původní písmo (無為, nibbāna) je půvab téhle
  čtečky; když nejsou vůbec, `keywords_orig` se neplní a stojí za to
  zjistit proč.
- **Velikost souboru.** Nad ~2 MB na dílo se assety appky zvětší znatelně;
  řešení je `--top-terms 30`, ne ořezávání souhrnů.

Porovnat s ručním demo bundlem (`Kindlify/assets/bundles/dao_de_jing.json`)
— ten je kurátorský a slouží jako měřítko, ne jako pravda.

## Fáze 3 — do appky

```bash
# na M2 (Kindlify je vedle WorldLibraryProject)
make bundles BUNDLE_ARGS="--work zh.daodejing" BUNDLE_OUT=../../Kindlify/assets/bundles
```

Pozor: demo bundly `dao_de_jing.json` / `analects.json` jsou **ručně
psané a v gitu** — nový export je přepíše jen tehdy, když se slug trefí
(`zh-daodejing` → `zh_daodejing.json`, tedy jiný soubor než `dao_de_jing.json`).
Nové soubory je potřeba přidat do `_demoBundles` v
`Kindlify/lib/features/library/presentation/library_screen.dart` a do
`pubspec.yaml` (assety se přibalují po adresáři, takže tam nejspíš stačí
existující `assets/bundles/`) — ověřit, ne předpokládat.

Ověření na skutečné ploše (macOS, potřebuje Mac):

```bash
cd Kindlify && flutter drive --driver=test_driver/integration_test.dart \
  --target=integration_test/verify_flow_test.dart -d macos
```

Řídit se skillem `.claude/skills/verify/SKILL.md` — hlavně: **žádné
`pumpAndSettle`**, do kapitol chodit přes `node://` odkazy, ne přes chipy.

## Fáze 4 — hromadný export

```bash
make bundles                                    # všechna díla s prioritou 1
make bundles BUNDLE_ARGS="--all --priority 2"   # + běžná díla
```

Report vypíše řádek na dílo; díla s `bez souhrnu` u většiny uzlů z výběru
vyřadit ručně (nebo doobohatit a exportovat znovu).

---

## Co při tom nejspíš praskne

- **Chunky nevisí na kapitolách** (`chunks.chapter_id IS NULL`) u děl bez
  detektoru kapitol. Export to ustojí — takové chunky spadnou pod kořen —
  ale strom bude plochý a cloud bude mít jen kořen. Poznat to jde dotazem
  `SELECT count(*) FROM chunks WHERE work_id = … AND chapter_id IS NULL`.
- **Víc než dvě úrovně kapitol** (Mahábhárata: parva → sekce → …).
  `node_kind()` mapuje 1 → chapter, 2 → section, hlouběji → paragraph.
  Zkontrolovat, že Kindlify takový strom kreslí rozumně; breadcrumb na
  130 px byl slabé místo už v návrhu.
- **Perseus (`greek_latin`, ~1 080 děl)** má jiný původ struktury (CTS
  URN, `__cts__.xml`). Export na něj nespadne, ale `--all --priority 2`
  vyrobí tisíc souborů — filtrovat `--work`.
- **Velká kniha v appce.** Import v Kindlify vkládá uzel po uzlu bez
  transakce (`bundle_loader.dart`), `_compositeSummary()` dělá N+1 dotazů
  a `jumpTo()` šplhá ke kořeni po jednom. Na 81 kapitolách se to ztratí,
  na Mahábhárátě ne. **Neopravovat naslepo** — nejdřív změřit na skutečném
  bundlu, ať je vidět, co je opravdu úzké hrdlo.

## Rollback

Export nic v databázi nemění; stačí smazat vygenerované JSON. V Kindlify
je vrácení `git checkout -- assets/bundles/` a odebrání položek
z `_demoBundles`.

## Rozhodnutí, která čekají (ne v tomhle plánu)

1. **Text díla v bundlu.** `byteStart`/`byteEnd` jsou dnes 0 a nic je
   nečte. Buď se do bundlu přibalí text chunků (a Kindlify dostane
   čtenářský mód nad originálem), nebo se ta pole z modelu vyhodí. Držet
   je nepoužité je nejhorší varianta.
2. **Barva pro `kind: "orig"`.** `WordBubble` barví `entity` a `phrase`,
   `orig` propadne na výchozí — termy v původním písmu mají být poznat.
3. **Druhá locale.** Bundle nese jen `cs`. `en` by znamenalo anglické
   souhrny v PG, což je nový sloupec a nový běh obohacení.
4. **Doručení bundlů.** Dnes assety v gitu appky. Server (`GET
   /works/{id}/bundle.json` v `rag/server.py`) je malý krok a Kindlify
   už má v Settings pole na URL — ale zatím nikdo nedefinoval, kdo
   bundly hostuje.

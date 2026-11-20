# PLAN — obohacení korpusu na L40S (dokud je sandbox)

Cíl: doobohatit korpus (`enrich_chunks` → `enrich_chapters`) na L40S, aby
`kindlify_sync` mohl vyexportovat zbytek děl do Kindlify. SPARK zůstává
hlavní cestou; L40S je dočasný akcelerátor.

## Proč to jde bez instalace na VM

Klient běží **na Macu (M2)**, ne na L40S. M2 dosáhne na Postgres na JODĚ
(`rag/.env`, `PG_DSN`) a launchd ssh tunel přeposílá vLLM z L40S
na `localhost:8000`. Na VM se nic nekopíruje, žádná data ani DB tam neleží,
odcházejí jen prompty. Resume je v DB přes `input_sha`, takže pád tunelu
nebo konec sandboxu nic neztratí — dál se pokračuje na SPARKu.

## Model

| Model na L40S | Rychlost (MODEL_NOTES v repu sandboxu) | Čeština |
|---|---|---|
| Qwen3-30B-A3B-Instruct-2507-FP8 (už běží, 32k kontext) | 1 800–4 300 výstup tok/s | horší než Gemma (pilot 30. 10., dojem) |
| Gemma-3-27B-it FP8 | pilot Knihovník: 53 úryvků/min při souběhu 32 | nejlepší z 5 modelů v pilotu |

Do DB se zapisuje `model` ke každému chunku/kapitole, takže díla lze později
přeobohatit lepším modelem (`input_sha` se změní jen s promptem — přepis
podle modelu by chtěl `--model`-filtr, zatím neexistuje).

## Zbývá (odhad z `rag/works.jsonl` k 2026-11-17, bez díla už v Kindlify)

| Skupina | Díla | Chunky | Kapitoly | Do Kindlify? |
|---|---|---|---|---|
| P1, originály ≥ 2 kapitoly | 36 (32 pálí, 3 lat, 1 lzh) | 21 255 | 4 707 | ano |
| P1, překlady a ploché | 41 | 26 136 | 3 813 | ne (sync je vynechává) |
| P2 | 991 (702 grc, 288 lat) | 92 783 | 28 471 | ano |
| P3 | 13 | 1 607 | 188 | ano |

Horní odhad: část z toho už SPARK mohl obohatit (ověřit `enrichment_status_v`).

## Naměřeno (2026-11-17, okno wlp-enrich-p1, profil B)

Profil B = Qwen3-30B-A3B, 32k kontext, gpu-mem 0.80, vedle běží embed.

| Úloha | Souběh | Rychlost | Pozn. |
|---|---|---|---|
| benchmark 200 chunků | 64 | 80/min | rozjezd a doběh dávky |
| chunky, ostrý běh | 64 | **~120/min**, výstup ~1 280 tok/s | 1,4 % useknuto na `max_tokens 900`, 0 fallbacků |
| kapitoly | 4 | **~7,5/min** | 32 vláken zahltí KV (3 běží, 29 čeká) |

Za 95 min: +11,5 k chunků, +658 kapitol. Na SPARKu běží director 6,7 chunku/min.

Zbývá po okně: P2 63,7 k chunků + 13 k kapitol, P3 1,6 k + 188. Chunky
vystačí na jednu noc. Úzkým hrdlem jsou kapitoly: při 7,5/min ~30 h, takže
po dokončení chunků pouštět kapitoly s víc vlákny (KV se uvolní).

Noční okno wlp-enrich-n1k (profil K: bez embedu, gpu-mem 0.92) jede
`rag/l40s_run.sh` s fázemi souběhu 64+4 → 128+8 → 192+8 → 256+8. Výsledek
srovnání K proti B sem doplnit.

## Filler (od 2026-11-17)

WorldLibraryProject je výplň L40S: qctl úloha `wlp-filler` na profilu K běží,
kdykoli nic jiného nečeká. Obsluhuje ji stálý `rag/l40s_filler.py` na Macu:

- markery v `L40S_QUEUE` (adresář fronty na VM): `WLP_F_READY` (jen když slot běží), `WLP_F_STOP`
  (klienty ukončit do 1–2 min), `WLP_F_DONE` (posílá runner, až je korpus hotový),
- kontrola každých 30 s, výpadek VM nebo tunelu = pauza, ne chyba,
- chunky 128 vláken + kapitoly 8; po vyčerpání chunků kapitoly 24,
- hotovo = méně než 1 % chunků i kapitol bez obohacení (`DONE_SHARE`).

Spuštění: `nohup .venv/bin/python3 l40s_filler.py > logs/l40s_filler.log 2>&1 & disown`
(a `caffeinate -i -w <pid>`, ať Mac neusne). Okna s vlastními markery
(`WLP_N1_*`) jedou dál přes `l40s_run.sh`.

## Kroky

1. **Rezervace GPU na L40S** (session L40S / `queue/qctl`): vLLM s vybraným
   modelem, `--max-num-seqs 128`, fp8 KV, prefix caching, 32k kontext kvůli
   kapitolám. Ověřit z Macu: `curl -s localhost:8000/v1/models`.
2. **Benchmark kvality a rychlosti** (zapisuje do DB):
   ```bash
   cd rag
   .venv/bin/python3 enrich_chunks.py --llm-url http://localhost:8000/v1 \
     --model qwen3-30b-a3b --workers 64 --priority 1 --benchmark 200
   ```
   Porovnat 10 výstupů s dosavadními (Nemotron / translate) v `chunk_enrichment`.
3. **Okno** (chunky a kapitoly zároveň, uvolní okno markerem `<MARK>_DONE`):
   ```bash
   MARK=WLP_N1 WINDOW_MIN=470 PHASES="64:4:30 128:8:30 192:8:30 256:8:0" \
     nohup ./l40s_run.sh > logs/l40s_n1.log 2>&1 & echo $! > logs/l40s_n1.pid; disown
   ```
   Souběh ladit podle `vllm:num_requests_waiting` v `localhost:8000/metrics`
   (cíl ≈ 0, KV < 90 %). V prvních minutách hlídat fallback/chyby v logu.
4. **Souhrny kapitol** stejně: `enrich_chapters.py --llm-url http://localhost:8000/v1
   --model qwen3-30b-a3b --workers 4 --max-model-len 32768 --priority 3` (víc vláken až po dokončení chunků).
5. **Export**: `make kindlify-sync` nanečisto, pak `--write --commit` na `main` Kindlify.

## Rizika

- **Souběh se SPARKem**: noční `library-enrich` / `library-chapters` berou
  stejnou frontu. Upsert je idempotentní, ale práce se zdvojí — na dobu běhu
  na L40S noční služby pozastavit přes broker, nebo L40S pustit `--order depth`
  od konce.
- **JODA** (2 jádra) a zápisy: ~10 zápisů/s je v pohodě, hlídat `load`.
- **Velikost appky**: 94 bundlů = 18 MB assetů (s textem a listy u malých děl); ~1 000 děl P2
  odhadem +60–100 MB. Pro P2 rozhodnout o stahování bundlů (endpoint
  `/works/{id}/bundle.json` v `rag/server.py`) místo assetů.

# PLAN — knihovní Chroma ze JODY na SPARK

Zadání pro implementaci (Claude Sonnet nebo ruční provedení). Cíl: ulevit
JODĚ, které dochází paměť, přesunem **vektorového indexu knihovny** na SPARK.
Postgres zůstává na JODĚ (fáze C je jen připravená volba, ne součást tohohle
běhu). Následník `PLAN-joda-chroma.md` — ten dal Chromu na JODU, tenhle ji
odtamtud bere zpátky, a říká proč.

Všechna čísla níž jsou **naměřená 12. 9. 2026**, ne odhadnutá. Kde je něco
rozhodnutí místo faktu, je to označené.

---

## 0. Co se děje a proč zrovna Chroma

### JODA dochází paměť — ověřeno

x86_64, 2 jádra, **3,8 GB RAM**. `free -m`: swap **3577 / 3879 MB (92 %)**,
available kolísá 0–1,7 GB podle zátěže. OOM kill zatím žádný (dmesg čistý),
ale 11. 9. večer při procházení galerie stroj thrashoval (load 11 při 90 %
idle CPU) a `finetune.ol1n.com` přestal odpovídat — tunel logoval
`timeout: no recent network activity` na origin, ne pád služby.

Kdo drží swap (`/proc/*/status` VmSwap → kontejner):

| MB ve swapu | proces | kontejner | poznámka |
|---|---|---|---|
| 1 111 | uvicorn | `finetune-wd14` | WD14 tagger, 20 MB resident — model načtený, sedí ve swapu |
| **896** | chroma | **`library_chroma` :8007** | produkční index Knihovníka, `mem_limit: 1g` |
| **419** | chroma | **`swarm-chromadb` :8006** | legacy kolekce `books`, nic ji nepoužívá |
| 225 | Plex | `plex` | |
| 154 | uvicorn | `mangabot` | |

### Co na JODĚ opravdu běží pro knihovnu (opravuje dřívější domněnky)

Živý Knihovník (`library-chat.service` na SPARKu, `rag/.env`) jede **v PG
režimu**: `CHROMA_URL=http://192.168.88.88:8007`, `COLLECTION=books_v2`,
`PG_DSN` → JODA :5433. `curl localhost:8090/status` na SPARKu:

```
mode=pg  collection=books_v2  documents=231526  works=1173
pg: chunks=164344  chapters=40721  enriched=12093 (7,4 %)
gloss_collection=null   ← books_gloss NEEXISTUJE, kanál glos je vypnutý
```

Tedy:

- **`library_chroma` (:8007, `books_v2`, 231 526 pasáží, 3,1 GB na SSD)** —
  produkce. Tohle se stěhuje.
- **`library_postgres` (:5433, 1,5 GB db, 2,3 GB na disku)** — produkce,
  `mem_limit 768m`, sedí na 281 MB. Zůstává (fáze C je volitelná).
- **`swarm-chromadb` (:8006, `books`, 41 232 záznamů, 1 GB na HDD)** —
  legacy podle README („režim bez PG"); SwarmBattle stack (`swarm-rag`)
  je 2 měsíce dole. Server ji má jako *výchozí* hodnotu v `server.py`, ale
  `.env` ji přepisuje. Ruší se (fáze B).
- `bipolar_api` a `swarm-chromadb` hlásí `unhealthy` **pět týdnů jen proto,
  že jejich healthcheck volá `curl`, který v obrazu není** (exit −1). Obě
  služby odpovídají 200. Není to signál k vypnutí.

### Mechanismus thrashingu

HNSW index pro 231 526 × 1024 dim × fp32 ≈ **0,95 GB**. `library_chroma` má
`mem_limit: 1g`. Pracovní sada je tedy rovna stropu: v klidu 165 MB, pod
dotazem naměřeno **1 003 MiB / 1 GiB** — kernel mu neustále bere page cache
a index se stránkuje ze swapu. Na 3,8 GB stroji je to čtvrtina RAM pro jednu
službu, která má na SPARKu k dispozici desítky GB.

### Proč re-index a ne kopie dat

- JODA je **x86_64**, SPARK **aarch64**. HNSW segmenty jsou binární
  a přenos přes architektury není zaručený; SQLite část ano, ale to nestačí.
- Vstup je **identický**: `rag/books.jsonl` na SPARKu má 164 344 řádků,
  sha256 `2b6a3096…`, datum 29. 8. 16:06 — přesně soubor, ze kterého vznikl
  index na JODĚ (kontejner založen 29. 8. 15:17). PG hlásí týchž 164 344
  chunků.
- Embedding na SPARKu jede **95 pasáží/s** (fp16 CUDA, `logs/embed_v2.log`)
  → 231 526 pasáží ≈ **41 minut**. Na JODĚ to trvalo dny (proto je tam
  index na SSD, 2 upserty/s z HDD).
- `embed_books.py` je idempotentní a `--reset` založí kolekci čistě.

### Co se NEmění

- **Cloudflare tunely.** Chroma nemá veřejný hostname; je to LAN služba.
  `finetune`, `ugc`, `tgbot`, `bipolar` tunely zůstávají na JODĚ beze změny.
- **Postgres** (v tomhle běhu), **bipolar** stack, **galerie**, **UGC**.
- Kód serveru. Mění se jen `rag/.env` (SPARK i M2) a compose soubory.

### Co tím JODA získá

Fáze A vrátí ~0,9 GB swapu a 1 GiB stropu; fáze B dalších ~0,4 GB swapu.
Dohromady **≈ 1,3 GB z 3,9 GB swapu**. `finetune-wd14` (1,1 GB) je větší
páka než obojí, ale je to jiná služba a jiné rozhodnutí — viz §8.

---

## 1. Předpoklady — ověřit před začátkem

Na SPARKu (`ssh spark`):

```bash
free -g | sed -n 2,3p            # available ≥ 20 GB (12. 9. bylo 42), swap < 8/15
ss -ltn | grep -E ':(8006|8007)\b' && echo OBSAZENO || echo "porty volné"
df -h /home/ol1n | tail -1       # NVMe, 12. 9. 668 GB volných
docker version --format '{{.Server.Version}} {{.Server.Arch}}'   # 29.x arm64
cd ~/deploy/WorldLibraryProject && git status --short && git log --oneline -1
wc -l rag/books.jsonl            # 164344
sha256sum rag/books.jsonl | cut -c1-16   # 2b6a3096ebe99d52
curl -s localhost:8090/status | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["mode"],d["collection"],d["documents"])'
# → pg books_v2 231526   (BEFORE snapshot)
~/deploy/WorldLibraryProject/deploy/spark/rag-schedule.sh selftest >/dev/null && date +%H
```

Re-index a cutover dělat **v denním režimu (06–02)**. V noci běží
`swarm-director` (~91 GiB) a `library-enrich` píše do PG; embedding je malý,
ale nemá smysl soutěžit. Cutover ne přesně ve 02:00 / 06:00, kdy
`rag-schedule` přepíná stacky.

Obraz: `chromadb/chroma:latest` je od **5. 5. 2026** a od té doby se nehnul;
JODA běží jeho amd64 build (image id `a221f3391b5e`). Pro arm64 je to
**tentýž release**, jiný build — digest níž. Verzované tagy jsou jen
`1.5.10.devNNN`, proto se pinuje digest, ne tag.

---

## 2. Fáze A1 — Chroma na SPARKu (bez dotčení produkce)

Nový soubor v repu: `deploy/spark/docker-compose.library.yaml`

```yaml
# Chroma knihovny na SPARKu — přesun z JODY, důvody v PLAN-spark-library-storage.md.
#
#   cd ~/deploy/WorldLibraryProject && docker compose -f deploy/spark/docker-compose.library.yaml up -d
#
# Proč tak, jak to je:
# - image přišpendlený na DIGEST arm64 buildu téhož `latest` (2026-05-05), na
#   kterém běží JODA (:8007 i :8006); klient chromadb 1.5.9 s ním mluví.
#   Verzované tagy jsou jen dev buildy, `latest` by se mohl pohnout pod námi.
# - bind 0.0.0.0:8007: na SPARKu takhle běží i 8080/8090/8188; M2 dělá
#   make eval / chroma-drop po LAN. SPARK není vystavený ven — žádný
#   cloudflared na tenhle port.
# - data na NVMe pod ~/deploy/WorldLibraryProject/chroma-data
#   (stejná konvence jako deploy/joda; docker adresář založí sám)
# - mem_limit 4g: HNSW 231 526 × 1024 × fp32 ≈ 0,95 GB + rezerva. Strop je
#   tu proto, aby Chroma v unified memory nesoutěžila s modely bez hranice,
#   ne proto, že by se do 1 GB nevešla — přesně to byl problém na JODĚ.
# - bez healthchecku: image nemá curl ani python3 (na JODĚ proto :8006 hlásí
#   unhealthy a funguje). Zdraví hlídá heartbeat zvenku a /status serveru.
#   Důsledek: container-healer.timer na SPARKu ho nevidí (stav "none").
services:
  library-chroma:
    image: chromadb/chroma@sha256:bd21353aee6ccdf4a57bd91e6001626826700f3838e1f230d4aae75bfd4889a1
    container_name: library_chroma
    restart: unless-stopped
    environment:
      - IS_PERSISTENT=TRUE
      - ANONYMIZED_TELEMETRY=FALSE
    ports:
      - "0.0.0.0:8007:8000"
    volumes:
      - ${LIBRARY_CHROMA_DATA:-/home/ol1n/deploy/WorldLibraryProject/chroma-data}:/data
    mem_limit: 4g
```

Nasazení a smoke test:

```bash
# M2: commit + push souboru, pak na SPARKu:
ssh spark 'cd ~/deploy/WorldLibraryProject && git pull --ff-only \
  && docker compose -f deploy/spark/docker-compose.library.yaml up -d \
  && sleep 10 && docker ps --filter name=library_chroma --format "{{.Status}}" \
  && curl -sf http://127.0.0.1:8007/api/v2/heartbeat && echo " heartbeat OK"'

# klient, kterým poběží embed i server, s ním musí mluvit:
ssh spark 'cd ~/deploy/WorldLibraryProject/rag && .venv/bin/python3 -c "
import chromadb; c=chromadb.HttpClient(host=\"127.0.0.1\", port=8007)
print(\"kolekce:\", [x.name for x in c.list_collections()])"'
# → kolekce: []
```

Když heartbeat neprojde: `docker logs library_chroma`. Když projde heartbeat,
ale klient ne → nesedí verze klienta a serveru; **nepokračovat**, digest
zkontrolovat proti JODĚ (`docker inspect library_chroma --format
'{{.Image}}'` na obou).

---

## 3. Fáze A2 — re-index (≈ 41 min, produkce dál jede z JODY)

Na SPARKu, z `rag/`. `rag/.env` se **nemění** — Makefile bere proměnné
z příkazové řádky přednostně, takže server dál čte z JODY a embed píše na
SPARK:

```bash
ssh spark
cd ~/deploy/WorldLibraryProject/rag
nohup make embed CHROMA_URL=http://127.0.0.1:8007 COLLECTION=books_v2 \
  EMBED_FLAGS=--reset > logs/embed_spark.log 2>&1 &
tail -f logs/embed_spark.log      # očekávej ~95 pasáží/s
```

`--reset` je tu bezpečné: kolekce na SPARKu je nová a prázdná; na JODĚ se
nic nedotýká. `EMBED_FLAGS` v Makefile nese jen `--embed-url`, když je
nastavený — tady je prázdný, takže se přes něj předá `--reset`.

Hotovo, když:

```bash
ID=$(curl -s http://127.0.0.1:8007/api/v2/tenants/default_tenant/databases/default_database/collections \
     | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["id"])')
curl -s http://127.0.0.1:8007/api/v2/tenants/default_tenant/databases/default_database/collections/$ID/count
# → 231526   (přesně; JODA :8007 má totéž)
du -sh ~/deploy/WorldLibraryProject/chroma-data     # ≈ 3 GB
```

Jiný počet = re-index nedoběhl nebo vstup není ten správný (sha výš).
Nepokračovat, dokud nesedí.

**`books_gloss` schválně nestavět.** Živý server má `gloss_collection: null`,
kanál glos je vypnutý. `make embed-gloss` by ho zapnul a změnil retrieval —
to je samostatná změna s vlastním evalem, ne součást přesunu.

---

## 4. Fáze A3 — cutover (≈ 15 min, jediný okamžik, kdy se produkce dotkne)

```bash
ssh spark
cd ~/deploy/WorldLibraryProject/rag
cp .env .env.bak-joda                                    # rollback
sed -i 's#^CHROMA_URL=.*#CHROMA_URL=http://127.0.0.1:8007#' .env
grep -E '^(CHROMA_URL|COLLECTION)=' .env                 # 127.0.0.1:8007, books_v2
systemctl --user restart library-chat && sleep 25
curl -s localhost:8090/status | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["mode"],d["collection"],d["documents"],d["pg"]["chunks"])'
# → pg books_v2 231526 164344
```

Latence — srovnat s BEFORE (12. 9.: `wu-wej` 2,1 s, `Milinda` 6,7 s;
větší část je LLM, ne Chroma, takže čekej podobné nebo mírně lepší):

```bash
for q in wu-wej Milinda; do /usr/bin/time -f "  %es $q" curl -s -o /dev/null "localhost:8090/search?q=$q"; done
```

Eval parity — tentýž běh, který je uložený jako `v2-hybrid-golden2.json`
(mode `hybrid`, top_k 5, `golden_v2`): očekávej **work_hit_rate 1.0,
group_hit_rate 0.923**. Menší číslo = index není ekvivalentní, vrátit se
na JODU (rollback níž) a hledat proč.

```bash
.venv/bin/python3 eval/eval_retrieval.py --retrieve-mode hybrid --collection books_v2 \
  --golden eval/golden_v2.jsonl --chroma-url http://127.0.0.1:8007 \
  --compare eval/results/v2-hybrid-golden2.json
```

End-to-end přes Ol1nLLM cestu (SSE, s citacemi):

```bash
curl -s -N -X POST localhost:8090/chat/stream -H 'Content-Type: application/json' \
  -d '{"message":"Co říká Tao te ťing o vodě?"}' | head -c 600
```

Na M2 (`rag/.env` je mimo git):

```bash
sed -i '' 's#^CHROMA_URL=.*#CHROMA_URL=http://192.168.88.66:8007#' rag/.env
curl -sf http://192.168.88.66:8007/api/v2/heartbeat && echo LAN OK
```

Když M2 heartbeat neprojde a na SPARKu lokálně ano → firewall na SPARKu
(`sudo ufw status`); ostatní LAN porty (8090, 8188) procházejí, takže to
není očekávané.

**Rollback A3** (sekundy, bez ztráty dat):

```bash
ssh spark 'cd ~/deploy/WorldLibraryProject/rag && cp .env.bak-joda .env && systemctl --user restart library-chat'
```

JODA :8007 celou dobu běží a má stejný index — proto se vypíná až v A4.

---

## 5. Fáze A4 — po 24 h stabilního provozu: uvolnit JODU

Teprve tady se JODĚ uleví. **`stop` jedné služby, ne `down` celého compose**
— ten soubor definuje i `library-postgres` a `library-backup`; `down` by
shodil produkční Postgres.

```bash
ssh joda
cd ~/deploy/WorldLibraryProject/deploy/joda        # uživatel oli, cesta /home/oli
docker compose -f docker-compose.library.yaml --env-file .env stop library-chroma
docker ps -a --format '{{.Names}}\t{{.Status}}' | grep -E 'library_'
# library_chroma  Exited (0) …    library_postgres  Up … (healthy)    library_backup  Up …
free -m | sed -n 2,3p                               # swap má klesnout o ~0,9 GB
```

`stop` je trvalý i přes `restart: unless-stopped` (to restartuje jen po
pádu). Data v `/home/oli/deploy/WorldLibraryProject/chroma-data` (3,1 GB)
**nechat dva týdny**, pak smazat a službu z `docker-compose.library.yaml`
odstranit samostatným commitem.

**Rollback A4:** `docker compose … start library-chroma` + rollback A3.

---

## 6. Fáze B — zrušit legacy `swarm-chromadb` :8006 (5 min)

Nikdo ji nepoužívá: kolekce `books` (41 232) je v README označená legacy,
`swarm-rag` je 2 měsíce dole, `.env` na SPARKu i M2 ukazují na :8007. Ověřit
jednou provždy a shodit:

```bash
ssh spark 'grep -rn "8006" ~/deploy/WorldLibraryProject/rag/.env ~/.config/systemd/user/*.service; echo "(prázdné = nic živého)"'
ssh joda 'cd ~/chromadb && docker compose -f docker-compose.swarm.nas.yaml down'
# ten compose má jedinou službu, down je tu bezpečný; /media/storage/chromadb (1 GB) zůstává
```

Kolekce `books` jde kdykoli postavit znovu z `books.jsonl`
(`make embed COLLECTION=books`), takže data nejsou nenahraditelná.

Úklid v repu (jeden commit, po fázi B):

- `rag/README.md` řádky 148–150: JODA → `library_postgres :5433` zůstává,
  Chroma přesunout do řádku SPARK, `swarm-chromadb` vyškrtnout.
- `rag/Makefile` hlavička (řádky 3–5) a výchozí `CHROMA_URL ?=` — nový default
  `http://127.0.0.1:8007` (na SPARKu) je poctivější než mrtvý :8006; M2 má
  svoje v `.env`.
- `rag/.env.example`: komentář u `CHROMA_URL` → SPARK :8007 (LAN
  `192.168.88.66`), zmínku o :8006 smazat.
- `server.py:1155`, `embed_books.py:201`, `eval_retrieval.py:209`: výchozí
  `http://192.168.88.88:8006` → `http://127.0.0.1:8007`. Chování se nemění
  (všude to přepisuje `.env`), jen aby default neukazoval na neexistující
  službu.
- `PLAN-joda-chroma.md`: na začátek řádek „Nahrazeno: Chroma je od 9/2026 na
  SPARKu, viz PLAN-spark-library-storage.md."
- AiStack `.env` na SPARKu má `CHROMADB_URL=http://192.168.88.88:8006` pro
  SwarmBattle — stack je dole, nechat, jen vědět.

---

## 7. Fáze C — Postgres na SPARK (VOLITELNÉ, samostatné go/no-go)

Není součást tohohle běhu. Zapsáno, aby se to nemuselo znovu vymýšlet.

**Co by to dalo:** 768 MB stropu na JODĚ; všichni klienti PG (server,
`embed_books --source pg`, `enrich_*`, `load_pg`, `pg_migrate`, eval) jsou
na SPARKu nebo M2 — na JODĚ na něj nesahá nic než záložní sidecar. Hybridní
retrieval (fts kanály) by šel po localhostu.

**Co by to stálo:** SPARK se restartuje kvůli GPU práci častěji než JODA —
ale Knihovník je na SPARKu taky, takže výpadek je stejný. Zálohy nesmí ležet
jen na NVMe SPARKu; `library_backup` píše do `/media/storage/library-pg-backups`
na 17TB poolu (denně 5:30, ~165 MB gz, 14 dní/4 týdny/3 měsíce) — na SPARKu by
musel dump padat lokálně a rsyncovat se na JODU.

**Jak (kdyby):** `postgres:16-alpine` je multi-arch. Datový adresář se
mezi x86_64 a aarch64 **nekopíruje** — `pg_dump`/`pg_restore`, k čemuž
existující denní dump stačí (nebo čerstvý `pg_dump -Fc`). Pořadí: zastavit
`library-enrich` (noc) a `library-chat`, dump, na SPARKu compose se stejnými
`-c` flagy ale `shared_buffers` ~1 GB, restore, `make pg-migrate` →
`--status` (schema_migrations musí sedět, 4 migrace), `PG_DSN` v `rag/.env`
na SPARKu i M2, start služeb, `/status` → `pg.chunks=164344`,
`pg.enriched=12093`, eval jako v A3. `initdb/00_extensions.sql` (pg_trgm,
unaccent) běží při založení db automaticky.

---

## 8. Mimo rozsah, ale rozhodnout

- **`finetune-wd14`: 1,1 GB ve swapu, 20 MB resident.** Největší jednotlivý
  žrout swapu na JODĚ, WD14 tagger galerie — model načtený, nepoužívaný.
  Kandidát na „zastavit a spouštět na vyžádání" nebo na přesun na SPARK
  (GPU by mu slušelo víc než 2 jádrům JODY). Jiná služba, jiný plán.
- **Fantomové healthchecky** (`bipolar_api`, `swarm-chromadb`; po přesunu
  totéž u `library_chroma`): obrazy nemají curl ani python3, healthcheck
  proto nejde opravit uvnitř. Buď ho z compose vyhodit (ať `unhealthy`
  znamená něco), nebo hlídat zvenku. Zatím jen vědět, že červená tam
  nic neznamená.

---

## 9. Definice hotovo

- [ ] `library_chroma` běží na SPARKu z pinovaného digestu, `count` = 231 526
- [ ] `/status` na SPARKu: `mode=pg`, `documents=231526`, `pg.chunks=164344`
- [ ] eval `hybrid` / `golden_v2`: work_hit 1.0, group_hit 0.923
- [ ] Ol1nLLM Knihovník odpovídá s citacemi (SSE test výš)
- [ ] M2 `rag/.env` míří na `192.168.88.66:8007`, heartbeat z M2 prošel
- [ ] po 24 h: `library_chroma` na JODĚ `stop`, `library_postgres` dál healthy
- [ ] `swarm-chromadb` down, `/media/storage/chromadb` ponechán
- [ ] JODA `free -m`: swap klesl o ≥ 1 GB proti 3 577 MB
- [ ] README, Makefile, `.env.example`, defaulty v `.py`, `PLAN-joda-chroma.md` aktualizované
- [ ] `rag/.env.bak-joda` na SPARKu smazán až po A4

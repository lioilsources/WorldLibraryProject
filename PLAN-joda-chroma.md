# PLAN — ChromaDB na JODA

Plán pro Claude Code (nebo ruční provedení) na JODA — Ubuntu server
s Dockerem, `192.168.88.88`. Cíl: ChromaDB v server režimu na portu
**8006**, dostupná z LAN pro SPARK (embedding, chatbot) a M2.

Kontext: jediná role JODY v knihovním chatbotu. Vektory jde kdykoli
znovu postavit z `books.jsonl` na M2 (`embed_books.py` je idempotentní),
takže data tady nejsou nenahraditelná. Compose soubor je z AiStack repa
(`deploy/docker-compose.swarm.nas.yaml`); SPARK nemá ssh klíč na JODA,
proto se tenhle plán spouští přímo tady.

---

## Fáze 1 — prerekvizity

```bash
docker --version && docker compose version   # compose v2 plugin
sudo systemctl enable --now docker           # autostart po rebootu
ip -4 addr | grep 192.168.88.88              # ověřit, že tohle je JODA
```

## Fáze 2 — compose soubor

```bash
mkdir -p ~/chromadb && cd ~/chromadb
# JODA nemá klon AiStacku — stáhnout jen ten jeden soubor:
curl -fsSL -o docker-compose.swarm.nas.yaml \
  https://raw.githubusercontent.com/lioilsources/AiStack/main/deploy/docker-compose.swarm.nas.yaml
```

(Alternativa: `git clone --depth 1 https://github.com/lioilsources/AiStack`
a použít `AiStack/deploy/docker-compose.swarm.nas.yaml`.)

## Fáze 3 — konfigurace

```bash
# .env vedle compose souboru — CHROMADB_DATA nastavit PŘED prvním startem,
# pozdější přesun = ruční kopírování adresáře
cat > .env <<'EOF'
CHROMADB_DATA=/data/chromadb
# CHROMA_TOKEN=<tajný-token>   # odkomentovat jen při zapnutí auth v compose
EOF
sudo mkdir -p /data/chromadb
```

Volitelná auth: v compose souboru jsou `CHROMA_SERVER_AUTHN_*` řádky
zakomentované — v domácí LAN za firewallem netřeba; při odkomentování
musí SPARK posílat token (úprava `rag/embed_books.py`/`server.py` zatím
není — nechat vypnuté, dokud nebude potřeba).

## Fáze 4 — start + verifikace

```bash
docker compose -f docker-compose.swarm.nas.yaml up -d
docker ps --filter name=swarm-chromadb        # running, healthy po ~20 s
curl -sf http://localhost:8006/api/v2/heartbeat && echo OK
```

**[ČLOVĚK/SPARK nebo M2]** ověřit dostupnost přes LAN:
`curl -sf http://192.168.88.88:8006/api/v2/heartbeat`
Pokud neprojde a lokálně ano → firewall na JODA (`sudo ufw allow 8006/tcp`
pokud je ufw aktivní).

## Údržba

```bash
docker logs -f swarm-chromadb                  # logy
docker compose -f docker-compose.swarm.nas.yaml restart
docker compose -f docker-compose.swarm.nas.yaml down   # data v /data/chromadb přežijí
du -sh /data/chromadb                          # velikost vektorů
```

## Rollback / reset

- Kontejner: `docker compose ... down` — data zůstávají.
- Jen kolekce `books` (bez dotčení SwarmBattle dat): ze SPARKu
  `python3 embed_books.py --reset --input books.jsonl`.
- Úplný reset: `down`, smazat `/data/chromadb`, `up -d`, na SPARKu
  znovu `make embed`.

## Pasti

1. Kontejner uvnitř poslouchá na 8000, ven je mapovaný **8006** — v LAN
   URL vždy `:8006`.
2. Bind je `0.0.0.0` schválně (SPARK/M2 přes síť) — nezužovat na
   127.0.0.1, tím by se odřízl SPARK.
3. `restart: unless-stopped` je v compose — po rebootu JODY se Chroma
   zvedne sama, ale jen pokud je docker `enabled` (fáze 1).
4. Healthcheck v compose používá `curl` uvnitř kontejneru — u novějších
   chroma images bez curlu může hlásit unhealthy, i když API funguje;
   rozhodující je heartbeat zvenku.

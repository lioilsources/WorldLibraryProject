#!/usr/bin/env bash
# Rozvrh SPARKu — profily, v každém okně jen to, co se do 121,7 GiB unified
# vejde se zachováním pravidel z incidentů (AiStack/PLAN-spark-scheduler.md):
# ComfyUI render nikdy vedle LLM ≥ 30 GiB (29. 9. 2026 CPU render + přehřátí),
# kontejnery jen `docker stop`, nikdy `compose down` (30. 9. tím zmizel
# qwen36-agent a promo okno zůstalo bez modelu), vLLM chce util × total volných.
#
#   comfy    (07–13)  ComfyUI + audio (~52+25 GiB) + flux-schnell NIM (17); žádný
#                     velký LLM. Experimenty uživatele (Ol1nLLM appka, lab),
#                     StoryTeller, Kirian, Stickers, tributy PromoClowna (12:30).
#   rag      (13–19)  denní směna directora — stejné dávky jako v noci.
#   llm      (19–01)  qwen36-agent (36,5) + flux-schnell NIM (17): chat Právníka
#                     a Knihovníka, ToyShaders, tier 0 obrázky, PromoClown
#                     (heartbeat 19:05–00:55). qwen36 tu běží celý večer.
#   gemma    (ručně / fronta)  jako llm, ale místo qwen36 Gemma-4 (util 0,40):
#                     agent Právníka na vyžádání. Zpět `rag-schedule.sh llm`.
#   rag      (01–07)  swarm-director 0.75 (91 GiB) sám; obohacení Knihovníka,
#                     souhrny kapitol pro Kindlify a dávka StoryTelleru
#                     (DIRECTOR_JOBS), sloty 6 + 4 + 2, chat 4.
#
# Okna 2026-10-01 odpoledne (uživatel): ComfyUI nikdo nepotřebuje denně, director
# má frontu na desítky hodin → comfy jen 07–13, director i přes den 13–19.
# AiSwarmBattle a Aukrofy jsou cold (PLAN §6) — Nano, embed a swarm-litellm
# proto v profilu llm nejsou, rozvrh je jen zastavuje.
#
# translate (Qwen3-32B TRT-LLM) vypadl z rozvrhu (2026-10-01, uživatel): bench
# AiStack/PLAN-model-bench.md §6a ho ve všem předčí qwen36 (6× rychlejší, 64k
# kontext, nástroje, JSON). Chat knihovny jde v LiteLLM řetězem
# translate → openclaw-default (qwen36) → swarm-director → fallback, takže
# odpovídá v každém okně.
#
# Proč v noci director: obohacení se zapéká do databáze NATRVALO, rozhoduje
# kvalita (A/B na 30 chuncích: translate napsal o Beowulfovi „staroslovanský
# epos", director „anglosaský").
#
# Volá se z rag-schedule.service (timer 01, 07, 13, 19 h + po bootu). Ručně:
#   ~/deploy/WorldLibraryProject/deploy/spark/rag-schedule.sh comfy|llm|gemma|rag|auto
#   (synonyma: day = comfy, night = director = rag, promo = llm)
# Vypnout rozvrh:  systemctl --user stop rag-schedule.timer
#
# `auto` odvodí režim z hodin, takže timer smí mít Persistent=true —
# po restartu stroje ve 3 ráno se srovná do nočního režimu, ne do denního.
#
# Kontrola logiky bez zásahu do stroje:  rag-schedule.sh selftest

set -euo pipefail

AISTACK="${AISTACK:-$HOME/deploy/AiStack}"
# Rozvrh „hodina:režim“ — okno trvá do začátku dalšího (přes půlnoc taky).
SCHEDULE="${SCHEDULE:-1:rag 7:comfy 13:rag 19:llm}"
# Kontejnery AiStacku mimo tenhle rozvrh, které se v noci musí uhnout:
# audio-music + audio-sfx (nasazené 7. 9. 2026) drží ~25 GiB a director
# (0.75 × 121,7 = 91,3 GiB) se vedle nich nevejde — 8. 9. 00:13 padal
# v restart-loopu na „Free memory 75 GiB < 91 GiB".
AUDIO_CONTAINERS="${AUDIO_CONTAINERS:-audio-music audio-sfx}"
# qwen36-agent (ClownPROMO, vLLM --gpu-memory-utilization 0.30 = 36,5 GiB
# zabraných bez ohledu na zátěž) — 15. a 16. 9. 2026 v tomhle seznamu chyběl
# a director se vedle něj dvě noci po sobě nevešel. Promo běží jen ve svém
# okně, přes den ani při obohacení ho nikdo nepotřebuje.
AGENT_CONTAINERS="${AGENT_CONTAINERS:-qwen36-agent}"
# flux-schnell (NIM, ~17 GiB) naběhne s ComfyUI v comfy okně a nikde se
# nezastavuje — 17.–18. 9. 2026 tak přes noc mlel naprázdno a director se
# vedle něj (89 GiB < potřebných 92) dvě noci nevešel. Startuje ho něco
# mimo tenhle skript (spolu s comfyui službou), tady se jen ruší před rag.
FLUX_CONTAINERS="${FLUX_CONTAINERS:-flux-schnell}"
# Kontejnery cold projektů (AiSwarmBattle): v žádném profilu, rozvrh je jen
# zastavuje. Až se AiSwarmBattle odblokuje, vrátit do llm přes swarm compose
# s --no-deps (Nano util 0.22 — s 0.15 od vLLM 0.21 nemá KV cache).
# Gemma (profil gemma) je jednorázový kontejner z AiStack bench/serve.sh.
LLM_CONTAINERS="${LLM_CONTAINERS:-swarm-nano swarm-embed swarm-litellm}"
GEMMA_CONTAINERS="${GEMMA_CONTAINERS:-bench-gemma}"
# Noční dávky na directoru — každá je systemd --user služba, která se sama
# dokončí / resumuje; workery v součtu 12, aby chatu zbyly 4 sloty z 16.
DIRECTOR_JOBS="${DIRECTOR_JOBS:-library-enrich library-chapters storyteller-night}"

log() { printf '%s  %s\n' "$(date '+%F %T')" "$*"; }
HERE="$(cd "$(dirname "$0")" && pwd)"
# Selhání se hlásí Clown botem (notify.sh) — 15. a 16. 9. 2026 spadl noční
# režim dvě noci po sobě a přišlo se na to náhodou. Jednotka má k tomu ještě
# OnFailure=, tohle je navíc s důvodem v lidské řeči.
notify() { "$HERE/notify.sh" "$@" >/dev/null 2>&1 || true; }
fail() { log "CHYBA: $*"; notify "❌ <b>rag-schedule</b> ($mode): $*"; exit 1; }

# Director chce při startu volné DIRECTOR_GPU_UTIL × celkem (vLLM jinak
# odmítne start a docker ho točí v restart-loopu; smoke test pak vidí jen
# prázdnou odpověď). Zkontrolovat dřív a říct, kdo paměť drží — 15. a 16. 9.
# 2026 to byl qwen36-agent (0.30 × 121,7 = 36,5 GiB), který v seznamu
# kontejnerů k uhnutí nebyl, a dvě noci selhaly bez jediného slova o paměti.
memory_check() {
  local util="${DIRECTOR_GPU_UTIL:-0.75}" total avail need holders comfy
  # awk musí tisknout \n: bez ní read narazí na EOF bez řádku a vrátí 1,
  # i když total/avail vyplnil správně — pod set -e to potichu, beze
  # slova, zabije celý skript. 17. a 18. 9. 2026 dvě noci za sebou spadlo
  # okno rag hned na startu, bez jediného CHYBA/log řádku navíc.
  read -r total avail < <(awk '/^MemTotal:/ {t=$2} /^MemAvailable:/ {a=$2} END {printf "%d %d\n", t/1048576, a/1048576}' /proc/meminfo)
  need=$(awk -v u="$util" -v t="$total" 'BEGIN {printf "%d", u * t + 2}')
  if [ "$avail" -ge "$need" ]; then
    log "paměť: k dispozici ${avail} GiB, director chce ${need} GiB — ok"; return 0
  fi
  # `|| true` na obou: grep bez zásahu (pipefail) a `is-active` na neběžící
  # službu (vrací 3) jsou tu OČEKÁVANÝ výsledek, ne chyba — pod set -e bez
  # toho umřou potichu úplně stejně jako řádek s read výš, jen o pár řádků
  # dál a bez jediného CHYBA hlášení. Přesně to se stalo 18. 9. 2026 01:05.
  holders="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E 'translate|director|qwen|agent|audio|comfy|nano|embed|gemma|flux' | sort | paste -sd ', ' -)" || true
  comfy="$(systemctl --user is-active comfyui 2>/dev/null)" || true
  fail "director se nevejde: k dispozici ${avail} GiB, potřebuje ${need} GiB (util ${util} × ${total}). Drží: kontejnery ${holders:-žádné}; comfyui ${comfy}"
}

# Okno buď přechází půlnoc (22→08), nebo ne (02→06) — a plete se to snadno:
# s naivním `h >= NIGHT_START || h < DAY_START` by okno 02–06 platilo i ve
# 13:00 a stroj by v nočním režimu uvízl napořád.
# Health check nestačí: 3. 9. 2026 director na /v1/models odpověděl 200,
# ale generoval degenerovaný text („ÚСудиСудиСуди…") a 500. Za celé noční
# okno vzniklo 16 chunků místo ~1 550. Krátký dotaz na JSON to odhalí —
# rozbitá instance vrátí buď smetí, nebo nic.
smoke_test() {
  local port="$1" name="$2" out
  out=$(curl -s -m 120 "http://localhost:${port}/v1/chat/completions" \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"${name}\",\"messages\":[{\"role\":\"user\",\"content\":\"Vrať jen JSON {\\\"a\\\":1}\"}],\"max_tokens\":200,\"temperature\":0,\"chat_template_kwargs\":{\"enable_thinking\":false}}" \
        | python3 -c 'import sys,json
try:
    d = json.load(sys.stdin)
    t = (d["choices"][0]["message"]["content"] or "").strip()
except Exception as e:
    print("CHYBA " + str(e)[:60]); raise SystemExit
# rozbitá instance vrací dlouhou smyčku opakovaného tokenu
print("OK " + t[:60] if len(t) < 200 and "\"a\"" in t and "1" in t else "SMETI " + t[:60])' 2>&1)
  case "$out" in
    OK*) log "$name smoke test ok"; return 0 ;;
    *)   log "$name smoke test SELHAL: ${out:0:120}"; return 1 ;;
  esac
}

# Ani smoke test nestačí: 3.–8. 9. 2026 director krátký prompt zvládl, ale na
# obohacovacím promptu kazil ~0,4 % tokenů (FULL CUDA grafy na GB10) — smyčky
# „Суди…", EOS uprostřed věty, překlepy v klíčích. Sonda posílá skutečný
# prompt na 4 chunky × 3 varianty a musí projít všech 12 (rag/probe_llm.py).
RAG="${RAG:-$HOME/deploy/WorldLibraryProject/rag}"
probe_test() {
  local out
  if out=$( cd "$RAG" && .venv/bin/python3 probe_llm.py --limit 4 --workers 6 2>&1 ); then
    log "sonda ok: $(printf '%s\n' "$out" | tail -1)"; return 0
  fi
  log "sonda SELHALA: $(printf '%s\n' "$out" | grep -E 'fin=|EXC|HOTOVO|Traceback' | grep -v 'parse=OK' | head -3 | tr '\n' ' ' | cut -c1-300)"
  return 1
}

# Hodina → okno. Hodina patří do okna s nejpozdějším začátkem ≤ hodina; před
# prvním začátkem dne do posledního okna (to přešlo půlnoc). Proto je jedno,
# jestli a které okno přes půlnoc přechází.
mode_for_hour() {  # hodina "h:režim h:režim …"
  local h="$1" best="" best_start=-1 last="" last_start=-1 pair name start
  for pair in $2; do
    start="${pair%%:*}"; name="${pair##*:}"
    if [ "$start" -gt "$last_start" ]; then last="$name"; last_start="$start"; fi
    if [ "$h" -ge "$start" ] && [ "$start" -gt "$best_start" ]; then best="$name"; best_start="$start"; fi
  done
  echo "${best:-$last}"
}

if [ "${1:-}" = selftest ]; then
  fail=0
  check() { # hodina rozvrh očekávané
    got=$(mode_for_hour "$1" "$2")
    [ "$got" = "$3" ] || { echo "CHYBA: h=$1 rozvrh '$2' → $got, čekáno $3"; fail=1; }
  }
  S="1:rag 7:comfy 13:rag 19:llm"   # ostrý rozvrh
  for h in 7 8 12; do check $h "$S" comfy; done
  for h in 13 16 18; do check $h "$S" rag; done
  for h in 19 22 23 0; do check $h "$S" llm; done
  for h in 1 3 6; do check $h "$S" rag; done
  # pořadí v řetězci nehraje roli; okno přes půlnoc smí být kterékoli
  check 0 "19:llm 7:comfy 1:rag 13:rag" llm
  check 0 "7:comfy 17:llm 23:rag" rag; check 23 "7:comfy 17:llm 23:rag" rag; check 22 "7:comfy 17:llm 23:rag" llm
  [ $fail = 0 ] && echo "rag-schedule.sh: selftest ok"
  exit $fail
fi

mode="${1:-auto}"
if [ "$mode" = auto ]; then
  h=$(date +%-H)
  mode=$(mode_for_hour "$h" "$SCHEDULE")
  log "auto → $mode (je ${h}:xx; rozvrh $SCHEDULE)"
fi

# Čeká, až model zase odpovídá — bez toho by chat i obohacení chvíli mlely
# naprázdno a LiteLLM by tiše přepadl na fallback.
wait_endpoint() {
  local port="$1" name="$2"
  for _ in $(seq 1 40); do
    sleep 15
    if curl -sf -m 5 "http://localhost:${port}/v1/models" >/dev/null 2>&1; then
      log "$name odpovídá"; return 0
    fi
  done
  log "POZOR: $name do 10 min nenaběhl"; return 1
}

# --- skupiny služeb ------------------------------------------------------------
# Vše jen `docker stop` / `systemctl stop`: kontejnery AiStacku musí zůstat
# existovat, protože se tady jen startují (`docker start`). Výjimkou jsou
# director a translate, které se staví přes make (compose up s profilem).
stop_director() {
  local j; for j in $DIRECTOR_JOBS; do systemctl --user stop "$j" 2>/dev/null || true; done
  ( cd "$AISTACK" && make down-swarm-director >/dev/null 2>&1 ) || true
}
stop_llm() {
  docker stop $AGENT_CONTAINERS $LLM_CONTAINERS >/dev/null 2>&1 || true
  docker rm -f $GEMMA_CONTAINERS >/dev/null 2>&1 || true   # jednorázový docker run, ne compose
}
stop_comfy() {
  systemctl --user stop comfyui || true
  docker stop $AUDIO_CONTAINERS $FLUX_CONTAINERS >/dev/null 2>&1 || true
}
stop_translate() { ( cd "$AISTACK" && make down-translate >/dev/null 2>&1 ) || true; }

admit() {  # kontejner util — AiStack mem-admit (rezerva 2 GiB), jinak fail s důvodem
  ( cd "$AISTACK" && scripts/mem-admit.sh "$1" "$2" ) || fail "$1 se nevejde do paměti (util $2)"
}
swarm_up() {  # služby swarm compose bez závislostí; embed je v profilu embed
  ( cd "$AISTACK" && docker compose -f deploy/docker-compose.swarm.yaml --env-file .env --profile embed \
      up -d --no-deps "$@" >/dev/null 2>&1 )
}
qwen36_up() {
  admit qwen36-agent 0.30
  # kontejner smí chybět (30. 9. ho `make down-agent` smazal) — pak ho postaví compose
  docker start $AGENT_CONTAINERS >/dev/null 2>&1 || ( cd "$AISTACK" && make up-agent >/dev/null )
  wait_endpoint 8040 qwen36-agent || fail "qwen36-agent nenaběhl — večerní okno bez modelu"
}
flux_up() {  # tier 0 obrázky (gen-queue /nim/flux-schnell); TensorRT si paměť bere celou
  # při startu a nepadá na CPU, proto smí vedle qwen36 — vedle directora ne (25. 9. pád stroje)
  local avail; avail=$(awk '/MemAvailable/ {printf "%d", $2/1048576}' /proc/meminfo)
  if [ "$avail" -lt 22 ]; then
    notify "⚠️ <b>rag-schedule</b> ($mode): flux-schnell se nevejde (volno ${avail} GiB) — tier 0 obrázky nepoběží"; return 0
  fi
  docker start $FLUX_CONTAINERS >/dev/null 2>&1 || ( cd "$AISTACK" && make up-image-schnell >/dev/null 2>&1 ) \
    || notify "⚠️ <b>rag-schedule</b> ($mode): flux-schnell nenaběhl"
}

case "$mode" in
  comfy|day)
    log "režim comfy: LLM i director dole, ComfyUI, audio a flux-schnell nahoru"
    stop_director; stop_llm; stop_translate
    systemctl --user start comfyui
    docker start $AUDIO_CONTAINERS >/dev/null 2>&1 || true
    flux_up
    ;;
  llm|promo)
    log "režim llm: ComfyUI, audio a director dole, qwen36 + flux-schnell nahoru"
    systemctl --user stop comfyui || true
    docker stop $AUDIO_CONTAINERS $LLM_CONTAINERS >/dev/null 2>&1 || true
    stop_director; stop_translate
    docker rm -f $GEMMA_CONTAINERS >/dev/null 2>&1 || true
    sleep 5
    qwen36_up
    flux_up
    ;;
  gemma)
    # Agent Právníka na vyžádání (uživatel 2026-10-01): místo qwen36 Gemma-4,
    # util 0.40 — při 0.30 měla jen 9k tokenů KV cache a 12k dotaz nepřijala.
    log "režim gemma: qwen36 dole, Gemma-4 + flux-schnell nahoru (agent Právníka)"
    systemctl --user stop comfyui || true
    docker stop $AUDIO_CONTAINERS $LLM_CONTAINERS $AGENT_CONTAINERS >/dev/null 2>&1 || true
    stop_director; stop_translate
    sleep 5
    ( cd "$AISTACK" && GEMMA_UTIL=0.40 bench/serve.sh gemma >/dev/null ) || fail "Gemma se nevešla nebo nenaběhla"
    for _ in $(seq 1 60); do
      docker logs bench-gemma 2>&1 | grep -q "Application startup complete" && { log "Gemma odpovídá"; break; }
      sleep 15
    done
    docker logs bench-gemma 2>&1 | grep -q "Application startup complete" || fail "Gemma do 15 min nenaběhla"
    flux_up
    ;;
  rag|night|director)
    log "režim rag: ComfyUI, LLM profil, audio a flux-schnell dole, director nahoru, dávky jedou"
    stop_comfy; stop_llm; stop_translate
    sleep 5
    memory_check
    ( cd "$AISTACK" && make up-director-night >/dev/null )
    wait_endpoint 8012 swarm-director || true
    if ! smoke_test 8012 swarm-director; then
      log "director naběhl rozbitý — zkouším ho jednou přehodit"
      ( cd "$AISTACK" && make down-swarm-director >/dev/null 2>&1 ) || true
      ( cd "$AISTACK" && make up-director-night >/dev/null )
      wait_endpoint 8012 swarm-director || true
      if ! smoke_test 8012 swarm-director; then
        fail "director je rozbitý i po restartu — noční dávky NESPOUŠTÍM"
      fi
    fi
    if ! probe_test; then
      fail "director generuje poškozené odpovědi — noční dávky NESPOUŠTÍM"
    fi
    for j in $DIRECTOR_JOBS; do
      if systemctl --user cat "$j" >/dev/null 2>&1; then
        systemctl --user start "$j" && log "dávka $j běží"
      else
        log "dávka $j není nainstalovaná, přeskakuji"
      fi
    done
    ;;
  *)
    echo "použití: $0 comfy|llm|gemma|rag|auto (day = comfy, promo = llm, night = director = rag)" >&2; exit 2
    ;;
esac
log "hotovo ($mode)"

#!/usr/bin/env bash
# Denní/noční režim SPARKu — ComfyUI přes den, obohacení korpusu v noci.
#
#   den (06:00)  ComfyUI + translate úsporný (~36 GiB), obohacení stojí
#                → Image Studio i chat mají GPU pro sebe
#   noc (02:00)  ComfyUI i translate dole, nahoru swarm-director (~93 GiB),
#                obohacení korpusu jede na něm
#
# Proč v noci director a ne translate, když je 2,8× pomalejší (6,7 vs 18,9
# chunku/min): obohacení se zapéká do databáze NATRVALO, takže rozhoduje
# kvalita, ne rychlost — a noční okno je stejně prázdné. A/B na 30 chuncích
# v deseti jazycích originálu: translate napsal o Beowulfovi „staroslovanský
# epos" (director „anglosaský") a u Hérodota „kvůli královny dcera Io".
#
# Chat knihovny (library-chat) běží v obou režimech. V noci translate neběží,
# ale LiteLLM má řetěz translate → swarm-director → fallback, takže chat sáhne
# na directora — tedy na LEPŠÍ model, ne na 4B nouzovku.
#
# Volá se z rag-schedule.service (timer 08:00 a 22:00 + po bootu). Ručně:
#   ~/deploy/WorldLibraryProject/deploy/spark/rag-schedule.sh day|night|auto
# Vypnout rozvrh:  systemctl --user stop rag-schedule.timer
#
# `auto` odvodí režim z hodin, takže timer smí mít Persistent=true —
# po restartu stroje ve 3 ráno se srovná do nočního režimu, ne do denního.
# Okno smí, ale nemusí přecházet půlnoc (02–06 i 22–08), viz mode_for_hour.
#
# Kontrola logiky bez zásahu do stroje:  rag-schedule.sh selftest

set -euo pipefail

AISTACK="${AISTACK:-$HOME/deploy/AiStack}"
DAY_START="${DAY_START:-6}"     # hodina, od které platí denní režim
NIGHT_START="${NIGHT_START:-2}"

log() { printf '%s  %s\n' "$(date '+%F %T')" "$*"; }

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

mode_for_hour() {
  local h="$1" night="$2" day="$3"
  if [ "$night" -lt "$day" ]; then          # 02–06, uvnitř jednoho dne
    if [ "$h" -ge "$night" ] && [ "$h" -lt "$day" ]; then echo night; else echo day; fi
  else                                       # 22–08, přes půlnoc
    if [ "$h" -ge "$night" ] || [ "$h" -lt "$day" ]; then echo night; else echo day; fi
  fi
}

if [ "${1:-}" = selftest ]; then
  fail=0
  check() { # hodina noc den očekávané
    got=$(mode_for_hour "$1" "$2" "$3")
    [ "$got" = "$4" ] || { echo "CHYBA: h=$1 okno $2–$3 → $got, čekáno $4"; fail=1; }
  }
  for h in 2 3 5; do check $h 2 6 night; done
  for h in 0 1 6 7 13 21 23; do check $h 2 6 day; done      # okno bez půlnoci
  for h in 22 23 0 3 7; do check $h 22 8 night; done
  for h in 8 12 21; do check $h 22 8 day; done              # okno přes půlnoc
  [ $fail = 0 ] && echo "rag-schedule.sh: selftest ok"
  exit $fail
fi

mode="${1:-auto}"
if [ "$mode" = auto ]; then
  h=$(date +%-H)
  mode=$(mode_for_hour "$h" "$NIGHT_START" "$DAY_START")
  log "auto → $mode (je ${h}:xx, noční okno ${NIGHT_START}–${DAY_START})"
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

case "$mode" in
  day)
    log "denní režim: obohacení stop, director dole, translate úsporný, ComfyUI nahoru"
    systemctl --user stop library-enrich || true
    ( cd "$AISTACK" && make down-swarm-director >/dev/null 2>&1 ) || true
    ( cd "$AISTACK" && make up-translate-lean >/dev/null )
    wait_endpoint 8004 translate || true
    systemctl --user start comfyui
    ;;
  night)
    log "noční režim: ComfyUI i translate dole, director nahoru, obohacení jede"
    systemctl --user stop comfyui || true
    # translate musí pryč DŘÍV, než se pustí director: vLLM odmítne start,
    # když je volné paměti míň než util × total (0.75 = 91 GiB)
    ( cd "$AISTACK" && make down-translate >/dev/null 2>&1 ) || true
    ( cd "$AISTACK" && make up-director-night >/dev/null )
    wait_endpoint 8012 swarm-director || true
    if ! smoke_test 8012 swarm-director; then
      log "director naběhl rozbitý — zkouším ho jednou přehodit"
      ( cd "$AISTACK" && make down-swarm-director >/dev/null 2>&1 ) || true
      ( cd "$AISTACK" && make up-director-night >/dev/null )
      wait_endpoint 8012 swarm-director || true
      if ! smoke_test 8012 swarm-director; then
        log "CHYBA: director je rozbitý i po restartu — obohacení NESPOUŠTÍM"
        exit 1
      fi
    fi
    systemctl --user start library-enrich
    ;;
  *)
    echo "použití: $0 day|night|auto" >&2; exit 2
    ;;
esac
log "hotovo ($mode)"

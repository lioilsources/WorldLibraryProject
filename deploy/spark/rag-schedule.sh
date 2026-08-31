#!/usr/bin/env bash
# Denní/noční režim SPARKu — ComfyUI přes den, obohacení korpusu v noci.
#
#   den (08:00)  ComfyUI + translate úsporný (~36 GiB), obohacení stojí
#                → Image Studio i chat mají GPU pro sebe
#   noc (22:00)  ComfyUI i translate dole, nahoru swarm-director (~100 GiB),
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

set -euo pipefail

AISTACK="${AISTACK:-$HOME/deploy/AiStack}"
DAY_START="${DAY_START:-8}"     # hodina, od které platí denní režim
NIGHT_START="${NIGHT_START:-22}"

log() { printf '%s  %s\n' "$(date '+%F %T')" "$*"; }

mode="${1:-auto}"
if [ "$mode" = auto ]; then
  h=$(date +%-H)
  if [ "$h" -ge "$NIGHT_START" ] || [ "$h" -lt "$DAY_START" ]; then mode=night; else mode=day; fi
  log "auto → $mode (je ${h}:xx)"
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
    # když je volné paměti míň než util × total (0.80 = 97 GiB)
    ( cd "$AISTACK" && make down-translate >/dev/null 2>&1 ) || true
    ( cd "$AISTACK" && make up-director-night >/dev/null )
    wait_endpoint 8012 swarm-director || true
    systemctl --user start library-enrich
    ;;
  *)
    echo "použití: $0 day|night|auto" >&2; exit 2
    ;;
esac
log "hotovo ($mode)"

#!/usr/bin/env bash
# Denní/noční režim SPARKu — ComfyUI přes den, obohacení korpusu v noci.
#
#   den (08:00)  ComfyUI běží, translate v úsporném profilu (~36 GiB),
#                obohacení stojí → Image Studio má GPU pro sebe
#   noc (22:00)  ComfyUI dole, translate plný (57 GiB), obohacení jede
#                na plný plyn (18,9 chunku/min místo 9,7)
#
# Chat knihovny (library-chat) běží v obou režimech — jen v úsporném má
# translate menší KV cache, což jednomu dotazu bohatě stačí.
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

# Čeká, až translate zase odpovídá — bez toho by chat i obohacení chvíli
# mlely naprázdno a LiteLLM by tiše přepadl na fallback.
wait_translate() {
  for _ in $(seq 1 40); do
    sleep 15
    if curl -sf -m 5 http://localhost:8004/v1/models >/dev/null 2>&1; then
      log "translate odpovídá"; return 0
    fi
  done
  log "POZOR: translate do 10 min nenaběhl"; return 1
}

case "$mode" in
  day)
    log "denní režim: obohacení stop, translate úsporný, ComfyUI nahoru"
    systemctl --user stop library-enrich || true
    ( cd "$AISTACK" && make up-translate-lean >/dev/null )
    wait_translate || true
    systemctl --user start comfyui
    ;;
  night)
    log "noční režim: ComfyUI dole, translate plný, obohacení jede"
    systemctl --user stop comfyui || true
    ( cd "$AISTACK" && make up-translate >/dev/null )
    wait_translate || true
    systemctl --user start library-enrich
    ;;
  *)
    echo "použití: $0 day|night|auto" >&2; exit 2
    ;;
esac
log "hotovo ($mode)"

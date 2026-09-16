#!/usr/bin/env bash
# comfyui-cpu-watch.sh — hlídá, kdy ComfyUI počítá na CPU, a hlásí to Clown botem.
#
# Na GB10 sdílí CPU i GPU jednu paměť; když jí je pod `--reserve-vram` (8 GiB),
# ComfyUI model na GPU nenahraje vůbec a zaloguje
#   loaded partially; 0.00 MB usable, 0.00 MB loaded, … MB offloaded
# a render se plazí na CPU (bench hair-r1, 13. 9. 2026: 100 s buňky → 420 s).
# Uživatel appky vidí jen pomalý render, na stroji to nikdo nevidí. Proto
# tenhle sledovač journalu: každý takový řádek počítá a nejvýš jednou za
# COOLDOWN pošle zprávu s tím, kdo paměť drží (`free`, ComfyUI, běžící
# kontejnery). „loaded partially; 7456 MB usable“ je jen lowvram režim —
# pomalejší, ale na GPU — ten se nehlásí.
#
#   systemctl --user enable --now comfyui-cpu-watch
set -u
COOLDOWN="${COOLDOWN:-3600}"
SIGNATURE='loaded partially; 0.00 MB usable'
here="$(cd "$(dirname "$0")" && pwd)"
last=0 count=0

report() {
  local mem comfy queue containers
  mem="$(free -g | awk '/^Mem:/ {printf "použito %d GiB, volno %d GiB, k dispozici %d GiB", $3, $4, $7}')"
  comfy="$(systemctl --user show comfyui.service -p MemoryCurrent --value 2>/dev/null | awk '{printf "%.0f GiB", $1/1073741824}')"
  queue="$(curl -s -m 5 http://127.0.0.1:8188/queue | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d["queue_running"]), "běží,", len(d["queue_pending"]), "čeká")' 2>/dev/null || echo "?")"
  containers="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E 'translate|director|qwen|agent|audio' | sort | paste -sd ', ' -)"
  "$here/notify.sh" "🐢 <b>ComfyUI počítá na CPU</b> — ${count}× za poslední $((COOLDOWN / 60)) min
${mem}
ComfyUI drží ${comfy:-?}, fronta: ${queue}
kontejnery s modely: ${containers:-žádné}"
  # Do journalu taky: bez toho nejde zpětně říct, jestli hlášení odešlo, nebo
  # jestli hlídač jen nic neviděl — a tichý hlídač je horší než žádný.
  echo "hlášení odesláno: ${count}× CPU render, ${mem}"
}

journalctl --user -u comfyui -f -n 0 -o cat 2>/dev/null | while IFS= read -r line; do
  case "$line" in
    *"$SIGNATURE"*) ;;
    *) continue ;;
  esac
  count=$((count + 1))
  now=$(date +%s)
  if [ $((now - last)) -ge "$COOLDOWN" ]; then
    report
    last=$now
    count=0
  fi
done

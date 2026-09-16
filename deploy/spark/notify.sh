#!/usr/bin/env bash
# notify.sh "zpráva"      — pošle zprávu majiteli přes Clown bota (@ol1n_promo_bot)
# echo "zpráva" | notify.sh
#
# Token a chat id bere z ~/.config/promoclown/openclaw.env (TELEGRAM_AGENT_BOT_TOKEN,
# TELEGRAM_OWNER_ID) — tentýž bot, kterým OpenClaw chatuje; sendMessage z jiného
# procesu polleru nevadí (jen getUpdates smí mít jednoho). Zpráva je HTML,
# nic se neescapuje — volající posílá jen text, který sám napsal, a výpisy
# z journalu jdou do <pre>, kde se '<' a '&' escapují tady.
#
# Nikdy neselže (exit 0): volá se z OnFailure= a z hlídačů, kde by chyba
# notifikace jen zakryla tu původní. Proč vůbec: 15. a 16. 9. 2026 spadl
# noční přepínač dvě noci po sobě a přišlo se na to náhodou o dva dny později.
set -u
env_file="${SPARK_NOTIFY_ENV:-$HOME/.config/promoclown/openclaw.env}"
if [ -r "$env_file" ]; then
  # shellcheck disable=SC1090
  . "$env_file"
fi
: "${TELEGRAM_AGENT_BOT_TOKEN:=}" "${TELEGRAM_OWNER_ID:=}"
if [ -z "$TELEGRAM_AGENT_BOT_TOKEN" ] || [ -z "$TELEGRAM_OWNER_ID" ]; then
  echo "notify.sh: chybí TELEGRAM_AGENT_BOT_TOKEN / TELEGRAM_OWNER_ID v $env_file" >&2
  exit 0
fi

if [ $# -gt 0 ]; then text="$*"; else text="$(cat)"; fi
text="🖥 <b>SPARK</b> · $(date '+%d.%m. %H:%M')
$text"

curl -sS -m 20 -o /dev/null \
  --data-urlencode "chat_id=$TELEGRAM_OWNER_ID" \
  --data-urlencode "text=$text" \
  --data-urlencode "parse_mode=HTML" \
  --data-urlencode "disable_web_page_preview=true" \
  "https://api.telegram.org/bot$TELEGRAM_AGENT_BOT_TOKEN/sendMessage" \
  || echo "notify.sh: odeslání selhalo" >&2
exit 0

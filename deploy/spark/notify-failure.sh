#!/usr/bin/env bash
# notify-failure.sh <unit> — ohlásí selhání systemd unity s posledními řádky
# jejího journalu. Volá ho notify-failure@.service z OnFailure= (rag-schedule,
# comfyui, library-enrich): instance je jméno unity, která spadla.
set -u
unit="${1:?unit}"
esc() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
tail_log="$(journalctl --user -u "$unit" -n 14 -o cat --no-pager 2>/dev/null | cut -c1-160 | esc)"
here="$(cd "$(dirname "$0")" && pwd)"
"$here/notify.sh" "❌ <b>$unit</b> selhala
<pre>$tail_log</pre>"

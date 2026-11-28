#!/usr/bin/env bash
# One L40S window of corpus enrichment (PLAN-l40s-enrich.md).
# Waits for the L40S queue to hand over the GPU (READY marker + vLLM up via
# an ssh tunnel on localhost:8000), optionally benchmarks, then runs chunks
# and chapter summaries in parallel for WINDOW_MIN minutes and releases the
# window (DONE marker). Killing the clients mid-run is safe: resume is in DB.
#
#   MARK=WLP_N1 WINDOW_MIN=470 PHASES="64:4:30 128:8:30 192:8:0" nohup ./l40s_run.sh > logs/l40s_n1.log 2>&1 & disown
#
# Chapter prompts are close to 32k tokens; more than ~4 chapter workers fill
# the KV cache and starve the chunk queue (measured 2026-11-17: 32 workers →
# 3 running / 29 waiting; 4 workers → 67 running / 0 waiting).
set -u
cd "$(dirname "$0")"
MARK=${MARK:?MARK=WLP_<window> required}
WINDOW_MIN=${WINDOW_MIN:-110}
PRIORITY=${PRIORITY:-3}
MODEL=${MODEL:-qwen3-30b-a3b}
CHUNK_WORKERS=${CHUNK_WORKERS:-64}
CHAPTER_WORKERS=${CHAPTER_WORKERS:-4}
BENCH=${BENCH:-0}
URL=http://localhost:8000/v1
PY=.venv/bin/python3
# L40S_SSH (ssh host alias) and L40S_QUEUE (marker dir on the VM) come from rag/.env.
set -a; . "$(dirname "$0")/.env"; set +a
: "${L40S_SSH:?set L40S_SSH in rag/.env}" "${L40S_QUEUE:?set L40S_QUEUE in rag/.env}"
Q=$L40S_QUEUE
tag=$(echo "$MARK" | tr '[:upper:]' '[:lower:]')
ts() { date '+%F %T'; }

echo "$(ts) waiting for $Q/${MARK}_READY and $MODEL on :8000"
until ssh -n -o ConnectTimeout=10 "$L40S_SSH" test -e "$Q/${MARK}_READY" 2>/dev/null \
      && curl -s -m 5 "$URL/models" | grep -q "\"$MODEL\""; do
  sleep 60
done
start=$(date +%s)
echo "$(ts) READY"

release() { ssh -n "$L40S_SSH" touch "$Q/${MARK}_DONE" && echo "$(ts) ${MARK}_DONE sent"; }
trap release EXIT

if [ "$BENCH" -gt 0 ]; then
  timeout 15m $PY enrich_chunks.py --llm-url $URL --model "$MODEL" --workers "$CHUNK_WORKERS" \
    --priority "$PRIORITY" --benchmark "$BENCH" > "logs/${tag}_benchmark.log" 2>&1
  echo "$(ts) benchmark exit $? :: $(grep -E '/min' "logs/${tag}_benchmark.log" | tail -1)"
fi

# PHASES: "chunkW:chapterW:minutes ..."; the last phase runs to the end of
# the window. A phase after the first only starts if the previous one left
# headroom (KV < 95 %, no waiting requests); otherwise the previous
# concurrency is kept.
PHASES=${PHASES:-"$CHUNK_WORKERS:$CHAPTER_WORKERS:0"}
end=$(( start + (WINDOW_MIN - 5) * 60 ))
metric() { curl -s -m 5 localhost:8000/metrics | awk -v m="$1" '$1 ~ "^vllm:"m"\\{" {print $2; exit}'; }
headroom() {
  local kv w; kv=$(metric kv_cache_usage_perc); w=$(metric num_requests_waiting)
  echo "$(ts) metrics: kv=${kv:-?} waiting=${w:-?}"
  awk -v kv="${kv:-1}" -v w="${w:-1}" 'BEGIN { exit !(kv < 0.95 && w < 1) }'
}
run_phase() {  # chunkW chapterW seconds
  local n=$1 k=$2 secs=$3
  echo "$(ts) phase chunks=$n chapters=$k for $(( secs / 60 )) min"
  timeout "${secs}s" $PY enrich_chunks.py --llm-url $URL --model "$MODEL" --workers "$n" \
    --priority "$PRIORITY" --order depth >> "logs/${tag}_chunks.log" 2>&1 &
  local c=$!
  timeout "${secs}s" $PY enrich_chapters.py --llm-url $URL --model "$MODEL" --workers "$k" \
    --max-model-len 32768 --priority "$PRIORITY" >> "logs/${tag}_chapters.log" 2>&1 &
  local h=$!
  # judge headroom under load, two minutes before the phase ends
  room=0
  if [ "$secs" -gt 900 ]; then sleep $(( secs - 120 )); headroom && room=1; fi
  wait $c; echo "$(ts) chunks exit $? :: $(grep -E '/min' "logs/${tag}_chunks.log" | tail -1)"
  wait $h; echo "$(ts) chapters exit $? :: $(grep -E '/min' "logs/${tag}_chapters.log" | tail -1)"
}
set -- $PHASES
cur_n=0; cur_k=0; room=1
while [ $# -gt 0 ]; do
  IFS=: read -r n k m <<< "$1"; shift
  now=$(date +%s); [ "$now" -ge "$end" ] && break
  if [ "$cur_n" -gt 0 ] && [ "$room" -eq 0 ]; then n=$cur_n; k=$cur_k; echo "$(ts) no headroom, keeping $n+$k"; fi
  if [ $# -eq 0 ] || [ "$m" -eq 0 ]; then secs=$(( end - now )); else secs=$(( m * 60 )); fi
  [ $(( now + secs )) -gt "$end" ] && secs=$(( end - now ))
  run_phase "$n" "$k" "$secs"
  cur_n=$n; cur_k=$k
done
echo "$(ts) END"

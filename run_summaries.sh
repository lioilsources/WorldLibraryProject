#!/usr/bin/env bash
# Tenký orchestrátor Go pipeline pro česká shrnutí.
# Načte .env (pokud existuje) a spustí pipeline; argumenty se předají dál.
#
#   ./run_summaries.sh --dry-run --only dao
#   ./run_summaries.sh --only dao
#   ./run_summaries.sh                 # všech 10 děl
set -euo pipefail
cd "$(dirname "$0")"

set -a
[ -f .env ] && . ./.env
set +a

exec go -C pipeline run ./cmd/summarize "$@"

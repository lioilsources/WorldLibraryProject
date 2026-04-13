#!/usr/bin/env bash
# run_git_wget.sh
# Pipeline pro stahování přes git clone a wget (rekurzivní scraping)
#
# Spuštění:
#   ./run_git_wget.sh
#   ./run_git_wget.sh --dry-run
#   ./run_git_wget.sh --sources=moje_sources.txt
#   BASE_DIR=/jiná/cesta ./run_git_wget.sh

set -euo pipefail

# ── Konfigurace ────────────────────────────────────────────────────────────────
BASE_DIR="${BASE_DIR:-$(pwd)/downloads}"
SOURCES_FILE="${SOURCES_FILE:-git_wget_sources.txt}"
DRY_RUN=false

# ── Argument parsing ───────────────────────────────────────────────────────────
for arg in "$@"; do
  case $arg in
    --dry-run)      DRY_RUN=true ;;
    --sources=*)    SOURCES_FILE="${arg#*=}" ;;
    --base=*)       BASE_DIR="${arg#*=}" ;;
    *) echo "Neznámý argument: $arg"; exit 1 ;;
  esac
done

# ── Kontrola závislostí ────────────────────────────────────────────────────────
check_dep() {
  if ! command -v "$1" &>/dev/null; then
    echo "CHYBA: '$1' není nainstalováno. Instalace: brew install $1"
    exit 1
  fi
}
check_dep git
check_dep wget

echo "══════════════════════════════════════════════════"
echo "  Git/Wget Pipeline"
echo "══════════════════════════════════════════════════"
echo "  Base dir:    $BASE_DIR"
echo "  Sources:     $SOURCES_FILE"
echo "  Dry-run:     $DRY_RUN"
echo ""

# ── Parser ─────────────────────────────────────────────────────────────────────
# Načte git_wget_sources.txt a volá handle_git / handle_wget pro každý záznam.

handle_git() {
  local url="$1" dir="$2" branch="$3" depth="$4"
  local dest="$BASE_DIR/$dir"

  if [ "$DRY_RUN" = true ]; then
    local depth_flag=""
    [ -n "$depth" ] && depth_flag="--depth $depth"
    local branch_flag=""
    [ -n "$branch" ] && branch_flag="-b $branch"
    echo "[DRY] git clone $depth_flag $branch_flag $url $dest"
    return
  fi

  mkdir -p "$(dirname "$dest")"

  if [ -d "$dest/.git" ]; then
    echo "  → update: $dir"
    git -C "$dest" pull --ff-only
  else
    echo "  → clone: $url"
    echo "           → $dest"
    local args=()
    [ -n "$depth"  ] && args+=("--depth" "$depth")
    [ -n "$branch" ] && args+=("-b" "$branch")
    git clone "${args[@]}" "$url" "$dest"
  fi
}

handle_wget() {
  local url="$1" dir="$2" accept="$3" extra_args="$4"
  local dest="$BASE_DIR/$dir"

  local accept_flag=""
  [ -n "$accept" ] && accept_flag="--accept=$accept"

  if [ "$DRY_RUN" = true ]; then
    echo "[DRY] wget $extra_args $accept_flag -P $dest $url"
    return
  fi

  mkdir -p "$dest"
  echo "  → wget: $url"
  echo "          → $dest"

  # shellcheck disable=SC2086
  wget $extra_args \
    ${accept_flag} \
    --no-host-directories \
    --cut-dirs=10 \
    --directory-prefix="$dest" \
    "$url" || {
      echo "  WARN: wget skončil s chybou (ignoruji)"
    }
}

# ── Hlavní smyčka ──────────────────────────────────────────────────────────────
parse_and_run() {
  local type="" url="" dir="" branch="" depth="" accept="" args=""
  local total=0 ok=0 failed=0

  flush() {
    [ -z "$type" ] && return
    total=$((total + 1))
    echo ""
    echo "── [$total] $type: $dir ────────────────────────────"
    case "$type" in
      git)
        if handle_git "$url" "$dir" "$branch" "$depth"; then
          ok=$((ok + 1))
        else
          echo "  CHYBA: selhalo"
          failed=$((failed + 1))
        fi
        ;;
      wget)
        if handle_wget "$url" "$dir" "$accept" "$args"; then
          ok=$((ok + 1))
        else
          echo "  CHYBA: selhalo"
          failed=$((failed + 1))
        fi
        ;;
      *)
        echo "  CHYBA: neznámý type=$type"
        failed=$((failed + 1))
        ;;
    esac
    # reset
    type=""; url=""; dir=""; branch=""; depth=""; accept=""; args=""
  }

  while IFS= read -r raw || [ -n "$raw" ]; do
    local line
    line="$(echo "$raw" | sed 's/[[:space:]]*$//')"  # rtrim

    # prázdný řádek nebo komentář → flush
    if [ -z "$line" ] || [[ "$line" == \#* ]]; then
      flush
      continue
    fi

    local key val
    key="${line%%=*}"
    val="${line#*=}"

    case "$key" in
      type)   type="$val" ;;
      url)    url="$val" ;;
      dir)    dir="$val" ;;
      branch) branch="$val" ;;
      depth)  depth="$val" ;;
      accept) accept="$val" ;;
      args)   args="$val" ;;
    esac
  done < "$SOURCES_FILE"

  flush  # poslední záznam

  echo ""
  echo "══════════════════════════════════════════════════"
  printf "  Hotovo: %d OK, %d selhalo, celkem %d\n" "$ok" "$failed" "$total"
  echo "══════════════════════════════════════════════════"
}

parse_and_run

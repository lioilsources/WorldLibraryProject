#!/usr/bin/env bash
# run_pipeline.sh
# Kompletní pipeline na Mac Mini M2:
#   1. Stáhni dataset (aria2c přes Go orchestrátor)
#   2. Extrahuj ZIP archivy
#   3. Zobraz statistiku
#
# Spuštění:
#   chmod +x run_pipeline.sh
#   ./run_pipeline.sh
#   ./run_pipeline.sh --dry-run        # jen zobraz co by se stáhlo
#   ./run_pipeline.sh --skip-download  # přeskoč stahování, jen extrahuj

set -euo pipefail

# ── Konfigurace ────────────────────────────────────────────────────────────────
BASE_DIR="${BASE_DIR:-$(pwd)/downloads}"
URLS_FILE="${URLS_FILE:-urls.txt}"
PARALLEL_JOBS="${PARALLEL_JOBS:-4}"        # paralelní soubory najednou
CONNECTIONS_PER="${CONNECTIONS_PER:-8}"    # spojení na jeden soubor
DRY_RUN=false
SKIP_DOWNLOAD=false

# ── Argument parsing ───────────────────────────────────────────────────────────
for arg in "$@"; do
  case $arg in
    --dry-run)        DRY_RUN=true ;;
    --skip-download)  SKIP_DOWNLOAD=true ;;
    --base=*)         BASE_DIR="${arg#*=}" ;;
    --urls=*)         URLS_FILE="${arg#*=}" ;;
    -j*)              PARALLEL_JOBS="${arg#-j}" ;;
    *) echo "Neznámý argument: $arg"; exit 1 ;;
  esac
done

# ── Kontrola závislostí ────────────────────────────────────────────────────────
check_dep() {
  if ! command -v "$1" &>/dev/null; then
    echo "CHYBA: '$1' není nainstalováno."
    echo "  Instalace: brew install $1"
    exit 1
  fi
}

check_dep aria2c
check_dep go

echo "══════════════════════════════════════════════════"
echo "  Filozofický Dataset Pipeline – Mac Mini M2"
echo "══════════════════════════════════════════════════"
echo "  Base dir:   $BASE_DIR"
echo "  URLs file:  $URLS_FILE"
echo "  Parallel:   $PARALLEL_JOBS souborů × $CONNECTIONS_PER spojení"
echo ""

# ── Krok 1: Stahování ──────────────────────────────────────────────────────────
if [ "$SKIP_DOWNLOAD" = false ]; then
  echo "── Krok 1: Stahování ────────────────────────────"

  DOWNLOADER_DIR="$(dirname "$0")/downloader"

  if [ ! -f "$DOWNLOADER_DIR/go.mod" ]; then
    echo "Inicializuji Go modul..."
    cd "$DOWNLOADER_DIR"
    go mod init philosophy-downloader
    cd -
  fi

  if [ "$DRY_RUN" = true ]; then
    go run "$DOWNLOADER_DIR/main.go" \
      -input "$URLS_FILE" \
      -base  "$BASE_DIR" \
      -j     "$PARALLEL_JOBS" \
      -x     "$CONNECTIONS_PER" \
      --dry-run
    echo ""
    echo "Dry-run hotov. Spusť bez --dry-run pro skutečné stahování."
    exit 0
  fi

  go run "$DOWNLOADER_DIR/main.go" \
    -input "$URLS_FILE" \
    -base  "$BASE_DIR" \
    -j     "$PARALLEL_JOBS" \
    -x     "$CONNECTIONS_PER"

  echo ""
fi

# ── Krok 2: Extrakce ZIP archivů ───────────────────────────────────────────────
echo "── Krok 2: Extrakce ZIP archivů ─────────────────"

extract_zip() {
  local zip="$1"
  local dest_dir
  dest_dir="$(dirname "$zip")"

  # Přeskoč pokud již existuje příznak .extracted
  if [ -f "${zip%.zip}.extracted" ]; then
    echo "  skip (již extrahováno): $(basename "$zip")"
    return
  fi

  echo "  Extrahuji: $(basename "$zip")"

  # Použij unzip s potlačením přepisování existujících souborů
  if unzip -n -q "$zip" -d "$dest_dir" 2>/dev/null; then
    touch "${zip%.zip}.extracted"
    echo "  OK: $(basename "$zip")"
  else
    echo "  WARN: extrakce selhala pro $(basename "$zip")"
  fi
}

export -f extract_zip

# Najdi všechny ZIP soubory a extrahuj (max 4 paralelně)
find "$BASE_DIR" -name "*.zip" -not -name "*.extracted" | \
  xargs -P 4 -I{} bash -c 'extract_zip "$@"' _ {}

# ── Krok 3: Extrakce tar.gz (Perseus corpus) ──────────────────────────────────
echo ""
echo "── Krok 3: Extrakce TAR archivů ─────────────────"
find "$BASE_DIR" -name "*.tar.gz" | while read -r archive; do
  dest_dir="$(dirname "$archive")"
  marker="${archive%.tar.gz}.extracted"
  if [ -f "$marker" ]; then
    echo "  skip: $(basename "$archive")"
    continue
  fi
  echo "  Extrahuji: $(basename "$archive")"
  tar -xzf "$archive" -C "$dest_dir" && touch "$marker"
done

# ── Krok 4: Statistika ────────────────────────────────────────────────────────
echo ""
echo "── Statistika datasetu ──────────────────────────"
echo ""

if command -v du &>/dev/null; then
  echo "Využití disku:"
  du -sh "$BASE_DIR"/*/  2>/dev/null | sort -rh | head -20
  echo ""
  echo "Celkem:"
  du -sh "$BASE_DIR" 2>/dev/null
fi

echo ""
echo "Počty souborů:"
printf "  %-30s  %s\n" "Tradice" "PDF  ZIP  TXT"
printf "  %s\n" "$(printf '─%.0s' {1..55})"
for dir in "$BASE_DIR"/*/; do
  tradition="$(basename "$dir")"
  pdf_count=$(find "$dir" -name "*.pdf" 2>/dev/null | wc -l | tr -d ' ')
  zip_count=$(find "$dir" -name "*.zip" 2>/dev/null | wc -l | tr -d ' ')
  txt_count=$(find "$dir" -name "*.txt" 2>/dev/null | wc -l | tr -d ' ')
  printf "  %-30s  %-4s %-4s %-4s\n" "$tradition" "$pdf_count" "$zip_count" "$txt_count"
done

echo ""
echo "══════════════════════════════════════════════════"
echo "  Pipeline dokončena."
echo "  Další krok: spusť extract.py + Go pipeline"
echo "  pro generaci Q&A JSONL datasetu."
echo "══════════════════════════════════════════════════"

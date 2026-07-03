# WorldLibraryProject — CLAUDE.md

## Overview

Download pipeline for a multilingual philosophical corpus. Go orchestrator parses a URL list and drives `aria2c` for parallel downloads. Companion model server for processing. Target machine: Mac Mini M2.

## Prerequisites

```bash
brew install aria2 go
```

## Usage

```bash
# Main pipeline (reads urls.txt, downloads via aria2c)
./run_pipeline.sh

# Git-based wget sources (alternative download method)
./run_git_wget.sh

# Summarize downloaded texts
./run_summaries.sh
```

## Structure

```
urls.txt                     # List of source URLs (one per line)
run_pipeline.sh              # Main entry point
run_git_wget.sh              # Git/wget alternative downloader
run_summaries.sh             # Text summarization runner

downloader/
  main.go                    # Go orchestrator: parses urls.txt, runs aria2c
downloads/                   # Downloaded files (git-ignored)

pipeline/                    # Processing pipeline stages
model_server/                # Local model server for text processing

KATALOG_KNIH.md              # Book catalog / source list
git_wget_sources.txt         # Alternative source list for git_wget
```

## Go Downloader

```bash
cd downloader
go build -o ../bin/downloader .
```

Parses `urls.txt`, groups by domain, runs `aria2c` with configurable parallelism. Supports resume (aria2c session files).

## Notes

- Downloads go to `downloads/` — large files, git-ignored
- `KATALOG_KNIH.md` tracks what was downloaded and from where
- Pipeline stages: download → clean → deduplicate → summarize → index

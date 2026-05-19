// Package assemble writes the per-work markdown, the aggregated index and the
// dataset.jsonl artifact. It never reads or writes KATALOG_KNIH.md.
package assemble

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"worldlibrary/pipeline/internal/manifest"
)

// WorkResult is the outcome for one work after map+reduce.
type WorkResult struct {
	Work      manifest.Work
	Status    string // MATERIALIZED | SKIPPED | ERROR
	Body      string
	SummaryCS string
	TOCCS     string
	Model     string
	NChunks   int
	SourceSHA []string
	Note      string
}

func ensureDir(p string) error { return os.MkdirAll(p, 0o755) }

func atomicWrite(dest string, b []byte) error {
	if err := ensureDir(filepath.Dir(dest)); err != nil {
		return err
	}
	tmp := dest + ".part"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, dest)
}

// WriteWork emits summaries/<slug>.md.
func WriteWork(outRoot string, r WorkResult) error {
	var sb strings.Builder
	w := r.Work
	fmt.Fprintf(&sb, "# %s\n\n", w.TitleCS)
	fmt.Fprintf(&sb, "*Jazyk: %s · Písmo: %s · Zdrojových souborů: %d*\n\n",
		w.LangLabel, w.Script, len(w.Sources))
	if r.Status != "MATERIALIZED" {
		fmt.Fprintf(&sb, "> Stav: %s — %s\n", r.Status, r.Note)
		return atomicWrite(filepath.Join(outRoot, "summaries", w.Slug+".md"), []byte(sb.String()))
	}
	fmt.Fprintf(&sb, "## Shrnutí\n\n%s\n\n", strings.TrimSpace(r.SummaryCS))
	if strings.TrimSpace(r.TOCCS) != "" {
		fmt.Fprintf(&sb, "## Obsah\n\n%s\n\n", strings.TrimSpace(r.TOCCS))
	}
	fmt.Fprintf(&sb, "---\n*Vygenerováno modelem `%s`; stav obsahu: %s.*\n", r.Model, r.Status)
	return atomicWrite(filepath.Join(outRoot, "summaries", w.Slug+".md"), []byte(sb.String()))
}

// WriteIndex emits OBSAH_CESKY.md (an aggregated table, never KATALOG_KNIH.md).
func WriteIndex(outRoot string, results []WorkResult) error {
	var sb strings.Builder
	sb.WriteString("# Obsah knih v češtině (generovaný)\n\n")
	sb.WriteString("Automaticky generovaná česká shrnutí a obsahy. Ruční katalog ")
	sb.WriteString("metadat zůstává v `KATALOG_KNIH.md` a tímto souborem se nemění.\n\n")
	sb.WriteString("| Dílo | Jazyk | Písmo | Stav | Shrnutí |\n")
	sb.WriteString("|---|---|---|---|---|\n")
	for _, r := range results {
		link := "—"
		if r.Status == "MATERIALIZED" {
			link = fmt.Sprintf("[%s](summaries/%s.md)", "otevřít", r.Work.Slug)
		}
		status := r.Status
		if r.Status != "MATERIALIZED" && r.Note != "" {
			status = r.Status + " (" + r.Note + ")"
		}
		fmt.Fprintf(&sb, "| %s | %s | %s | %s | %s |\n",
			r.Work.TitleCS, r.Work.LangLabel, r.Work.Script, status, link)
	}
	fmt.Fprintf(&sb, "\n*Vygenerováno %s.*\n", time.Now().UTC().Format("2006-01-02"))
	return atomicWrite(filepath.Join(outRoot, "OBSAH_CESKY.md"), []byte(sb.String()))
}

type datasetRow struct {
	Slug        string   `json:"slug"`
	TitleCS     string   `json:"title_cs"`
	SourceFiles []string `json:"source_files"`
	Lang        string   `json:"lang"`
	Script      string   `json:"script"`
	SummaryCS   string   `json:"summary_cs"`
	TOCCS       string   `json:"toc_cs"`
	Model       string   `json:"model"`
	SourceSHA   []string `json:"source_sha256"`
	GeneratedAt string   `json:"generated_at"`
}

// WriteDataset emits data/final/dataset.jsonl for successfully summarised works.
func WriteDataset(outRoot string, results []WorkResult) error {
	now := time.Now().UTC().Format(time.RFC3339)
	var lines [][]byte
	for _, r := range results {
		if r.Status != "MATERIALIZED" {
			continue
		}
		var files []string
		for _, s := range r.Work.Sources {
			files = append(files, s.Path)
		}
		sort.Strings(files)
		row := datasetRow{
			Slug: r.Work.Slug, TitleCS: r.Work.TitleCS, SourceFiles: files,
			Lang: r.Work.LangLabel, Script: r.Work.Script,
			SummaryCS: strings.TrimSpace(r.SummaryCS), TOCCS: strings.TrimSpace(r.TOCCS),
			Model: r.Model, SourceSHA: r.SourceSHA, GeneratedAt: now,
		}
		b, err := marshalNoEscape(row)
		if err != nil {
			return err
		}
		lines = append(lines, b)
	}
	out := append(bytes_join(lines, []byte{'\n'}), '\n')
	return atomicWrite(filepath.Join(outRoot, "data", "final", "dataset.jsonl"), out)
}

func marshalNoEscape(v any) ([]byte, error) {
	var sb strings.Builder
	enc := json.NewEncoder(&sb)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return []byte(strings.TrimRight(sb.String(), "\n")), nil
}

func bytes_join(parts [][]byte, sep []byte) []byte {
	if len(parts) == 0 {
		return nil
	}
	out := append([]byte{}, parts[0]...)
	for _, p := range parts[1:] {
		out = append(out, sep...)
		out = append(out, p...)
	}
	return out
}

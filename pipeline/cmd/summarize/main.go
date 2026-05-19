// Command summarize is the Go processing pipeline: acquire content, chunk it,
// map+reduce via the Python model-server, and assemble Czech summaries.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"worldlibrary/pipeline/internal/acquire"
	"worldlibrary/pipeline/internal/assemble"
	"worldlibrary/pipeline/internal/chunk"
	"worldlibrary/pipeline/internal/llm"
	"worldlibrary/pipeline/internal/manifest"
	"worldlibrary/pipeline/internal/mapreduce"
	"worldlibrary/pipeline/internal/textnorm"
)

func main() {
	var (
		dryRun    = flag.Bool("dry-run", false, "print the plan, make no network calls, write nothing outside .cache")
		only      = flag.String("only", "", "process only works whose slug contains this substring")
		stage     = flag.String("stage", "all", "acquire|all (acquire = only materialise content)")
		noAcquire = flag.Bool("no-acquire", false, "assume content is already on disk")
		downloads = flag.String("downloads", "downloads", "downloads directory")
		out       = flag.String("out", ".", "output root for summaries/, OBSAH_CESKY.md, data/")
	)
	flag.Parse()

	repoRoot := gitRoot()
	downloadsDir := absUnder(repoRoot, *downloads)
	outRoot := absUnder(repoRoot, *out)
	cacheRoot := filepath.Join(repoRoot, ".cache")

	model := env("MODEL_NAME", "qwen2.5-72b-instruct")
	ctxTokens := envInt("MODEL_CONTEXT", 32768)
	workers := envInt("MODEL_WORKERS", 3)
	budget := minInt(8000, ctxTokens/4)

	works := manifest.Find(*only)
	if len(works) == 0 {
		fmt.Fprintf(os.Stderr, "no works match --only=%q\n", *only)
		os.Exit(1)
	}

	ctx := context.Background()
	mrOpts := mapreduce.Options{
		CacheRoot: cacheRoot, Workers: workers, ReduceBudget: budget, DryRun: *dryRun,
	}

	// Stage: acquire.
	if !*dryRun && !*noAcquire {
		acq := &acquire.Acquirer{
			DownloadsDir: downloadsDir,
			RepoRoot:     repoRoot,
			CacheRoot:    cacheRoot,
			UserAgent:    "WorldLibraryProject/1.0 (+pipeline)",
		}
		rep := acq.Run(ctx)
		ok := 0
		for _, r := range rep {
			if r.Status == acquire.StatusMaterialized {
				ok++
			}
		}
		fmt.Fprintf(os.Stderr, "acquire: %d/%d source files materialised\n", ok, len(rep))
		if *stage == "acquire" {
			return
		}
	}

	var client *llm.Client
	if !*dryRun {
		base := os.Getenv("MODEL_BASE_URL")
		if base == "" {
			fmt.Fprintln(os.Stderr, "MODEL_BASE_URL is not set (need the Tailscale model-server URL)")
			os.Exit(1)
		}
		client = llm.New(strings.TrimRight(base, "/"), os.Getenv("MODEL_API_KEY"), model)
	}

	var results []assemble.WorkResult
	for _, w := range works {
		contents, shas := readSources(downloadsDir, w)
		chunks := chunk.WorkChunks(w, contents, budget)

		if *dryRun {
			texts := make([]string, len(chunks))
			for i, c := range chunks {
				texts[i] = c.Text
			}
			hits, total := mrOpts.PlanStats(w.Slug, model, texts)
			fmt.Printf("%-24s soubory=%d chunky=%d cache=%d/%d plánovaných_volání=%d\n",
				w.Slug, len(w.Sources), total, hits, total, total-hits)
			continue
		}

		if len(chunks) == 0 {
			results = append(results, assemble.WorkResult{
				Work: w, Status: "SKIPPED", Model: model,
				Note: "obsah nedostupný (Stage 0 nezískala soubory)",
			})
			fmt.Fprintf(os.Stderr, "%s: SKIPPED (no content)\n", w.Slug)
			continue
		}

		_, notes, err := mapreduce.MapWork(ctx, client, mrOpts, w, chunks)
		if err != nil {
			results = append(results, assemble.WorkResult{
				Work: w, Status: "ERROR", Model: model, Note: err.Error(),
			})
			fmt.Fprintf(os.Stderr, "%s: ERROR %v\n", w.Slug, err)
			continue
		}
		body, err := mapreduce.ReduceWork(ctx, client, mrOpts, w, notes)
		if err != nil {
			results = append(results, assemble.WorkResult{
				Work: w, Status: "ERROR", Model: model, Note: err.Error(),
			})
			fmt.Fprintf(os.Stderr, "%s: ERROR %v\n", w.Slug, err)
			continue
		}
		summary, toc := mapreduce.SplitBody(body)
		results = append(results, assemble.WorkResult{
			Work: w, Status: "MATERIALIZED", Body: body,
			SummaryCS: summary, TOCCS: toc, Model: model,
			NChunks: len(chunks), SourceSHA: shas,
		})
		fmt.Fprintf(os.Stderr, "%s: OK (%d chunks)\n", w.Slug, len(chunks))
	}

	if *dryRun {
		return
	}

	for _, r := range results {
		if err := assemble.WriteWork(outRoot, r); err != nil {
			fmt.Fprintf(os.Stderr, "write %s: %v\n", r.Work.Slug, err)
		}
	}
	if err := assemble.WriteIndex(outRoot, results); err != nil {
		fmt.Fprintf(os.Stderr, "write index: %v\n", err)
	}
	if err := assemble.WriteDataset(outRoot, results); err != nil {
		fmt.Fprintf(os.Stderr, "write dataset: %v\n", err)
	}
}

func readSources(downloadsDir string, w manifest.Work) (map[string]string, []string) {
	contents := map[string]string{}
	var shas []string
	for _, s := range w.Sources {
		b, err := os.ReadFile(filepath.Join(downloadsDir, s.Path))
		if err != nil {
			continue
		}
		if strings.HasPrefix(string(b), "version https://git-lfs.github.com/spec/v1") {
			continue // unresolved LFS pointer
		}
		sum := sha256.Sum256(b)
		shas = append(shas, hex.EncodeToString(sum[:]))
		contents[s.Path] = textnorm.Clean(b)
	}
	return contents, shas
}

func gitRoot() string {
	out, err := exec.Command("git", "rev-parse", "--show-toplevel").Output()
	if err != nil {
		wd, _ := os.Getwd()
		return wd
	}
	return strings.TrimSpace(string(out))
}

func absUnder(root, p string) string {
	if filepath.IsAbs(p) {
		return p
	}
	return filepath.Join(root, p)
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

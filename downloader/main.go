// cmd/downloader/main.go
// Aria2c download orchestrator pro filozofický dataset
// Spuštění: go run . -input urls.txt -base /Volumes/ancient_origins_1TB
package main

import (
	"bufio"
	"flag"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"
)

// Entry je jeden stažitelný soubor parsovaný z urls.txt
type Entry struct {
	URL      string
	Dir      string // relativní nebo absolutní cesta
	Out      string // výstupní název souboru
	Tradition string // odvozeno z Dir (první segment za base)
}

func main() {
	inputFile := flag.String("input", "urls.txt", "Cesta k souboru s URL")
	baseDir   := flag.String("base", mustPwd()+"/downloads", "Kořenový adresář pro stahování")
	parallel  := flag.Int("j", 4, "Počet paralelních stahování (aria2c -j)")
	connPerFile := flag.Int("x", 8, "Spojení na soubor (aria2c -x)")
	dryRun    := flag.Bool("dry-run", false, "Jen vypiš příkazy, nestahuj")
	listOnly  := flag.Bool("list", false, "Vypiš parsované záznamy a skonči")
	flag.Parse()

	entries, err := parseURLFile(*inputFile)
	if err != nil {
		log.Fatalf("parse %s: %v", *inputFile, err)
	}
	log.Printf("Načteno %d souborů ke stažení", len(entries))

	if *listOnly {
		printSummary(entries)
		return
	}

	// Odděl soubory které již existují
	toDownload := filterExisting(entries, *baseDir)
	log.Printf("%d souborů chybí, %d již existuje", len(toDownload), len(entries)-len(toDownload))

	if len(toDownload) == 0 {
		log.Println("Vše staženo.")
		return
	}

	if *dryRun {
		for _, e := range toDownload {
			fmt.Println(buildAriaCmd(e, *baseDir, *parallel, *connPerFile))
		}
		return
	}

	runDownloads(toDownload, *baseDir, *parallel, *connPerFile)
}

// ── Parser ────────────────────────────────────────────────────────────────────

func parseURLFile(path string) ([]Entry, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("open: %w", err)
	}
	defer f.Close()

	var entries []Entry
	var cur Entry

	flush := func() {
		if cur.URL == "" {
			return
		}
		// Odvoď tradici z prvního segmentu cesty za /ancient_origins_1TB/
		parts := strings.Split(strings.TrimPrefix(cur.Dir, "/ancient_origins_1TB/"), "/")
		if len(parts) > 0 {
			cur.Tradition = parts[0]
		}
		// Pokud Out není specifikováno, odvoď z URL
		if cur.Out == "" {
			segs := strings.Split(cur.URL, "/")
			cur.Out = segs[len(segs)-1]
		}
		entries = append(entries, cur)
		cur = Entry{}
	}

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		raw := scanner.Text()
		line := strings.TrimSpace(raw)

		// prázdný řádek nebo komentář → flush aktuálního záznamu
		if line == "" || strings.HasPrefix(line, "#") {
			flush()
			continue
		}

		switch {
		case strings.HasPrefix(line, "http://") || strings.HasPrefix(line, "https://"):
			flush() // flush předchozího pokud existuje
			cur.URL = line
		case strings.HasPrefix(line, "dir="):
			cur.Dir = strings.TrimPrefix(line, "dir=")
		case strings.HasPrefix(line, "out="):
			cur.Out = strings.TrimPrefix(line, "out=")
		}
	}
	flush() // poslední záznam

	return entries, scanner.Err()
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func filterExisting(entries []Entry, base string) []Entry {
	var out []Entry
	for _, e := range entries {
		dest := resolveDestination(e, base)
		if _, err := os.Stat(dest); os.IsNotExist(err) {
			out = append(out, e)
		} else {
			log.Printf("  skip (exists): %s", filepath.Base(dest))
		}
	}
	return out
}

func resolveDestination(e Entry, base string) string {
	dir := e.Dir
	// Pokud Dir začíná /ancient_origins_1TB, nahraď base
	if strings.HasPrefix(dir, "/ancient_origins_1TB") {
		rel := strings.TrimPrefix(dir, "/ancient_origins_1TB")
		dir = filepath.Join(base, rel)
	} else if !filepath.IsAbs(dir) {
		dir = filepath.Join(base, dir)
	}
	return filepath.Join(dir, e.Out)
}

func buildAriaCmd(e Entry, base string, j, x int) string {
	dir := e.Dir
	if strings.HasPrefix(dir, "/ancient_origins_1TB") {
		rel := strings.TrimPrefix(dir, "/ancient_origins_1TB")
		dir = filepath.Join(base, rel)
	}
	return fmt.Sprintf(
		`aria2c -j%d -x%d -s%d --auto-file-renaming=false --continue=true `+
			`--retry-wait=5 --max-tries=5 `+
			`-d "%s" -o "%s" "%s"`,
		j, x, x, dir, e.Out, e.URL,
	)
}

// ── Executor ──────────────────────────────────────────────────────────────────

func runDownloads(entries []Entry, base string, j, x int) {
	total := int64(len(entries))
	var done, failed int64

	start := time.Now()

	for i, e := range entries {
		dest := resolveDestination(e, base)
		dir := filepath.Dir(dest)

		if err := os.MkdirAll(dir, 0o755); err != nil {
			log.Printf("[%d/%d] mkdir fail %s: %v", i+1, total, dir, err)
			atomic.AddInt64(&failed, 1)
			continue
		}

		args := []string{
			fmt.Sprintf("-j%d", j),
			fmt.Sprintf("-x%d", x),
			fmt.Sprintf("-s%d", x),
			"--auto-file-renaming=false",
			"--continue=true",
			"--retry-wait=5",
			"--max-tries=5",
			"--console-log-level=warn",
			"-d", dir,
			"-o", e.Out,
			e.URL,
		}

		log.Printf("[%d/%d] %s → %s", i+1, total, e.Tradition, e.Out)

		cmd := exec.Command("aria2c", args...)
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr

		if err := cmd.Run(); err != nil {
			log.Printf("  CHYBA: %v", err)
			atomic.AddInt64(&failed, 1)
			// Pokračuj dál — nepřerušuj pro jeden chybný soubor
			continue
		}
		atomic.AddInt64(&done, 1)
	}

	elapsed := time.Since(start)
	log.Printf("Hotovo: %d/%d staženo, %d selhalo, čas: %s",
		done, total, failed, elapsed.Round(time.Second))
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func mustPwd() string {
	dir, err := os.Getwd()
	if err != nil {
		return "."
	}
	return dir
}

// ── Summary ───────────────────────────────────────────────────────────────────

func printSummary(entries []Entry) {
	traditions := make(map[string]int)
	for _, e := range entries {
		traditions[e.Tradition]++
	}
	fmt.Printf("\n=== PŘEHLED DATASETU ===\n")
	fmt.Printf("Celkem souborů: %d\n\n", len(entries))
	fmt.Printf("%-35s  %s\n", "Tradice", "Počet souborů")
	fmt.Printf("%s\n", strings.Repeat("─", 50))
	for t, n := range traditions {
		fmt.Printf("%-35s  %d\n", t, n)
	}
	fmt.Println()
	for _, e := range entries {
		fmt.Printf("  [%s] %s\n    → %s/%s\n", e.Tradition, e.URL, e.Dir, e.Out)
	}
}

// Package mapreduce runs the per-chunk map step (bounded worker pool + on-disk
// cache) and the hierarchical Czech reduce step.
package mapreduce

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"worldlibrary/pipeline/internal/chunk"
	"worldlibrary/pipeline/internal/llm"
	"worldlibrary/pipeline/internal/manifest"
)

// PromptVersion is bumped whenever a prompt changes so stale cache entries are
// deterministically invalidated.
const PromptVersion = "v1"

// Options configures a run.
type Options struct {
	CacheRoot    string
	Workers      int
	ReduceBudget int // max input tokens per reduce call
	DryRun       bool
}

type cacheEntry struct {
	Model string    `json:"model"`
	Text  string    `json:"text"`
	When  time.Time `json:"when"`
}

func key(parts ...string) string {
	h := sha256.New()
	for _, p := range parts {
		h.Write([]byte(p))
		h.Write([]byte{0})
	}
	return hex.EncodeToString(h.Sum(nil))
}

func (o Options) cachePath(slug, k string) string {
	return filepath.Join(o.CacheRoot, "summaries", slug, k+".json")
}

func (o Options) readCache(slug, k string) (string, bool) {
	b, err := os.ReadFile(o.cachePath(slug, k))
	if err != nil {
		return "", false
	}
	var e cacheEntry
	if json.Unmarshal(b, &e) != nil {
		return "", false
	}
	return e.Text, true
}

func (o Options) writeCache(slug, k, model, text string) {
	p := o.cachePath(slug, k)
	_ = os.MkdirAll(filepath.Dir(p), 0o755)
	b, _ := json.MarshalIndent(cacheEntry{Model: model, Text: text, When: time.Now().UTC()}, "", "  ")
	tmp := p + ".part"
	if os.WriteFile(tmp, b, 0o644) == nil {
		_ = os.Rename(tmp, p)
	}
}

func mapSystemPrompt(w manifest.Work) string {
	return fmt.Sprintf(`Jsi odborný analytik historických textů. Dílo: %q (jazyk: %s, písmo: %s).
Z následujícího úryvku vytěž STRUČNĚ a v ČEŠTINĚ:
1) klíčové myšlenky a tvrzení,
2) zmíněné postavy, pojmy a místa,
3) strukturní orientační body (kapitola/oddíl/verš/zpěv/parva), pokud jsou v textu.
Piš v odrážkách. NEVYMÝŠLEJ obsah, který v úryvku není; u nejistot to označ.`,
		w.TitleCS, w.LangLabel, w.Script)
}

func reduceSystemPrompt() string {
	return `Z níže uvedených dílčích českých poznámek sestav výstup v ČEŠTINĚ přesně v tomto formátu:

## Shrnutí
<souvislý odborný text, zhruba jedna normostrana: co dílo je, autorství, hlavní témata a význam>

## Obsah
<strukturovaný přehled v odrážkách: knihy/oddíly/zpěvy/parvy a jejich náplň>

Drž se výhradně poznámek, nic nepřidávej. Nepoužívej jiné nadpisy než "## Shrnutí" a "## Obsah".`
}

// chunkNote is one mapped note tied to its parva for grouped reduction.
type chunkNote struct {
	Parva int
	Text  string
}

// MapWork runs the map step over all chunks of a work.
func MapWork(ctx context.Context, c *llm.Client, o Options, w manifest.Work, chunks []chunk.Chunk) ([]string, []chunkNote, error) {
	notes := make([]string, len(chunks))
	sys := mapSystemPrompt(w)

	if o.DryRun {
		return nil, nil, nil
	}

	sem := make(chan struct{}, max1(o.Workers))
	var wg sync.WaitGroup
	var mu sync.Mutex
	var firstErr error

	for i := range chunks {
		ch := chunks[i]
		k := key(c.Model, PromptVersion, "map", ch.Text)
		if cached, ok := o.readCache(w.Slug, k); ok {
			notes[i] = cached
			continue
		}
		wg.Add(1)
		sem <- struct{}{}
		go func(idx int, ch chunk.Chunk, k string) {
			defer wg.Done()
			defer func() { <-sem }()
			mu.Lock()
			stop := firstErr != nil
			mu.Unlock()
			if stop {
				return
			}
			out, err := c.Chat(ctx, sys, ch.Text, 1024)
			if err != nil {
				mu.Lock()
				if firstErr == nil {
					firstErr = fmt.Errorf("map %s#%d (%s): %w", w.Slug, idx, ch.SourceFile, err)
				}
				mu.Unlock()
				return
			}
			o.writeCache(w.Slug, k, c.Model, out)
			notes[idx] = out
		}(i, ch, k)
	}
	wg.Wait()
	if firstErr != nil {
		return nil, nil, firstErr
	}

	cn := make([]chunkNote, 0, len(chunks))
	for i, ch := range chunks {
		if strings.TrimSpace(notes[i]) == "" {
			continue
		}
		cn = append(cn, chunkNote{Parva: ch.Parva, Text: notes[i]})
	}
	return notes, cn, nil
}

// ReduceWork combines mapped notes into the final "## Shrnutí" + "## Obsah".
func ReduceWork(ctx context.Context, c *llm.Client, o Options, w manifest.Work, notes []chunkNote) (string, error) {
	if len(notes) == 0 {
		return "", fmt.Errorf("reduce %s: no notes", w.Slug)
	}

	if w.PerFileParva {
		// First reduce per parva into a digest, then reduce the digests.
		byParva := map[int][]string{}
		var order []int
		for _, n := range notes {
			if _, seen := byParva[n.Parva]; !seen {
				order = append(order, n.Parva)
			}
			byParva[n.Parva] = append(byParva[n.Parva], n.Text)
		}
		var digests []string
		for _, p := range order {
			joined := strings.Join(byParva[p], "\n\n")
			d, err := o.cachedReduce(ctx, c, w.Slug,
				fmt.Sprintf("parvadigest:%d", p),
				"Shrň tyto poznámky k jedné parvě do stručného českého odstavce (název parvy + děj). Bez nadpisů.",
				joined)
			if err != nil {
				return "", err
			}
			digests = append(digests, fmt.Sprintf("Parva %d:\n%s", p, d))
		}
		return o.hierReduce(ctx, c, w, digests)
	}

	texts := make([]string, len(notes))
	for i, n := range notes {
		texts[i] = n.Text
	}
	return o.hierReduce(ctx, c, w, texts)
}

// hierReduce recursively shrinks the note list until it fits the budget, then
// performs the final formatted reduction.
func (o Options) hierReduce(ctx context.Context, c *llm.Client, w manifest.Work, notes []string) (string, error) {
	joined := strings.Join(notes, "\n\n")
	if chunk.EstimateTokens(joined, manifest.LangLatin) <= o.ReduceBudget || len(notes) == 1 {
		return o.cachedReduce(ctx, c, w.Slug, "final", reduceSystemPrompt(), joined)
	}
	// Partition into groups that each fit, digest each, recurse.
	var groups [][]string
	var cur []string
	curTok := 0
	for _, n := range notes {
		t := chunk.EstimateTokens(n, manifest.LangLatin)
		if curTok+t > o.ReduceBudget && len(cur) > 0 {
			groups = append(groups, cur)
			cur, curTok = nil, 0
		}
		cur = append(cur, n)
		curTok += t
	}
	if len(cur) > 0 {
		groups = append(groups, cur)
	}
	var digests []string
	for gi, g := range groups {
		d, err := o.cachedReduce(ctx, c, w.Slug,
			fmt.Sprintf("digest:%d:%s", gi, key(g...)),
			"Shrň tyto dílčí poznámky do stručného českého souhrnu (zachovej strukturní body). Bez nadpisů.",
			strings.Join(g, "\n\n"))
		if err != nil {
			return "", err
		}
		digests = append(digests, d)
	}
	return o.hierReduce(ctx, c, w, digests)
}

func (o Options) cachedReduce(ctx context.Context, c *llm.Client, slug, tag, sys, user string) (string, error) {
	k := key(c.Model, PromptVersion, "reduce", tag, sys, user)
	if cached, ok := o.readCache(slug, k); ok {
		return cached, nil
	}
	if o.DryRun {
		return "", nil
	}
	out, err := c.Chat(ctx, sys, user, 2048)
	if err != nil {
		return "", fmt.Errorf("reduce %s/%s: %w", slug, tag, err)
	}
	o.writeCache(slug, k, c.Model, out)
	return out, nil
}

// PlanStats reports, without any network call, how many of the given chunk
// texts already have a cached map result.
func (o Options) PlanStats(slug, model string, chunkTexts []string) (hits, total int) {
	total = len(chunkTexts)
	for _, t := range chunkTexts {
		if _, ok := o.readCache(slug, key(model, PromptVersion, "map", t)); ok {
			hits++
		}
	}
	return hits, total
}

// SplitBody separates the reduced markdown into the summary and TOC sections.
func SplitBody(body string) (summary, toc string) {
	idx := strings.Index(body, "## Obsah")
	if idx < 0 {
		return strings.TrimSpace(stripHeader(body, "## Shrnutí")), ""
	}
	summary = strings.TrimSpace(stripHeader(body[:idx], "## Shrnutí"))
	toc = strings.TrimSpace(stripHeader(body[idx:], "## Obsah"))
	return summary, toc
}

func stripHeader(s, h string) string {
	s = strings.TrimSpace(s)
	if strings.HasPrefix(s, h) {
		return strings.TrimSpace(s[len(h):])
	}
	return s
}

func max1(n int) int {
	if n < 1 {
		return 1
	}
	return n
}

// Package chunk splits normalised text into overlapping windows sized for the
// model context, never breaking a verse/paragraph line mid-way.
package chunk

import (
	"strings"

	"worldlibrary/pipeline/internal/manifest"
)

// Chunk is one window of source text plus its provenance.
type Chunk struct {
	WorkSlug   string
	SourceFile string
	Parva      int // 0 unless the work is per-parva
	Ordinal    int // 0-based within its source file
	Text       string
	EstTokens  int
}

// EstimateTokens approximates the token count for a piece of text given the
// language class of the work.
func EstimateTokens(text string, lang manifest.LangClass) int {
	r := lang.TokenRatio()
	n := float64(len([]rune(text))) / r
	return int(n) + 1
}

// Split breaks text into chunks targeting budgetTokens per chunk with
// overlapFrac fractional overlap, splitting only on blank-line (paragraph)
// boundaries, falling back to single lines for very long paragraphs.
func Split(text string, lang manifest.LangClass, budgetTokens int, overlapFrac float64) []string {
	if budgetTokens <= 0 {
		budgetTokens = 8000
	}
	paras := splitParagraphs(text)
	var chunks []string
	var cur []string
	curTok := 0
	flush := func() {
		if len(cur) == 0 {
			return
		}
		chunks = append(chunks, strings.Join(cur, "\n\n"))
		// Carry the tail paragraphs as overlap into the next chunk.
		ov := int(float64(budgetTokens) * overlapFrac)
		var carry []string
		carryTok := 0
		for i := len(cur) - 1; i >= 0 && carryTok < ov; i-- {
			carry = append([]string{cur[i]}, carry...)
			carryTok += EstimateTokens(cur[i], lang)
		}
		cur = carry
		curTok = carryTok
	}
	for _, p := range paras {
		pt := EstimateTokens(p, lang)
		if pt > budgetTokens {
			// Oversized paragraph: emit current, then split by lines.
			flush()
			for _, part := range splitLong(p, lang, budgetTokens) {
				chunks = append(chunks, part)
			}
			cur, curTok = nil, 0
			continue
		}
		if curTok+pt > budgetTokens && len(cur) > 0 {
			flush()
		}
		cur = append(cur, p)
		curTok += pt
	}
	if len(cur) > 0 {
		chunks = append(chunks, strings.Join(cur, "\n\n"))
	}
	return chunks
}

func splitParagraphs(text string) []string {
	raw := strings.Split(text, "\n\n")
	var out []string
	for _, p := range raw {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

func splitLong(p string, lang manifest.LangClass, budget int) []string {
	lines := strings.Split(p, "\n")
	var out, cur []string
	tok := 0
	for _, ln := range lines {
		lt := EstimateTokens(ln, lang)
		if tok+lt > budget && len(cur) > 0 {
			out = append(out, strings.Join(cur, "\n"))
			cur, tok = nil, 0
		}
		cur = append(cur, ln)
		tok += lt
	}
	if len(cur) > 0 {
		out = append(out, strings.Join(cur, "\n"))
	}
	return out
}

// WorkChunks builds all chunks for a work. Per-parva works are chunked within
// each source file independently so a chunk never spans two parvas.
func WorkChunks(w manifest.Work, contents map[string]string, budgetTokens int) []Chunk {
	var out []Chunk
	for _, sf := range w.Sources {
		body, ok := contents[sf.Path]
		if !ok || strings.TrimSpace(body) == "" {
			continue
		}
		parts := Split(body, w.Lang, budgetTokens, 0.10)
		for i, part := range parts {
			out = append(out, Chunk{
				WorkSlug:   w.Slug,
				SourceFile: sf.Path,
				Parva:      sf.Parva,
				Ordinal:    i,
				Text:       part,
				EstTokens:  EstimateTokens(part, w.Lang),
			})
		}
	}
	return out
}

// Package textnorm decodes raw source bytes and strips Project Gutenberg
// boilerplate while preserving verse line breaks.
package textnorm

import (
	"bytes"
	"regexp"
	"strings"
	"unicode/utf8"
)

var (
	gutStart = regexp.MustCompile(`(?i)\*\*\* ?START OF (THE|THIS) PROJECT GUTENBERG EBOOK.*`)
	gutEnd   = regexp.MustCompile(`(?i)\*\*\* ?END OF (THE|THIS) PROJECT GUTENBERG EBOOK.*`)
	manyNL   = regexp.MustCompile(`\n{3,}`)
	trailing = regexp.MustCompile(`[ \t]+\n`)
)

// Decode converts raw bytes to a string. Valid UTF-8 (optionally BOM-prefixed)
// is used as-is; otherwise the bytes are treated as Latin-1.
func Decode(b []byte) string {
	b = bytes.TrimPrefix(b, []byte{0xEF, 0xBB, 0xBF}) // UTF-8 BOM
	s := string(b)
	if utf8.ValidString(s) {
		return s
	}
	// Latin-1 fallback: every byte maps to the same Unicode code point.
	var sb strings.Builder
	sb.Grow(len(b))
	for _, c := range b {
		sb.WriteRune(rune(c))
	}
	return sb.String()
}

// StripGutenberg removes the Project Gutenberg header and license footer.
// Texts without the markers (ctext.org, Mahabharata) are returned unchanged
// apart from whitespace normalisation.
func StripGutenberg(s string) string {
	if loc := gutStart.FindStringIndex(s); loc != nil {
		if nl := strings.IndexByte(s[loc[1]:], '\n'); nl >= 0 {
			s = s[loc[1]+nl+1:]
		} else {
			s = s[loc[1]:]
		}
	}
	if loc := gutEnd.FindStringIndex(s); loc != nil {
		s = s[:loc[0]]
	}
	return s
}

// Normalize collapses 3+ blank lines to 2 and trims trailing whitespace,
// keeping single newlines so verse line breaks survive.
func Normalize(s string) string {
	s = strings.ReplaceAll(s, "\r\n", "\n")
	s = strings.ReplaceAll(s, "\r", "\n")
	s = trailing.ReplaceAllString(s, "\n")
	s = manyNL.ReplaceAllString(s, "\n\n")
	return strings.TrimSpace(s)
}

// Clean is the full pipeline: Decode -> StripGutenberg -> Normalize.
func Clean(b []byte) string {
	return Normalize(StripGutenberg(Decode(b)))
}

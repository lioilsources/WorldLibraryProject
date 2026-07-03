// Package manifest is the single source of truth mapping the 28 round-1
// plaintext source files into 10 logical works, with the metadata and the
// fallback source URLs needed when Git-LFS content is unavailable.
package manifest

// LangClass selects the chars-per-token ratio used for chunk sizing.
type LangClass string

const (
	LangLatin    LangClass = "latin"    // English / Latin transliteration prose
	LangCJK      LangClass = "cjk"      // Classical Chinese (dense glyphs)
	LangTranslit LangClass = "translit" // long romanised Sanskrit tokens
)

// TokenRatio returns the approximate characters-per-token divisor.
func (l LangClass) TokenRatio() float64 {
	switch l {
	case LangCJK:
		return 1.6
	case LangTranslit:
		return 4.5
	default:
		return 4.0
	}
}

// SourceFile is one on-disk file (relative to the downloads root) plus the
// information needed to re-acquire it if the LFS object is missing.
type SourceFile struct {
	// Path is relative to the downloads directory, e.g.
	// "chinese/laozi/dao_de_jing.txt".
	Path string
	// FetchURL is a direct download URL (empty if only available via ZIP).
	FetchURL string
	// ZipURL + ZipMember describe a file that must be extracted from an
	// archive (Mahabharata Tokunaga edition).
	ZipURL    string
	ZipMember string
	// Parva is the 1-based parva index for Mahabharata files (0 otherwise).
	Parva int
}

// Work is one logical book assembled from one or more SourceFiles.
type Work struct {
	Slug      string
	TitleCS   string
	LangLabel string // human-readable, e.g. "klasická čínština"
	Script    string
	Lang      LangClass
	// PerFileParva marks works where each SourceFile is a structural unit
	// (Mahabharata: one file per parva) so the reducer can build a
	// per-parva table of contents.
	PerFileParva bool
	Sources      []SourceFile
}

const gutenberg = "https://www.gutenberg.org/cache/epub/"

func gut(id string) string { return gutenberg + id + "/pg" + id + ".txt" }

// Works is the static catalogue. Slugs are stable identifiers also used as
// cache subdirectory names; they are plain lowercase ASCII.
var Works = []Work{
	{
		Slug: "dao-de-jing", TitleCS: "Tao-te-ťing",
		LangLabel: "klasická čínština", Script: "čínské znaky (chan)", Lang: LangCJK,
		Sources: []SourceFile{{Path: "chinese/laozi/dao_de_jing.txt", FetchURL: "https://ctext.org/dao-de-jing/plaintext"}},
	},
	{
		Slug: "analekta", TitleCS: "Hovory (Lun-jü)",
		LangLabel: "klasická čínština", Script: "čínské znaky (chan)", Lang: LangCJK,
		Sources: []SourceFile{{Path: "chinese/konfucius/analects.txt", FetchURL: "https://ctext.org/analects/plaintext"}},
	},
	{
		Slug: "mengzi", TitleCS: "Mengzi",
		LangLabel: "klasická čínština", Script: "čínské znaky (chan)", Lang: LangCJK,
		Sources: []SourceFile{{Path: "chinese/mengzi/mengzi.txt", FetchURL: "https://ctext.org/mengzi/plaintext"}},
	},
	{
		Slug: "zhuangzi", TitleCS: "Čuang-c'",
		LangLabel: "klasická čínština", Script: "čínské znaky (chan)", Lang: LangCJK,
		Sources: []SourceFile{{Path: "chinese/zhuangzi/zhuangzi.txt", FetchURL: "https://ctext.org/zhuangzi/plaintext"}},
	},
	{
		Slug: "beowulf", TitleCS: "Beowulf (dva anglické překlady)",
		LangLabel: "angličtina (překlad ze staroangličtiny)", Script: "latinka", Lang: LangLatin,
		Sources: []SourceFile{
			{Path: "european/beowulf/pg16328.txt", FetchURL: gut("16328")},
			{Path: "european/beowulf/pg981.txt", FetchURL: gut("981")},
		},
	},
	{
		Slug: "edda-poeticka", TitleCS: "Poetická (Starší) Edda",
		LangLabel: "angličtina (překlad ze staré severštiny)", Script: "latinka", Lang: LangLatin,
		Sources: []SourceFile{{Path: "european/edda/poetic/pg1220.txt", FetchURL: gut("1220")}},
	},
	{
		Slug: "edda-prozaicka", TitleCS: "Prozaická (Snorriho) Edda",
		LangLabel: "angličtina (překlad ze staré severštiny)", Script: "latinka", Lang: LangLatin,
		Sources: []SourceFile{{Path: "european/edda/prose/pg43627.txt", FetchURL: gut("43627")}},
	},
	{
		Slug: "avesta-vendidad", TitleCS: "Avesta — Vendidád",
		LangLabel: "angličtina (překlad z avestánštiny)", Script: "latinka", Lang: LangLatin,
		Sources: []SourceFile{{Path: "avesta/sacred_texts/pg2131.txt", FetchURL: gut("2131")}},
	},
	{
		Slug: "avesta-yasna", TitleCS: "Avesta — Jasna",
		LangLabel: "angličtina (překlad z avestánštiny)", Script: "latinka", Lang: LangLatin,
		Sources: []SourceFile{{Path: "avesta/sacred_texts/pg18997.txt", FetchURL: gut("18997")}},
	},
	mahabharata(),
}

// mahabharata builds the single Mahabharata work from the 18 Tokunaga files,
// each marked as its own parva and sourced from one ZIP archive.
func mahabharata() Work {
	const zipURL = "https://sacred-texts.com/hin/maha/mahatxt.zip"
	w := Work{
		Slug: "mahabharata-tokunaga", TitleCS: "Mahábhárata – Tokunagova elektronická edice",
		LangLabel: "sanskrt", Script: "latinská transliterace", Lang: LangTranslit,
		PerFileParva: true,
	}
	for i := 1; i <= 18; i++ {
		name := "maha" + twoDigit(i) + ".txt"
		w.Sources = append(w.Sources, SourceFile{
			Path:      "sanskrit/mahabharata/" + name,
			ZipURL:    zipURL,
			ZipMember: name,
			Parva:     i,
		})
	}
	return w
}

func twoDigit(i int) string {
	if i < 10 {
		return "0" + string(rune('0'+i))
	}
	return string(rune('0'+i/10)) + string(rune('0'+i%10))
}

// AllSourcePaths returns every source path (relative to downloads root).
func AllSourcePaths() []string {
	var out []string
	for _, w := range Works {
		for _, s := range w.Sources {
			out = append(out, s.Path)
		}
	}
	return out
}

// Find returns the work whose slug contains sub (case-sensitive substring),
// or all works when sub is empty.
func Find(sub string) []Work {
	if sub == "" {
		return Works
	}
	var out []Work
	for _, w := range Works {
		if contains(w.Slug, sub) {
			out = append(out, w)
		}
	}
	return out
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return len(sub) == 0
}

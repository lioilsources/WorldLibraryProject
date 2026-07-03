// Package acquire materialises real file bytes for the manifest source files.
// Primary path: git-lfs. Fallbacks: direct URL download and a manual_drop/
// side-load directory. Every file is independent and the run never aborts on
// a single failure.
package acquire

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"worldlibrary/pipeline/internal/manifest"
)

const lfsMagic = "version https://git-lfs.github.com/spec/v1"

// Status values recorded per file.
const (
	StatusMaterialized = "MATERIALIZED"
	StatusFailed       = "FAILED"
	StatusSkipped      = "SKIPPED"
)

// Result is the per-file outcome.
type Result struct {
	Path   string `json:"path"`
	Status string `json:"status"`
	Source string `json:"source"` // disk|lfs|url|zip|manual
	SHA256 string `json:"sha256,omitempty"`
	Bytes  int64  `json:"bytes,omitempty"`
	Note   string `json:"note,omitempty"`
}

// Acquirer holds run configuration.
type Acquirer struct {
	DownloadsDir string // absolute path to downloads/
	RepoRoot     string // git repo root (for git lfs / git show)
	CacheRoot    string // .cache
	UserAgent    string
	NoLFS        bool
	NoNet        bool
}

// Report maps a downloads-relative path to its Result.
type Report map[string]Result

func isStub(path string) bool {
	f, err := os.Open(path)
	if err != nil {
		return false
	}
	defer f.Close()
	buf := make([]byte, len(lfsMagic))
	n, _ := io.ReadFull(f, buf)
	return strings.HasPrefix(string(buf[:n]), lfsMagic)
}

func sha256File(path string) (string, int64, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(h.Sum(nil)), n, nil
}

func materialized(path string) bool {
	st, err := os.Stat(path)
	if err != nil || st.Size() <= 1024 {
		return false
	}
	return !isStub(path)
}

// Run acquires every source path referenced by the manifest works.
func (a *Acquirer) Run(ctx context.Context) Report {
	srcByPath := map[string]manifest.SourceFile{}
	var paths []string
	for _, w := range manifest.Works {
		for _, s := range w.Sources {
			srcByPath[s.Path] = s
			paths = append(paths, s.Path)
		}
	}
	sort.Strings(paths)

	rep := Report{}

	// 1) Already on disk?
	var stubs []string
	for _, p := range paths {
		full := filepath.Join(a.DownloadsDir, p)
		if materialized(full) {
			sum, n, _ := sha256File(full)
			rep[p] = Result{Path: p, Status: StatusMaterialized, Source: "disk", SHA256: sum, Bytes: n}
			continue
		}
		stubs = append(stubs, p)
	}

	// 2) git lfs pull for the remaining (batched).
	if !a.NoLFS && len(stubs) > 0 {
		a.lfsPull(ctx, stubs)
		var still []string
		for _, p := range stubs {
			full := filepath.Join(a.DownloadsDir, p)
			if materialized(full) {
				sum, n, _ := sha256File(full)
				rep[p] = Result{Path: p, Status: StatusMaterialized, Source: "lfs", SHA256: sum, Bytes: n}
				continue
			}
			still = append(still, p)
		}
		stubs = still
	}

	// 3) manual_drop / URL fallback, per file.
	for _, p := range stubs {
		rep[p] = a.fallback(ctx, p, srcByPath[p])
	}

	a.writeReport(rep)
	return rep
}

func (a *Acquirer) lfsPull(ctx context.Context, paths []string) {
	includes := make([]string, len(paths))
	for i, p := range paths {
		includes[i] = "downloads/" + p
	}
	cctx, cancel := context.WithTimeout(ctx, 30*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(cctx, "git", "lfs", "pull",
		"--include="+strings.Join(includes, ","))
	cmd.Dir = a.RepoRoot
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "acquire: git lfs pull failed (continuing with fallbacks): %v\n", err)
	}
}

func (a *Acquirer) fallback(ctx context.Context, p string, sf manifest.SourceFile) Result {
	full := filepath.Join(a.DownloadsDir, p)

	// manual_drop/<same relative path>
	manual := filepath.Join(a.RepoRoot, "manual_drop", p)
	if st, err := os.Stat(manual); err == nil && st.Size() > 0 {
		if err := copyFile(manual, full); err == nil {
			sum, n, _ := sha256File(full)
			return Result{Path: p, Status: StatusMaterialized, Source: "manual", SHA256: sum, Bytes: n}
		}
	}

	if a.NoNet {
		return Result{Path: p, Status: StatusFailed, Source: "url", Note: "obsah nedostupný (offline, bez LFS)"}
	}

	var err error
	switch {
	case sf.FetchURL != "":
		err = a.download(ctx, sf.FetchURL, full)
	case sf.ZipURL != "":
		err = a.fromZip(ctx, sf.ZipURL, sf.ZipMember, full)
	default:
		err = fmt.Errorf("no fallback source")
	}
	if err != nil {
		return Result{Path: p, Status: StatusFailed, Source: "url", Note: "obsah nedostupný: " + err.Error()}
	}
	sum, n, _ := sha256File(full)
	src := "url"
	if sf.ZipURL != "" {
		src = "zip"
	}
	return Result{Path: p, Status: StatusMaterialized, Source: src, SHA256: sum, Bytes: n}
}

func (a *Acquirer) httpGet(ctx context.Context, url string) ([]byte, error) {
	var lastErr error
	backoff := 2 * time.Second
	for attempt := 0; attempt < 5; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(backoff):
			}
			backoff *= 2
		}
		cctx, cancel := context.WithTimeout(ctx, 30*time.Second)
		req, _ := http.NewRequestWithContext(cctx, http.MethodGet, url, nil)
		req.Header.Set("User-Agent", a.UserAgent)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			cancel()
			lastErr = err
			continue
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		cancel()
		if resp.StatusCode >= 500 || resp.StatusCode == 429 {
			lastErr = fmt.Errorf("http %d", resp.StatusCode)
			continue
		}
		if resp.StatusCode >= 400 {
			return nil, fmt.Errorf("http %d", resp.StatusCode)
		}
		return body, nil
	}
	return nil, fmt.Errorf("exhausted retries: %w", lastErr)
}

func (a *Acquirer) download(ctx context.Context, url, dest string) error {
	body, err := a.httpGet(ctx, url)
	if err != nil {
		return err
	}
	// Reject obvious HTML homepages (ctext.org redirect failure mode).
	head := strings.ToLower(strings.TrimSpace(string(body[:min(len(body), 256)])))
	if strings.HasPrefix(head, "<!doctype html") || strings.HasPrefix(head, "<html") {
		return fmt.Errorf("upstream returned HTML, not the text")
	}
	return atomicWrite(dest, body)
}

func (a *Acquirer) fromZip(ctx context.Context, url, member, dest string) error {
	cache := filepath.Join(a.CacheRoot, "zips", sha1Name(url)+".zip")
	var data []byte
	if b, err := os.ReadFile(cache); err == nil {
		data = b
	} else {
		b, err := a.httpGet(ctx, url)
		if err != nil {
			return err
		}
		data = b
		_ = os.MkdirAll(filepath.Dir(cache), 0o755)
		_ = atomicWrite(cache, b)
	}
	zr, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return fmt.Errorf("zip open: %w", err)
	}
	for _, f := range zr.File {
		if filepath.Base(f.Name) != member {
			continue
		}
		rc, err := f.Open()
		if err != nil {
			return err
		}
		defer rc.Close()
		content, err := io.ReadAll(rc)
		if err != nil {
			return err
		}
		return atomicWrite(dest, content)
	}
	return fmt.Errorf("member %q not found in zip", member)
}

func (a *Acquirer) writeReport(rep Report) {
	_ = os.MkdirAll(a.CacheRoot, 0o755)
	b, _ := json.MarshalIndent(rep, "", "  ")
	_ = atomicWrite(filepath.Join(a.CacheRoot, "acquire_report.json"), b)
}

func atomicWrite(dest string, b []byte) error {
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	tmp := dest + ".part"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, dest)
}

func copyFile(src, dest string) error {
	b, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	return atomicWrite(dest, b)
}

func sha1Name(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:8])
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

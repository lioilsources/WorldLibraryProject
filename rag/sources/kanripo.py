"""Kanripo (github.com/kanripo): jeden repozitář = jedno dílo, jeden soubor
`<ID>_<juan>.txt` = jeden svazek (卷), formát mandoku (org-mode).

V registru je `path` adresář s vybranými soubory díla v `downloads/`
a `source` nese původ, ať jde výběr zopakovat:

    source: {adapter: kanripo, repo: KR5c0126, branch: tls, commit: <sha>}

Větev `tls` je interpunkční vydání bez komentářů s kapitolami jako org
nadpisy (`** 1 逍遙遊第一`) — kde existuje, má přednost. Ostatní větve
(SBCK, WYG…) jsou edice bez interpunkce, komentář v nich je v závorkách
uvnitř řádku; `strip_notes` (výchozí ano) ho vyhodí, knihovna drží
základní text.

Co se zahazuje: hlavička `#…`, značky stránek `<pb:…>`, konce řádků
zdroje `¶`. Řádky jednoho odstavce se spojí bez mezery (čínština),
odstavce oddělí `\\n`.

Kapitola = org nadpis nejvyšší úrovně, která v díle je; dílo bez nadpisů
má kapitolu na svazek. Edice bez org nadpisů (Sï-kchu) mají kapitoly jako
odsazené řádky: `chapter_re` (regex nad řádkem i s ideografickými
mezerami) z nich udělá nadpisy, `drop_re` vyhodí šablonové řádky
(欽定四庫全書, 墨子卷一). `ref` je číslo z nadpisu (`** 1 逍遙遊第一` → '1'),
jinak pořadí.

Stažení díla (klon do $UPSTREAM_DIR na připnutém commitu, výběr do downloads):
    python3 -m sources.kanripo fetch KR5c0126 tls ../downloads/chinese/kanripo
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from sources import Chapter

CLEAN = True
PB_RE = re.compile(r"<pb:[^>]*>")
NOTE_RE = re.compile(r"\([^()]*\)")
HEADING_RE = re.compile(r"^(\*+)\s+(.*\S)\s*$")
REF_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+(.+)$")
JUAN_RE = re.compile(r"_(\d{3})\.txt$")


def juan_files(root: Path) -> list[Path]:
    return sorted(f for f in root.iterdir() if f.is_file() and JUAN_RE.search(f.name))


def _lines(file: Path, strip_notes: bool, chapter_re=None, drop_re=None):
    for raw in file.read_text(encoding="utf-8").splitlines():
        if raw.startswith("#"):
            continue
        line = PB_RE.sub("", raw).replace("¶", "")
        if strip_notes:
            line = NOTE_RE.sub("", line)
        line = line.rstrip()
        if drop_re and drop_re.search(line):
            continue
        if chapter_re and chapter_re.search(line):
            line = "* " + line.strip()
        yield line.strip().replace("　", "")


def extract(root: Path, entry: dict | None = None) -> list[Chapter]:
    src = (entry or {}).get("source") or {}
    strip_notes = src.get("strip_notes", True)
    chapter_re = re.compile(src["chapter_re"]) if src.get("chapter_re") else None
    drop_re = re.compile(src["drop_re"]) if src.get("drop_re") else None
    files = juan_files(root)
    if not files:
        raise ValueError(f"{root}: žádné soubory <ID>_NNN.txt")

    # (juan, úroveň nadpisu | None, text řádku)
    items = []
    for f in files:
        juan = int(JUAN_RE.search(f.name).group(1))
        for line in _lines(f, strip_notes, chapter_re, drop_re):
            m = HEADING_RE.match(line)
            items.append((juan, len(m.group(1)) if m else None, m.group(2) if m else line))
    levels = [lv for _, lv, _ in items if lv]
    top = min(levels) if levels else None

    chapters: list[tuple[str | None, str, list[str]]] = []   # (ref, nadpis, řádky)
    cur_juan = None
    for juan, lv, text in items:
        if top is not None:
            if lv == top:
                m = REF_RE.match(text)
                chapters.append((m.group(1) if m else None, m.group(2) if m else text, []))
                continue
            if not chapters:          # titulek před první kapitolou
                continue
        elif juan != cur_juan:
            chapters.append((str(juan), f"卷{juan}", []))
            cur_juan = juan
        if lv:                         # hlubší nadpis zůstane v textu jako odstavec
            chapters[-1][2].extend(["", text, ""])
        else:
            chapters[-1][2].append(text)

    out = []
    for i, (ref, heading, lines) in enumerate(chapters, 1):
        paras, buf = [], []
        for line in lines + [""]:
            if line:
                buf.append(line)
            elif buf:
                paras.append("".join(buf))
                buf = []
        text = "\n".join(paras)
        if text.strip():
            out.append(Chapter(ref=ref or str(i), heading=heading, text=text))
    return out


# --- stažení ------------------------------------------------------------------

def fetch(repo: str, branch: str, dest_root: Path, upstream: Path) -> str:
    """Klon `kanripo/<repo>` větve `branch` do upstream, kopie TXT svazků do
    dest_root/<repo>. Vrací SHA commitu pro registr."""
    clone = upstream / "kanripo" / repo
    url = f"https://github.com/kanripo/{repo}"
    if not (clone / ".git").exists():
        clone.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(clone), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(clone), "remote", "add", "origin", url], check=True)
    subprocess.run(["git", "-C", str(clone), "fetch", "-q", "--depth", "1", "origin", branch], check=True)
    subprocess.run(["git", "-C", str(clone), "checkout", "-q", "--detach", "FETCH_HEAD"], check=True)
    sha = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    dest = dest_root / repo
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for f in juan_files(clone):
        shutil.copy2(f, dest / f.name)
    return sha


def main() -> int:
    p = argparse.ArgumentParser(description="Kanripo: stažení díla do downloads/")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("repo")
    f.add_argument("branch")
    f.add_argument("dest", type=Path)
    f.add_argument("--upstream", type=Path,
                   default=Path(os.environ.get("UPSTREAM_DIR", Path.home() / "wlp-upstream")))
    s = sub.add_parser("stats", help="kapitoly a délky z adresáře díla")
    s.add_argument("dir", type=Path)
    s.add_argument("--keep-notes", action="store_true")
    s.add_argument("--chapter-re")
    s.add_argument("--drop-re")
    args = p.parse_args()
    if args.cmd == "fetch":
        sha = fetch(args.repo, args.branch, args.dest, args.upstream)
        print(f"{args.repo} {args.branch} {sha}")
        return 0
    chs = extract(args.dir, {"source": {"strip_notes": not args.keep_notes, "chapter_re": args.chapter_re,
                                        "drop_re": args.drop_re}})
    for c in chs:
        print(f"{c.ref:>5} {len(c.text):>7}  {c.heading[:40]}")
    print(f"kapitol {len(chs)}, znaků {sum(len(c.text) for c in chs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

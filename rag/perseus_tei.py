"""Perseus Digital Library (canonical-greekLit / canonical-latinLit) → díla,
kapitoly a text pro ingest. Bere **jen originály**: řecký autor (`tlg*`) v
řečtině, latinský (`phi*`, `stoa*`) v latině. Anglické (`-eng*`) i latinské
překlady řeckých autorů (Vallův Thúkýdidés, `1st1K-lat`) vypadnou — knihovna
drží originály, to je premisa projektu.

Metadata: `__cts__.xml` na úrovni autora (groupname) a díla (title, edice,
popis edice); kde chybí, `teiHeader` (title, author). Struktura: kapitola =
první úroveň `<div type="textpart">` pod edicí (subtype se porovnává bez
ohledu na velikost — Ílias má 'Book'), hlubší `textpart` jdou do citace
`ref` chunku, ne do kapitol. Víc edic téhož díla (grc2/grc3/grc4) → jedna:
nejdelší text, remíza → nejnižší číslo; volba se zapisuje do
registry/perseus_editions.tsv, aby šla ručně přebít.

Použití (test):
    python3 perseus_tei.py --root ../downloads/greek_latin/perseus --stats-only
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

TEI = "http://www.tei-c.org/ns/1.0"
CTS = "http://chs.harvard.edu/xmlns/cts"
XML = "http://www.w3.org/XML/1998/namespace"
NS = {"tei": TEI, "ti": CTS, "xml": XML}

# repozitář → jazyk originálu; edice v jiném jazyce jsou překlady
ORIGINAL_LANG = {"greek": "grc", "latin": "lat"}
EDITION_RE = re.compile(r"^(tlg|phi|stoa)\d+\.(tlg|phi|stoa)\d+\.([\w-]+?)-(grc|lat|eng|ger|fre|ita|ara|heb)(\d*)\.xml$")

# subtype → český popisek do cesty kapitoly
SUBTYPE_CS = {
    "book": "Kniha", "chapter": "Kapitola", "section": "Oddíl", "verse": "Verš", "line": "Verš",
    "poem": "Báseň", "epigram": "Epigram", "letter": "Dopis", "hymn": "Hymnus", "ode": "Óda",
    "satire": "Satira", "speech": "Řeč", "oration": "Řeč", "fable": "Bajka", "episode": "Epizoda",
    "part": "Část", "volume": "Svazek", "fragment": "Fragment", "entry": "Heslo", "strophe": "Strofa",
    "antistrophe": "Antistrofa", "epode": "Epóda", "act": "Jednání", "scene": "Scéna", "card": "Karta",
    "paragraph": "Odstavec", "subsection": "Pododdíl", "subchapter": "Podkapitola", "number": "Číslo",
    "life": "Život", "sermon": "Homilie", "title": "Titul", "epistle": "List", "dialogue": "Dialog",
    "work": "Dílo", "eclogue": "Ekloga", "idyll": "Idyla", "elegy": "Elegie", "argument": "Argument",
}

# díla, kterým se nedává prioritu 2, ale 3 (fragmenty, lexika, scholia, sborníky)
LOW_PRIORITY_HINTS = ("fragment", "scholia", "lexicon", "epitome", "excerpt", "testimon")
LOW_PRIORITY_GROUPS = {"tlg7000", "tlg1389", "tlg4083", "tlg9010"}  # Anthologia Graeca, Harpokration, lexika…


@dataclass
class PerseusWork:
    work_id: str            # grc.tlg0012.tlg001
    group: str              # tlg0012
    work: str               # tlg001
    lang: str               # grc | lat
    subgroup: str           # greek | latin
    author: str
    title: str              # originální název (label edice), jinak eng title
    title_en: str
    edition_desc: str
    urn: str
    file: Path
    candidates: list[str] = field(default_factory=list)   # ostatní edice
    priority: int = 2


@dataclass
class TeiChapter:
    ref: str                # '1', '1.3'
    subtype: str            # 'book'
    heading: str            # 'Kniha 1' nebo <head> text
    text: str
    level: int = 1


# --- metadata -----------------------------------------------------------------

def _text(el) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _cts_text(tree, xpath: str, lang: str | None = None) -> str:
    els = tree.findall(xpath, NS)
    if lang:
        for e in els:
            if e.get(f"{{{XML}}}lang") == lang:
                return _text(e)
    return _text(els[0]) if els else ""


def _parse_cts(path: Path):
    try:
        return etree.parse(str(path))
    except (etree.XMLSyntaxError, OSError):
        return None


def _tei_header(file: Path) -> tuple[str, str, str]:
    """(title, author, lang edice) z teiHeader — fallback bez __cts__."""
    try:
        tree = etree.parse(str(file))
    except (etree.XMLSyntaxError, OSError):
        return "", "", ""
    ts = tree.find(".//tei:teiHeader//tei:titleStmt", NS)
    title = author = ""
    if ts is not None:
        t = ts.find("tei:title", NS)
        a = ts.find("tei:author", NS)
        title, author = _text(t), _text(a)
    lang_el = tree.find(".//tei:text", NS)
    lang = lang_el.get(f"{{{XML}}}lang", "") if lang_el is not None else ""
    return title, author, lang


def scan_works(root: Path) -> list[PerseusWork]:
    """Projde greek/ a latin/ a vrátí jedno dílo na (skupina, dílo) — jen
    originály, jedna edice."""
    works: list[PerseusWork] = []
    for repo_dir, lang in ORIGINAL_LANG.items():
        data = root / repo_dir / "data"
        if not data.is_dir():
            continue
        for grp_dir in sorted(p for p in data.iterdir() if p.is_dir() and not p.name.startswith(".")):
            author = ""
            gx = grp_dir / "__cts__.xml"
            if gx.exists() and (t := _parse_cts(gx)) is not None:
                author = _cts_text(t, ".//ti:groupname", "eng") or _cts_text(t, ".//ti:groupname")
            work_dirs = sorted(p for p in grp_dir.iterdir() if p.is_dir())
            names = {p.name for p in work_dirs}
            for wd in work_dirs:
                # Livius: 'phi001' (celé dílo) + 'phi0011'…'phi00145' (po knihách)
                # → rozsekané verze jsou duplicita celku
                base = re.match(r"^([a-z]+\d{3})\d+$", wd.name)
                if base and base.group(1) in names:
                    continue
                files = [f for f in wd.iterdir() if f.suffix == ".xml" and f.name != "__cts__.xml"]
                editions = []
                for f in files:
                    m = EDITION_RE.match(f.name)
                    if m and m.group(4) == lang:
                        editions.append(f)
                if not editions:
                    continue
                title_en = label = desc = urn = ""
                wx = wd / "__cts__.xml"
                if wx.exists() and (t := _parse_cts(wx)) is not None:
                    title_en = _cts_text(t, ".//ti:title", "eng") or _cts_text(t, ".//ti:title")
                    urn = t.getroot().get("urn", "")
                    for ed in t.findall(".//ti:edition", NS):
                        ed_urn = ed.get("urn", "")
                        if ed_urn and any(f.stem == ed_urn.split(":")[-1] for f in editions):
                            label = label or _cts_text(ed, "ti:label")
                            desc = desc or _cts_text(ed, "ti:description")
                best = choose_edition(editions)
                if not (title_en or label):
                    h_title, h_author, _ = _tei_header(best)
                    label = label or h_title
                    author = author or h_author
                work_id = f"{lang}.{grp_dir.name}.{wd.name}"
                w = PerseusWork(
                    work_id=work_id, group=grp_dir.name, work=wd.name, lang=lang, subgroup=repo_dir,
                    author=nfc(author), title=nfc(label or title_en or wd.name), title_en=nfc(title_en),
                    edition_desc=nfc(desc), urn=urn or f"urn:cts:{'greekLit' if lang == 'grc' else 'latinLit'}:{grp_dir.name}.{wd.name}",
                    file=best, candidates=[f.name for f in editions if f != best],
                )
                w.priority = default_priority(w)
                works.append(w)
    return works


def choose_edition(editions: list[Path]) -> Path:
    """Nejdelší soubor; remíza → nejnižší číslo edice (perseus-grc1 < grc2)."""
    def key(f: Path):
        m = EDITION_RE.match(f.name)
        num = int(m.group(5) or 0) if m else 0
        return (-f.stat().st_size, num)
    return sorted(editions, key=key)[0]


def default_priority(w: PerseusWork) -> int:
    t = (w.title_en + " " + w.title).lower()
    if w.group in LOW_PRIORITY_GROUPS or any(h in t for h in LOW_PRIORITY_HINTS):
        return 3
    return 2


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


# --- text a kapitoly ----------------------------------------------------------

_DROP_TAGS = {"note", "bibl", "del", "app", "rdg", "teiHeader", "figDesc", "pb", "lb", "milestone", "fw", "ref", "witDetail"}


def _strip_noise(el) -> None:
    """Odstraní aparát a poznámky (in place); <lem> se ponechá jako text."""
    for tag in list(_DROP_TAGS):
        for n in el.iter(f"{{{TEI}}}{tag}"):
            parent = n.getparent()
            if parent is None:
                continue
            # zachovat tail (text za elementem)
            if n.tail:
                prev = n.getprevious()
                if prev is not None:
                    prev.tail = (prev.tail or "") + n.tail
                else:
                    parent.text = (parent.text or "") + n.tail
            parent.remove(n)


def _render(el) -> str:
    """Text elementu s řádky pro verše, odstavce a repliky."""
    parts: list[str] = []

    def walk(node):
        tag = etree.QName(node).localname if isinstance(node.tag, str) else ""
        if tag in ("l", "p", "head", "speaker", "label", "lg", "sp", "item", "list", "quote", "cit", "said", "ab"):
            parts.append("\n")
        if tag == "gap":
            parts.append(" … ")
        if tag == "speaker":
            txt = _text(node)
            if txt:
                parts.append(txt.upper() + ": ")
            if node.tail:
                parts.append(node.tail)
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                parts.append(child.tail)
        if tag in ("l", "p", "head", "lg", "item", "quote", "ab"):
            parts.append("\n")

    walk(el)
    text = "".join(parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _textparts(div):
    return [d for d in div if isinstance(d.tag, str) and etree.QName(d).localname == "div"
            and (d.get("type") or "").lower() == "textpart"]


def extract(file: Path) -> tuple[list[TeiChapter], str]:
    """Kapitoly + celý text díla. Kapitola = první úroveň textpartů pod
    edicí; když je jen jeden, sestoupí se o úroveň (Ílias: Book › line se
    nedělí dál — verše jsou už uvnitř Book)."""
    parser = etree.XMLParser(recover=True, huge_tree=True)
    tree = etree.parse(str(file), parser)
    body = tree.find(".//tei:text/tei:body", NS)
    if body is None:
        # TEI P4 (<TEI.2>, bez namespace): Appendix Vergiliana, Tacitovy Anály…
        body = tree.find(".//text/body")
        if body is None:
            return [], ""
        return _extract_p4(body)
    _strip_noise(body)
    edition = None
    for d in body.iter(f"{{{TEI}}}div"):
        if (d.get("type") or "").lower() in ("edition", "translation"):
            edition = d
            break
    top = edition if edition is not None else body
    parts = _textparts(top)
    while len(parts) == 1 and _textparts(parts[0]):
        parts = _textparts(parts[0])
    chapters: list[TeiChapter] = []
    for d in parts:
        subtype = (d.get("subtype") or "textpart").lower()
        ref = d.get("n") or str(len(chapters) + 1)
        head = d.find("tei:head", NS)
        head_txt = _text(head) if head is not None else ""
        label = f"{SUBTYPE_CS.get(subtype, subtype.capitalize())} {ref}"
        heading = f"{label} — {head_txt}" if head_txt and head_txt.lower() != label.lower() else label
        text = _render(d)
        if text:
            chapters.append(TeiChapter(ref=ref, subtype=subtype, heading=nfc(heading), text=nfc(text)))
    if not chapters:
        text = _render(top)
        if text:
            chapters.append(TeiChapter(ref="1", subtype="work", heading="(bez členění)", text=nfc(text)))
    return chapters, "\n\n".join(c.text for c in chapters)


def _extract_p4(body) -> tuple[list[TeiChapter], str]:
    """Starý TEI.2: kapitoly jsou <div1 type="book" n="1">, hlouběji div2…
    Bez namespace, ale localname logika _render funguje stejně."""
    for tag in ("note", "bibl", "del", "app", "rdg", "pb", "lb", "milestone", "fw"):
        for n in list(body.iter(tag)):
            parent = n.getparent()
            if parent is None:
                continue
            if n.tail:
                prev = n.getprevious()
                if prev is not None:
                    prev.tail = (prev.tail or "") + n.tail
                else:
                    parent.text = (parent.text or "") + n.tail
            parent.remove(n)
    parts = [d for d in body if isinstance(d.tag, str) and d.tag in ("div1", "div")]
    while len(parts) == 1:
        inner = [d for d in parts[0] if isinstance(d.tag, str) and d.tag in ("div1", "div2", "div")]
        if not inner:
            break
        parts = inner
    chapters = []
    for d in parts:
        subtype = (d.get("type") or d.get("subtype") or "textpart").lower()
        ref = d.get("n") or str(len(chapters) + 1)
        head = d.find("head")
        head_txt = _text(head) if head is not None else ""
        label = f"{SUBTYPE_CS.get(subtype, subtype.capitalize())} {ref}"
        heading = f"{label} — {head_txt}" if head_txt and head_txt.lower() != label.lower() else label
        text = _render(d)
        if text:
            chapters.append(TeiChapter(ref=ref, subtype=subtype, heading=nfc(heading), text=nfc(text)))
    if not chapters:
        text = _render(body)
        if text:
            chapters.append(TeiChapter(ref="1", subtype="work", heading="(bez členění)", text=nfc(text)))
    return chapters, "\n\n".join(c.text for c in chapters)


def write_editions_tsv(works: list[PerseusWork], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["work_id", "author", "title", "edition_file", "other_editions", "priority"])
        for wk in works:
            w.writerow([wk.work_id, wk.author, wk.title, wk.file.name, "|".join(wk.candidates), wk.priority])


def main() -> int:
    p = argparse.ArgumentParser(description="Perseus TEI → statistika/kapitoly")
    p.add_argument("--root", default="../downloads/greek_latin/perseus")
    p.add_argument("--stats-only", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--editions-tsv", help="zapsat volbu edic")
    args = p.parse_args()
    works = scan_works(Path(args.root))
    print(f"děl (originály, 1 edice): {len(works)}  grc={sum(w.lang=='grc' for w in works)} "
          f"lat={sum(w.lang=='lat' for w in works)}  s více edicemi={sum(bool(w.candidates) for w in works)}")
    if args.editions_tsv:
        write_editions_tsv(works, Path(args.editions_tsv))
    total_chars = total_ch = 0
    for i, w in enumerate(works):
        if args.limit and i >= args.limit:
            break
        chapters, text = extract(w.file)
        total_chars += len(text)
        total_ch += len(chapters)
        if args.stats_only and (i < 12 or not chapters):
            kinds = sorted({c.subtype for c in chapters})
            print(f"  {w.work_id:<24} {w.author[:22]:<23} {w.title[:28]:<29} kap {len(chapters):>4} {kinds} {len(text)/1000:.0f}k zn")
    print(f"\ncelkem: {total_ch} kapitol, {total_chars/1e6:.1f} M znaků")
    return 0


if __name__ == "__main__":
    sys.exit(main())

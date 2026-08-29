"""Detekce kapitol v textu díla — per tradice, podle názvu detektoru
z registru (`chapters: {detector: …}` v registry/works.yaml).

Vstup jsou řádky vyčištěného textu (clean_text.clean), výstup seznam
`Chapter` pokrývající celý dokument: každá kapitola je úsek řádků od svého
nadpisu k dalšímu nadpisu (jakékoli úrovně). Text před první kapitolou se
stane kapitolou „(úvod)", je-li podstatný, jinak se přilepí k první.

Vzory jsou z měření na korpusu (viz PLAN-ol1nllm-integration.md a
docstringy níže) — každý detektor má očekávaný počet zásahů na reálném
souboru a ten hlídá tests/test_chapters.py nad výřezy v tests/fixtures/.

Chunkování probíhá až nad kapitolami (ingest_books.py) — chunk nikdy
nekříží hranici kapitoly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- datové typy --------------------------------------------------------------


@dataclass
class Mark:
    """Nalezený nadpis: index řádku, úroveň (1 = nejvyšší), citace a text."""
    line: int
    level: int
    ref: str
    heading: str


@dataclass
class Chapter:
    ordinal: int              # 1..n v pořadí dokumentu, napříč úrovněmi
    level: int
    parent_ordinal: int | None
    ref: str                  # '8', 'SECTION XII', '1.3', 'Vagga 3'
    heading: str
    start: int                # index prvního řádku (nadpis) — včetně
    end: int                  # index za posledním řádkem — bez
    path: str = ""            # 'Kniha 1 › Kapitola 3' — složí _build
    children: list[int] = field(default_factory=list)

    def text(self, lines: list[str]) -> str:
        return "\n".join(lines[self.start:self.end]).strip()


# --- pomocníci ----------------------------------------------------------------

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def cn_to_int(s: str) -> int:
    """'八' → 8, '十二' → 12, '八十一' → 81, '一百' → 100."""
    total, num = 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            total += (num if num else 1) * unit
            num = 0
    return total + num


_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman_to_int(s: str) -> int:
    total, prev = 0, 0
    for ch in reversed(s.upper()):
        v = _ROMAN.get(ch, 0)
        total += -v if v < prev else v
        prev = max(prev, v)
    return total


def _is_blank(lines: list[str], i: int) -> bool:
    return i < 0 or i >= len(lines) or not lines[i].strip()


def _next_nonblank(lines: list[str], i: int, limit: int = 3) -> int | None:
    for j in range(i + 1, min(len(lines), i + 1 + limit)):
        if lines[j].strip():
            return j
    return None


def _drop_toc(marks: list[Mark], key) -> list[Mark]:
    """Gutenbergovy edice mají před textem obsah se stejnými nadpisy.
    Číselná řada se tam „restartuje" (…XXXIII, pak zase I) — bereme až
    poslední monotónní řadu."""
    if len(marks) < 2:
        return marks
    start = 0
    for i in range(1, len(marks)):
        try:
            if key(marks[i]) <= key(marks[i - 1]):
                start = i
        except (ValueError, TypeError):
            continue
    return marks[start:]


def _drop_toc_by_gap(marks: list[Mark], max_gap: int = 4) -> list[Mark]:
    """Obsah na začátku knihy = řada nadpisů, mezi nimiž je jen pár řádků.
    Zahodí úvodní běh marks s rozestupy ≤ max_gap, pokud za ním následuje
    aspoň jeden nadpis s reálným textem."""
    if len(marks) < 2:
        return marks
    # první nadpis, za kterým je reálný text (rozestup > max_gap)
    k = next((k for k in range(len(marks) - 1) if marks[k + 1].line - marks[k].line > max_gap), None)
    if k is None or k == 0:
        return marks
    # nadpis části těsně před ním (úroveň výš) k němu patří
    if marks[k - 1].level < marks[k].level and marks[k].line - marks[k - 1].line <= max_gap:
        k -= 1
    return marks[k:]


FRONT_MATTER_RE = re.compile(
    r"^(?:top|cover|front matter|title page|copyright|contents|table of contents|index|"
    r"pronouncing index.*|glossary|bibliography|abbreviations|list of .*|errata?|"
    r"acknowledg(?:e)?ments|notes|introductory note|obsah|rejstřík)\.?$",
    re.IGNORECASE,
)


def is_front_matter(heading: str) -> bool:
    """Kapitoly z PDF osnovy nebo Gutenbergu, které nejsou textem díla
    (obálka, tiráž, obsah, rejstřík). Ingest je nechunkuje."""
    h = " ".join(heading.split())
    return bool(FRONT_MATTER_RE.match(h)) or h.endswith("— Err")


# --- detektory ----------------------------------------------------------------
# každý: (lines) -> list[Mark]

def d_zh_zhang(lines):
    """Tao te ťing: '第一章' … '第八十一章' (81)."""
    rx = re.compile(r"^\s*第([一二三四五六七八九十百零〇]+)章")
    return [Mark(i, 1, str(cn_to_int(m.group(1))), line.strip())
            for i, line in enumerate(lines) if (m := rx.match(line))]


def d_zh_lunyu(lines):
    """Hovory: '學而第一' … '堯曰第二十' (20 knih)."""
    rx = re.compile(r"^\s*(\S{1,6})第([一二三四五六七八九十]+)\s*$")
    return [Mark(i, 1, str(cn_to_int(m.group(2))), line.strip())
            for i, line in enumerate(lines) if (m := rx.match(line))]


def d_zh_juan(lines):
    """Mencius: '卷之一梁惠王上' … (14 svazků); oddíly uvnitř jsou řádky
    s čínským číslem odsazené plnošířkovými mezerami (úroveň 2)."""
    top = re.compile(r"^\s*卷之(\S+)")
    sub = re.compile(r"^[\s　]*([一二三四五六七八九十]+)[\s　]*$")
    marks = []
    for i, line in enumerate(lines):
        if (m := top.match(line)):
            marks.append(Mark(i, 1, m.group(1), line.strip()))
        elif marks and (m := sub.match(line)):
            marks.append(Mark(i, 2, str(cn_to_int(m.group(1))), line.strip()))
    return marks


def d_en_chapter_roman(lines):
    """'CHAPTER I.' … (Zhuangzi/Giles 33, Prozaická Edda). Obsah před
    textem se zahodí přes _drop_toc."""
    rx = re.compile(r"^\s*CHAPTER\s+([IVXLC]+)\.?\s*$")
    marks = []
    for i, line in enumerate(lines):
        if (m := rx.match(line)):
            j = _next_nonblank(lines, i)
            title = lines[j].strip() if j is not None and lines[j].strip().isupper() else ""
            heading = f"{line.strip()} {title}".strip()
            marks.append(Mark(i, 1, str(roman_to_int(m.group(1))), heading))
    return _drop_toc(marks, key=lambda m: int(m.ref))


def _roman_with_title(lines, rx):
    marks = []
    for i, line in enumerate(lines):
        if (m := rx.match(line)):
            j = _next_nonblank(lines, i)
            title = lines[j].strip() if j is not None and lines[j].strip().isupper() and len(lines[j].strip()) > 3 else ""
            heading = f"{m.group(1)}. {title}".strip(". ")
            marks.append(Mark(i, 1, str(roman_to_int(m.group(1))), heading))
    return _drop_toc(marks, key=lambda m: int(m.ref))


def d_roman_dot_upper_title(lines):
    """Beowulf (Hall): 'I.' na řádku, pod ním titulek VELKÝMI."""
    return _roman_with_title(lines, re.compile(r"^\s*([IVXLC]+)\.\s*$"))


def d_roman_bare(lines):
    """Beowulf (Gummere): 'I' na řádku bez tečky."""
    return _roman_with_title(lines, re.compile(r"^\s*([IVXLC]+)\s*$"))


_EDDA_SKIP = {"INTRODUCTORY NOTE", "NOTES", "CONTENTS", "PREFACE", "GENERAL INTRODUCTION",
              "INTRODUCTION", "BIBLIOGRAPHY", "PRONOUNCING INDEX", "INDEX"}


def d_edda_poetic(lines):
    """Poetická Edda (Bellows): 'PART I' / 'PART II' (úroveň 1) a názvy
    básní VELKÝMI na samostatném řádku mezi prázdnými (úroveň 2).
    'INTRODUCTORY NOTE' a 'NOTES' patří k básni, nejsou kapitolou."""
    part = re.compile(r"^\s*PART\s+([IVX]+)\b")
    marks = []
    for i, line in enumerate(lines):
        s = line.strip()
        if (m := part.match(line)):
            marks.append(Mark(i, 1, str(roman_to_int(m.group(1))), s))
            continue
        if not marks:
            continue  # před PART I je jen aparát
        if (s.isupper() and 3 <= len(s) <= 40 and not any(ch.isdigit() for ch in s)
                and re.fullmatch(r"[A-Z][A-Z' \-]+", s) and s not in _EDDA_SKIP
                and not any(w in s for w in ("INDEX", "NOTE", "CONTENTS", "GLOSSARY", "APPENDIX"))
                and _is_blank(lines, i - 1) and _is_blank(lines, i + 1)):
            # 'LAYS OF THE GODS' / 'LAYS OF THE HEROES' jsou oddíly nad básněmi
            if s.startswith("LAYS OF"):
                marks.append(Mark(i, 2, s.title(), s))
                continue
            # 'VOLUSPO' (severský název, jedno slovo) a dva řádky pod ním
            # 'THE WISE-WOMAN'S PROPHECY' je jedna báseň — anglický titul se
            # přilepí k severskému
            if (marks and marks[-1].level == 3 and i - marks[-1].line <= 3
                    and " " not in marks[-1].heading and " " in s):
                marks[-1].heading = f"{marks[-1].heading} — {s.title()}"
                continue
            marks.append(Mark(i, 3, s.title(), s))
    return marks


_PROSE_PART_SKIP = {"PREFACE", "CONTENTS", "INTRODUCTION", "FOREWORD", "NOTES", "INDEX",
                    "VOCABULARY", "BIBLIOGRAPHY", "AFTERWORD"}


def d_edda_prose(lines):
    """Mladší Edda (Anderson): části VELKÝMI ('THE FOOLING OF GYLFE.',
    'BRAGE'S TALK') jako úroveň 1, pod nimi 'CHAPTER I.' … s číslováním od
    začátku v každé části (úroveň 2). Obsah na začátku má rozestupy 3 řádky
    → _drop_toc_by_gap; titulek pod CHAPTER (VELKÝMI) není část."""
    chap = re.compile(r"^\s*CHAPTER\s+([IVXLC]+)\.?\s*$")
    part = re.compile(r"^[A-Z][A-Z'’ ,\-]{4,60}\.?$")
    marks, last_chapter_line = [], -10
    for i, line in enumerate(lines):
        s = line.strip()
        if (m := chap.match(line)):
            j = _next_nonblank(lines, i)
            title = lines[j].strip() if j is not None and lines[j].strip().isupper() else ""
            marks.append(Mark(i, 2, str(roman_to_int(m.group(1))), f"{s} {title}".strip()))
            last_chapter_line = i
        elif part.match(s) and i - last_chapter_line > 2:
            key = s.rstrip(".").strip().replace("’", "'")
            if key in _PROSE_PART_SKIP or key.startswith(("AFTERWORD", "NOTES")):
                continue
            marks.append(Mark(i, 1, key.title(), s))
    return _drop_toc_by_repeat(marks) or _drop_toc_by_gap(marks)


def _drop_toc_by_repeat(marks: list[Mark]) -> list[Mark]:
    """Obsah na začátku opakuje nadpisy částí: první nadpis úrovně 1, který
    se později objeví znovu, označuje obsah — tělo začíná jeho druhým
    výskytem. Vrací [] když se nic neopakuje (volající má fallback)."""
    seen: dict[str, int] = {}
    for k, m in enumerate(marks):
        if m.level != 1:
            continue
        if m.ref in seen:
            return marks[k:]
        seen[m.ref] = k
    return []


def d_mbh_section(lines):
    """Mahábhárata (Ganguli): 'SECTION XII' (11 z 18 svazků)."""
    rx = re.compile(r"^\s*SECTION\s+([IVXLCDM]+)(?:-[IVXLCDM]+)?\s*$")
    return [Mark(i, 1, str(roman_to_int(m.group(1))), line.strip())
            for i, line in enumerate(lines) if (m := rx.match(line))]


def d_mbh_bare_number(lines):
    """Mahábhárata svazky 08–11, 16–18: sekce jsou holá arabská čísla na
    řádku, monotónně od 1. Bere se jen souvislá řada (1, 2, 3 …), aby
    číslo verše nebo stránky nezaložilo kapitolu."""
    rx = re.compile(r"^\s*(\d{1,3})\s*$")
    marks, expected = [], 1
    for i, line in enumerate(lines):
        if (m := rx.match(line)) and int(m.group(1)) == expected:
            marks.append(Mark(i, 1, str(expected), f"Section {expected}"))
            expected += 1
    return marks


def d_pali_vagga(lines):
    """VRI Tipiṭaka, tři úrovně: saṃyutta/paṇṇāsaka/nipāta (1) ›
    '1. Yamakavaggo' (2) › '1. Brahmajālasuttaṃ' (3). Zásah jen na řádku,
    který je celý nadpisem — v próze se 'suttaṃ' objevuje uprostřed věty."""
    group = re.compile(r"^\s*(?:\((\d+)\)\s*)?(\d+)\.\s*(\S+(?:saṃyuttaṃ|paṇṇāsakaṃ|nipāto|nipātapāḷi|khandhakaṃ|paṇṇāsapāḷi))\s*$")
    vagga = re.compile(r"^\s*(?:\((\d+)\)\s*)?(\d+)\.\s*(\S+(?:vaggo|vaggapāḷi))\s*$")
    sutta = re.compile(r"^\s*(\d+)\.\s*(\S+(?:suttaṃ|sikkhāpadaṃ|jātakaṃ|vatthu|kathā|niddeso|apadānaṃ|pañho))\s*$")
    marks = []
    for i, line in enumerate(lines):
        if (m := group.match(line)):
            marks.append(Mark(i, 1, m.group(2), line.strip()))
        elif (m := vagga.match(line)):
            marks.append(Mark(i, 2, m.group(2), line.strip()))
        elif (m := sutta.match(line)):
            marks.append(Mark(i, 3, m.group(1), line.strip()))
    return marks


_DE_ORDINAL = "Erstes|Zweites|Drittes|Viertes|Fünftes|Sechstes|Siebentes|Siebtes|Achtes|Neuntes|Zehntes"


_DE_TOP = re.compile(rf"^\s*(Vorrede|Vorwort|Einleitung|Vorbericht|Anhang|"
                     rf"(?:{_DE_ORDINAL}|Erster|Zweiter|Dritter|Vierter|Fünfter|Sechster|Siebenter|Achter|Neunter)"
                     rf"\s+(?:Kapitel|Buch|Abschnitt|Teil)\b.*)$")
_DE_TOC_ENTRY = re.compile(r"^\s*([A-C]|[IVX]{1,4}|[a-c]|\d{1,2})\.\s+(\S.*)$")


def _norm_letters(s: str) -> str:
    return re.sub(r"[^a-zäöüß]", "", s.lower())


def d_hegel(lines):
    """Suhrkamp Hegel z PDF. Nadpisy v těle nejsou odděleny prázdnými
    řádky a jsou buď v proložené sazbě ('B. D I E K U N S T R E L I G I O N'),
    nebo zalomené do dvou řádků ('II. Die' / 'Aufklärung') — regex nad
    tělem je nespolehlivý. PDF má ale na začátku obsah se stránkami, a ten
    se vezme jako seznam nadpisů, které se pak dohledají v těle podle
    prvních písmen (bez mezer, bez velikosti). Úrovně: Vorrede/Einleitung/
    'Erstes Kapitel' 1, 'A.' 2, 'I.' 3, 'a.'/'1.' 4."""
    tops = [i for i, line in enumerate(lines) if _DE_TOP.match(line) and len(line.strip()) < 80]
    body_start = tops[0] if tops else 0

    # obsah: položky před tělem, zalomené pokračování se přilepí
    toc: list[tuple[str, str, int]] = []   # (prefix, normalizovaný text, level)
    i = 0
    while i < body_start:
        m = _DE_TOC_ENTRY.match(lines[i])
        if m:
            text = m.group(2)
            j = i + 1
            while j < body_start and lines[j].strip() and not _DE_TOC_ENTRY.match(lines[j]) \
                    and not re.fullmatch(r"\s*[\d ]+\s*", lines[j]) and not _DE_TOP.match(lines[j]) \
                    and len(text) < 90:
                text += " " + lines[j].strip()
                j += 1
            pre = m.group(1)
            level = 2 if pre in "ABC" else 3 if re.fullmatch(r"[IVX]+", pre) else 4
            toc.append((pre, _norm_letters(text), level))
        i += 1

    marks = [Mark(i, 1, lines[i].strip(), lines[i].strip()) for i in tops]
    used = set()
    for i in range(body_start, len(lines)):
        m = _DE_TOC_ENTRY.match(lines[i])
        if not m or len(lines[i].strip()) > 90:
            continue
        pre = m.group(1)
        here = _norm_letters(m.group(2))
        nxt = _norm_letters(lines[i + 1]) if i + 1 < len(lines) else ""
        for k, (tpre, ttext, level) in enumerate(toc):
            if k in used or tpre != pre or not ttext:
                continue
            probe = ttext[:min(14, len(ttext))]
            if here.startswith(probe) or (here + nxt).startswith(probe):
                heading = f"{pre}. {_unspace(m.group(2).strip())}"
                if not here.startswith(probe):
                    heading += " " + lines[i + 1].strip()
                marks.append(Mark(i, level, pre, heading))
                used.add(k)
                break
    marks.sort(key=lambda m: m.line)
    return marks


def _unspace(caps: str) -> str:
    """'D I E K U N S T R E L I G I O N' → 'DIE KUNSTRELIGION': proložená
    sazba má mezi písmeny jednu mezeru, mezi slovy dvě."""
    if "  " in caps:
        return " ".join(w.replace(" ", "") for w in caps.split("  "))
    # jediná mezera všude: písmena po jednom → slova nejde rozlišit, slepit
    tokens = caps.split(" ")
    if all(len(t) == 1 for t in tokens):
        return "".join(tokens)
    return caps


def d_rigveda_hymn(lines):
    """Griffithův Rgvéd (OCR): 'BOOK THE FIRST.' (1) a 'HYMN I.' s OCR
    šumem — 'HYMN TIL', 'HYMN 19.]' (2). Jen VELKÉ 'HYMN' — malé 'hymn'
    je v próze poznámek. Číslo se bere, jak leze; pořadí drží ordinal."""
    book = re.compile(r"^\s*BOOK\s+THE\s+([A-Z]+)\.?\s*$")
    hymn = re.compile(r"^\s*H\s?YMN\s+([A-Z0-9]{1,8})[.\]'’]?")
    marks = []
    for i, line in enumerate(lines):
        if (m := book.match(line)):
            marks.append(Mark(i, 1, m.group(1).title(), line.strip()))
        elif (m := hymn.match(line)) and len(line.strip()) < 60:
            marks.append(Mark(i, 2, m.group(1), line.strip()))
    return marks


def d_none(lines):
    return []


DETECTORS = {
    "zh_zhang": d_zh_zhang,
    "zh_lunyu": d_zh_lunyu,
    "zh_juan": d_zh_juan,
    "en_chapter_roman": d_en_chapter_roman,
    "roman_dot_upper_title": d_roman_dot_upper_title,
    "roman_bare": d_roman_bare,
    "edda_poetic": d_edda_poetic,
    "edda_prose": d_edda_prose,
    "mbh_section": d_mbh_section,
    "mbh_bare_number": d_mbh_bare_number,
    "pali_vagga": d_pali_vagga,
    "hegel": d_hegel,
    "rigveda_hymn": d_rigveda_hymn,
    "none": d_none,
    # pdf_bookmarks nemá řádkový detektor — marks staví ingest z PDF osnovy
    # (marks_from_toc) a pak volá build()
}

MIN_PREAMBLE_CHARS = 400


def marks_from_toc(toc: list[tuple[int, str, int]], page_first_line: list[int]) -> list[Mark]:
    """PDF osnova (PyMuPDF get_toc: (level, title, page 1-based)) → marks na
    prvním řádku stránky. Víc položek na téže stránce se sloučí do jedné
    (nadpis nejvyšší úrovně), jinak by vznikly prázdné kapitoly."""
    by_line: dict[int, Mark] = {}
    for level, title, page in toc:
        if not (1 <= page <= len(page_first_line)):
            continue
        line = page_first_line[page - 1]
        title = " ".join(title.split())
        if line in by_line and by_line[line].level <= level:
            continue
        by_line[line] = Mark(line, level, title, title)
    return [by_line[k] for k in sorted(by_line)]


def build(lines: list[str], marks: list[Mark]) -> list[Chapter]:
    """Z marks složí kapitoly pokrývající celý dokument, doplní rodiče a
    cestu ('Kniha 1 › Kapitola 3'). Bez marks = jedna kapitola '(bez členění)'."""
    n = len(lines)
    if not marks:
        return [Chapter(1, 1, None, "1", "(bez členění)", 0, n, path="(bez členění)")]

    chapters: list[Chapter] = []
    first = marks[0].line
    preamble = "\n".join(lines[:first]).strip()
    if len(preamble) >= MIN_PREAMBLE_CHARS:
        chapters.append(Chapter(0, 1, None, "0", "(úvod)", 0, first, path="(úvod)"))
    else:
        # krátký úvod (titul, obsah) se přilepí k první kapitole
        first = 0

    for k, m in enumerate(marks):
        start = m.line if k > 0 else first
        end = marks[k + 1].line if k + 1 < len(marks) else n
        chapters.append(Chapter(0, m.level, None, m.ref, m.heading, start, end))

    # ordinály a rodiče: rodič = poslední předchozí kapitola s nižší úrovní;
    # „(úvod)" před první kapitolou není rodič ničeho
    stack: list[Chapter] = []
    for i, ch in enumerate(chapters, 1):
        ch.ordinal = i
        if ch.heading == "(úvod)":
            continue
        while stack and stack[-1].level >= ch.level:
            stack.pop()
        if stack:
            ch.parent_ordinal = stack[-1].ordinal
            stack[-1].children.append(ch.ordinal)
        stack.append(ch)

    by_ord = {c.ordinal: c for c in chapters}
    for ch in chapters:
        parts, cur = [], ch
        while cur is not None:
            parts.append(_short(cur.heading))
            cur = by_ord.get(cur.parent_ordinal) if cur.parent_ordinal else None
        ch.path = " › ".join(reversed(parts))
    return chapters


def _short(heading: str, limit: int = 60) -> str:
    h = " ".join(heading.split())
    return h if len(h) <= limit else h[:limit - 1].rstrip() + "…"


def detect(lines: list[str], detector: str) -> list[Chapter]:
    if detector not in DETECTORS:
        raise KeyError(f"neznámý detektor kapitol: {detector!r} (znám: {sorted(DETECTORS)})")
    return build(lines, DETECTORS[detector](lines))


def leaves(chapters: list[Chapter]) -> list[Chapter]:
    """Kapitoly, do kterých patří text: každá je úsek řádků, i rodič má
    vlastní úsek (od nadpisu k prvnímu dítěti), takže leaf = všechny."""
    return chapters


# --- selftest -----------------------------------------------------------------


def _selftest() -> None:
    assert cn_to_int("八") == 8 and cn_to_int("十二") == 12 and cn_to_int("八十一") == 81
    assert cn_to_int("二十") == 20 and cn_to_int("一百") == 100
    assert roman_to_int("XIV") == 14 and roman_to_int("XL") == 40

    tao = ["道德經", "", "第一章", "道可道，非常道。", "", "第二章", "天下皆知美之為美。", "第八十一章", "信言不美。"]
    ch = detect(tao, "zh_zhang")
    assert [c.ref for c in ch] == ["1", "2", "81"], [c.ref for c in ch]
    assert ch[0].start == 0 and ch[0].text(tao).startswith("道德經")  # krátký úvod přilepen
    assert ch[-1].text(tao) == "第八十一章\n信言不美。"

    lunyu = ["學而第一", "1. 子曰：學而時習之", "為政第二", "2. 子曰：為政以德"]
    assert [c.ref for c in detect(lunyu, "zh_lunyu")] == ["1", "2"]

    # obsah před textem se zahodí
    zz = ["CONTENTS", "CHAPTER I.", "CHAPTER II.", "", "text", "CHAPTER I.", "", "HAPPY EXCURSIONS", "body", "CHAPTER II.", "", "THE IDENTITY", "body2"]
    ch = detect(zz, "en_chapter_roman")
    assert [c.ref for c in ch] == ["1", "2"] and ch[0].heading == "CHAPTER I. HAPPY EXCURSIONS", ch

    mbh = ["SECTION I", "text\r".strip(), "SECTION II", "t", "SECTION XII", "t"]
    assert [c.ref for c in detect(mbh, "mbh_section")] == ["1", "2", "12"]
    bare = ["1", "text", "17", "2", "text", "3", "text", "99"]
    assert [c.ref for c in detect(bare, "mbh_bare_number")] == ["1", "2", "3"]

    pali = ["Dhammapadapāḷi", "1. Yamakavaggo", "1. Manopubbaṅgamā dhammā", "2. Appamādavaggo", "verš",
            "Dīghanikāyo", "1. Sīlakkhandhavaggapāḷi", "1. Brahmajālasuttaṃ", "Evaṃ me sutaṃ… suttaṃ niṭṭhitaṃ."]
    ch = detect(pali, "pali_vagga")
    refs = [(c.level, c.ref) for c in ch]
    assert refs == [(2, "1"), (2, "2"), (2, "1"), (3, "1")], refs
    assert ch[-1].parent_ordinal == ch[-2].ordinal and ch[-1].path.startswith("1. Sīlakkhandhavaggapāḷi › ")
    sn = ["(1) 1. Devatāsaṃyuttaṃ", "1. Naḷavaggo", "1. Oghataraṇasuttaṃ", "t", "2. Nimokkhasuttaṃ", "t"]
    ch = detect(sn, "pali_vagga")
    assert [(c.level, c.path) for c in ch][-1] == (3, "(1) 1. Devatāsaṃyuttaṃ › 1. Naḷavaggo › 2. Nimokkhasuttaṃ"), ch[-1].path

    hegel = ["A . BEWUSSTSEIN", "I. Die sinnliche Gewißheit oder das Diese und das", "Meinen", "82",
             "II. Die Wahrnehmung oder das Ding und die", "Täuschung", "93", "B. SELBSTBEWUSSTSEIN",
             "IV. Die Wahrheit der Gewißheit seiner selbst", "137", "B. Die Kunstreligion", "512",
             "Vorrede", "Eine Erklärung, wie sie", "Einleitung", "Es ist eine natürliche",
             "I. D I E S I N N L I C H E G E W I S S H E I T", "text", "II. Die", "Wahrnehmung oder das Ding", "text",
             "IV. Die Wahrheit der Gewißheit seiner selbst", "t", "B. D I E K U N S T R E L I G I O N", "Der Geist ist Künstler."]
    ch = detect(hegel, "hegel")
    got = [(c.level, c.ref) for c in ch]
    assert got == [(1, "Vorrede"), (1, "Einleitung"), (3, "I"), (3, "II"), (3, "IV"), (2, "B")], got
    assert ch[3].heading == "II. Die Wahrnehmung oder das Ding", ch[3].heading
    assert ch[2].path == "Einleitung › I. DIESINNLICHEGEWISSHEIT"

    edda = ["PREFACE", "", "PART I", "", "VOLUSPO", "", "INTRODUCTORY NOTE", "", "text", "", "HOVAMOL", "", "text 12"]
    ch = detect(edda, "edda_poetic")
    assert [(c.level, c.ref) for c in ch] == [(1, "1"), (3, "Voluspo"), (3, "Hovamol")], [(c.level, c.ref) for c in ch]
    edda2 = ["PART I", "", "LAYS OF THE GODS", "", "VOLUSPO", "", "THE WISE-WOMAN'S PROPHECY", "", "INTRODUCTORY NOTE", "", "text"]
    ch = detect(edda2, "edda_poetic")
    assert [c.heading for c in ch] == ["PART I", "LAYS OF THE GODS", "VOLUSPO — The Wise-Woman'S Prophecy"], [c.heading for c in ch]
    assert ch[2].path == "PART I › LAYS OF THE GODS › VOLUSPO — The Wise-Woman'S Prophecy"

    rv = ["BOOK THE FIRST.", "HYMN I. Agni.", "text", "H YMN XYIL", "hymn because there", "HYMN 19.] Agni.", "t"]
    assert [c.ref for c in detect(rv, "rigveda_hymn")] == ["First", "I", "XYIL", "19"]

    prose = ["CONTENTS.", "THE FOOLING OF GYLFE.", "CHAPTER I.", "CHAPTER II.", "BRAGE'S TALK.", "CHAPTER I.",
             "INTRODUCTION.", "long intro text", "THE FOOLING OF GYLFE.", "", "CHAPTER I.", "", "OF THE HIGHEST GOD.",
             "text", "text", "text", "text", "text", "CHAPTER II.", "", "THE CREATION.", "text", "text", "text",
             "text", "text", "BRAGE'S TALK.", "", "CHAPTER I.", "", "text", "text", "text", "text"]
    ch = detect(prose, "edda_prose")
    got = [(c.level, c.ref) for c in ch]
    assert got == [(1, "The Fooling Of Gylfe"), (2, "1"), (2, "2"), (1, "Brage'S Talk"), (2, "1")], got
    assert ch[1].heading == "CHAPTER I. OF THE HIGHEST GOD." and ch[-1].path.startswith("BRAGE'S TALK. › ")
    assert is_front_matter("Title Page") and is_front_matter("INDEX.") and not is_front_matter("Fargard I")

    # bez členění
    ch = detect(["a", "b"], "none")
    assert len(ch) == 1 and ch[0].heading == "(bez členění)" and ch[0].end == 2

    # PDF osnova → marks
    marks = marks_from_toc([(1, "Fargard I", 1), (2, "Fargard I, 1", 1), (1, "Fargard II", 3)], [0, 10, 20])
    assert [(m.line, m.level, m.heading) for m in marks] == [(0, 1, "Fargard I"), (20, 1, "Fargard II")]

    # dlouhý úvod se stane kapitolou (úvod), ale není rodičem dalších
    long_intro = ["x" * 500, "1. Yamakavaggo", "1. Cakkhusuttaṃ", "t"]
    ch = detect(long_intro, "pali_vagga")
    assert ch[0].heading == "(úvod)" and ch[1].parent_ordinal is None and ch[2].path == "1. Yamakavaggo › 1. Cakkhusuttaṃ", [c.path for c in ch]
    print("chapters.py: selftest ok")


if __name__ == "__main__":
    _selftest()

#!/usr/bin/env python3
"""Jednorázový bootstrap registru děl (works.yaml) z toho, co už existuje:
katalog v Chromě (cesty, skupiny), summaries.json (name_cs), aliasy
z retrieval.py a tabulka parv z ingestu. Pálí (60 děl) a Mahábhárata (18)
se dají odvodit z cesty úplně; zbylých 14 děl má ručně psaný slovník níže.

Výstup je YAML k ruční revizi — po ní se skript už nepouští (registr je
kurátorský, `name_cs`, autor a jazyk se odtud nikdy nepřegenerovávají).

Použití (M2, Chroma na JODA):
    python3 registry/bootstrap_works.py --out registry/works.yaml
"""

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from retrieval import ALIASES, fold  # noqa: E402

NFC = lambda s: unicodedata.normalize("NFC", s)  # noqa: E731

# nikáje a koše → kód do work_id + subgroup + forma
PALI_SECTIONS = {
    "Vinayapiṭaka": ("vin", "vinaya", "vinaya"),
    "Dīghanikāyo": ("dn", "sutta", "sutta"),
    "Majjhimanikāyo": ("mn", "sutta", "sutta"),
    "Saṃyuttanikāyo": ("sn", "sutta", "sutta"),
    "Aṅguttaranikāyo": ("an", "sutta", "sutta"),
    "Khuddakanikāyo": ("kn", "sutta", "sutta"),
    "Abhidhammapiṭaka": ("abh", "abhidhamma", "abhidhamma"),
}

# Ručně: díla mimo pálí a Mahábháratu. Klíč = dnešní `work` z Chromy (NFC).
# lang_corpus je poctivý — většina z nich jsou anglické překlady.
MANUAL = {
    "dao de jing": dict(id="zh.daodejing", title="道德經", author="Laozi (připisováno)",
        author_cs="Lao-c'", lang_original="lzh", lang_corpus="lzh", form="aforismy",
        period="4.–3. stol. př. n. l.", edition="Gutenberg, text Wang Bi", priority=1,
        detector="zh_zhang"),
    "analects": dict(id="zh.lunyu", title="論語", author="Konfucius a žáci (Kongzi)",
        author_cs="Konfucius", lang_original="lzh", lang_corpus="lzh", form="dialog",
        period="5.–3. stol. př. n. l.", edition="Gutenberg", priority=1, detector="zh_lunyu"),
    "mengzi": dict(id="zh.mengzi", title="孟子", author="Mengzi (Mencius)", author_cs="Mencius",
        lang_original="lzh", lang_corpus="lzh", form="dialog", period="4. stol. př. n. l.",
        edition="Gutenberg", priority=1, detector="zh_juan"),
    "zhuangzi": dict(id="zh.zhuangzi", title="莊子 (Chuang Tzŭ)", author="Zhuangzi (Zhuang Zhou)",
        author_cs="Čuang-c'", lang_original="lzh", lang_corpus="en", form="traktat",
        period="4.–3. stol. př. n. l.", edition="překlad Herbert A. Giles, 1889 (Gutenberg)",
        priority=1, detector="en_chapter_roman"),
    "Beowulf": dict(id="ang.beowulf_gummere", title="Beowulf", author="anonym",
        lang_original="ang", lang_corpus="en", form="epos", period="8.–11. stol.",
        edition="překlad Francis B. Gummere, 1910 (Gutenberg #981)", priority=1,
        detector="roman_bare"),
    "Beowulf: An Anglo-Saxon Epic Poem": dict(id="ang.beowulf_hall", title="Beowulf: An Anglo-Saxon Epic Poem",
        author="anonym", lang_original="ang", lang_corpus="en", form="epos", period="8.–11. stol.",
        edition="překlad John Lesslie Hall, 1892 (Gutenberg #16328)", priority=1,
        detector="roman_dot_upper_title"),
    "The poetic Edda": dict(id="non.edda_poetic", title="The Poetic Edda (Sæmundar Edda)",
        author="anonym", lang_original="non", lang_corpus="en", form="epos", period="9.–13. stol.",
        edition="překlad Henry Adams Bellows, 1923 (Gutenberg #73533)", priority=1,
        detector="edda_poetic"),
    "The Younger Edda; Also called Snorre's Edda, or The Prose Edda": dict(id="non.edda_prose",
        title="The Younger Edda (Snorra Edda)", author="Snorri Sturluson", author_cs="Snorri Sturluson",
        lang_original="non", lang_corpus="en", form="traktat", period="okolo 1220",
        edition="překlad Rasmus B. Anderson, 1880 (Gutenberg #18947)", priority=1,
        detector="edda_prose"),
    "hegel werke band01 fruehe schriften": dict(id="de.hegel_werke01", title="Frühe Schriften (Werke 1)",
        author="Georg Wilhelm Friedrich Hegel", author_cs="G. W. F. Hegel", lang_original="de",
        lang_corpus="de", form="traktat", period="1793–1800", edition="Suhrkamp Werke, Bd. 1",
        priority=2, detector="hegel"),
    "hegel werke band03 phaenomenologie": dict(id="de.hegel_werke03", title="Phänomenologie des Geistes (Werke 3)",
        author="Georg Wilhelm Friedrich Hegel", author_cs="G. W. F. Hegel", lang_original="de",
        lang_corpus="de", form="traktat", period="1807", edition="Suhrkamp Werke, Bd. 3",
        priority=1, detector="hegel"),
    "hegel werke band05 wissenschaft logik": dict(id="de.hegel_werke05", title="Wissenschaft der Logik I (Werke 5)",
        author="Georg Wilhelm Friedrich Hegel", author_cs="G. W. F. Hegel", lang_original="de",
        lang_corpus="de", form="traktat", period="1812–1832", edition="Suhrkamp Werke, Bd. 5",
        priority=1, detector="hegel"),
    "rigveda complete sanskrit": dict(id="sa.rigveda_griffith", title="The Hymns of the Rigveda",
        author="anonym (ršiové)", lang_original="sa", lang_corpus="en", form="hymnus",
        period="1500–1200 př. n. l.", edition="překlad Ralph T. H. Griffith, 1896 (OCR ze skenu)",
        priority=1, detector="rigveda_hymn"),
    "avesta darmesteter complete": dict(id="ae.avesta_darmesteter", title="The Zend-Avesta",
        author="anonym (zoroastrijský kánon)", lang_original="ae", lang_corpus="en", form="hymnus",
        period="2. tis. – 1. tis. př. n. l.", edition="překlad James Darmesteter, SBE 4/23/31 (1880–1887)",
        priority=1, detector="pdf_bookmarks"),
    "pyramid texts allen": dict(id="egy.pyramid_texts_allen", title="The Ancient Egyptian Pyramid Texts",
        author="anonym", lang_original="egy", lang_corpus="en", form="hymnus",
        period="24.–22. stol. př. n. l.", edition="překlad James P. Allen, 2005", priority=1,
        detector="pdf_bookmarks"),
}

MBH_DETECTOR = {n: ("mbh_section" if n in (1, 2, 3, 4, 5, 6, 7, 12, 13, 14, 15) else "mbh_bare_number")
                for n in range(1, 19)}


def slug(s: str) -> str:
    """'Jātakapāḷi 1' → 'jataka-1', 'Mahāvaggo' → 'mahavagga'."""
    s = fold(s)
    m = re.match(r"^(.*?)[ _-]?(\d+)$", s)
    base, num = (m.group(1), m.group(2)) if m else (s, "")
    base = re.sub(r"pali$", "", base).strip()
    base = re.sub(r"vaggo$", "vagga", base)
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return f"{base}-{num}" if num else base


def load_catalog(chroma_url: str) -> dict[str, dict]:
    import chromadb
    from urllib.parse import urlparse
    u = urlparse(chroma_url)
    col = chromadb.HttpClient(host=u.hostname, port=u.port or 8000).get_collection("books")
    out: dict[str, dict] = {}
    limit, offset = 1000, 0
    while True:
        page = col.get(include=["metadatas"], limit=limit, offset=offset)
        metas = page["metadatas"] or []
        for m in metas:
            w = (m or {}).get("work")
            if w and NFC(w) not in out:
                out[NFC(w)] = {"path": NFC(m.get("path", "")), "group": m.get("group")}
        if len(metas) < limit:
            break
        offset += limit
    return out


def aliases_for(work: str) -> list[str]:
    return sorted({a for a, works in ALIASES.items() if NFC(work) in {NFC(w) for w in works}})


def entry_pali(work: str, info: dict) -> dict:
    parts = Path(info["path"]).parts
    section = next((p for p in parts if NFC(p) in PALI_SECTIONS), None)
    code, subgroup, form = PALI_SECTIONS.get(NFC(section), ("kn", "sutta", "sutta")) if section else ("kn", "sutta", "sutta")
    return dict(
        id=f"pi.{code}.{slug(work)}", title=work, subgroup=subgroup, author="(kánon Theravády)",
        lang_original="pi", lang_corpus="pi", form=form, period="5.–1. stol. př. n. l. (redakce)",
        edition="Vipassana Research Institute, Chaṭṭha Saṅgāyana (PDF)", priority=1,
        detector="pali_vagga",
    )


def entry_mbh(work: str, info: dict) -> dict:
    n = int(re.search(r"maha(\d\d)", info["path"]).group(1))
    return dict(
        id=f"sa.mbh{n:02d}", title=work, subgroup="mahabharata", author="Vyāsa (tradiční připsání)",
        author_cs="Vjása", lang_original="sa", lang_corpus="en", form="epos",
        period="4. stol. př. n. l. – 4. stol. n. l.",
        edition="překlad Kisari Mohan Ganguli, 1883–1896", priority=1, detector=MBH_DETECTOR[n],
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--chroma-url", default="http://192.168.88.88:8006")
    p.add_argument("--summaries", default=str(Path(__file__).parent.parent / "summaries.json"))
    p.add_argument("--out", default=str(Path(__file__).parent / "works.yaml"))
    args = p.parse_args()

    catalog = load_catalog(args.chroma_url)
    summaries = {NFC(k): v for k, v in json.loads(Path(args.summaries).read_text(encoding="utf-8")).items()}

    works: dict[str, dict] = {}
    missing = []
    for work, info in sorted(catalog.items(), key=lambda kv: fold(kv[0])):
        if work in MANUAL:
            e = dict(MANUAL[work])
        elif info["group"] == "pali":
            e = entry_pali(work, info)
        elif "mahabharata/maha" in info["path"]:
            e = entry_mbh(work, info)
        else:
            missing.append(work)
            continue
        wid = e.pop("id")
        detector = e.pop("detector")
        rec = {
            "path": info["path"],
            "group": info["group"],
            "work_legacy": work,
            "title": e.pop("title"),
            "name_cs": (summaries.get(work) or {}).get("name_cs"),
        }
        rec.update(e)
        rec["aliases"] = aliases_for(work)
        rec["chapters"] = {"detector": detector}
        works[wid] = {k: v for k, v in rec.items() if v not in (None, [], "")}

    header = (
        "# Kurátorský registr děl. name_cs, autor, jazyk a edice jsou ruční — LLM je\n"
        "# nikdy nepřepisuje. lang_original = jazyk DÍLA, lang_corpus = jazyk TEXTU\n"
        "# v korpusu (poctivě: u překladů 'en'). Klíč = work_id (ASCII slug, stabilní).\n"
        "# Perseus tady není — ten se čte z __cts__.xml, jen výjimky jsou\n"
        "# v perseus_overrides.yaml. Vygenerováno bootstrap_works.py, pak ručně revidováno.\n\n"
    )
    works = dict(sorted(works.items()))
    Path(args.out).write_text(
        header + yaml.safe_dump(works, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    print(f"zapsáno {len(works)} děl → {args.out}")
    if missing:
        print("BEZ ZÁZNAMU (doplň do MANUAL):", missing)
    ids = [w for w in works]
    assert len(ids) == len(set(ids)), "duplicitní work_id"
    return 0


if __name__ == "__main__":
    sys.exit(main())

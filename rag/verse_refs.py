#!/usr/bin/env python3
"""Doplní `chunks.ref_start` / `ref_end` u děl z Persea podle TEI.

Ingest (perseus_tei.extract) vykreslí kapitolu jako souvislý text a chunky
z něj vyřeže — čísla veršů (NZ: chapter › verse) nebo oddílů (Platón:
Stephanovy strany) se cestou ztratí. Přeingestovat nejde: jiný text chunku
by podle `text_sha` zahodil hotové obohacení. Proto bokem: kapitola se
vykreslí stejně jako při ingestu, zapamatuje se, kde v ní začíná který
podřízený textpart, chunk se v kapitole najde podle svého začátku a dostane
CTS odkaz prvního a posledního verše, který zasahuje ('1.1', '1.13').

Zapisuje jen ref_start/ref_end; text, obohacení ani embeddingy se nemění.

    python3 verse_refs.py --work grc.tlg0031.tlg002            # nanečisto
    python3 verse_refs.py --work-prefix grc.tlg0031 --write    # celý NZ
"""

from __future__ import annotations

import argparse
import bisect
import os
import re
import sys
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).parent))
import perseus_tei as pt  # noqa: E402

DOWNLOADS = Path(__file__).resolve().parent.parent / "downloads"
WS = re.compile(r"\s+")
PROBE = 80          # kolik znaků začátku chunku se hledá v kapitole


def norm(s: str) -> str:
    return WS.sub(" ", s).strip()


def chapter_divs(file: Path):
    """Kapitolní divy přesně tak, jak je vybírá perseus_tei.extract()."""
    tree = etree.parse(str(file), etree.XMLParser(recover=True, huge_tree=True))
    body = tree.find(".//tei:text/tei:body", pt.NS)
    if body is None:
        return []
    pt._strip_noise(body)
    edition = None
    for d in body.iter(f"{{{pt.TEI}}}div"):
        if (d.get("type") or "").lower() in ("edition", "translation"):
            edition = d
            break
    parts = pt._textparts(edition if edition is not None else body)
    while len(parts) == 1 and pt._textparts(parts[0]):
        parts = pt._textparts(parts[0])
    return [d for d in parts if pt._render(d)]


def verse_offsets(div) -> tuple[str, list[int], list[str]]:
    """(normalizovaný text kapitoly, začátky veršů v něm, CTS odkazy veršů)."""
    chapter_ref = div.get("n") or ""
    text = norm(pt.nfc(pt._render(div)))
    starts, refs, pos = [], [], 0
    for v in pt._textparts(div):
        vt = norm(pt.nfc(pt._render(v)))
        if not vt:
            continue
        at = text.find(vt[:PROBE], pos)
        if at < 0:
            continue
        starts.append(at)
        refs.append(f"{chapter_ref}.{v.get('n') or len(refs) + 1}")
        pos = at + 1
    return text, starts, refs


def refs_for_chunks(text: str, starts: list[int], refs: list[str],
                    chunks: list[str]) -> list[tuple[str | None, str | None]]:
    """CTS (začátek, konec) pro každý chunk kapitoly, v pořadí."""
    out, pos = [], 0
    for chunk in chunks:
        c = norm(chunk)
        at = text.find(c[:PROBE], max(0, pos - len(c)))   # chunky se překrývají
        if at < 0 or not starts:
            out.append((None, None))
            continue
        end = at + len(c) - 1
        i = max(0, bisect.bisect_right(starts, at) - 1)
        j = max(0, bisect.bisect_right(starts, end) - 1)
        out.append((refs[i], refs[j]))
        pos = at + 1
    return out


def process(conn, work_id: str, write: bool) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT source_path FROM works WHERE id = %s", (work_id,))
        row = cur.fetchone()
        if not row or not row[0]:
            print(f"  {work_id}: bez source_path", file=sys.stderr)
            return 0, 0
        file = DOWNLOADS / row[0]
        cur.execute("SELECT id, ordinal FROM chapters WHERE work_id = %s AND level = 1 ORDER BY ordinal",
                    (work_id,))
        chapters = cur.fetchall()
        divs = chapter_divs(file)
        if len(divs) != len(chapters):
            print(f"  {work_id}: TEI {len(divs)} kapitol, PG {len(chapters)} — přeskakuji", file=sys.stderr)
            return 0, 0
        done = total = 0
        for (chapter_id, _), div in zip(chapters, divs):
            text, starts, refs = verse_offsets(div)
            cur.execute("SELECT id, text FROM chunks WHERE chapter_id = %s ORDER BY seq_in_chapter",
                        (chapter_id,))
            rows = cur.fetchall()
            pairs = refs_for_chunks(text, starts, refs, [t for _, t in rows])
            total += len(rows)
            for (chunk_id, _), (a, b) in zip(rows, pairs):
                if a is None:
                    continue
                done += 1
                if write:
                    cur.execute("UPDATE chunks SET ref_start = %s, ref_end = %s WHERE id = %s",
                                (a, b, chunk_id))
    if write:
        conn.commit()
    return done, total


def main() -> int:
    p = argparse.ArgumentParser(description="Verše/oddíly z TEI → chunks.ref_start/ref_end")
    p.add_argument("--work", action="append", default=[])
    p.add_argument("--work-prefix", help="např. grc.tlg0031 (celý Nový zákon)")
    p.add_argument("--write", action="store_true", help="zapsat (bez něj jen výpis)")
    p.add_argument("--dsn", default=os.getenv("PG_DSN"))
    args = p.parse_args()

    import psycopg
    with psycopg.connect(args.dsn) as conn:
        ids = list(args.work)
        if args.work_prefix:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM works WHERE id LIKE %s ORDER BY id", (args.work_prefix + "%",))
                ids += [r[0] for r in cur.fetchall()]
        for wid in ids:
            done, total = process(conn, wid, args.write)
            print(f"{wid:24s} chunků s odkazem {done}/{total}", file=sys.stderr)
    if not args.write:
        print("nanečisto — zapíše až --write", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

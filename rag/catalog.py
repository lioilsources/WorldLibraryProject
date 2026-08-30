"""Katalogové odpovědi: z plánu (intent + filtry) postaví deterministický
kontext z Postgresu — tabulku děl, přehled díla, seznam kapitol, obsah
kapitoly — a strukturovaný payload pro appku. LLM pak kontext jen
prezentuje česky a nesmí přidávat díla, která v něm nejsou.

Délka podle počtu (detail=auto): ≤ 3 díla long, 4–15 medium, > 15 short,
> 60 jen názvy; greek_latin (1 080 děl) se vždy nejdřív skládá po
autorech. Díla s prioritou 3 (fragmenty, scholia) se ve výchozím
katalogu schovají a jen sečtou.
"""

from __future__ import annotations

from collections import defaultdict

from retrieval import fold

GROUP_LABEL = {
    "pali": "Pálijský kánon (Theraváda)", "sanskrit": "Sanskrtská tradice", "chinese": "Čínská tradice",
    "greek_latin": "Řecko-římská antika", "european": "Staroanglická a staroseverská tradice",
    "european_philosophy": "Německá filosofie", "avesta": "Avesta (zoroastrismus)", "egypt": "Staroegyptské texty",
}
LANG_CS = {"pi": "pálí", "sa": "sanskrt", "lzh": "klasická čínština", "zh": "čínština", "grc": "stará řečtina",
           "lat": "latina", "de": "němčina", "en": "angličtina", "ang": "staroangličtina", "non": "staroseverština",
           "ae": "avestština", "egy": "egyptština"}

MAX_TABLE_ROWS = 120
AUTHOR_FOLD_THRESHOLD = 60   # nad tolik děl ve skupině se skládá po autorech


def pick_detail(n: int, requested: str) -> str:
    if requested in ("short", "medium", "long"):
        return requested
    if n <= 3:
        return "long"
    if n <= 15:
        return "medium"
    return "short"


def summary_for(w: dict, detail: str) -> str:
    order = {"long": ("summary_long", "summary_medium", "summary_short"),
             "medium": ("summary_medium", "summary_short", "summary_long"),
             "short": ("summary_short", "summary_medium", "summary_long")}[detail]
    for k in order:
        if w.get(k):
            s = w[k]
            if detail == "short" and k != "summary_short":
                s = s.split(". ")[0].rstrip(".") + "."
            return s
    return ""


def corpus_note(w: dict) -> str:
    lo, lc = w.get("lang_original"), w.get("lang_corpus")
    if lo and lc and lo != lc:
        return f"{LANG_CS.get(lc, lc)} překlad" + (f" ({w['edition']})" if w.get("edition") else "")
    return "originál"


# --- dotazy do PG ---------------------------------------------------------------

WORK_COLS = ["id", "group", "subgroup", "title", "name_cs", "author", "author_cs", "lang_original", "lang_corpus",
             "edition", "form", "period", "priority", "chunk_count", "chapter_count",
             "summary_short", "summary_medium", "summary_long", "topic_ids"]


def query_works(conn, *, groups=None, topics=None, author=None, work_ids=None, hide_priority=3, limit=2000) -> list[dict]:
    where, params = [], []
    if work_ids:
        where.append("id = ANY(%s)"); params.append(list(work_ids))
    if groups:
        where.append('"group" = ANY(%s)'); params.append(list(groups))
    if topics:
        where.append("topic_ids && %s"); params.append(list(topics))
    if author:
        # bez diakritiky a velikosti: plánovač napíše „Platon", registr má „Platón",
        # Perseus „Plato" — unaccent + LIKE to srovná
        a = f"%{author}%"
        where.append("(unaccent(lower(coalesce(author, ''))) LIKE unaccent(lower(%s)) "
                     "OR unaccent(lower(coalesce(author_cs, ''))) LIKE unaccent(lower(%s)) "
                     "OR unaccent(lower(coalesce(name_cs, ''))) LIKE unaccent(lower(%s)))"); params += [a, a, a]
    if hide_priority and not work_ids and not author:
        where.append("priority < %s"); params.append(hide_priority)
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    with conn.cursor() as cur:
        cur.execute(
            f'SELECT {", ".join(chr(34) + c + chr(34) for c in WORK_COLS)} FROM catalog_v{sql_where} '
            f'ORDER BY "group", priority, coalesce(author_cs, author), coalesce(name_cs, title) LIMIT %s',
            params + [limit],
        )
        return [dict(zip(WORK_COLS, r)) for r in cur.fetchall()]


def count_hidden(conn, groups=None, hide_priority=3) -> int:
    with conn.cursor() as cur:
        if groups:
            cur.execute('SELECT count(*) FROM works WHERE priority >= %s AND "group" = ANY(%s)', (hide_priority, list(groups)))
        else:
            cur.execute("SELECT count(*) FROM works WHERE priority >= %s", (hide_priority,))
        return cur.fetchone()[0]


def query_chapters(conn, work_id: str, topic: str | None = None) -> list[dict]:
    cols = ["id", "ordinal", "level", "parent_id", "ref", "heading", "heading_cs", "path", "chunk_count",
            "summary_short", "summary_medium", "summary_long", "topic_ids"]
    with conn.cursor() as cur:
        extra, params = "", [work_id]
        if topic:
            extra, params = " AND %s = ANY(topic_ids)", [work_id, topic]
        cur.execute(f'SELECT {", ".join(cols)} FROM chapters_v WHERE work_id = %s{extra} ORDER BY ordinal', params)
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def topic_names(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, name_cs FROM topics")
        return dict(cur.fetchall())


# --- stavba kontextu ------------------------------------------------------------

def work_line(w: dict, detail: str, tnames: dict[str, str]) -> str:
    name = w.get("name_cs") or w.get("title")
    author = w.get("author_cs") or w.get("author") or "—"
    lang = LANG_CS.get(w.get("lang_original"), w.get("lang_original") or "?")
    topics = ", ".join(tnames.get(t, t) for t in (w.get("topic_ids") or [])[:3])
    summ = summary_for(w, detail)
    return f"| {name} | {author} | {lang} | {corpus_note(w)} | {topics} | {summ} |"


def build_catalog(conn, plan, *, group_by: str = "tradition", detail: str = "auto", groups=None, topics=None,
                  author=None, work_ids=None, hide_priority: int = 3, note: str | None = None) -> tuple[str, dict]:
    """→ (kontext pro LLM, payload pro appku). `note` je věta pro LLM
    o původu výběru (např. že témata ještě nejsou přiřazena)."""
    tnames = topic_names(conn)
    works = query_works(conn, groups=groups, topics=topics, author=author, work_ids=work_ids, hide_priority=hide_priority)
    hidden = count_hidden(conn, groups, hide_priority) if not (author or work_ids) else 0
    n = len(works)
    detail = pick_detail(n, detail)
    if n == 0:
        ctx = ("Katalog: pro zadaný filtr (" + ", ".join(x for x in [
            "tradice " + ", ".join(groups) if groups else "", "téma " + ", ".join(tnames.get(t, t) for t in topics) if topics else "",
            f"autor {author}" if author else ""] if x) + ") knihovna NEMÁ žádné dílo."
            + (f" {note}" if note else ""))
        return ctx, {"detail": detail, "group_by": group_by, "total": 0, "hidden": hidden, "truncated": False, "groups": []}

    # seskupení podle tématu bez přiřazených témat by dalo jeden koš
    # „(bez tématu)" — pak raději po autorech (víc autorů) nebo tradici
    if group_by == "topic" and not any(w.get("topic_ids") for w in works):
        authors = {w.get("author_cs") or w.get("author") for w in works}
        group_by = "author" if len(authors) > 1 else "tradition"
    key_fn = {
        "tradition": lambda w: w["group"],
        "author": lambda w: (w.get("author_cs") or w.get("author") or "neznámý autor"),
        "topic": lambda w: (w.get("topic_ids") or ["(bez tématu)"])[0],
        "chapter": lambda w: w["group"],
        "none": lambda w: "",
    }.get(group_by, lambda w: w["group"])
    grouped: dict[str, list[dict]] = defaultdict(list)
    for w in works:
        grouped[key_fn(w)].append(w)

    lines = [f"Katalog: {n} děl" + (f" (+ {hidden} menších textů, fragmentů a scholií, které se nevypisují)" if hidden else "") + "."
             + (f" {note}" if note else "")]
    payload_groups = []
    rows_used, truncated = 0, False
    for key, ws in grouped.items():
        label = GROUP_LABEL.get(key, tnames.get(key, key)) if key else "Díla"
        # velké skupiny (antika) → po autorech s počty, ne 1 000 řádků
        if len(ws) > AUTHOR_FOLD_THRESHOLD and group_by != "author":
            by_author: dict[str, list[dict]] = defaultdict(list)
            for w in ws:
                by_author[w.get("author_cs") or w.get("author") or "neznámý autor"].append(w)
            lines.append(f"\n### {label} — {len(ws)} děl od {len(by_author)} autorů (výběr podle autora zúží seznam)")
            alist = sorted(by_author.items(), key=lambda kv: (-sum(1 for w in kv[1] if w['priority'] == 1), -len(kv[1])))
            for a, aw in alist[:40]:
                top = [w.get("name_cs") or w.get("title") for w in aw if w["priority"] == 1][:4]
                lines.append(f"- {a}: {len(aw)} děl" + (f" (např. {', '.join(top)})" if top else ""))
            if len(alist) > 40:
                lines.append(f"- … a dalších {len(alist) - 40} autorů")
            payload_groups.append({"key": key, "label": label, "count": len(ws), "authors": [
                {"author": a, "count": len(aw), "examples": [w.get("name_cs") or w.get("title") for w in aw if w["priority"] == 1][:4]}
                for a, aw in alist[:40]]})
            continue
        lines.append(f"\n### {label} ({len(ws)})")
        lines.append("| Dílo | Autor | Jazyk originálu | V korpusu | Témata | Anotace |")
        lines.append("|---|---|---|---|---|---|")
        items = []
        for w in ws:
            if rows_used >= MAX_TABLE_ROWS:
                truncated = True
                break
            lines.append(work_line(w, detail, tnames))
            rows_used += 1
            items.append({
                "id": w["id"], "name_cs": w.get("name_cs"), "title": w.get("title"), "author": w.get("author"),
                "author_cs": w.get("author_cs"), "lang_original": w.get("lang_original"), "lang_corpus": w.get("lang_corpus"),
                "is_translation": bool(w.get("lang_original") != w.get("lang_corpus")), "edition": w.get("edition"),
                "summary": summary_for(w, detail), "topics": [tnames.get(t, t) for t in (w.get("topic_ids") or [])],
                "chapter_count": w.get("chapter_count"), "priority": w.get("priority"),
            })
        if truncated:
            lines.append(f"| … | | | | | (zkráceno, celkem {len(ws)}) |")
        payload_groups.append({"key": key, "label": label, "count": len(ws), "works": items})
    context = "\n".join(lines)
    payload = {"detail": detail, "group_by": group_by, "total": n, "hidden": hidden, "truncated": truncated, "groups": payload_groups}
    return context, payload


def build_work_overview(conn, w: dict, tnames: dict[str, str], max_chapters: int = 15) -> tuple[str, dict]:
    chapters = query_chapters(conn, w["id"])
    name = w.get("name_cs") or w["title"]
    lines = [
        f"Dílo: {name} ({w.get('title')})",
        f"Autor: {w.get('author_cs') or w.get('author') or 'neznámý'}; jazyk originálu: {LANG_CS.get(w.get('lang_original'), w.get('lang_original'))}; "
        f"v korpusu: {corpus_note(w)}; období: {w.get('period') or '?'}; forma: {w.get('form') or '?'}",
        f"Témata: {', '.join(tnames.get(t, t) for t in (w.get('topic_ids') or [])) or '(zatím neurčena)'}",
        f"Rozsah: {w.get('chapter_count')} kapitol, {w.get('chunk_count')} úryvků v indexu",
        "Anotace: " + (w.get("summary_long") or w.get("summary_medium") or w.get("summary_short") or "(zatím bez anotace — odpovídej z obecné znalosti a řekni to)"),
    ]
    top = [c for c in chapters if c["level"] == 1][:max_chapters] or chapters[:max_chapters]
    if top:
        lines.append(f"\nKapitoly (prvních {len(top)} z {len(chapters)}):")
        for c in top:
            lines.append(f"- {c['path']}" + (f" — {c['heading_cs']}" if c.get("heading_cs") else "")
                         + (f": {c['summary_short']}" if c.get("summary_short") else ""))
    payload = {"work": {"id": w["id"], "name_cs": w.get("name_cs"), "title": w.get("title"), "author": w.get("author"),
                        "author_cs": w.get("author_cs"), "lang_original": w.get("lang_original"), "lang_corpus": w.get("lang_corpus"),
                        "summary": w.get("summary_long") or w.get("summary_medium"), "chapter_count": w.get("chapter_count")},
               "chapters": [{"id": c["id"], "ordinal": c["ordinal"], "ref": c["ref"], "path": c["path"],
                             "heading_cs": c.get("heading_cs"), "summary": c.get("summary_short")} for c in top]}
    return "\n".join(lines), payload


def build_chapters(conn, w: dict, tnames: dict[str, str], *, detail: str = "short", group_by: str = "chapter",
                   topic: str | None = None, offset: int = 0, page: int = 40) -> tuple[str, dict]:
    chapters = query_chapters(conn, w["id"], topic)
    name = w.get("name_cs") or w["title"]
    total = len(chapters)
    detail = "short" if detail == "auto" else detail
    lines = [f"Dílo: {name} — {total} kapitol" + (f" (téma {tnames.get(topic, topic)})" if topic else "")]
    view = chapters[offset: offset + page]
    if group_by == "topic":
        by_t: dict[str, list[dict]] = defaultdict(list)
        for c in view:
            by_t[(c.get("topic_ids") or ["(bez tématu)"])[0]].append(c)
        for t, cs in by_t.items():
            lines.append(f"\n### {tnames.get(t, t)}")
            for c in cs:
                lines.append(f"- {c['path']}" + (f" — {c['heading_cs']}" if c.get("heading_cs") else "") + (f": {summary_for(c, detail)}" if summary_for(c, detail) else ""))
    else:
        for c in view:
            indent = "  " * max(0, c["level"] - 1)
            lines.append(f"{indent}- {c['path'].split(' › ')[-1]}" + (f" — {c['heading_cs']}" if c.get("heading_cs") else "")
                         + (f": {summary_for(c, detail)}" if summary_for(c, detail) else ""))
    if offset + page < total:
        lines.append(f"\n(zobrazeno {offset + 1}–{offset + len(view)} z {total}; další na „pokračuj“)")
    payload = {"work_id": w["id"], "name_cs": w.get("name_cs"), "title": w.get("title"), "total": total,
               "offset": offset, "items": [{"id": c["id"], "ordinal": c["ordinal"], "level": c["level"], "ref": c["ref"],
                                            "path": c["path"], "heading_cs": c.get("heading_cs"),
                                            "summary": summary_for(c, detail), "topics": [tnames.get(t, t) for t in (c.get("topic_ids") or [])]}
                                           for c in view]}
    return "\n".join(lines), payload


def resolve_work(catalog: dict, legacy_to_id: dict, alias_index, hint: str | None, author: str | None) -> list[str]:
    """work_hint/author → work_id(s) přes aliasy (retrieval.route) a katalog."""
    from retrieval import route
    ids: list[str] = []
    if hint:
        for legacy in route(hint, alias_index):
            wid = legacy_to_id.get(legacy)
            if wid and wid not in ids:
                ids.append(wid)
        if not ids:
            h = fold(hint)
            for wid, w in catalog.items():
                if h and (h in fold(w.get("name_cs") or "") or h in fold(w.get("title") or "")):
                    ids.append(wid)
    if not ids and author:
        a = fold(author)
        for wid, w in catalog.items():
            if a and (a in fold(w.get("author_cs") or "") or a in fold(w.get("author") or "")):
                ids.append(wid)
    return ids

#!/usr/bin/env python3
"""Průběžné plnění Kindlify hotovými díly z knihovního Postgresu.

Knihovník obohacuje korpus po nocích (enrich_chunks → enrich_chapters), takže
hotových děl přibývá. Tenhle skript je sbírá: dílo, které má obohacené chunky
i souhrny kapitol, vyexportuje jako bundle do assetů Kindlify a zapíše ho do
`index.json`, ze kterého appka staví seznam knih. Kindlify tak roste s tím,
jak Knihovník postupuje, bez ruční úpravy Dartu.

    python3 kindlify_sync.py --out ../../Kindlify/assets/bundles            # nanečisto: co je hotové
    python3 kindlify_sync.py --out ../../Kindlify/assets/bundles --write    # zapsat bundly + index
    python3 kindlify_sync.py --out … --write --commit                       # a commitnout v Kindlify (bez push)

Jen SELECT, žádný LLM — běží v kterémkoli okně SPARKu i přes den.

Co je „hotové" (`ready_works`): aspoň `--min-chunks` chunků s obohacením
a aspoň `--min-chapters` kapitol (s textem) se souhrnem. Výchozí 0,95, ne
1,0: jeden chunk, na kterém model trvale selhává, by jinak dílo zablokoval
navždy. Dílo pak může přijít o pár souhrnů dřív, než Knihovník doběhne —
další běh ho přeexportuje a appka ho podle `pipelineVersion` reimportuje.

Bundle se přepisuje jen při změně obsahu (`pipelineVersion` je otisk), ne
při každém běhu — `generatedAt` by jinak dělal z každé noci commit všech děl.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from catalog import query_works  # noqa: E402
from export_bundle import asset_name, export_work, report, walk_nodes  # noqa: E402

INDEX = "index.json"


def ready_works(conn, *, priority: int, min_chunks: float, min_chapters: float) -> list[str]:
    """ID děl s dost obohacenými chunky i dost souhrny kapitol."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT w.id
            FROM works w
            JOIN LATERAL (
                SELECT count(*) FILTER (WHERE ch.summary_medium IS NOT NULL) AS done,
                       count(*) AS total
                FROM chapters ch WHERE ch.work_id = w.id AND ch.chunk_count > 0
            ) ch ON ch.total > 0
            JOIN LATERAL (
                SELECT count(*) AS done FROM chunks c
                JOIN chunk_enrichment e ON e.chunk_id = c.id
                WHERE c.work_id = w.id
            ) en ON true
            WHERE w.priority <= %s AND w.chunk_count > 0
              AND ch.done >= ch.total * %s
              AND en.done >= w.chunk_count * %s
            ORDER BY w.priority, w.id
            """,
            (priority, min_chapters, min_chunks),
        )
        return [r[0] for r in cur.fetchall()]


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build_index(out: Path) -> dict:
    """Index ze všech bundlů v adresáři — i ručních demo, které skript
    nevyrábí. Ruční `label` u existující položky se zachová (demo knihy mají
    kratší popisek, na který spoléhá integrační test Kindlify)."""
    old = {e["asset"]: e for e in (read_json(out / INDEX) or {}).get("bundles", [])}
    entries = []
    for path in sorted(out.glob("*.json")):
        if path.name == INDEX or not (b := read_json(path)):
            continue
        m = b.get("manifest") or {}
        asset = path.stem
        entries.append({
            "asset": asset,
            "slug": m.get("slug", asset.replace("_", "-")),
            "label": old.get(asset, {}).get("label") or m.get("title") or asset,
            "sourceLanguage": m.get("sourceLanguage", ""),
            "nodes": sum(1 for _ in walk_nodes(m["tree"])) if m.get("tree") else 0,
            "pipelineVersion": m.get("pipelineVersion", ""),
        })
    # Demo (ručně psané) napřed, ať zůstanou tam, kde je uživatel zná.
    entries.sort(key=lambda e: (not e["pipelineVersion"].startswith("demo"), e["label"]))
    return {"schemaVersion": "1.0", "bundles": entries}


def git_commit(repo: Path, paths: list[Path], message: str) -> bool:
    """Commit jen vlastních souborů. Nic nepushuje."""
    subprocess.run(["git", "-C", str(repo), "add", "--", *map(str, paths)], check=True)
    staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"])
    if staged.returncode == 0:
        return False
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True)
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="Hotová díla z Postgresu → bundly v assetech Kindlify")
    p.add_argument("--out", type=Path, required=True, help="Kindlify/assets/bundles")
    p.add_argument("--priority", type=int, default=1, help="díla s prioritou ≤ N")
    p.add_argument("--min-chunks", type=float, default=0.95, help="podíl obohacených chunků")
    p.add_argument("--min-chapters", type=float, default=0.95, help="podíl kapitol se souhrnem")
    p.add_argument("--top-terms", type=int, default=50)
    p.add_argument("--write", action="store_true", help="zapsat (bez něj jen výpis hotových děl)")
    p.add_argument("--commit", action="store_true", help="s --write: commitnout změny v repu Kindlify")
    p.add_argument("--branch", default="main",
                   help="--commit jen na téhle větvi Kindlify (jinde by bundly přistály v cizí práci)")
    p.add_argument("--dsn", default=os.getenv("PG_DSN"))
    args = p.parse_args()

    if not args.dsn:
        print("CHYBA: chybí --dsn / PG_DSN", file=sys.stderr)
        return 2
    if not args.out.is_dir():
        print(f"CHYBA: {args.out} neexistuje (Kindlify vedle WorldLibraryProject?)", file=sys.stderr)
        return 2

    import psycopg

    changed: list[str] = []
    with psycopg.connect(args.dsn) as conn:
        ids = ready_works(conn, priority=args.priority,
                          min_chunks=args.min_chunks, min_chapters=args.min_chapters)
        print(f"hotových děl: {len(ids)}", file=sys.stderr)
        for work in query_works(conn, work_ids=ids, hide_priority=None) if ids else []:
            bundle = export_work(conn, work, top=args.top_terms, chapter_detail="medium")
            path = args.out / asset_name(bundle["manifest"]["slug"])
            old = read_json(path)
            same = old and old["manifest"].get("pipelineVersion") == bundle["manifest"]["pipelineVersion"]
            print(f"{'  ' if same else '+ '}{report(bundle)}", file=sys.stderr)
            if same or not args.write:
                continue
            path.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            changed.append(bundle["manifest"]["slug"])

    if not args.write:
        print("nanečisto — zapíše až --write", file=sys.stderr)
        return 0

    index_path = args.out / INDEX
    index = build_index(args.out)
    if read_json(index_path) != index:
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"změněno bundlů: {len(changed)}, v indexu {len(index['bundles'])}", file=sys.stderr)

    if args.commit:
        repo = Path(subprocess.check_output(
            ["git", "-C", str(args.out), "rev-parse", "--show-toplevel"], text=True).strip())
        branch = subprocess.check_output(
            ["git", "-C", str(repo), "branch", "--show-current"], text=True).strip()
        if branch != args.branch:
            print(f"commit vynechán: Kindlify je na větvi '{branch}', ne '{args.branch}'", file=sys.stderr)
            return 0
        msg = f"bundles: sync z knihovny ({len(changed)} změněno)\n\n" + "\n".join(changed)
        if git_commit(repo, [args.out], msg):
            print(f"commit v {repo}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

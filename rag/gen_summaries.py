#!/usr/bin/env python3
"""Krátké české anotace děl do summaries.json. Běží na SPARK (LLM přes gateway).

Pro každé dílo v kolekci vezme první chunky jako ukázku a nechá LLM
napsat 1–2 věty do katalogu. Idempotentní: existující anotace přeskakuje
(--force přegeneruje vše). Server (server.py) soubor načítá při startu
— po doplnění anotací tedy `systemctl restart library-chat`.

Použití (na SPARK):
    python3 gen_summaries.py            # doplní chybějící
    python3 gen_summaries.py --force    # přegeneruje vše
"""

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

import chromadb
from openai import OpenAI

PROMPT = (
    "Napiš 1–2 věty česky jako katalogovou anotaci díla „{work}“ "
    "(tradice: {group}). Věcně: co to je a o čem to je. Bez hodnocení, "
    "bez uvozovek okolo. Vycházej z těchto ukázek textu:\n\n{samples}"
)


def main() -> int:
    p = argparse.ArgumentParser(description="Anotace děl do summaries.json")
    p.add_argument("--chroma-url", default="http://192.168.88.88:8006")
    p.add_argument("--collection", default="books")
    p.add_argument("--llm-url", default="http://localhost:8080/v1",
                   help="AiStack gateway (LiteLLM není publikovaný na host)")
    p.add_argument("--llm-model", default="translate")
    p.add_argument("--output", default=str(Path(__file__).parent / "summaries.json"))
    p.add_argument("--force", action="store_true", help="přegenerovat i existující")
    args = p.parse_args()

    url = urlparse(args.chroma_url)
    col = chromadb.HttpClient(host=url.hostname, port=url.port or 8000).get_collection(
        args.collection
    )
    llm = OpenAI(base_url=args.llm_url, api_key="dummy")

    out = Path(args.output)
    summaries = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}

    # díla + tradice z metadat (stránkovaně, jako server._build_catalog)
    works: dict[str, str] = {}
    limit, offset = 1000, 0
    while True:
        page = col.get(include=["metadatas"], limit=limit, offset=offset)
        metas = page["metadatas"] or []
        for m in metas:
            work = (m or {}).get("work")
            if work and work not in works:
                works[work] = m.get("group") or "misc"
        if len(metas) < limit:
            break
        offset += limit

    # anotace děl, která z katalogu zmizela, nedržet
    stale = set(summaries) - set(works)
    for work in stale:
        del summaries[work]
    if stale:
        print(f"odstraněno {len(stale)} anotací děl mimo katalog")

    for work, group in sorted(works.items()):
        if work in summaries and not args.force:
            continue
        got = col.get(where={"work": work}, limit=3, include=["documents"])
        samples = "\n---\n".join(d[:600] for d in got["documents"])
        resp = llm.chat.completions.create(
            model=args.llm_model,
            messages=[{"role": "user", "content": PROMPT.format(
                work=work, group=group, samples=samples)}],
            temperature=0.3,
            max_tokens=200,
        )
        summaries[work] = (resp.choices[0].message.content or "").strip()
        # průběžný zápis — přerušený běh neztratí hotové anotace
        out.write_text(
            json.dumps(summaries, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"{work}: {summaries[work][:90]}")

    print(f"\nHotovo: {len(summaries)} anotací → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

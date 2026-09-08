#!/usr/bin/env python3
"""Sonda obohacovacího modelu: pošle prvních N chunků z fronty přesně tím
promptem, který používá enrich_chunks.py, a vypíše, co model vrátil.

Proč: 3.–8. 9. 2026 swarm-director procházel smoke testem (krátký prompt,
200 tokenů), ale na skutečné úloze náhodně kazil tokeny (~0,4 % na token):
smyčky `"gloss_cs": 1, "gloss_cs": 1, …`, `Суди…`, EOS uprostřed věty,
překlepy v klíčích (`glos_cs`). Zdravý model dává ~350–550 tokenů,
finish_reason=stop a parsovatelný JSON. Sonda to odhalí za pár minut
bez zápisu do databáze.

Varianty na každý chunk:
  json    response_format=json_object (tak jede obohacení)
  nojson  bez gramatiky — odliší chybu xgrammaru od chyby modelu
  short   text zkrácený na 600 znaků — odliší závislost na délce

    python3 probe_llm.py                                   # director :8012
    python3 probe_llm.py --llm-url http://localhost:8002/v1 --model fallback
    python3 probe_llm.py --limit 10 --variants json --max-tokens 900

Nedeterministické výsledky (tentýž chunk jednou OK, podruhé BAD) ukazují
na korupci za běhu, ne na obsah chunku. Kontrolní běh na jiném modelu
na téže GPU odliší hardware od stacku modelu.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import psycopg
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).parent))
from enrich_chunks import build_messages, load_dotenv, load_topics, pending  # noqa: E402
from llm_batch import parse_json  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--llm-url", default="http://localhost:8012/v1")
    p.add_argument("--model", default="swarm-director")
    p.add_argument("--limit", type=int, default=6, help="kolik chunků z hlavy fronty")
    p.add_argument("--priority", type=int, default=1)
    p.add_argument("--max-tokens", type=int, default=1300, help="stejně jako library-enrich.service")
    p.add_argument("--workers", type=int, default=6, help="musí být pod --max-num-seqs modelu")
    p.add_argument("--variants", default="json,nojson,short")
    p.add_argument("--timeout", type=float, default=420.0)
    args = p.parse_args()

    here = Path(__file__).parent
    load_dotenv(here / ".env")
    conn = psycopg.connect(os.environ["PG_DSN"])
    _, hint = load_topics(here / "registry")
    items = list(pending(conn, args.priority, None, None, args.limit, "", "breadth"))
    print(f"{len(items)} chunků z fronty, model {args.model} @ {args.llm_url}:")
    for it in items:
        print(f"  {it['id'][:48]:48} lang={it['lang']:4} len={len(it['text']):5} {it['text'][:50]!r}")
    print(flush=True)

    client = OpenAI(base_url=args.llm_url, api_key="dummy", timeout=args.timeout, max_retries=0)

    def call(item: dict, variant: str) -> tuple[bool, str]:
        it = dict(item)
        if variant == "short":
            it["text"] = it["text"][:600]
        kw: dict = {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
        if variant != "nojson":
            kw["response_format"] = {"type": "json_object"}
        t0 = time.time()
        head = f"{item['id'][:40]:40} {item['lang']:4} {variant:6}"
        try:
            r = client.chat.completions.create(model=args.model, messages=build_messages(it, hint),
                                               temperature=0.2, max_tokens=args.max_tokens, **kw)
        except Exception as exc:  # noqa: BLE001 — chyba serveru je taky výsledek sondy
            return False, f"{head} EXC {time.time() - t0:4.0f}s {str(exc)[:90]}"
        c = r.choices[0]
        txt = c.message.content or ""
        ok = c.finish_reason == "stop" and parse_json(txt) is not None
        u = r.usage
        return ok, (f"{head} fin={c.finish_reason:6} in={u.prompt_tokens:5} out={u.completion_tokens:5} "
                    f"parse={'OK ' if parse_json(txt) is not None else 'BAD'} {time.time() - t0:4.0f}s | {txt[:100]!r}")

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    jobs = [(it, v) for it in items for v in variants]
    good = 0
    with ThreadPoolExecutor(args.workers) as ex:
        for fut in as_completed([ex.submit(call, it, v) for it, v in jobs]):
            ok, line = fut.result()
            good += ok
            print(line, flush=True)
    print(f"\nHOTOVO: {good}/{len(jobs)} čistých (finish=stop a parsovatelný JSON)")
    return 0 if good == len(jobs) else 1


if __name__ == "__main__":
    sys.exit(main())

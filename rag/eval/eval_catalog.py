#!/usr/bin/env python3
"""Eval plánovače a katalogových odpovědí — přes běžící server (POST /plan,
GET /works, GET /works/{id}/chapters), bez generování odpovědi.

Zlatý standard eval/golden_v3.jsonl: {"q", "intent", "expect_work"?,
"expect_author"?, "expect_topics"?, "expect_groups"?, "expect_chapter_ref"?,
"min_works"?}. Měří:
  intent_acc        shoda intentu plánovače
  work_resolved     work_hint/author → očekávané work_id mezi vyřešenými
  topic_hit         očekávané téma mezi topics plánu
  catalog_ok        katalogový dotaz vrátí ≥ min_works děl (přes /works s filtry)
  latency_plan_ms   medián

Použití (server v PG režimu):
    python3 eval/eval_catalog.py --url http://localhost:8099
"""

import argparse
import json
import statistics
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def call(url: str, path: str, payload: dict | None = None, timeout: float = 120.0):
    req = urllib.request.Request(url + path, method="POST" if payload is not None else "GET")
    req.add_header("Content-Type", "application/json")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8090")
    p.add_argument("--golden", default=str(Path(__file__).parent / "golden_v3.jsonl"))
    p.add_argument("--out", default="")
    args = p.parse_args()
    items = [json.loads(l) for l in open(args.golden, encoding="utf-8") if l.strip()]

    rows = []
    for it in items:
        plan = call(args.url, "/plan", {"message": it["q"]})
        row = {"q": it["q"], "intent_expected": it.get("intent"), "intent": plan.get("intent"),
               "ms": plan.get("ms"), "model": plan.get("model")}
        row["intent_ok"] = (plan.get("intent") == it.get("intent")) if it.get("intent") else None
        if it.get("expect_topics"):
            row["topic_ok"] = any(t in (plan.get("topics") or []) for t in it["expect_topics"])
        if it.get("expect_groups"):
            row["group_ok"] = any(g in (plan.get("groups") or []) for g in it["expect_groups"])
        # dílo přes /works?q= (název z plánu) nebo autor
        if it.get("expect_work"):
            hint = plan.get("work_hint") or plan.get("author") or ""
            found = []
            if hint:
                res = call(args.url, "/works?" + urllib.parse.urlencode({"q": hint, "limit": 50}))
                found = [w["id"] for w in res.get("works", [])]
                if not found:
                    res = call(args.url, "/works?" + urllib.parse.urlencode({"author": hint, "limit": 50}))
                    found = [w["id"] for w in res.get("works", [])]
            row["work_ok"] = it["expect_work"] in found
            row["work_hint"] = hint
        if it.get("expect_author"):
            res = call(args.url, "/works?" + urllib.parse.urlencode({"author": plan.get("author") or "", "limit": 5}))
            row["author_ok"] = bool(plan.get("author")) and res.get("total", 0) >= it.get("min_works", 1)
        if it.get("intent") == "catalog" and it.get("min_works"):
            q = {"limit": 5}
            if plan.get("topics"):
                q["topic"] = plan["topics"][0]
            if plan.get("groups"):
                q["group"] = plan["groups"][0]
            if plan.get("author"):
                q["author"] = plan["author"]
            res = call(args.url, "/works?" + urllib.parse.urlencode(q))
            row["catalog_ok"] = res.get("total", 0) >= it["min_works"]
            row["catalog_total"] = res.get("total")
        rows.append(row)
        flag = "✓" if row.get("intent_ok") else ("✗" if row.get("intent_ok") is False else "·")
        print(f"{flag} {it['q'][:52]:<53} {row['intent']:<15} {row.get('ms') or 0:>6} ms  "
              f"{'work✓' if row.get('work_ok') else ('work✗' if row.get('work_ok') is False else '')} "
              f"{'topic✓' if row.get('topic_ok') else ('topic✗' if row.get('topic_ok') is False else '')} "
              f"{'cat✓' if row.get('catalog_ok') else ('cat✗' if row.get('catalog_ok') is False else '')}")

    def rate(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    agg = {"questions": len(rows), "intent_acc": rate("intent_ok"), "work_resolved": rate("work_ok"),
           "topic_hit": rate("topic_ok"), "group_hit": rate("group_ok"), "catalog_ok": rate("catalog_ok"),
           "author_ok": rate("author_ok"),
           "latency_plan_ms_median": statistics.median([r["ms"] for r in rows if r.get("ms")]) if any(r.get("ms") for r in rows) else None,
           "model": rows[0].get("model") if rows else None}
    print("\n== agregát ==")
    for k, v in agg.items():
        print(f"  {k:24s} {v}")
    if args.out:
        Path(args.out).write_text(json.dumps({"aggregate": agg, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

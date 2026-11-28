"""load_pg --drop-work: mazání překladu nahrazeného originálem z Postgresu i Chromy.

Bez Postgresu: SQLite v paměti s tabulkami, na kterých dílo visí (cizí klíče
s ON DELETE CASCADE jako v sql/0001_init.sql), a tenký obal, který přeloží
psycopg `%s` na `?` a dodá `conn.transaction()`.
"""

import json
import sqlite3
from contextlib import contextmanager

import pytest

import load_pg

SCHEMA = """
CREATE TABLE works (id TEXT PRIMARY KEY, title TEXT);
CREATE TABLE chapters (id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
                       ordinal INT, summary_medium TEXT);
CREATE TABLE chunks (id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
                     seq INT);
CREATE TABLE chunk_enrichment (chunk_id TEXT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE, gloss_cs TEXT);
CREATE TABLE work_topics (work_id TEXT REFERENCES works(id) ON DELETE CASCADE, topic_id TEXT);
"""


class Cur:
    def __init__(self, cur):
        self._c = cur

    def execute(self, sql, params=()):
        self._c.execute(sql.replace("%s", "?"), params)

    def __getattr__(self, name):
        return getattr(self._c, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._c.close()


class Conn:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def cursor(self):
        return Cur(self.db.cursor())

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def count(self, table):
        return self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@pytest.fixture
def conn():
    c = Conn()
    for wid in ("zh.zhuangzi", "zh.zhuangzi_lzh"):
        c.db.execute("INSERT INTO works VALUES (?, ?)", (wid, wid))
        for i in range(3):
            c.db.execute("INSERT INTO chapters VALUES (?, ?, ?, ?)", (f"{wid}:{i}", wid, i, "souhrn" if i else None))
            c.db.execute("INSERT INTO chunks VALUES (?, ?, ?)", (f"{wid}:{i}:0", wid, i))
            c.db.execute("INSERT INTO chunk_enrichment VALUES (?, ?)", (f"{wid}:{i}:0", f"glosa {i}"))
        c.db.execute("INSERT INTO work_topics VALUES (?, 'daoism')", (wid,))
    return c


def test_dry_run_counts_and_touches_nothing(conn, tmp_path):
    counts, backup = load_pg.drop_work(conn, "zh.zhuangzi", {"zh.zhuangzi"}, tmp_path, dry_run=True)
    assert counts == {"kapitol": 3, "chunků": 3, "obohacení": 3, "souhrnů kapitol": 2, "témat díla": 1}
    assert backup is None and not list(tmp_path.iterdir())
    assert conn.count("works") == 2 and conn.count("chunk_enrichment") == 6


def test_refuses_work_still_in_registry(conn, tmp_path):
    with pytest.raises(ValueError):
        load_pg.drop_work(conn, "zh.zhuangzi", {"zh.zhuangzi"}, tmp_path, dry_run=False)
    assert conn.count("works") == 2


def test_unknown_work(conn, tmp_path):
    with pytest.raises(LookupError):
        load_pg.drop_work(conn, "zh.nic", set(), tmp_path, dry_run=True)


def test_drop_deletes_only_that_work_and_writes_backup(conn, tmp_path):
    counts, backup = load_pg.drop_work(conn, "zh.zhuangzi", {"zh.zhuangzi_lzh"}, tmp_path, dry_run=False)
    assert counts["chunků"] == 3
    # originál zůstal celý, překlad i se vším, co na něm viselo, je pryč
    assert conn.db.execute("SELECT id FROM works").fetchall() == [("zh.zhuangzi_lzh",)]
    for table in ("chapters", "chunks", "chunk_enrichment", "work_topics"):
        assert conn.count(table) in (3, 1)
    rows = [json.loads(x) for x in backup.read_text(encoding="utf-8").splitlines()]
    by = {}
    for r in rows:
        by.setdefault(r["table"], []).append(r["row"])
    assert [w["id"] for w in by["works"]] == ["zh.zhuangzi"]
    assert len(by["chapters"]) == 3 and len(by["chunk_enrichment"]) == 3 and len(by["work_topics"]) == 1
    assert by["chunk_enrichment"][0] == {"chunk_id": "zh.zhuangzi:0:0", "gloss_cs": "glosa 0"}


def test_failed_backup_deletes_nothing(conn, tmp_path, monkeypatch):
    real = load_pg.json.dumps
    calls = []

    def broken(obj, **kw):           # zápis zálohy selže uprostřed (plný disk apod.)
        calls.append(obj)
        if len(calls) > 2:
            raise OSError("disk plný")
        return real(obj, **kw)

    monkeypatch.setattr(load_pg.json, "dumps", broken)
    with pytest.raises(OSError):
        load_pg.drop_work(conn, "zh.zhuangzi", set(), tmp_path, dry_run=False)
    assert conn.count("works") == 2 and conn.count("chunk_enrichment") == 6


# --- Chroma ---------------------------------------------------------------


class FakeCollection:
    """Jen to, co drop_work_chroma z kolekce volá: get podle work_id a delete podle ID."""

    def __init__(self, passages: dict[str, int]):
        self.rows = {f"{wid}:{i}:p": wid for wid, n in passages.items() for i in range(n)}
        self.deletes: list[int] = []

    def get(self, where, include):
        assert include == []          # jen ID, žádné vektory ani texty
        return {"ids": [pid for pid, wid in self.rows.items() if wid == where["work_id"]]}

    def delete(self, ids):
        self.deletes.append(len(ids))
        for pid in ids:
            del self.rows[pid]

    def left(self, wid):
        return sum(1 for w in self.rows.values() if w == wid)


def test_chroma_dry_run_only_counts():
    col = FakeCollection({"zh.zhuangzi": 5, "zh.zhuangzi_lzh": 2})
    assert load_pg.drop_work_chroma(col, "zh.zhuangzi") == 5
    assert col.deletes == [] and col.left("zh.zhuangzi") == 5


def test_chroma_drop_deletes_only_that_work_in_batches():
    col = FakeCollection({"zh.zhuangzi": 5, "zh.zhuangzi_lzh": 2})
    assert load_pg.drop_work_chroma(col, "zh.zhuangzi", dry_run=False, batch=2) == 5
    assert col.deletes == [2, 2, 1]
    assert col.left("zh.zhuangzi") == 0 and col.left("zh.zhuangzi_lzh") == 2


def run_main_drop(monkeypatch, conn, col, tmp_path, *work_ids, dry_run=False, registry=("zh.zhuangzi_lzh",)):
    import argparse
    import ingest_books

    @contextmanager
    def connect(_dsn):
        yield conn

    monkeypatch.setattr(load_pg.psycopg, "connect", connect)
    monkeypatch.setattr(load_pg, "chroma_collection", lambda url, name: col)
    monkeypatch.setattr(ingest_books, "load_works", lambda _path: dict.fromkeys(registry))
    args = argparse.Namespace(drop_work=list(work_ids), dry_run=dry_run, registry="registry", dsn="x",
                              backup_dir=tmp_path, chroma_url="http://chroma", collection="books_v2",
                              no_chroma=col is None)
    return load_pg.main_drop(args)


def test_main_drop_clears_postgres_and_chroma(conn, tmp_path, monkeypatch, capsys):
    col = FakeCollection({"zh.zhuangzi": 4, "zh.zhuangzi_lzh": 2})
    assert run_main_drop(monkeypatch, conn, col, tmp_path, "zh.zhuangzi") == 0
    assert conn.db.execute("SELECT id FROM works").fetchall() == [("zh.zhuangzi_lzh",)]
    assert col.left("zh.zhuangzi") == 0 and col.left("zh.zhuangzi_lzh") == 2
    assert "pasáží v books_v2 4" in capsys.readouterr().out


def test_main_drop_dry_run_touches_neither(conn, tmp_path, monkeypatch, capsys):
    col = FakeCollection({"zh.zhuangzi": 4})
    assert run_main_drop(monkeypatch, conn, col, tmp_path, "zh.zhuangzi", dry_run=True) == 0
    assert conn.count("works") == 2 and col.left("zh.zhuangzi") == 4
    assert "DRY-RUN zh.zhuangzi: smazalo by se" in capsys.readouterr().out


def test_main_drop_cleans_chroma_leftovers_when_postgres_is_already_done(conn, tmp_path, monkeypatch, capsys):
    """Minulý běh smazal Postgres a spadl před Chromou — druhý běh vektory doklidí."""
    col = FakeCollection({"zh.zhuangzi": 4})
    conn.db.execute("DELETE FROM works WHERE id = 'zh.zhuangzi'")
    assert run_main_drop(monkeypatch, conn, col, tmp_path, "zh.zhuangzi") == 0
    assert col.left("zh.zhuangzi") == 0
    assert "v Postgresu už není" in capsys.readouterr().out


def test_main_drop_unknown_work_fails_and_registered_work_keeps_its_vectors(conn, tmp_path, monkeypatch):
    col = FakeCollection({"zh.zhuangzi_lzh": 2})
    assert run_main_drop(monkeypatch, conn, col, tmp_path, "zh.nic") == 1
    # dílo, které je pořád v registru, se nemaže ani z Chromy
    assert run_main_drop(monkeypatch, conn, col, tmp_path, "zh.zhuangzi_lzh") == 1
    assert col.left("zh.zhuangzi_lzh") == 2 and conn.count("works") == 2

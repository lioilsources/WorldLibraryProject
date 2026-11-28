"""load_pg.drop_work: mazání překladu nahrazeného originálem.

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

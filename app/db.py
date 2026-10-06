"""SQLite 持久化层：展厅 / 通道 / 讲解站 / 封厅修订号 / 参观会话。

所有业务读写都走 :func:`connect`，开启外键约束与 WAL，
容器重启后数据仍在（数据文件位于挂载卷 /data）。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

DB_PATH = os.environ.get("GUIDE_DB_PATH", "/data/guide.db")

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS halls (
    hall_id   TEXT PRIMARY KEY,
    is_closed INTEGER NOT NULL DEFAULT 0
);

-- 封厅修订号：任何一次封厅/重开成功提交都会 +1，全程单调递增。
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    closure_revision INTEGER NOT NULL,
    entrance_hall TEXT REFERENCES halls(hall_id)
);
INSERT OR IGNORE INTO meta (id, closure_revision) VALUES (1, 0);

-- 双向通道只存一行；(a,b) 规范化为 a < b，保证同一条通道不重复登记。
CREATE TABLE IF NOT EXISTS corridors (
    hall_a TEXT NOT NULL REFERENCES halls(hall_id),
    hall_b TEXT NOT NULL REFERENCES halls(hall_id),
    PRIMARY KEY (hall_a, hall_b),
    CHECK (hall_a < hall_b)
);

CREATE TABLE IF NOT EXISTS stations (
    station_id TEXT PRIMARY KEY,
    hall_id    TEXT NOT NULL REFERENCES halls(hall_id),
    seq        INTEGER NOT NULL,          -- 全站导览顺序（越小越早）
    text       TEXT NOT NULL,
    UNIQUE (seq)
);

CREATE TABLE IF NOT EXISTS visits (
    session_id TEXT PRIMARY KEY,
    hall_id    TEXT NOT NULL REFERENCES halls(hall_id)  -- 当前位置（所在展厅）
);

CREATE TABLE IF NOT EXISTS heard (
    session_id TEXT NOT NULL REFERENCES visits(session_id),
    station_id TEXT NOT NULL REFERENCES stations(station_id),
    PRIMARY KEY (session_id, station_id)
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db(db_path: str | None = None) -> None:
    """建表并灌入演示展馆数据（已存在则跳过）。"""
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        cur = conn.execute("SELECT COUNT(*) AS n FROM halls")
        if cur.fetchone()["n"] == 0:
            seed_demo(conn)
        conn.commit()
    finally:
        conn.close()


def seed_demo(conn: sqlite3.Connection) -> None:
    """演示布局：入口 A，通道 A-B、A-C、B-D、C-D、D-E（含两条等长最短路径）。

    讲解站按导览顺序：s1(A) s2(B) s3(C) s4(D) s5(E)。
    """
    halls = ["A", "B", "C", "D", "E"]
    conn.executemany("INSERT INTO halls(hall_id) VALUES (?)", [(h,) for h in halls])
    corridors = [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D"), ("D", "E")]
    conn.executemany(
        "INSERT INTO corridors(hall_a, hall_b) VALUES (?,?)", corridors
    )
    stations = [
        ("s1", "A", 1, "欢迎来到序厅 A，展馆整体概览。"),
        ("s2", "B", 2, "展厅 B：青铜器专题。"),
        ("s3", "C", 3, "展厅 C：书画专题。"),
        ("s4", "D", 4, "展厅 D：交流互动中心。"),
        ("s5", "E", 5, "展厅 E：尾厅，感谢参观。"),
    ]
    conn.executemany(
        "INSERT INTO stations(station_id, hall_id, seq, text) VALUES (?,?,?,?)",
        stations,
    )
    conn.execute("UPDATE meta SET entrance_hall = 'A' WHERE id = 1")


@contextmanager
def txn(conn: sqlite3.Connection, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """事务上下文。

    immediate=True 时使用 ``BEGIN IMMEDIATE`` 获取写锁，
    保证「核对 + 写入」在同一写事务中原子完成。
    """
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise

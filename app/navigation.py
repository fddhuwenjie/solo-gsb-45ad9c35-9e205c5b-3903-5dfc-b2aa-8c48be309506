"""导览导航：在单个事务提供的数据快照上计算下一站与路径。

路径规则：
* 通道数最少（无权最短路）；
* 等长路径中，取整条展厅序列（含起点、终点）字典序最小者；
* 封厅展厅不得穿过；但会话当前所在展厅若事后被封，
  仍可沿开放通道“离开”，只是不能继续留在其中听站。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    id: str
    hall_id: str
    seq: int
    text: str


@dataclass(frozen=True)
class Snapshot:
    revision: int
    current_hall: str
    closed: frozenset[str]
    adjacency: dict[str, frozenset[str]]
    stations: tuple[Station, ...]       # 已按导览顺序 seq 排序
    heard: frozenset[str]


def load_snapshot(conn: sqlite3.Connection, session_id: str) -> Snapshot:
    """在调用方的事务内读取所有相关数据（同一快照）。"""
    row = conn.execute(
        "SELECT m.closure_revision AS rev, v.hall_id AS cur "
        "FROM meta m, visits v WHERE v.session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise KeyError(session_id)

    closed = frozenset(
        r["hall_id"]
        for r in conn.execute("SELECT hall_id FROM halls WHERE is_closed = 1")
    )

    adj: dict[str, set[str]] = {}
    for r in conn.execute("SELECT hall_a, hall_b FROM corridors"):
        adj.setdefault(r["hall_a"], set()).add(r["hall_b"])
        adj.setdefault(r["hall_b"], set()).add(r["hall_a"])

    stations = tuple(
        Station(r["station_id"], r["hall_id"], r["seq"], r["text"])
        for r in conn.execute(
            "SELECT station_id, hall_id, seq, text FROM stations ORDER BY seq"
        )
    )

    heard = frozenset(
        r["station_id"]
        for r in conn.execute(
            "SELECT station_id FROM heard WHERE session_id = ?", (session_id,)
        )
    )

    return Snapshot(
        revision=row["rev"],
        current_hall=row["cur"],
        closed=closed,
        adjacency={h: frozenset(ns) for h, ns in adj.items()},
        stations=stations,
        heard=heard,
    )


def reachable_paths(snap: Snapshot) -> dict[str, tuple[str, ...]]:
    """返回从当前展厅可达的开放展厅 -> 最短且字典序最小的展厅序列。

    采用逐层 BFS：每一层把候选路径按完整展厅元组比较，保留最小者，
    因此距离最小；同距离时整条展厅序列字典序最小。
    当前展厅即使被封也作为 BFS 起点（允许离开），但返回结果只含
    *开放* 展厅（封闭展厅内不能收听）。
    """
    start = snap.current_hall
    best: dict[str, tuple[str, ...]] = {start: (start,)}
    frontier = {start: (start,)}
    while frontier:
        candidates: dict[str, tuple[str, ...]] = {}
        for u, prefix in frontier.items():
            for v in snap.adjacency.get(u, frozenset()):
                if v in best or v in snap.closed:
                    continue  # 已得更短路径，或封闭展厅不可穿过
                cand = prefix + (v,)
                old = candidates.get(v)
                if old is None or cand < old:
                    candidates[v] = cand
        best.update(candidates)
        frontier = candidates
    return {h: p for h, p in best.items() if h not in snap.closed}


@dataclass(frozen=True)
class NextResult:
    status: str                           # "next" | "blocked" | "finished"
    station: Station | None
    path: tuple[str, ...]                 # status == "next" 时非空
    corridors: int                        # 路径通道数
    remaining: int                        # 未听站点总数（含不可达）


def next_station(snap: Snapshot) -> NextResult:
    """同一快照内计算：封厅修订号 + 导览顺序最早可达的未听站 + 路径。"""
    paths = reachable_paths(snap)
    pending = [s for s in snap.stations if s.id not in snap.heard]
    chosen = next((s for s in pending if s.hall_id in paths), None)
    if chosen is not None:
        path = paths[chosen.hall_id]
        return NextResult("next", chosen, path, len(path) - 1, len(pending))
    if pending:
        return NextResult("blocked", None, (), 0, len(pending))
    return NextResult("finished", None, (), 0, 0)

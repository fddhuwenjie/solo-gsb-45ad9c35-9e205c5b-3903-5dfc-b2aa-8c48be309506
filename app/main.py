"""展馆分站讲解续行 HTTP API。

启动后提供：
* 展馆登记：展厅、双向通道、入口、有序讲解站；
* 封厅 / 重开：封厅修订号单调递增；
* 参观会话：开始、查询（下一站 + 路径）、确认收听（乐观并发校验）。
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from . import db
from .navigation import NextResult, Snapshot, load_snapshot, next_station


# ------------------------------- 请求模型 ------------------------------- #


class HallsIn(BaseModel):
    hall_ids: list[str] = Field(min_length=1)


class CorridorsIn(BaseModel):
    corridors: list[list[str]] = Field(min_length=1)


class EntranceIn(BaseModel):
    hall_id: str


class StationIn(BaseModel):
    station_id: str
    hall_id: str
    seq: int = Field(ge=0)
    text: str


class StationsIn(BaseModel):
    stations: list[StationIn] = Field(min_length=1)


class HallIn(BaseModel):
    hall_id: str


class AcknowledgeIn(BaseModel):
    revision: int = Field(ge=0, description="客户端所见封厅修订号")
    station_id: str


# ------------------------------- 序列化 ------------------------------- #


def _next_payload(res: NextResult, snap: Snapshot) -> dict:
    data: dict = {
        "status": res.status,
        "revision": snap.revision,
        "current_hall": snap.current_hall,
        "remaining": res.remaining,
    }
    if res.status == "next":
        data["station"] = {
            "station_id": res.station.id,
            "hall_id": res.station.hall_id,
            "seq": res.station.seq,
            "text": res.station.text,
        }
        data["path"] = list(res.path)
        data["corridors"] = res.corridors
    return data


def _session_payload(conn: sqlite3.Connection, session_id: str) -> dict:
    snap = load_snapshot(conn, session_id)
    res = next_station(snap)
    heard = [
        r["station_id"]
        for r in conn.execute(
            "SELECT s.station_id FROM heard h "
            "JOIN stations s ON s.station_id = h.station_id "
            "WHERE h.session_id = ? ORDER BY s.seq",
            (session_id,),
        )
    ]
    return {
        "session_id": session_id,
        "current_hall": snap.current_hall,
        "heard": heard,
        **_next_payload(res, snap),
    }


# ------------------------------- 应用 ------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="展馆分站讲解续行 API", version="1.0.0", lifespan=lifespan)


def get_conn() -> sqlite3.Connection:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(sqlite3.IntegrityError)
def integrity_handler(_request, exc: sqlite3.IntegrityError):
    # 唯一冲突 / 外键缺失 / CHECK 失败等登记错误
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": f"数据约束错误: {exc}"},
    )


def _revision(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT closure_revision AS r FROM meta WHERE id=1").fetchone()[
        "r"
    ]


def _require_hall(conn: sqlite3.Connection, hall_id: str) -> None:
    row = conn.execute("SELECT 1 FROM halls WHERE hall_id=?", (hall_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"展厅不存在: {hall_id}")


# ------------------------------- 展馆登记 ------------------------------- #


@app.post("/api/admin/halls", status_code=status.HTTP_201_CREATED)
def add_halls(body: HallsIn, conn: sqlite3.Connection = Depends(get_conn)):
    if len(set(body.hall_ids)) != len(body.hall_ids):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "hall_ids 含重复项")
    with db.txn(conn, immediate=True):
        conn.executemany(
            "INSERT OR IGNORE INTO halls(hall_id) VALUES (?)",
            [(h,) for h in body.hall_ids],
        )
    return {"added": body.hall_ids}


@app.post("/api/admin/corridors", status_code=status.HTTP_201_CREATED)
def add_corridors(body: CorridorsIn, conn: sqlite3.Connection = Depends(get_conn)):
    pairs = []
    for item in body.corridors:
        if len(item) != 2 or item[0] == item[1]:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"非法通道（需两个不同展厅）: {item}"
            )
        pairs.append(tuple(sorted(item)))
    if len(set(pairs)) != len(pairs):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "corridors 含重复通道")
    with db.txn(conn, immediate=True):
        for a, b in pairs:
            _require_hall(conn, a)
            _require_hall(conn, b)
            conn.execute(
                "INSERT OR IGNORE INTO corridors(hall_a, hall_b) VALUES (?,?)", (a, b)
            )
    return {"added": [list(p) for p in pairs]}


@app.put("/api/admin/entrance")
def set_entrance(body: EntranceIn, conn: sqlite3.Connection = Depends(get_conn)):
    with db.txn(conn, immediate=True):
        _require_hall(conn, body.hall_id)
        conn.execute("UPDATE meta SET entrance_hall=? WHERE id=1", (body.hall_id,))
    return {"entrance_hall": body.hall_id}


@app.post("/api/admin/stations", status_code=status.HTTP_201_CREATED)
def upsert_stations(body: StationsIn, conn: sqlite3.Connection = Depends(get_conn)):
    ids = [s.station_id for s in body.stations]
    if len(set(ids)) != len(ids):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "station_id 含重复项")
    seqs = [s.seq for s in body.stations]
    if len(set(seqs)) != len(seqs):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "seq 含重复序号")
    with db.txn(conn, immediate=True):
        for s in body.stations:
            _require_hall(conn, s.hall_id)
            # UPSERT：按 station_id 覆盖所在展厅、顺序与文本。
            conn.execute(
                """INSERT INTO stations(station_id, hall_id, seq, text)
                   VALUES (?,?,?,?)
                   ON CONFLICT(station_id) DO UPDATE SET
                       hall_id=excluded.hall_id,
                       seq=excluded.seq,
                       text=excluded.text""",
                (s.station_id, s.hall_id, s.seq, s.text),
            )
    return {"upserted": ids}


@app.get("/api/admin/venue")
def get_venue(conn: sqlite3.Connection = Depends(get_conn)):
    with db.txn(conn):  # 单一读事务 => 同一快照
        halls = [
            {"hall_id": r["hall_id"], "is_closed": bool(r["is_closed"])}
            for r in conn.execute("SELECT hall_id, is_closed FROM halls ORDER BY hall_id")
        ]
        corridors = [
            [r["hall_a"], r["hall_b"]]
            for r in conn.execute(
                "SELECT hall_a, hall_b FROM corridors ORDER BY hall_a, hall_b"
            )
        ]
        stations = [
            {
                "station_id": r["station_id"],
                "hall_id": r["hall_id"],
                "seq": r["seq"],
                "text": r["text"],
            }
            for r in conn.execute(
                "SELECT station_id, hall_id, seq, text FROM stations ORDER BY seq"
            )
        ]
        entrance = conn.execute(
            "SELECT entrance_hall AS e FROM meta WHERE id=1"
        ).fetchone()["e"]
        return {
            "entrance_hall": entrance,
            "halls": halls,
            "corridors": corridors,
            "stations": stations,
            "closure_revision": _revision(conn),
        }


# ------------------------------- 封厅 / 重开 ------------------------------- #


def _set_closed(conn: sqlite3.Connection, hall_id: str, closed: bool) -> int:
    with db.txn(conn, immediate=True):
        _require_hall(conn, hall_id)
        row = conn.execute(
            "SELECT is_closed FROM halls WHERE hall_id=?", (hall_id,)
        ).fetchone()
        changed = bool(row["is_closed"]) != closed
        if changed:
            conn.execute(
                "UPDATE halls SET is_closed=? WHERE hall_id=?",
                (1 if closed else 0, hall_id),
            )
            conn.execute(
                "UPDATE meta SET closure_revision = closure_revision + 1 WHERE id=1"
            )
        return _revision(conn)


@app.post("/api/admin/closures/{hall_id}")
def close_hall(hall_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    revision = _set_closed(conn, hall_id, True)
    return {"hall_id": hall_id, "is_closed": True, "closure_revision": revision}


@app.delete("/api/admin/closures/{hall_id}")
def reopen_hall(hall_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    revision = _set_closed(conn, hall_id, False)
    return {"hall_id": hall_id, "is_closed": False, "closure_revision": revision}


@app.get("/api/admin/closures")
def list_closures(conn: sqlite3.Connection = Depends(get_conn)):
    with db.txn(conn):
        closed = [
            r["hall_id"]
            for r in conn.execute(
                "SELECT hall_id FROM halls WHERE is_closed=1 ORDER BY hall_id"
            )
        ]
        return {"closed_halls": closed, "closure_revision": _revision(conn)}


# ------------------------------- 参观会话 ------------------------------- #


@app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
def start_session(conn: sqlite3.Connection = Depends(get_conn)):
    session_id = uuid.uuid4().hex
    with db.txn(conn, immediate=True):
        entrance = conn.execute(
            "SELECT entrance_hall AS e FROM meta WHERE id=1"
        ).fetchone()["e"]
        if not entrance:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "尚未登记入口展厅，无法开始参观"
            )
        conn.execute(
            "INSERT INTO visits(session_id, hall_id) VALUES (?,?)",
            (session_id, entrance),
        )
    return {"session_id": session_id, "entrance_hall": entrance}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    with db.txn(conn):
        try:
            return _session_payload(conn, session_id)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")


@app.get("/api/sessions/{session_id}/next")
def get_next(session_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    with db.txn(conn):  # 单一读事务，全部字段来自同一快照
        try:
            snap = load_snapshot(conn, session_id)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
        return _next_payload(next_station(snap), snap)


@app.post("/api/sessions/{session_id}/acknowledge")
def acknowledge(
    session_id: str,
    body: AcknowledgeIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """确认收听：同一写事务内核对修订号、目标站点与路径可达性。

    任一条件不满足（封厅已变 / 站点非当前下一站 / 路径失效）均拒绝，
    位置与已听记录保持不变，返回 409。
    """
    with db.txn(conn, immediate=True):
        try:
            snap = load_snapshot(conn, session_id)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")

        if snap.revision != body.revision:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "error": "revision_mismatch",
                    "message": "封厅修订号已变化，请基于最新快照重新规划",
                    "seen_revision": body.revision,
                    "current_revision": snap.revision,
                },
            )

        res = next_station(snap)
        if res.status != "next" or res.station.id != body.station_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "error": "not_current_next",
                    "message": "该站点不是当前下一站或路径已失效",
                    "current": _next_payload(res, snap),
                },
            )

        # 同一事务中提交：写入已听 + 移动到站点所在展厅。
        conn.execute(
            "INSERT OR IGNORE INTO heard(session_id, station_id) VALUES (?,?)",
            (session_id, body.station_id),
        )
        conn.execute(
            "UPDATE visits SET hall_id=? WHERE session_id=?",
            (res.station.hall_id, session_id),
        )

        # 提交前基于写入后的状态返回最新会话信息。
        payload = _session_payload(conn, session_id)
    return payload


@app.get("/")
def root():
    return {
        "service": "展馆分站讲解续行 API",
        "docs": "/docs",
        "endpoints": [
            "POST /api/admin/halls",
            "POST /api/admin/corridors",
            "PUT  /api/admin/entrance",
            "POST /api/admin/stations",
            "GET  /api/admin/venue",
            "POST /api/admin/closures/{hall_id}",
            "DELETE /api/admin/closures/{hall_id}",
            "GET  /api/admin/closures",
            "POST /api/sessions",
            "GET  /api/sessions/{id}",
            "GET  /api/sessions/{id}/next",
            "POST /api/sessions/{id}/acknowledge",
        ],
    }

"""端到端行为验证（标准库 + FastAPI TestClient）。

运行：
    GUIDE_DB_PATH=/tmp/guide_test.db python3 test_api.py
脚本会先删除旧库，使用内置演示数据 A-B、A-C、B-D、C-D、D-E，站点 s1..s5。
"""

import os
import sys
import threading

os.environ.setdefault("GUIDE_DB_PATH", "/tmp/guide_test.db")
if os.path.exists(os.environ["GUIDE_DB_PATH"]):
    os.remove(os.environ["GUIDE_DB_PATH"])

# 允许直接从仓库根目录运行
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def must(c, r):
    assert 200 <= r.status_code < 300, f"{c} -> {r.status_code} {r.text}"
    return r.json()


with TestClient(app) as client:
    print("1) 基本续听：入口 A，依次 s1..s5，路径最短")
    sid = must("start", client.post("/api/sessions"))["session_id"]

    n = must("next", client.get(f"/api/sessions/{sid}/next"))
    check("首站 s1 / 原地 / 修订号0",
          n["status"] == "next" and n["station"]["station_id"] == "s1"
          and n["path"] == ["A"] and n["corridors"] == 0 and n["revision"] == 0, n)

    body = must("ack s1", client.post(
        f"/api/sessions/{sid}/acknowledge",
        json={"revision": 0, "station_id": "s1"}))
    check("确认后位置到 A，下一站 s2 路径 A-B",
          body["current_hall"] == "A" and body["station"]["station_id"] == "s2"
          and body["path"] == ["A", "B"] and body["corridors"] == 1, body)

    print("2) 封 D：s4/s5 暂不可达，s3 可绕路 B-A-C 先听（穿不过封闭的 D）")
    rev_after_close = must("close D", client.post("/api/admin/closures/D"))["closure_revision"]
    check("封厅后修订号=1", rev_after_close == 1, rev_after_close)
    n = must("next after close", client.get(f"/api/sessions/{sid}/next"))
    check("下一站仍为 s2（在 B），路径 A-B",
          n["station"]["station_id"] == "s2" and n["path"] == ["A", "B"], n)
    must("ack s2", client.post(f"/api/sessions/{sid}/acknowledge",
                               json={"revision": 1, "station_id": "s2"}))
    n = must("next s3 detour", client.get(f"/api/sessions/{sid}/next"))
    check("s3 绕路 B->A->C，2 条通道，不经过 D",
          n["station"]["station_id"] == "s3"
          and n["path"] == ["B", "A", "C"] and n["corridors"] == 2, n)
    must("ack s3", client.post(f"/api/sessions/{sid}/acknowledge",
                               json={"revision": 1, "station_id": "s3"}))
    n = must("next blocked", client.get(f"/api/sessions/{sid}/next"))
    check("只剩不可达的 s4/s5 -> blocked，remaining=2",
          n["status"] == "blocked" and n["remaining"] == 2, n)

    print("3) 重开 D：仍须补听 s4，随后 s5")
    rev = must("reopen D", client.delete("/api/admin/closures/D"))["closure_revision"]
    check("重开后修订号=2", rev == 2, rev)
    n = must("next s4", client.get(f"/api/sessions/{sid}/next"))
    check("从 C 到 D 为 C-D（1 条通道）",
          n["station"]["station_id"] == "s4"
          and n["path"] == ["C", "D"] and n["revision"] == 2, n)
    must("ack s4", client.post(f"/api/sessions/{sid}/acknowledge",
                               json={"revision": 2, "station_id": "s4"}))
    n = must("next s5", client.get(f"/api/sessions/{sid}/next"))
    check("s5 路径 D-E", n["station"]["station_id"] == "s5"
          and n["path"] == ["D", "E"], n)

    print("4) 身处后来封闭的展厅：可沿开放通道离开，但不得穿过其他封闭展厅")
    must("close D while in it", client.post("/api/admin/closures/D"))  # rev=3
    n = must("next leave closed D", client.get(f"/api/sessions/{sid}/next"))
    check("D 已封，仍可离开 D 到开放的 E 听 s5",
          n["station"]["station_id"] == "s5" and n["path"] == ["D", "E"], n)
    must("ack s5", client.post(f"/api/sessions/{sid}/acknowledge",
                               json={"revision": 3, "station_id": "s5"}))
    n = must("session state", client.get(f"/api/sessions/{sid}"))
    check("全部听完 -> finished，heard 完整",
          n["status"] == "finished" and n["heard"] == ["s1", "s2", "s3", "s4", "s5"], n)

    print("5) 乐观并发：修订号过期 / 非当前下一站 -> 拒绝且状态不变")
    s2 = must("start2", client.post("/api/sessions"))["session_id"]
    must("hear s1", client.post(f"/api/sessions/{s2}/acknowledge",
                                json={"revision": 3, "station_id": "s1"}))
    # 用旧修订号 2 确认 s2
    r = client.post(f"/api/sessions/{s2}/acknowledge",
                    json={"revision": 2, "station_id": "s2"})
    check("旧修订号被拒 409 revision_mismatch",
          r.status_code == 409 and r.json()["detail"]["error"] == "revision_mismatch",
          r.text)
    # 正确修订号但错误站点（s3 不是当前下一站 s2）
    r = client.post(f"/api/sessions/{s2}/acknowledge",
                    json={"revision": 3, "station_id": "s3"})
    check("非当前下一站被拒 409 not_current_next",
          r.status_code == 409 and r.json()["detail"]["error"] == "not_current_next",
          r.text)
    st = must("unchanged state", client.get(f"/api/sessions/{s2}"))
    check("拒绝后位置仍在 A、已听仅 s1、下一站仍 s2",
          st["current_hall"] == "A" and st["heard"] == ["s1"]
          and st["station"]["station_id"] == "s2", st)

    print("6) 并发重复确认：两个线程同时确认 s2，只有一个成功")
    results = []

    def ack():
        results.append(client.post(
            f"/api/sessions/{s2}/acknowledge",
            json={"revision": 3, "station_id": "s2"}).status_code)

    t1, t2 = threading.Thread(target=ack), threading.Thread(target=ack)
    t1.start(); t2.start(); t1.join(); t2.join()
    check("一个 200、一个 409", sorted(results) == [200, 409], results)
    st = must("after race", client.get(f"/api/sessions/{s2}"))
    check("只听了一次 s2，位置已到 B",
          st["heard"] == ["s1", "s2"] and st["current_hall"] == "B", st)

    print("7) 所在展厅被封：本站不可听，可离开；重开后回来补听")
    s3 = must("start3", client.post("/api/sessions"))["session_id"]
    must("reopen D first", client.delete("/api/admin/closures/D"))  # rev=4
    must("close A", client.post("/api/admin/closures/A"))            # rev=5
    n = must("next while A closed", client.get(f"/api/sessions/{s3}/next"))
    check("起点 A 被封：s1 跳过，最早可达未听为 s2，路径 A-B（允许离开 A）",
          n["station"]["station_id"] == "s2" and n["path"] == ["A", "B"], n)
    must("ack s2 from closed A", client.post(
        f"/api/sessions/{s3}/acknowledge",
        json={"revision": 5, "station_id": "s2"}))
    must("reopen A", client.delete("/api/admin/closures/A"))         # rev=6
    n = must("next s1 makeup", client.get(f"/api/sessions/{s3}/next"))
    check("重开后必须补听 s1（顺序最早），从 B 回 A",
          n["station"]["station_id"] == "s1" and n["path"] == ["B", "A"], n)

    print("8) 幂等封/重不刷修订号；重复封厅仍是同一修订号")
    rev_before = must("closures list", client.get("/api/admin/closures"))["closure_revision"]
    must("reopen A again", client.delete("/api/admin/closures/A"))
    rev_after = must("closures list2", client.get("/api/admin/closures"))["closure_revision"]
    check("重开已开放展厅不增加修订号", rev_before == rev_after == 6, (rev_before, rev_after))

print()
print("9) 持久化：重新初始化应用（模拟容器重启，同一数据文件）进度与封厅仍在")
# 不删库，重新建应用实例并直接读状态
from app.db import init_db  # noqa: E402
init_db()  # 幂等：不应清数据
with TestClient(app) as client2:
    st = client2.get(f"/api/sessions/{s3}")
    assert st.status_code == 200, st.text
    d = st.json()
    check("重启后会话仍在：位置 B、已听 s2、待补 s1",
          d["current_hall"] == "B" and d["heard"] == ["s2"]
          and d["station"]["station_id"] == "s1", d)
    cl = client2.get("/api/admin/closures").json()
    check("重启后封厅修订号仍为 6，无封闭展厅",
          cl["closure_revision"] == 6 and cl["closed_halls"] == [], cl)

print(f"\n结果：{PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)

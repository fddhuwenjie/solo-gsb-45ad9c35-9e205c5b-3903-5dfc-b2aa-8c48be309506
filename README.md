# 展馆分站讲解续行 HTTP API

Python 3.12 + FastAPI + SQLite（WAL）实现的展馆分站导览服务：
登记展厅之间的**双向通道**、**入口**与按导览顺序排列的**讲解站**；
为每个参观会话记录当前位置和已听站点，在封厅 / 重开动态变化下
持续给出“下一站 + 最短路径”，并通过**封厅修订号**做乐观并发校验。

## 业务规则

- **讲解站**：全局唯一 ID，隶属于某个展厅，带 `seq` 导览顺序和讲解文本。
- **下一站**：在**同一数据快照**（单个数据库事务）内计算并返回
  - 当前**封厅修订号**（任何一次实际封厅 / 重开都会让它 +1，单调递增）；
  - 按导览顺序（`seq`）**最早**的、当前可达且未听的站点及讲解文本；
  - 前往该站点的路径：**通道数最少**；若有多条等长路径，取整条展厅序列
    （含起点、终点）**字典序最小**的一条。
- **封厅的影响**：封厅会让其内部站点暂时不可达，导览继续向后面的站点推进；
  展厅重开后，之前跳过的站点**仍须补听**（因其 `seq` 更早，会重新成为下一站）。
- **离开封闭展厅**：身处后来才被封闭的展厅时，仍可沿开放通道离开，
  但移动路径不得穿过任何其他封闭展厅；封闭展厅内不能收听。
- **确认收听**：客户端必须提交“所见封厅修订号 + 站点 ID”。服务端在
  **同一个写事务**（`BEGIN IMMEDIATE`）中核对：
  1. 修订号与当前一致（封厅没变）；
  2. 目标站点确为当前下一站且路径可达。

  任一条件不满足则返回 `409` 拒绝，**位置与已听记录不变**。
  校验通过后，在同一事务中写入“已听”并把当前位置移动到该站所在展厅。
- **结束条件**：仍有未听站点但全部不可达时返回 `blocked`（受阻）；
  所有站点听完才返回 `finished`。
- **持久化**：SQLite 文件存放在 Docker 卷 `/data`（WAL 模式），
  容器 / 服务重启后，会话进度与封厅状态、修订号都还在。

## 一条命令启动

需要本机装有 Docker 与 Docker Compose 插件：

```bash
docker compose up --build -d
```

服务监听**本地端口 `8000`**：

- API 根路径：http://localhost:8000/
- Swagger 交互文档：http://localhost:8000/docs

停止 / 清理：

```bash
docker compose down          # 停止（数据卷保留，下次启动进度还在）
docker compose down -v       # 同时删除数据卷（彻底重置）
```

首次启动会自动建表并写入一套演示展馆：

```
入口 A，通道：A-B、A-C、B-D、C-D、D-E
站点（导览顺序）：s1@A  s2@B  s3@C  s4@D  s5@E
```

> 不用 Docker 本地运行：
> ```bash
> python3 -m venv .venv && . .venv/bin/activate
> pip install -r requirements.txt
> GUIDE_DB_PATH=./guide.db uvicorn app.main:app --host 0.0.0.0 --port 8000
> ```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/admin/halls` | 登记展厅（可多个） |
| POST | `/api/admin/corridors` | 登记双向通道（`[["A","B"], ...]`） |
| PUT | `/api/admin/entrance` | 设置入口展厅 |
| POST | `/api/admin/stations` | 登记 / 更新有序讲解站（按 ID upsert） |
| GET | `/api/admin/venue` | 查询完整展馆配置 |
| POST | `/api/admin/closures/{hall_id}` | 封厅（修订号 +1） |
| DELETE | `/api/admin/closures/{hall_id}` | 重开（修订号 +1） |
| GET | `/api/admin/closures` | 查询封厅列表与修订号 |
| POST | `/api/sessions` | 开始参观（从入口进入） |
| GET | `/api/sessions/{id}` | 查询会话完整状态（含下一站） |
| GET | `/api/sessions/{id}/next` | 只查下一站 / 路径 |
| POST | `/api/sessions/{id}/acknowledge` | 确认收听（带修订号） |

## 可执行调用示例

```bash
# 0. 查看演示展馆
curl -s http://localhost:8000/api/admin/venue | python3 -m json.tool

# 1. 开始一个参观会话（位置在入口 A）
SID=$(curl -s -X POST http://localhost:8000/api/sessions \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['session_id'])")
echo $SID

# 2. 查看下一站 -> s1@A，路径 ["A"]，封厅修订号 0
curl -s http://localhost:8000/api/sessions/$SID/next | python3 -m json.tool

# 3. 确认收听 s1（提交所见修订号 0）；响应体即为会话最新状态，下一站 s2@B
curl -s -X POST http://localhost:8000/api/sessions/$SID/acknowledge \
  -H 'Content-Type: application/json' \
  -d '{"revision":0,"station_id":"s1"}' | python3 -m json.tool

# 4. 封闭展厅 D（修订号变为 1）：s4/s5 暂时不可达
curl -s -X POST http://localhost:8000/api/admin/closures/D | python3 -m json.tool

# 5. 听完 s2 后，前往 s3 会绕开 D：路径 B-A-C，2 条通道
curl -s -X POST http://localhost:8000/api/sessions/$SID/acknowledge \
  -H 'Content-Type: application/json' \
  -d '{"revision":1,"station_id":"s2"}' > /dev/null
curl -s http://localhost:8000/api/sessions/$SID/next | python3 -m json.tool
#   {"station":{"station_id":"s3",...}, "path":["B","A","C"], "corridors":2}

# 6. s3 听完后只剩被封在 D 后面的 s4/s5 -> status=blocked
curl -s -X POST http://localhost:8000/api/sessions/$SID/acknowledge \
  -H 'Content-Type: application/json' \
  -d '{"revision":1,"station_id":"s3"}' > /dev/null
curl -s http://localhost:8000/api/sessions/$SID/next
#   {"status":"blocked","revision":1,"remaining":2,...}

# 7. 用过期修订号确认会被拒绝（409，状态不变）
curl -i -X POST http://localhost:8000/api/sessions/$SID/acknowledge \
  -H 'Content-Type: application/json' \
  -d '{"revision":0,"station_id":"s4"}'

# 8. 重开 D（修订号变为 2），必须补听 s4，然后 s5，最终 finished
curl -s -X DELETE http://localhost:8000/api/admin/closures/D | python3 -m json.tool
curl -s -X POST http://localhost:8000/api/sessions/$SID/acknowledge \
  -H 'Content-Type: application/json' \
  -d '{"revision":2,"station_id":"s4"}' > /dev/null
curl -s -X POST http://localhost:8000/api/sessions/$SID/acknowledge \
  -H 'Content-Type: application/json' \
  -d '{"revision":2,"station_id":"s5"}' | python3 -m json.tool
#   {"status":"finished", ...}
```

登记自定义展馆（在空库上操作；演示数据仅首次启动写入）：

```bash
curl -s -X POST http://localhost:8000/api/admin/halls \
  -H 'Content-Type: application/json' -d '{"hall_ids":["H1","H2","H3"]}'
curl -s -X POST http://localhost:8000/api/admin/corridors \
  -H 'Content-Type: application/json' \
  -d '{"corridors":[["H1","H2"],["H2","H3"],["H3","H1"]]}'
curl -s -X PUT http://localhost:8000/api/admin/entrance \
  -H 'Content-Type: application/json' -d '{"hall_id":"H1"}'
curl -s -X POST http://localhost:8000/api/admin/stations \
  -H 'Content-Type: application/json' -d '{"stations":[
    {"station_id":"t1","hall_id":"H1","seq":1,"text":"第一站讲解"},
    {"station_id":"t2","hall_id":"H2","seq":2,"text":"第二站讲解"}]}'
```

## 响应中的状态字段

`GET /api/sessions/{id}` 与 `GET .../next` 返回：

| 字段 | 含义 |
| --- | --- |
| `status` | `next`：有可达下一站；`blocked`：只剩不可达站点；`finished`：全部听完 |
| `revision` | 计算该结果时所见的封厅修订号（确认收听时原样回传） |
| `station` | 下一站（`station_id` / `hall_id` / `seq` / `text`） |
| `path` | 展厅序列，如 `["B","A","C"]`；`path[0]` 为当前位置 |
| `corridors` | 路径经过的通道数 = `len(path)-1` |
| `remaining` | 尚未收听的站点数（含暂时不可达的） |
| `current_hall` / `heard` | 当前位置与已听站点（仅会话详情接口） |

## 测试

仓库自带基于 FastAPI TestClient 的端到端验证（21 项断言，含并发确认、
封厅绕路、离开封闭展厅、重开补听、修订号冲突、重启持久化）：

```bash
pip install -r requirements.txt httpx
GUIDE_DB_PATH=/tmp/guide_test.db python3 test_api.py
```

## 设计要点

- **单事务快照**：下一站查询在一个读事务中取出封厅表、通道、站点、
  已听记录与修订号，保证“修订号 / 站点 / 路径”来自同一快照。
- **最短路 + 字典序**：`app/navigation.py` 使用逐层 BFS，每一层把
  同距离候选按完整展厅元组比较取最小，天然满足“通道最少、序列字典序最小”。
- **封闭展厅处理**：BFS 以当前位置为起点（即使该厅已封也允许离开），
  但不会把封闭展厅放入中间或终点集合。
- **写事务原子性**：确认收听使用 `BEGIN IMMEDIATE` 立即取写锁，
  “核对修订号 → 重算下一站 → 写已听 → 更新位置”全部在同一事务内提交，
  并发重复确认只会有一个成功。
- **WAL + 命名卷**：`PRAGMA journal_mode=WAL` 提升读写并发，
  数据文件位于 `/data/guide.db`，由 Compose 命名卷 `guide-data` 持久化。

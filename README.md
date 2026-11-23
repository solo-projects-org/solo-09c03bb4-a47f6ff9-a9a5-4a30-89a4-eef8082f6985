# 排水流向图：供数、隔离诊断与整批补测

FastAPI + SQLite 服务。导入一张矩形流向图，回答"哪些格子能顺流到哪个排口"，
把到不了排口的格子隔离并说明原因（缺测 / 出界 / 环路），再按行列成批补测，
只重判改动格和原先流经这些格的上游。

## 快速开始

```bash
docker compose up --build
# Swagger UI: http://localhost:8000/docs
```

跑一遍演示（导入带环路的示例图 -> 看供数与诊断 -> 补测一格解开环路 -> 再看结果）：

```bash
pip install -r requirements.txt -r requirements-dev.txt
python scripts/demo.py            # 默认 http://localhost:8000
```

本地不装 Docker 时也可以直接起服务：

```bash
DRAINAGE_DB=/tmp/drainage.sqlite3 uvicorn app.main:app --port 8000
pytest                            # 33 个测试
```

SQLite 文件放在 `/data/drainage.sqlite3`（Docker 里由 named volume `drainage-data`
持久化，可用 `DRAINAGE_DB` 覆盖）。

## 数据模型

每格只取一种状态：

| 记号 | 含义 |
| --- | --- |
| `O` | 排口（固定，补测不能新增或改写） |
| `?` | 缺测 |
| `N` `NE` `E` `SE` `S` `SW` `W` `NW` | 八向之一 |

坐标约定：`row` 向下增大、`col` 向右增大，所以 `N` 是 `row-1`、`W` 是 `col-1`。
八个方向都是**指向下游的相邻格**。

## 供数语义

对每个非排口格沿方向一路走，终点只有四种：

1. **到排口** -> 该格供数，记下排口；
2. **走出矩形** -> 隔离，记"出界前的最后一格"；
3. **走到缺测格** -> 隔离，记缺测格和指向它的那一格（`stop`）；缺测格本身也算隔离，但它不流向任何人，没有 `stop`；
4. **走回自己路径上的格** -> 隔离，记整个环路（规范化成从最小格开始，保证同一环路只报一次）。

`GET /grids/{id}/supply` 只返回能到排口的格子，按排口分组；排口本身不参与计数。
`GET /grids/{id}/diagnosis` 返回隔离分组、缺测格清单，以及每个缺测格是否真的被流向。

## 补测与增量重判

`POST /grids/{id}/resurvey` 接收一批 `{row, col, value}`，含义是**按整批补测后的流向重判**：

1. 先整批校验（值合法、在界内、不是排口），任何一条不合法就整批拒绝，一个字节都不写；
2. 只把**真正变化**的格子当作改动格；
3. 用 SQLite 递归 CTE 沿 `cells(grid_id, downstream_row, downstream_col)`
   反向索引向上游走，得到"原先流经这些格"的集合，再加上改动格本身：
   ```
   WITH RECURSIVE up(row,col) AS (
       SELECT ... FROM changed
       UNION
       SELECT c.row, c.col FROM cells c
       JOIN up ON c.downstream_row = up.row AND c.downstream_col = up.col
       WHERE c.grid_id = ?
   )
   ```
   这一步只走上游锥，不扫整张图；
4. 只重判这个集合。集合外的格子用缓存结果直接继承——如果某个下游格的路径经过改动格，
   它本来就在上游锥里，所以缓存不会过期；
5. 方向、供数结果、隔离原因在同一个事务里写入，失败整体回滚。

返回 `supply_transition`（每个排口补测前后的供数格数）、`changes`（每格 before/after）
和 `affected_cells`（这次实际重判了哪些格）。

## Swagger 演示：解开环路

`POST /grids/demo` 导入内置 5×6 示例：外圈是排口，内部有

```
(1,1) S -> (2,1) E -> (2,2) N -> (1,2) W -> (1,1)
```

这个 2×2 环加上汇入它的 (1,3)(2,3)(3,3)，一共 7 格被环路隔离；
(3,2) 向西流入缺测格 (3,1)，再加 1 格隔离。

在 Swagger 里 `POST /grids/1/resurvey`，body：

```json
{"updates": [{"row": 1, "col": 2, "value": "N"}]}
```

把 (1,2) 从 `W` 改成 `N`，环路断开。实测结果：

| | 补测前 | 补测后 |
| --- | --- | --- |
| 供数格 | 3 | 10 |
| 隔离格 | 8 | 1 |
| 环路 | 1 个（4 格） | 0 |
| 重判格数 | — | 7（改动格 + 6 个上游） |
| 排口 (0,2) 供数 | 0 | 7 |
| 排口 (2,5) 供数 | 3 | 3 |

缺测格 (3,1) 补测前后都照样挡住 (3,2)，所以补测后隔离的 1 格是它。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查（Compose healthcheck 用） |
| GET | `/grids` | 已导入的图 |
| POST | `/grids` | 导入 `{tokens, source_name?}`，201 |
| POST | `/grids/demo` | 导入内置示例图 |
| GET | `/grids/{id}` | 按 token 读回整张图 |
| GET | `/grids/{id}/supply` | 供数结果，按排口分组 |
| GET | `/grids/{id}/diagnosis` | 隔离诊断 |
| POST | `/grids/{id}/resurvey` | 整批补测 + 增量重判 |

错误码：`404` 图不存在，`409` 试图补测排口，`422` 非法 token / 非矩形 / 越界 /
空批次。

## 代码结构

```
app/grid.py     坐标、方向、token 解析、环路规范化
app/flow.py     单图流向求值（供数 / 三类隔离 / 环路），支持从缓存继承
app/store.py    SQLite schema、事务、上游递归 CTE
app/service.py  导入 / 供数 / 诊断 / 整批补测
app/main.py     FastAPI 路由、错误映射、OpenAPI 示例
tests/          flow、service、API 三组测试
scripts/demo.py 端到端演示脚本
```

## 已验证

- `pytest`：33 个测试全过，覆盖八个方向、三类隔离、环路去重、补测前后供数与诊断、
  排口不可改、整批校验原子性、模拟写库失败后旧结果仍可查、读快照隔离、写后重开仍在、
  404/409/422。
- 随机对拍：5 个种子 × 150 组随机图，导入结果与补测后的缓存结果都等于"从头全量重判"，
  0 处不一致。
- 真实 HTTP：起 uvicorn 跑通 `scripts/demo.py`，上面的前后对照表就是实测输出。
- `EXPLAIN QUERY PLAN` 确认上游递归走的是 `idx_cells_downstream` 索引查找，不是全表扫描。

## 待办

- **Docker 镜像没有在本环境实际构建过**：这里没有 Docker/Compose，只校验了
  `docker-compose.yml` 能被 YAML 解析、`Dockerfile` 结构正确、依赖在 Python 3.12
  Linux 上有可用 wheel。请在装好 Docker 的机器上跑一次 `docker compose up --build` 确认。
- 只展示一张图时没有分页；超大图（数十万格）的导入目前是一条 `executemany`，
  可以改成分批提交。
- 补测历史落在 `resurveys` / `resurvey_changes` 表里，暂未开放查询接口。
- 并发写靠 SQLite `BEGIN IMMEDIATE` 串行化，单实例够用；多实例需要换 PostgreSQL。

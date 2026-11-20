# 考古地层对接与树轮定年 API（FastAPI + SQLite）

管理同一考古遗址最多 40 个层位的探方编号、层位编号与可能沉积年代段（整年闭区间，
可含空档），登记跨探方层位对接与“下层 → 上层”叠压关系，并可按遗址提交一份
浮动树轮年表核定公历始年。自动完成：

- **分组**：直接或间接对接的层位视为同一次沉积，合并为连通组，全组共用一个年份；
- **成环检测**：合组后的叠压关系（含同组自环）不得成环；
- **年代约束**：下层沉积年严格早于上层；同组年份必须落在各成员年代段的交集内；
- **可行年代段求解**：对每个组给出可与其他全部层位约束同时成立的所有年份
  （保留空档的闭区间列表）；
- **撤销重算**：删除对接/叠压后按剩余关系重新分组、重新求解；
- **浮动树轮年表**：相对年序已排定、公历始年待核定时，提交 1～20 个候选始年
  与至少两个层位的木样末轮偏移（始年 + 偏移 = 沉积年），全遗址共用同一始年，
  筛出可行始年并给出各始年下可行年代段的并集。

## 快速开始（docker compose）

```bash
docker compose up --build
# API:      http://localhost:8000
# Swagger:  http://localhost:8000/docs   （可直接在页面上录入数据）
# ReDoc:    http://localhost:8000/redoc
```

SQLite 数据文件保存在命名卷 `archaeo-data`（容器内 `/data/archaeo.db`），
`docker compose down` 不会删除；如需清空：`docker compose down -v`。

## 本地裸跑

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库默认在 ./data/archaeo.db，可用环境变量 DB_PATH 覆盖
```

## 年代表示约定

- 年份均为整数；公元后用正整数，公元前用负整数（如 `-221` 表示公元前 221 年）。
- 每个层位给一个或多个闭区间 `[year_from, year_to]`，多段之间允许空档
  （如 `[-5000,-4000]` 与 `[-3000,-2000]`，中间一千年无沉积可能）。
- 同组年代域 = 组内各层位区间的交集；求解结果 `feasible_years` 同样是带空档的区间列表。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sites` | 建遗址 |
| GET | `/sites` / `/sites/{id}` | 遗址列表/详情 |
| POST | `/sites/{id}/strata` | 录入层位（探方、层位号、年代段；每遗址上限 40） |
| GET | `/sites/{id}/strata` / `/strata/{id}` | 层位查询 |
| POST | `/sites/{id}/correlations` | 登记两探方层位对接 |
| GET | `/sites/{id}/correlations` | 对接列表 |
| DELETE | `/correlations/{id}` | 撤销对接，按剩余对接重算 |
| POST | `/sites/{id}/superpositions` | 登记叠压（下层 → 上层） |
| GET | `/sites/{id}/superpositions` | 叠压列表 |
| DELETE | `/superpositions/{id}` | 撤销叠压并重算 |
| PUT/POST | `/sites/{id}/chronology` | 提交/整份替换浮动树轮年表（每处遗址一份） |
| GET | `/sites/{id}/chronology` | 查看当前浮动年表（未提交返回 404） |
| GET | `/sites/{id}/solution` | 整站分组、组间边、可行始年、各组可行年代段并集 |
| GET | `/strata/{id}/correlation-view` | 按原始层位查同组成员与可行年代段并集 |

## 浮动树轮年表

树轮的相对年序已经排定、公历始年还待核定时，按遗址提交一份浮动年表
（每处只保留一份，再提交就整份替换）：

```bash
curl -X PUT localhost:8000/sites/$SID/chronology -H 'Content-Type: application/json' -d '{
  "candidate_start_years": [-1600, -1550, -1500],
  "samples": [
    {"stratum_id": 1, "offset_years": 217},
    {"stratum_id": 2, "offset_years": 233}
  ]
}'
```

- `candidate_start_years`：1～20 个互不相同的候选公历始年；
- `samples`：至少两个现有层位各自的木样末轮距年表起点的整年偏移
  （只纳入砍伐年可视为该层沉积年的木样），每个层位至多一枚；
- **始年 + 偏移 = 沉积年**。全遗址（含互不连通的层位）须用同一个始年，
  同时满足原年代段、对接与叠压约束；满足条件的候选即**可行始年**；
- `GET /sites/{id}/solution` 列出 `feasible_start_years`，整站与按层位查询
  都给出这些始年下可行年代段的**并集**（保留空档）；`earliest_assignment` /
  `latest_assignment` 分别是最小、最大可行始年下各自的一套完整解；
- 全部候选保存在库中，每次查询现场重算：撤销对接/叠压后重新符合条件的
  始年会自动回到结果中；
- 登记/换表、新增对接或叠压若使**所有**候选失效，当次返回 `409`，
  年表、关系、版本与年代结论照旧；只使部分候选失效则可正常写入；
- 未提交年表的遗址沿用原行为（无钉年约束）。

### 录入示例

```bash
SID=1
# 层位：探方 T1 第 3 层，两个可能年代段（中间有空档）
curl -X POST localhost:8000/sites/$SID/strata -H 'Content-Type: application/json' -d '{
  "trench": "T1", "layer_no": "L3",
  "intervals": [{"year_from": -5000, "year_to": -4000},
                {"year_from": -3000, "year_to": -2000}]
}'
# 叠压：下层 1 -> 上层 2
curl -X POST localhost:8000/sites/$SID/superpositions \
  -H 'Content-Type: application/json' -d '{"lower_id": 1, "upper_id": 2}'
# 跨探方对接：层位 1 与层位 3 属同一次沉积
curl -X POST localhost:8000/sites/$SID/correlations \
  -H 'Content-Type: application/json' -d '{"stratum_a_id": 1, "stratum_b_id": 3}'
```

## 无解时的行为

写入会在**同一事务**内先重算再提交。若新记录导致无解（`EMPTY_DOMAIN` /
`GROUP_CYCLE` / `ORDER_CONFLICT` / `PIN_CONFLICT` / `PIN_OUT_OF_DOMAIN`；
有年表时指全部候选始年都不可行），事务回滚，接口返回 `409`，响应体包含：

- `contradiction`：一组本身即矛盾的**原始记录见证**（层位年代段、对接、叠压、
  木样，含可读原因与记录 id，便于定位）；
- `candidate_failures`：有年表时，每个失效候选始年的失败原因；
- `previous_solution`：回滚后库内已有年表、对接与年代结论（保持可行、保持可用）。

## 一致性与并发

- 所有写操作经进程内同一把锁串行化，按**提交顺序**核算；每次成功写入使全局
  `version` 加 1，查询结果带 `version`，可据此确认读到的是哪一次已完成提交。
- 分组与年代结论**不物化落库**，每次查询及重启都由原始记录现场求解；
  原始记录由 SQLite 事务保证原子性，因此任何查询看到的分组/年代都来自同一次
  完整提交，不存在半写入的中间态。
- SQLite 启用 WAL 与外键约束，连接带 30s busy timeout。

## 求解算法（`app/solver.py`）

1. 并查集按对接关系求连通组；组域为成员年代域交集（支持空档的区间运算）。
2. 叠压边提升为组间边；Kahn 拓扑排序判环，DFS 三色标记提取具体环与见证。
3. 可选钉年：木样把所属组的沉积年钉死在“候选始年 + 末轮偏移”；同组钉年冲突、
   钉年落在组域外或与叠压次序矛盾均判无解。
4. DAG 上两次贪心：
   - 最早解 E：拓扑序每组取域内 `> max(E[前驱])` 的最小年份（被钉年的组取钉年）；
   - 最晚解 L：逆拓扑序每组取域内 `< min(L[后继])` 的最大年份（被钉年的组取钉年）。
   任意可行解 F 都满足 E ≤ F ≤ L，故组 g 的全部可同时成立年份恰为
   其年代域 ∩ [E[g], L[g]]（仍可为含空档的多段区间）。
5. 有浮动年表时（`app/service.py`）：对每个候选始年分别求解，可行始年下的
   各组可行年代段取并集（保留空档）作为展示视图；最早、最晚赋值分别取最小、
   最大可行始年下各自的一套完整解。

## 测试

```bash
python -m pytest tests/ -q
```

覆盖区间工具、缺口域求解、成环/空域/次序冲突及矛盾见证、40 层上限、
无解拒绝与旧结论保留、撤销重算、并发写入串行化、重启后结论恢复，
以及浮动年表的候选始年筛选、并集视图、整份替换、409 回滚与撤销恢复。

# 工业三维打印批次追溯与放行平台

面向工业 3D 打印（增材制造）车间的**批次追溯与成品放行**平台。每份打印作业在
开始时**冻结**模型摘要、切片参数、设备校准版本、计划材料批次与环境范围；设备按
层上传进度与异常；成品只有在**层记录完整、材料余量守恒、质检绑定同一作业快照**
三项同时满足时才能放行。

仅依赖 Python 3.11 标准库（`http.server` + `sqlite3`），无需安装任何第三方包。

## 快速开始

```bash
cd /workspace/platform
python3 seed.py --reset          # 可选：写入 3 个演示作业
python3 -m app 8080              # 启动，浏览器访问 http://localhost:8080
```

演示数据：

| 作业 | 状态 | 说明 |
|---|---|---|
| JOB-001 | 已放行 | 60 层完整、物料守恒、质检 PASS，已签发成品证明 `CERT-001`（SN-AX7-2610-01） |
| JOB-002 | 在制 | 设备断电 → resume 续打、第 40 层后换用**已申报**新批次、第 47 层温度超界且喷嘴压力告警未闭环；缺 61–80 层 |
| JOB-003 | 已报废 | 20 层完整且物料平衡，但质检 FAIL，作业报废（不可再放行） |

测试：

```bash
python3 -m unittest tests.test_platform -v   # 18 个用例
```

## 核心不变量（业务红线）

1. **快照冻结**：作业创建时对规范化清单计算 SHA-256 `snapshot_hash`。每层层回执
   必须原样回传该哈希；哈希不一致（不同模型版本）直接 `409 snapshot_mismatch`，
   数据无法并入同一作业。模型修订不是原地改单，而是创建**新作业、新快照**
   （`POST /api/jobs/{id}/revise`，并要求版本号变更）。
2. **材料先申报后使用**：层回执引用未申报批次 → `422 undeclared_batch`；
   未申报批次上机上料同样被拒。中途换料只能换用**已申报**批次（可在作业进行中
   追加申报）。
3. **材料余量守恒**：只追加账本 `charge / return / consume`，结算时逐批次校验
   `上料 = 退料 + Σ层耗`（容差 ±5g），且每个上机批次必须有退料复称；层耗总量与
   切片预期用量在放行闸门交叉校验（±2%，同时可识别缺层）。
4. **层记录完整**：必须存在恰好 1..N 层；重复层回执幂等返回且不覆盖，同层不同
   回执 `409 duplicate_layer`；同一 `receipt_id` 重放但载荷不同 →
   `409 receipt_id_conflict`。
5. **环境与异常**：任一层温度/湿度超出冻结范围自动开异常单（可携带设备异常代码）；
   未闭环异常阻断放行，闭环需记录处置措施。
6. **断电恢复**：断电用 `power_loss` 结束会话；续打必须新建 `resume` 会话，已结束
   会话上的层回执一律拒绝，会话与设备编号强绑定。
7. **质检同快照**：质检必须绑定作业冻结哈希（默认自动带入），最新结论须为 `pass`。
8. **终态唯一**：放行 / 报废互为终态。每个状态迁移都包在 `BEGIN IMMEDIATE`
   事务中，配合 `products.job_id UNIQUE` 与 `product_events (serial,event_type)
   UNIQUE`，并发报废与放行**只有一个**会成功（见 `test_15` 并发用例）。
9. **成品证明防篡改**：放行时签发 JSON 证明（HMAC-SHA256 服务端密钥签名），内含
   快照、全部层回执 ID、材料守恒汇总、申报批次、质检结论；密钥按数据库一次性生成。
   可通过 `POST /api/certificates/verify` 验证，任何字段被改即验签失败。

## 作业 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/jobs` | 创建作业，冻结快照（请求体见下） |
| GET | `/api/jobs` | 作业列表 |
| GET | `/api/jobs/{id}` | 作业全量详情（快照/账本/会话/层/异常/质检/证明/事件流） |
| POST | `/api/jobs/{id}/revise` | 模型修订 → 生成新作业新快照 |
| POST | `/api/jobs/{id}/materials` | 追加申报材料批次 |
| POST | `/api/jobs/{id}/ledger/charge` | 上料 / 换料（`spool_change:true`） |
| POST | `/api/jobs/{id}/ledger/return` | 退料复称 |
| POST | `/api/jobs/{id}/material/close` | 物料结算（守恒校验） |
| POST | `/api/jobs/{id}/sessions` | 设备开机 / `resume` 恢复 |
| POST | `/api/jobs/{id}/sessions/{sid}/end` | 结束会话（completed/power_loss/aborted） |
| POST | `/api/jobs/{id}/layers` | 设备上传层回执（幂等） |
| POST | `/api/jobs/{id}/anomalies/{aid}/resolve` | 异常闭环 |
| POST | `/api/jobs/{id}/inspections` | 录入质检（pass/fail/hold） |
| GET | `/api/jobs/{id}/gate` | 放行闸门 8 项检查明细 |
| POST | `/api/jobs/{id}/release` | 放行成品并签发证明 |
| POST | `/api/jobs/{id}/scrap` | 报废成品（终态唯一） |
| GET | `/api/certificates/{cert_id}` / `/api/products/{serial}/certificate` | 取证明（含验签结果） |
| POST | `/api/certificates/verify` | 离线验证证明签名 |

创建作业请求体：

```json
{
  "part_name": "航空支架 AX-7", "customer": "某主机厂",
  "model":    {"name": "航空支架 AX-7", "file": "ax7.stl",
               "version": "v3.2", "checksum": "sha256:..."},
  "slicing":  {"profile": "PA12-Q2", "layer_height_mm": 0.1,
               "infill_pct": 20, "speed_mm_s": 120,
               "total_layers": 60, "expected_material_g": 360},
  "device":   {"device_id": "D-MJF-07",
               "calibration_version": "CAL-2026-09-22",
               "calibration_checksum": "sha256:cal-..."},
  "environment": {"temp_c_min": 20, "temp_c_max": 30,
                  "humidity_pct_min": 30, "humidity_pct_max": 60},
  "materials": [{"batch_code": "B-PA12-2609-A", "material_type": "PA12",
                 "grade": "航空级", "spool_id": "SP-1001",
                 "supplier": "华曙材料", "planned_weight_g": 400}]
}
```

## 页面

- `/`（哈希路由）作业列表 → 新建作业 → 作业详情
- 作业详情：放行闸门清单、冻结快照、逐层网格（正常/异常/缺失）、材料守恒汇总与
  只追加流水、设备会话时间线、异常台账、质检记录、终态操作、审计事件流
- `#/jobs/{id}/certificate`：成品证明（可打印 / 下载 JSON / 验签）

## 代码结构

```
app/
  db.py       SQLite schema 与连接（WAL + busy_timeout + 外键）
  core.py     规范化 JSON、快照哈希、HMAC 签名、ID 生成、ApiError
  guards.py   全部状态迁移（每个迁移一个 BEGIN IMMEDIATE 事务）与放行闸门
  server.py   stdlib HTTP：JSON API 路由 + SPA 静态托管
tests/        18 个端到端不变量用例（含并发终态竞争）
seed.py       演示数据
web/          index.html / styles.css / app.js（原生 JS SPA）
```

数据库位置可用环境变量 `PRINT_TRACE_DB` 覆盖。

# 实验室样本混样追踪 API

基于 **Python + FastAPI + SQLite** 的实验室样本混样（pooling）追踪服务。
客户端提交初始样本（体积 + 标记物浓度）与按时间排序的转移操作，服务逐步
计算每次转移后：

* 每支管的**当前体积**与各标记物的**混合浓度**；
* 管内液体的**来源构成与占比**（来自哪些初始样本、各占多少体积 / 百分比）；
* 可选的**移液损耗追踪**：`amount` 为吸取体积，`delivered_amount` 为实际
  注入体积，差额（挂壁 / 枪头残液）按源管当时浓度与来源比例计入废液 `waste`；
* 本次转移的**守恒校验**：管内口径的逐管体积变化，以及“所有管内 + 累计
  废液”系统口径的总量差。

转移量超出当前可用体积、时间逆序、引用未声明的样本时，返回 **HTTP 400** 和
定位明确的结构化错误（如 `transfers[2].source`），失败记录同样持久化。

---

## 一条命令启动

需要本机已安装 Docker 与 Docker Compose（无需任何第三方账号、无需外部数据库）：

```bash
docker compose up --build
```

启动后访问：

| 入口 | 地址 |
| --- | --- |
| API 服务 | http://localhost:8000 |
| Swagger 交互文档 | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |
| 健康检查 | http://localhost:8000/health |

SQLite 数据文件保存在 Docker 命名卷 `pooling-data` 中（容器内 `/data/pooling.db`），
容器重建后记录不丢失。

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时清空数据库卷
```

---

## 本地开发（不使用 Docker）

要求 Python 3.11+：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

uvicorn app.main:app --reload --port 8000
```

数据库默认写入 `./data/pooling.db`，可用环境变量覆盖：

```bash
export POOLING_DB_PATH=/自定义/路径/pooling.db
```

运行测试：

```bash
pytest -q
```

---

## 物理模型与计算规则

1. **充分混匀假设**：每次转移前，源管内液体视为完全混匀，转出液体携带源管
   当前的混合浓度与来源构成。
2. **吸取 / 到账分离（移液损耗）**：每次转移的 `amount` 是从源管**吸取**的
   体积，源管按 `amount` 扣减；可选的 `delivered_amount`
   （`0 < delivered_amount <= amount`）是实际注入目标管的体积，目标管只
   增加 `delivered_amount`。差额 `lost = amount - delivered_amount` 视为
   移液损耗，按源管当时的浓度与来源比例计入该步 `lost`，并累计到结果顶层
   的 `waste`。不传 `delivered_amount` 时默认 `delivered_amount = amount`，
   即无损（旧请求完全兼容）。
3. **超量校验按吸取量**：`amount` 不得超过源管当前可用体积（即使
   `delivered_amount` 很小也不行），超限返回 `INSUFFICIENT_VOLUME`。
4. **系统口径守恒**：`Σ 各管体积 + 累计废液体积` 全程不变；对每种标记物，
   `Σ 管内质量 + 废液中质量` 全程不变（`conservation.system_*_difference`
   恒为 0）。
5. **管内口径变化**：有损耗时仅看管内会少一个损耗体积 / 质量，该变化在
   `conservation.tube_*` 与每步 / 全程的 `tube_volume_changes`
   （逐管 `volume_before / volume_after / volume_change`）中给出。
6. **来源追踪**：每支管与废液都记录“来自各初始样本的净体积”。吸取液按源管
   当前来源占比分摊，再按 `delivered / lost` 的体积比例拆给目标管与废液；
   占比之和恒为 1（空管 / 无废液时列表为空）。
7. **浮点处理**：小于 `1e-12` 的残余量按 0 清理，结果保留 12 位小数。
8. **时间规则**：转移必须按时间**非递减**（允许同一时刻多次操作）。

标记物支持两种写法，可混用：

* `concentration`: 单一标记物，内部命名为 `default`；
* `markers`: `{标记物名: 浓度}`，一次追踪多种标记物（如 GFP / RFP）。

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/simulations` | 提交一次模拟（成功 201，业务失败 400，格式错误 422） |
| `GET` | `/api/simulations/{id}` | 查询某次模拟的完整逐步结果（失败记录返回 400） |
| `GET` | `/api/simulations` | 历史记录列表，支持 `limit` / `offset` / `status` |
| `GET` | `/health` | 健康检查 |

### 请求示例

```bash
curl -s -X POST http://localhost:8000/api/simulations \
  -H "Content-Type: application/json" \
  -d '{
    "name": "两管互混实验（含移液损耗）",
    "samples": [
      {"id": "S1", "initial_volume": 100, "concentration": 12.5},
      {"id": "S2", "initial_volume": 50,  "markers": {"RFP": 8}}
    ],
    "transfers": [
      {"time": "2026-09-18T09:00:00Z", "source": "S1", "target": "S2", "amount": 50, "delivered_amount": 48},
      {"time": "2026-09-18T10:30:00Z", "source": "S2", "target": "S1", "amount": 25}
    ]
  }'
```

第一步从 S1 **吸取** 50、实际只**注入** S2 48，损耗的 2（浓度 12.5、全部
来自 S1）计入废液；第二步未传 `delivered_amount`，视为无损。

`time` 既可以是 ISO 8601 字符串（`...Z` / 带时区偏移；无时区按 UTC），
也可以是数值序号（`1`、`2.5`），但同一请求中需保持可比较的递增顺序。

### 响应结构（节选）

```jsonc
{
  "id": "551a00db...",
  "status": "success",            // 或 "failed"
  "created_at": "2026-09-18T15:24:11Z",
  "initial_states": [ /* 每支管的初始体积/浓度/来源 */ ],
  "steps": [
    {
      "index": 0,
      "time": "2026-09-18T09:00:00Z",
      "source": "S1", "target": "S2",
      "amount": 50.0,              // 从源管吸取的体积
      "delivered_amount": 48.0,    // 实际注入目标管的体积
      "transferred": {             // 吸取液本身的浓度与来源构成
        "volume": 50.0,
        "concentrations": {"RFP": 0.0, "default": 12.5},
        "origins": [{"source_id": "S1", "volume": 50.0, "volume_fraction": 1.0}]
      },
      "lost": {                    // 本步移液损耗（无损耗时 volume=0、origins=[]）
        "volume": 2.0,
        "concentrations": {"RFP": 0.0, "default": 12.5},
        "marker_amount": {"RFP": 0.0, "default": 25.0},
        "origins": [{"source_id": "S1", "volume": 2.0, "volume_fraction": 1.0}]
      },
      "waste_after": { /* 截至本步的累计废液：体积/浓度/质量/来源 */ },
      "source_after": { /* 转移后源管状态：按 amount 50 扣减 */ },
      "target_after": { /* 转移后目标管状态：只增加 delivered 48 */ },
      "conservation": {
        // 管内口径：本步各管体积 / 质量净变化
        "tube_volume_before": 150.0, "tube_volume_after": 148.0,
        "tube_volume_change": -2.0,
        "tube_volume_changes": [    // 管内体积变化的逐管明细
          {"tube_id": "S1", "volume_before": 100.0, "volume_after": 50.0, "volume_change": -50.0},
          {"tube_id": "S2", "volume_before": 50.0,  "volume_after": 98.0, "volume_change": 48.0}
        ],
        // 废液前后
        "waste_volume_before": 0.0, "waste_volume_after": 2.0,
        // 系统口径：管内 + 累计废液，差值恒为 0
        "system_volume_before": 150.0, "system_volume_after": 150.0,
        "system_volume_difference": 0.0,
        "system_marker_amount_difference": {"RFP": 0.0, "default": 0.0}
      }
    }
  ],
  "final_states": [ /* 终态 */ ],
  "waste": {                       // 全程累计废液（无损耗时 volume=0）
    "volume": 2.0,
    "concentrations": {"RFP": 0.0, "default": 12.5},
    "marker_amount": {"RFP": 0.0, "default": 25.0},
    "origins": [{"source_id": "S1", "volume": 2.0, "volume_fraction": 1.0}]
  },
  "conservation": {
    // 管内口径（旧字段，语义不变）
    "initial_total_volume": 150.0, "final_total_volume": 148.0,
    "volume_difference": -2.0,
    "tube_volume_changes": [ /* 每支管的初始 -> 终态体积变化 */ ],
    // 累计废液
    "waste_total_volume": 2.0,
    "waste_marker_amount": {"RFP": 0.0, "default": 25.0},
    // 系统口径：管内 + 累计废液，差值为 0
    "system_initial_volume": 150.0, "system_final_volume": 150.0,
    "system_volume_difference": 0.0,
    "system_marker_amount_difference": {"RFP": 0.0, "default": 0.0}
  },
  "error": null
}
```

> 旧请求不传 `delivered_amount` 时，每步 `delivered_amount` 回填为
> `amount`、`lost.volume = 0`、顶层 `waste.volume = 0`，管内口径与系统口径
> 完全一致，原有响应字段语义保持不变。

### 错误响应（定位明确）

业务错误返回 `400`，响应体其余字段（已完成步骤、当时各管状态）仍可用于排查；
记录同样保存，可用返回的 `id` 通过 `GET` 复查。

```json
{
  "status": "failed",
  "error": {
    "code": "INSUFFICIENT_VOLUME",
    "message": "transfers[0] 试图从 'A' 转出 99.0，但该管当前仅有 5.0",
    "transfer_index": 0,
    "location": "transfers[0].amount"
  },
  "...": "initial_states / steps / final_states / conservation 照常返回"
}
```

错误码：

| code | 触发条件 | location 示例 |
| --- | --- | --- |
| `INSUFFICIENT_VOLUME` | 吸取量 `amount` 超过源管当前体积 | `transfers[i].amount` |
| `TIME_NOT_ORDERED` | 转移时间早于上一次转移 | `transfers[i].time` |
| `UNKNOWN_SAMPLE` | 源 / 目标管未在 `samples` 中声明 | `transfers[i].source` / `.target` |
| `SAME_SOURCE_TARGET` | 源管与目标管相同 | `transfers[i]` |
| `SIMULATION_NOT_FOUND` | 查询不存在的记录 id | `path.sim_id` |

请求体本身的字段问题返回 FastAPI 标准 `422` 结构，`detail[].loc` 指明出错
字段路径，例如：`amount` / `delivered_amount` 非正数、
`delivered_amount > amount`（定位到 `transfers[i]`）、负数体积、缺少浓度、
未知字段、样本 id 重复等。

### 查询与列表

```bash
# 完整结果
curl http://localhost:8000/api/simulations/<id>

# 列表（按创建时间倒序）
curl "http://localhost:8000/api/simulations?limit=20&offset=0"
curl "http://localhost:8000/api/simulations?status=failed"
```

---

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 路由（创建 / 查询 / 列表 / 健康检查）
│   ├── models.py      # Pydantic 请求与响应模型、时间解析
│   ├── engine.py      # 混样模拟核心（浓度 / 来源 / 守恒）
│   └── database.py    # SQLite 连接与记录持久化
├── tests/
│   ├── conftest.py
│   ├── test_engine.py # 引擎计算、移液损耗与错误定位
│   └── test_api.py    # API 端到端（32 个用例）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
└── README.md
```

## 设计说明

* **无外部依赖**：仅用 Python 标准库自带的 SQLite，开箱即用；启用 WAL 与
  busy timeout 以兼容多 worker 部署。
* **失败也持久化**：成功和失败的模拟都落库，失败记录保留已执行步骤与
  现场状态，方便审计与复盘。
* **浓度统一在质量域计算**：避免反复稀释 / 混合引入的累积误差，所有
  `conservation.system_*_difference` 均可直接用于自动校验。
* **移液损耗全程可追踪**：损耗液按源管当时的浓度和来源比例记入每步 `lost`
  并累计到顶层 `waste`（体积 / 浓度 / 质量 / 来源），持久化结果中同样保留；
  守恒同时给出管内口径的逐管变化与系统口径的总量差。
* **安全默认值**：容器以非 root 用户运行，附带 healthcheck；请求模型
  禁止额外字段（`extra=forbid`），防止拼写错误被静默忽略。

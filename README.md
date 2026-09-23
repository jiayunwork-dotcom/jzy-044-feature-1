# 填料吸收塔传质高度核算服务（Absorption Height Service）

面向上游流程模拟器调用的常驻 HTTP 后端：输入一股**稀溶质气液逆流吸收**的
操作条件，返回：

- **HOG** — 气相总传质单元高度：`HOG = G / (Kya·a·S)`
- **NOG** — 气相总传质单元数：`NOG = ∫_{y2}^{y1} dy/(y − y*)`
- **Z** — 所需填料层高度：`Z = HOG × NOG`
- **夹点判定** — 是否夹点受限，以及"离夹点还有多远"（最小气相推动力、
  实际 `L/G` 与最小液气比 `(L/G)_min` 的余量）

此外提供**反解**能力：给定分离目标（或塔高上限），由服务自己完成单调
迭代，反求恰好满足目标的吸收剂用量，并在解点用正算内核完整复算。

运行时固定 **Python 3.12**，框架为 **FastAPI**，无前端、无账户权限体系。

## 物理模型与约定

| 符号 | 含义 |
| --- | --- |
| `G`, `L` | 气 / 液相摩尔流量，均以塔截面单位面积为基准 |
| `Kya` | 气相总体积传质系数 |
| `a` | 填料比表面积；`S` 塔截面积 |
| `y1`,`y2` | 塔底（进气）/ 塔顶（出气）气相摩尔分率 |
| `x1`,`x2` | 塔底（富液）/ 塔顶（贫液）液相摩尔分率 |
| `m` | 亨利型平衡线斜率，`y* = m·x` |

- **操作线**由全塔物料衡算钉死：`G(y1−y2) = L(x1−x2)`，即
  `y = y2 + (L/G)(x−x2)`；服务强制校验四个端点分率与 G、L 自洽。
- **推动力方向严格为** `Δ = y − y*`（气相实际分率 − 平衡分率）。
- 稀相、两线均为直线时 NOG 取封闭解，服务内部自动选支：
  - `λ = mG/L ≠ 1`：对数平均推动力
    `Δ_lm = (Δ1−Δ2)/ln(Δ1/Δ2)`，`NOG = (y1−y2)/Δ_lm`；
  - `λ = 1`：两线平行、推动力处处相等，退化为
    `NOG = (y1−y2)/Δ`（**不会**一律套对数平均）。
- **夹点**：液气比逼近最小值、操作线与平衡线相切/相交时，局部
  `Δ→0`，NOG 积分发散（塔高无穷）。此时服务**拒绝返回有限高度**，
  错误码 `PINCH_LIMITED`，并提示需增大 `L/G`。端点推动力 ≤ 0
  或 ≤ 数值容差（`max(1e-10, 1e-9·y)`，可用环境变量调整）均判夹点。
- **合法退化**：`y1 = y2`（无净传质）时 `NOG = Z = 0`，不报错。

### 内置示范工况（可手算核对）

`air_water_demo`：`G=1, L=1.6, Kya=a=S=1, y1=0.10, y2=0.02, x1=0.05, x2=0, m=1`

- 衡算：`1·(0.10−0.02) = 1.6·(0.05−0) = 0.08` ✓
- `Δ1 = 0.05`，`Δ2 = 0.02`
- `Δ_lm = 0.03/ln 2.5 = 0.0327407` → **NOG = 0.08/Δ_lm ≈ 2.44344**
- **HOG = 1.0 m**，**Z ≈ 2.44344 m**
- `(L/G)_min = (y1−y2)/(y1/m−x2) = 0.8`，实际 `L/G=1.6` 高 100%，远离夹点。

## 非法输入（返回带原因的错误响应）

`G/L/Kya/a/S ≤ 0`；摩尔分率越出 `[0,1]`；`m < 0`；`y1 < y2`；
端点分率违反物料衡算；`y*=mx` 抬高到与操作线相切（夹点）；缺字段；
NaN/Infinity/布尔/字符串等非数值。错误统一形如：

```json
{ "error": { "code": "MASS_BALANCE_MISMATCH", "message": "……" } }
```

## 反解：给定分离目标，反求吸收剂用量

正算回答"这组操作条件需要多高塔"；反解回答**"要达到这个分离指标，吸收剂
得给多少、塔要做多高"**。底座（已知量）为 `G, Kya, a, S, y1, x2, m`（塔
几何、传质性能、进气、贫液、平衡线）；液相流量 `L`（等价于液气比 `L/G`）
是待解操作变量，塔底液相分率恒由衡算推出：
`x1 = x2 + (y1−y2)/(L/G)`，**不接受调用方另给 y2 / x1 作为底座**。

### 数学结构与可行性边界

固定目标 y2 时：

- `Z_required(L)` 随 `L` 单调**下降**——吸收剂越多，操作线离平衡线越远，
  所需塔越矮。服务据此用**二分法**夹住解，收敛判据 `xtol`（区间相对宽度，
  默认 1e-10）与最大迭代次数 `max_iterations`（默认 200）可逐条配置；
  达上限仍未收敛时返回 `SOLVER_DID_NOT_CONVERGE`，绝不返回没夹住的数。
- `L → (L/G)_min·G` 时塔高趋于无穷（**夹点**）；`L → ∞` 时趋于理论最小
  塔高 `Z_min = HOG·ln[(y1−mx2)/(y2−mx2)]`、出口分率极限 `y2 → mx2`。
- 目标一旦落进 `y2 ≤ mx2 + 夹点容差`（与正算夹点判定**同一容差口径**），
  或给定塔高低于该目标的 `Z_min`，明确判 **`TARGET_UNREACHABLE`** 并报出
  缺口（比可达极限深多少 / 还差多少米塔高），不硬吐贴着夹点的巨大流量。
  目标松到极小液气比即可满足时，老实返回贴物理下界的小解，不加码。
- 另有一条常被忽略的物理边界 `x1 ≤ 1`：高浓度进气时其对应的最小液气比
  `(y1−y2)/(1−x2)` 可能比夹点侧更紧，下界自动取两者大值。

### 两类诉求与"再钉死一个量"

目标 y2 单独不能唯一确定 L——`(L, Z)` 是一条权衡曲线，任意
`L/G > (L/G)_min` 都能达标，区别只在塔高。因此必须再钉死一个量：

**一、定分离（`mode=fixed_separation`，默认）**，目标用 `target_y2` 或
`removal_pct`（0-100，服务换算）给出，再四选一：

| 钉死的量 | 含义 | 求解方式 |
| --- | --- | --- |
| `Z` | 现成塔可用填料层高度 | 二分求根：恰好达标的**最小 L** |
| `design_factor` | 教科书选型 `L/G = c·(L/G)_min`（c>1） | 闭式，无需迭代 |
| `L` / `L_over_G` | 吸收剂用量已选定 | 正算复算所需塔高 |

**二、定塔高（`mode=fixed_height`）**，钉死 `Z_max`：

- **不给目标**：以"该塔高下无穷吸收剂极限出口"
  `y2_lim = mx2 + (y1−mx2)·exp(−Z_max/HOG)` 外侧一个可配置相对带
  `limit_band`（默认 1%，相对 `y1−y2_lim`）作为**可达最优点**，二分求把
  整段填料用尽所需的最小 L，响应同时给出理论极限 `y2_lim` 与本解差距。
  带越窄越贴近极限、所需 L 越大（趋近零时发散）。
- **给目标**：先判定该目标在 `Z_max` 内是否可达（`Z_max < Z_min` 即报
  不可行与缺口），可达则求根给出最小 L。

每次反解的响应都带 `verification`——**用正算内核在解点上完整复算一遍**的
结果（HOG/NOG/Z、两端推动力、离夹点距离、衡算输入等），供一眼核对正反算
自洽；并带 `limits`（无穷吸收剂出口极限、最小液气比、理论最小塔高）、
`solver`（方法、迭代次数、末次夹逼区间、是否收敛）与 `warnings`。

### 示范工况反解（可手算核对）

对内置 `air_water_demo` 底座（`y1=0.10, x2=0, m=1, G=Kya=a=S=1`）：

```bash
# 定分离 + 现成塔高 Z=2.44344：必须反解出 L/G=1.6、x1=0.05
curl -X POST localhost:8000/invert -H 'Content-Type: application/json' -d '{
  "profile": "air_water_demo", "target_y2": 0.02, "Z": 2.4434419516644135
}'
# 80% 脱除 + 设计倍率 2（=示范工况本身：(L/G)_min=0.8，2 倍即 1.6）
curl -X POST localhost:8000/invert -H 'Content-Type: application/json' -d '{
  "profile": "air_water_demo", "removal_pct": 80, "design_factor": 2.0
}'
# 定塔高：Z_max=2.44344 下能达到的最好出口（y2_lim=0.1·e^-2.44344≈0.00869）
curl -X POST localhost:8000/invert -H 'Content-Type: application/json' -d '{
  "profile": "air_water_demo", "mode": "fixed_height", "Z_max": 2.4434419516644135
}'
# 不可行：100% 脱除（y2=0 越过 mx2=0 极限）
curl -X POST localhost:8000/invert -H 'Content-Type: application/json' -d '{
  "profile": "air_water_demo", "target_y2": 0.0, "Z": 3.0
}'   # -> 422 TARGET_UNREACHABLE，报出比可达极限深多少
```

批量反解与正算批量同口径：HTTP 恒 200，逐条 `ok` / `error`，个别条目
不可行或非法不牵连其余条目：

```bash
curl -X POST localhost:8000/invert/batch -H 'Content-Type: application/json' -d '{
  "cases": [
    {"profile": "air_water_demo", "target_y2": 0.02, "design_factor": 2.0},
    {"profile": "air_water_demo", "target_y2": 0.0,  "Z": 3.0}
  ]
}'
```

新增错误码：`TARGET_UNREACHABLE`（目标越过理论极限 / 塔高不足）、
`INVALID_TARGET`（目标写法非法）、`INVERT_SPEC_INVALID`（已知/未知量指定
不全或冲突）、`SOLVER_DID_NOT_CONVERGE`（迭代达上限未收敛）。

## HTTP 接口

| 方法 路径 | 说明 |
| --- | --- |
| `POST /calculate` | 单组正算核算 |
| `POST /calculate/batch` | 批量正算（单条成败互不牵连，HTTP 恒 200） |
| `POST /invert` | 单组反解（定分离求吸收剂 / 定塔高求可行操作） |
| `POST /invert/batch` | 批量反解（逐条隔离，口径与正算批量一致） |
| `GET  /profiles` | 列出全部工况档 |
| `GET  /profiles/{name}` | 查看单个工况档 |
| `PUT  /profiles/{name}` | 登记 / 部分更新自定义工况档（持久化） |
| `DELETE /profiles/{name}` | 删除自定义工况档 |
| `GET  /health` | 健康检查；交互式文档见 `/docs` |

请求体字段全部可选：可只给 `"profile": "air_water_demo"` 点名工况档，
可临时给全套参数，也可工况档打底 + 内联字段覆盖（内联优先）。
反解点名工况档时**只取塔几何 / 传质 / 进气 / 平衡线底座**，档内的
`L / y2 / x1` 不参与（分别是待求量、目标、衡算导出量）。

```bash
curl -X POST localhost:8000/calculate -H 'Content-Type: application/json' -d '{
  "G": 1.0, "L": 1.6, "Kya": 1.0, "a": 1.0, "S": 1.0,
  "y1": 0.10, "y2": 0.02, "x1": 0.05, "x2": 0.0, "m": 1.0
}'
```

批量：

```bash
curl -X POST localhost:8000/calculate/batch -H 'Content-Type: application/json' -d '{
  "cases": [
    { "profile": "air_water_demo" },
    { "profile": "air_water_demo", "Kya": 2.0 },
    { "G": 1, "L": 0.8, "Kya": 1, "a": 1, "S": 1,
      "y1": 0.1, "y2": 0.02, "x1": 0.1, "x2": 0, "m": 1 }
  ]
}'
```

## 容器构建与运行

```bash
docker build -t absorption-height-service:latest .
docker run -d --name absorption -p 8000:8000 \
  -v absorption-data:/data absorption-height-service:latest
# 或
docker compose up -d --build
```

工况档写入 `/data/profiles.json`（容器内），挂载卷后**重启 / 重建仍在**。
存储采用临时文件 + 原子 `rename`，写前从磁盘重新加载，多请求并发 /
多进程共用同一文件时登记互不覆盖；核算本身是纯函数，并发结果互不污染。

可调环境变量：`SERVICE_DATA_DIR`、`SERVICE_PINCH_ATOL`、
`SERVICE_PINCH_RTOL`、`SERVICE_BALANCE_ATOL`、`SERVICE_BALANCE_RTOL`、
`SERVICE_LAMBDA_FLAT`、`SERVICE_LM_RATIO`、`SERVICE_INVERT_XTOL`、
`SERVICE_INVERT_MAXITER`、`SERVICE_INVERT_UPPER`、`SERVICE_INVERT_LG_FLOOR`、
`SERVICE_INVERT_BAND`。

## 本地开发与测试

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                 # 149 个用例
uvicorn app.main:app --reload
```

## 代码结构（按职责拆模块）

```
app/
  config.py       运行配置与容差（含反解求根默认参数）
  errors.py       错误码与领域异常（含反解四类错误）
  validation.py   请求校验（类型/符号/区间）
  balance.py      物料衡算、操作线、最小液气比
  nog.py          NOG 积分（对数平均支 / λ=1 退化支）
  engine.py       正算编排 + 夹点判定 + 离夹点距离
  invert.py       反解内核：目标/塔高 -> 单调二分求根 + 可行性边界
  profiles.py     工况档持久化（JSON/文件锁/原子写/内置示范档）
  service.py      profile 解析与单条/批量编排（正算 + 反解）
  schemas.py      pydantic 请求模型（正算 + 反解）
  routers/        calc.py、invert.py、profiles.py 三组 HTTP 路由
  main.py         应用装配与错误响应外壳
tests/            物理联动、夹点、非法输入、反解单调/自洽/边界、
                  HTTP、持久化、并发
```

正算测试守住的关键联动性质：Kya 加倍 → HOG、Z 减半而 NOG 不变；
远离夹点时增大 L/G → NOG 单调下降；y1=y2 → Z=0；λ=1 走常数推动力支；
精确/交叉/数值相切三类夹点均被拒绝；批量中坏条目不影响好条目；
工况档跨重启可用；并发登记无丢失。

反解测试守住的关键性质：解点正算复算的分离指标与塔高和目标在容差内
吻合；目标每苛刻一档，定塔高下反解的 L 单调变大、定倍率下所需塔高
单调变高；塔越高可达出口越净；目标越过 mx2 极限或塔高低于 Z_min 时
稳定报 `TARGET_UNREACHABLE`（含 100% 脱除、恰好落在 Z_min、x1≤1 边界
等情形），不超时、不吐夹点大数；迭代超限时如实报
`SOLVER_DID_NOT_CONVERGE`；示范工况反解精确还回 L/G=1.6、x1=0.05。

# 填料吸收塔传质高度核算服务（Absorption Height Service）

面向上游流程模拟器调用的常驻 HTTP 后端：输入一股**稀溶质气液逆流吸收**的
操作条件，返回：

- **HOG** — 气相总传质单元高度：`HOG = G / (Kya·a·S)`
- **NOG** — 气相总传质单元数：`NOG = ∫_{y2}^{y1} dy/(y − y*)`
- **Z** — 所需填料层高度：`Z = HOG × NOG`
- **夹点判定** — 是否夹点受限，以及"离夹点还有多远"（最小气相推动力、
  实际 `L/G` 与最小液气比 `(L/G)_min` 的余量）

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

## HTTP 接口

| 方法 路径 | 说明 |
| --- | --- |
| `POST /calculate` | 单组核算 |
| `POST /calculate/batch` | 批量核算（单条成败互不牵连，HTTP 恒 200，逐条 `ok`/`error`） |
| `GET  /profiles` | 列出全部工况档 |
| `GET  /profiles/{name}` | 查看单个工况档 |
| `PUT  /profiles/{name}` | 登记 / 部分更新自定义工况档（持久化） |
| `DELETE /profiles/{name}` | 删除自定义工况档 |
| `GET  /health` | 健康检查；交互式文档见 `/docs` |

请求体字段全部可选：可只给 `"profile": "air_water_demo"` 点名工况档，
可临时给全套参数，也可工况档打底 + 内联字段覆盖（内联优先）。

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
`SERVICE_LAMBDA_FLAT`、`SERVICE_LM_RATIO`。

## 本地开发与测试

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                 # 79 个用例
uvicorn app.main:app --reload
```

## 代码结构（按职责拆模块）

```
app/
  config.py       运行配置与容差
  errors.py       错误码与领域异常
  validation.py   请求校验（类型/符号/区间）
  balance.py      物料衡算、操作线、最小液气比
  nog.py          NOG 积分（对数平均支 / λ=1 退化支）
  engine.py       核算编排 + 夹点判定 + 离夹点距离
  profiles.py     工况档持久化（JSON/文件锁/原子写/内置示范档）
  service.py      profile 解析与单条/批量编排
  schemas.py      pydantic 请求模型
  routers/        calc.py、profiles.py 两组 HTTP 路由
  main.py         应用装配与错误响应外壳
tests/            物理联动、夹点、非法输入、HTTP、持久化、并发
```

测试守住的关键联动性质：Kya 加倍 → HOG、Z 减半而 NOG 不变；
远离夹点时增大 L/G → NOG 单调下降；y1=y2 → Z=0；λ=1 走常数推动力支；
精确/交叉/数值相切三类夹点均被拒绝；批量中坏条目不影响好条目；
工况档跨重启可用；并发登记无丢失。

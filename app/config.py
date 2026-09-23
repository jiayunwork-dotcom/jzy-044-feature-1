"""运行期配置。

所有配置均带默认值，可通过环境变量覆盖，便于容器化部署：

- ``SERVICE_DATA_DIR``          工况档 JSON 文件所在目录
- ``SERVICE_PINCH_ATOL``        夹点判定的绝对推动力容差
- ``SERVICE_PINCH_RTOL``        夹点判定相对 y 尺度的相对容差
- ``SERVICE_BALANCE_ATOL``      物料衡算 G(y1-y2)=L(x1-x2) 的绝对容差
- ``SERVICE_BALANCE_RTOL``      物料衡算的相对容差
- ``SERVICE_LAMBDA_FLAT``       斜率比 λ=mG/L 视为 1 的容差
- ``SERVICE_LM_RATIO``          对数平均退化到算术平均的 Δ2/Δ1 阈值
- ``SERVICE_INVERT_XTOL``       反解二分迭代液气比的相对收敛宽度
- ``SERVICE_INVERT_MAXITER``    反解二分迭代最大次数
- ``SERVICE_INVERT_UPPER``      反解夹取上界相对最小液气比的扩张倍数
- ``SERVICE_INVERT_LG_FLOOR``   m=0 等不存在有限最小液气比时的液气比地板
- ``SERVICE_INVERT_BAND``       定塔高反解逼近无穷吸收剂极限的相对收窄带
"""

from __future__ import annotations

import os
from pathlib import Path


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


DATA_DIR = Path(os.environ.get("SERVICE_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
PROFILES_FILE = DATA_DIR / "profiles.json"

# 推动力 ≤ 该容差即判定为夹点：max(1e-10, 1e-9·max(|y1|,|y2|))
PINCH_ATOL = _float_env("SERVICE_PINCH_ATOL", 1e-10)
PINCH_RTOL = _float_env("SERVICE_PINCH_RTOL", 1e-9)

# 物料衡算自洽性容差
BALANCE_ATOL = _float_env("SERVICE_BALANCE_ATOL", 1e-9)
BALANCE_RTOL = _float_env("SERVICE_BALANCE_RTOL", 1e-7)

# |λ - 1| ≤ 1e-9 时走 NOG = (y1-y2)/Δ 的退化支
LAMBDA_FLAT_TOL = _float_env("SERVICE_LAMBDA_FLAT", 1e-9)

# Δ2/Δ1 接近 1 时 log-mean 用算术平均兜底
LM_RATIO_TOL = _float_env("SERVICE_LM_RATIO", 1e-6)

# ---- 反解（单调求根）默认控制参数，可被请求逐条覆盖 ----
# 夹逼区间相对宽度 (hi-lo)/hi ≤ 该值即认为收敛
INVERT_XTOL = _float_env("SERVICE_INVERT_XTOL", 1e-10)
INVERT_MAX_ITER = int(_float_env("SERVICE_INVERT_MAXITER", 200))
# 上界初始取 上限倍数·最小液气比（夹不住则倍增）
INVERT_UPPER_FACTOR = _float_env("SERVICE_INVERT_UPPER", 1024.0)
# m=0 等"不存在有限最小液气比"情形下，搜索下界使用的液气比地板
INVERT_LG_FLOOR = _float_env("SERVICE_INVERT_LG_FLOOR", 1e-9)
# 定塔高反解：目标出口相对无穷吸收剂极限的收窄带（相对 span 计）
# 带越窄越贴近理论极限、所需液气比越大；1% 给出工程上可操作的点
INVERT_LIMIT_BAND = _float_env("SERVICE_INVERT_BAND", 1e-2)

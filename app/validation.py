"""请求校验。

只做"结构层面"的把关：字段是否齐全、是否为有限数值、物理量的符号与
摩尔分率区间。物料衡算的自洽性由 :mod:`app.balance` 负责，夹点判定由
:mod:`app.engine` 负责。
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .errors import (
    ERR_FRACTION_RANGE,
    ERR_INVALID_SLOPE,
    ERR_MISSING_FIELD,
    ERR_NON_POSITIVE,
    ERR_NOT_A_NUMBER,
    ERR_Y_ORDER,
    CalculationError,
)

# 核算所需的全部字段
ALL_FIELDS: tuple[str, ...] = (
    "G", "L", "Kya", "a", "S",
    "y1", "y2", "x1", "x2", "m",
)

# 必须严格为正的量
POSITIVE_FIELDS: tuple[str, ...] = ("G", "L", "Kya", "a", "S")

# 摩尔分率
FRACTION_FIELDS: tuple[str, ...] = ("y1", "y2", "x1", "x2")

# 中文说明，拼进错误信息，方便工艺人员直接读懂
FIELD_LABELS: dict[str, str] = {
    "G": "气相摩尔流量 G",
    "L": "液相摩尔流量 L",
    "Kya": "气相总体积传质系数 Kya",
    "a": "填料比表面积 a",
    "S": "塔截面积 S",
    "y1": "塔底气相摩尔分率 y1",
    "y2": "塔顶气相摩尔分率 y2",
    "x1": "塔底液相摩尔分率 x1",
    "x2": "塔顶液相摩尔分率 x2",
    "m": "平衡线斜率 m",
}


def _coerce_number(name: str, value: Any) -> float:
    # bool 是 int 的子类，但"开/关"绝不是流量，明确挡掉
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalculationError(
            ERR_NOT_A_NUMBER,
            f"{FIELD_LABELS[name]}（{name}）必须是数值，收到 {value!r}",
        )
    fv = float(value)
    if not math.isfinite(fv):
        raise CalculationError(
            ERR_NOT_A_NUMBER,
            f"{FIELD_LABELS[name]}（{name}）必须是有限数值，收到 {value!r}",
        )
    return fv


def validate_operating_conditions(raw: Mapping[str, Any]) -> dict[str, float]:
    """校验并返回一份干净的 float 参数字典。

    缺字段不在这一层抛错（工况档 + 临时参数合并后才知道是否真的缺），
    因此 :func:`coerce_known_fields` 只转换出现的字段；缺失检查见
    :func:`require_complete`。
    """
    params = coerce_known_fields(raw)
    require_complete(params)
    _check_ranges(params)
    return params


def coerce_known_fields(raw: Mapping[str, Any]) -> dict[str, float]:
    """把输入中出现的已知字段转成有限 float（忽略未知字段，未知字段
    在路由层的 pydantic 模型里已被拒）。"""
    params: dict[str, float] = {}
    for name in ALL_FIELDS:
        if name in raw and raw[name] is not None:
            params[name] = _coerce_number(name, raw[name])
    return params


def require_complete(params: Mapping[str, float]) -> None:
    missing = [f"{name}（{FIELD_LABELS[name]}）" for name in ALL_FIELDS if name not in params]
    if missing:
        raise CalculationError(
            ERR_MISSING_FIELD,
            "以下核算参数缺失：" + "、".join(missing),
            http_status=400,
        )


def _check_ranges(p: Mapping[str, float]) -> None:
    for name in POSITIVE_FIELDS:
        if p[name] <= 0.0:
            raise CalculationError(
                ERR_NON_POSITIVE,
                f"{FIELD_LABELS[name]}（{name}）必须为正，收到 {p[name]:.12g}",
            )

    if p["m"] < 0.0:
        raise CalculationError(
            ERR_INVALID_SLOPE,
            f"亨利型平衡线斜率 m 必须非负（y*=m·x，x∈[0,1]），收到 m={p['m']:.12g}",
        )

    for name in FRACTION_FIELDS:
        if not (0.0 <= p[name] <= 1.0):
            raise CalculationError(
                ERR_FRACTION_RANGE,
                f"{FIELD_LABELS[name]}（{name}）必须落在 [0,1]，收到 {p[name]:.12g}",
            )

    # 本服务只核算"稀溶质被吸收"：气相沿塔从 y1 净化到 y2，需 y1 ≥ y2。
    # y1 == y2 属于合法退化（无净传质，NOG=Z=0），在 engine 里处理。
    if p["y1"] < p["y2"]:
        raise CalculationError(
            ERR_Y_ORDER,
            f"吸收核算要求塔底进气分率 y1 ≥ 塔顶出气分率 y2，"
            f"收到 y1={p['y1']:.12g} < y2={p['y2']:.12g}",
        )

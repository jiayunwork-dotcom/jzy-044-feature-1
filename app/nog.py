"""NOG 积分：``NOG = ∫_{y2}^{y1} dy / (y - y*)``。

稀相、平衡线（y*=m·x）与操作线均为直线时，气相推动力沿塔线性变化，
积分可由两端推动力 Δ1=y1-mx1（塔底）、Δ2=y2-mx2（塔顶）封闭求出：

* λ = mG/L ≠ 1：``NOG = (y1-y2) / Δ_lm``，
  其中 Δ_lm = (Δ1-Δ2)/ln(Δ1/Δ2) 为对数平均推动力；
* λ = 1：两线平行、推动力处处相等，退化为 ``NOG = (y1-y2)/Δ``。

被积函数的方向严格取 **气相实际分率减去平衡分率**（y-y*），不能反号。
调用方（:mod:`app.engine`）必须先确认 Δ1、Δ2 均严格为正（未夹点），
再调用本模块。
"""

from __future__ import annotations

import math
from typing import Mapping

from . import balance, config


def log_mean_driving_force(delta_bottom: float, delta_top: float) -> float:
    """对数平均推动力，数值稳定实现。

    Δ1、Δ2 非常接近时直接算 (Δ1-Δ2)/ln(Δ1/Δ2) 会产生相消误差，
    此时以算术平均兜底；Δ1==Δ2 时对数平均本来就严格等于算术平均。
    """
    d1, d2 = delta_bottom, delta_top
    if d1 <= 0.0 or d2 <= 0.0:
        raise ValueError("log_mean_driving_force 只接受正推动力")
    if d1 < d2:
        d1, d2 = d2, d1
    ratio = d2 / d1
    if ratio >= 1.0 - config.LM_RATIO_TOL:
        # -ln(r) ≈ (1-r)·(1+(1-r)/2+...)，二者均值即算术平均，误差 O((1-r)^2)
        return 0.5 * (d1 + d2)
    return (d1 - d2) / math.log(d1 / d2)


def integrate_nog(p: Mapping[str, float], delta_bottom: float, delta_top: float) -> tuple[float, str]:
    """计算传质单元数。

    返回 ``(NOG, 所用封闭支)``，封闭支取 ``log_mean`` 或 ``constant_force``。
    """
    span = p["y1"] - p["y2"]
    lam = balance.slope_ratio(p)

    if math.isclose(lam, 1.0, rel_tol=0.0, abs_tol=config.LAMBDA_FLAT_TOL):
        # λ=1：操作线与平衡线平行，Δ 不随 y 变，NOG = Δy/Δ
        return span / delta_bottom, "constant_force"

    delta_lm = log_mean_driving_force(delta_bottom, delta_top)
    return span / delta_lm, "log_mean"

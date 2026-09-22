"""全塔物料衡算与操作线。

逆流吸收塔（下标 1 = 塔底 / 进气富液端，下标 2 = 塔顶 / 出气贫液端）：

* 衡算方程：``G·(y1 - y2) = L·(x1 - x2)``
* 操作线：``y = y2 + (L/G)·(x - x2)``，由两端点钉死
* 平衡线：``y* = m·x``
"""

from __future__ import annotations

from typing import Mapping

from . import config
from .errors import ERR_MASS_BALANCE, CalculationError


def balance_residual(p: Mapping[str, float]) -> float:
    """物料衡算左右两侧之差：G(y1-y2) - L(x1-x2)。"""
    return p["G"] * (p["y1"] - p["y2"]) - p["L"] * (p["x1"] - p["x2"])


def check_mass_balance(p: Mapping[str, float]) -> None:
    """校验进出口四个分率与 G、L 是否自洽。"""
    gas_side = p["G"] * (p["y1"] - p["y2"])
    liquid_side = p["L"] * (p["x1"] - p["x2"])
    tol = config.BALANCE_ATOL + config.BALANCE_RTOL * max(abs(gas_side), abs(liquid_side))
    if abs(gas_side - liquid_side) > tol:
        raise CalculationError(
            ERR_MASS_BALANCE,
            "进出口分率与物料衡算自相矛盾：G(y1-y2)="
            f"{gas_side:.12g} 与 L(x1-x2)={liquid_side:.12g} 不一致，"
            f"残差 {gas_side - liquid_side:.12g} 超出容差 ±{tol:.3g}。"
            "请调整某个分率，或由其余三个端点分率推出第四个。",
        )


def slope_ratio(p: Mapping[str, float]) -> float:
    """斜率比 λ = m·G/L（平衡线与操作线斜率之比，即脱吸因子）。"""
    return p["m"] * p["G"] / p["L"]


def operating_line(p: Mapping[str, float], x: float) -> float:
    """操作线：给定液相分率 x 处的气相实际分率 y。"""
    return p["y2"] + (p["L"] / p["G"]) * (x - p["x2"])


def equilibrium(m: float, x: float) -> float:
    """亨利型平衡关系 y* = m·x。"""
    return m * x


def driving_force_at_bottom(p: Mapping[str, float]) -> float:
    """塔底气相推动力 Δ1 = y1 - m·x1。"""
    return p["y1"] - p["m"] * p["x1"]


def driving_force_at_top(p: Mapping[str, float]) -> float:
    """塔顶气相推动力 Δ2 = y2 - m·x2。"""
    return p["y2"] - p["m"] * p["x2"]


def minimum_liquid_to_gas(p: Mapping[str, float]) -> float | None:
    """经典最小液气比 ``(L/G)_min = (y1-y2)/(y1/m - x2)``。

    纯溶剂（x2=0）时退化为 m(1-y2/y1)。返回 ``None`` 表示该参数组合下
    不存在有限的最小液气比（如 m=0 平衡线与横轴重合，任何液气比都不会
    夹点）。
    """
    if p["m"] <= 0.0:
        return None
    denom = p["y1"] / p["m"] - p["x2"]
    if denom <= 0.0:
        return None
    return (p["y1"] - p["y2"]) / denom

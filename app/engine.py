"""核算引擎：把校验、物料衡算、夹点判定与 NOG 积分串成一次完整核算。

输出中除 HOG / NOG / Z 外，还显式回答"离夹点还有多远"——既给出当前
最小气相推动力与夹点容差的对比，也给出经典最小液气比与实际液气比的
余量百分比。
"""

from __future__ import annotations

from typing import Any, Mapping

from . import balance, config, nog
from .errors import ERR_PINCH, CalculationError
from .validation import validate_operating_conditions


def pinch_tolerance(p: Mapping[str, float]) -> float:
    """判定夹点用的推动力容差：绝对下限 + 相对 y 尺度的分量。"""
    return config.PINCH_ATOL + config.PINCH_RTOL * max(abs(p["y1"]), abs(p["y2"]))


def _pinch_error(p: Mapping[str, float], delta_b: float, delta_t: float, at: str) -> CalculationError:
    lg = p["L"] / p["G"]
    lg_min = balance.minimum_liquid_to_gas(p)
    if lg_min is not None:
        ratio_text = f"当前 L/G={lg:.12g}，最小液气比 (L/G)_min={lg_min:.12g}。"
    else:
        ratio_text = f"当前 L/G={lg:.12g}。"
    return CalculationError(
        ERR_PINCH,
        f"夹点受限：{at}的气相推动力 y-y* 已趋于零或非正"
        f"（Δ1=y1-mx1={delta_b:.12g}，Δ2=y2-mx2={delta_t:.12g}），"
        "NOG 积分发散，物理上需要无穷高的填料层，拒绝返回虚假的有限高度。"
        f"{ratio_text}需提高液气比（增大 L/G，例如增大吸收剂用量 L）"
        "把操作线抬离平衡线以重新拉开推动力。",
    )


def run_calculation(raw: Mapping[str, Any]) -> dict[str, Any]:
    """对一组完整操作条件执行核算，返回结构化结果。

    非法输入或夹点受限时抛 :class:`~app.errors.CalculationError`。
    """
    p = validate_operating_conditions(raw)
    balance.check_mass_balance(p)

    g, l = p["G"], p["L"]
    y_span = p["y1"] - p["y2"]
    lam = balance.slope_ratio(p)
    lg = l / g

    # ---- 传质单元高度：HOG = G / (Kya·a·S) ----
    # Kya 在本服务的约定中为体积传质系数；式中保留 S（塔截面积），
    # 以单位塔截面给入 G、L 且 Kya 已按体积计时取 S=1。
    hog = g / (p["Kya"] * p["a"] * p["S"])

    # ---- 合法退化：y1 == y2，无净传质 ----
    if y_span == 0.0:
        delta_b = p["y1"] - p["m"] * p["x1"]
        delta_t = p["y2"] - p["m"] * p["x2"]
        lg_min = balance.minimum_liquid_to_gas(p)
        return {
            "HOG": hog,
            "NOG": 0.0,
            "Z": 0.0,
            "pinch_limited": False,
            "degenerate": True,
            "integral_method": "no_transfer",
            "slope_ratio": lam,
            "L_over_G": lg,
            "driving_force_bottom": delta_b,
            "driving_force_top": delta_t,
            "min_driving_force": min(delta_b, delta_t),
            "pinch_tolerance": pinch_tolerance(p),
            "minimum_L_over_G": lg_min,
            "L_over_G_excess_pct": _lg_excess_pct(lg, lg_min),
            "distance_to_pinch": {
                "min_driving_force": min(delta_b, delta_t),
                "pinch_tolerance": pinch_tolerance(p),
                "driving_force_margin": None,
                "pinch_location": "bottom" if delta_b <= delta_t else "top",
                "actual_L_over_G": lg,
                "minimum_L_over_G": lg_min,
                "L_over_G_excess_pct": _lg_excess_pct(lg, lg_min),
                "assessment": "进出口气相分率相同（y1=y2），无净传质，NOG=Z=0，"
                              "不进行夹点判定。",
            },
            "inputs": dict(p),
        }

    # ---- 两端气相推动力（严格 y-y*，塔底 Δ1、塔顶 Δ2） ----
    delta_b = balance.driving_force_at_bottom(p)
    delta_t = balance.driving_force_at_top(p)

    # ---- 夹点判定 ----
    # 直线对直线，推动力沿塔线性变化；最小推动力必在某个端点。
    # Δ ≤ 0：操作线与平衡线相交（夹点 / 交叉，NOG 发散或无物理意义）；
    # 0 < Δ ≤ 容差：数值相切，NOG 大到不可信，同样按夹点拒绝。
    tol = pinch_tolerance(p)
    if min(delta_b, delta_t) <= tol:
        if delta_b <= tol and delta_t <= tol:
            where = "塔底与塔顶"
        elif delta_b <= tol:
            where = "塔底端"
        else:
            where = "塔顶端"
        raise _pinch_error(p, delta_b, delta_t, where)

    # ---- NOG（服务内部自选对数平均支或 λ=1 退化支） ----
    n_og, method = nog.integrate_nog(p, delta_b, delta_t)
    z = hog * n_og

    lg_min = balance.minimum_liquid_to_gas(p)
    min_df = min(delta_b, delta_t)
    margin = min_df - tol

    return {
        "HOG": hog,
        "NOG": n_og,
        "Z": z,
        "pinch_limited": False,
        "degenerate": False,
        "integral_method": method,
        "slope_ratio": lam,
        "L_over_G": lg,
        "driving_force_bottom": delta_b,
        "driving_force_top": delta_t,
        "min_driving_force": min_df,
        "pinch_tolerance": tol,
        "minimum_L_over_G": lg_min,
        "L_over_G_excess_pct": _lg_excess_pct(lg, lg_min),
        "distance_to_pinch": {
            "min_driving_force": min_df,
            "pinch_tolerance": tol,
            "driving_force_margin": margin,
            "pinch_location": "bottom" if delta_b <= delta_t else "top",
            "actual_L_over_G": lg,
            "minimum_L_over_G": lg_min,
            "L_over_G_excess_pct": _lg_excess_pct(lg, lg_min),
            "assessment": _assessment(margin, min_df, lg, lg_min),
        },
        "inputs": dict(p),
    }


def _lg_excess_pct(lg: float, lg_min: float | None) -> float | None:
    """实际液气比相对最小液气比的余量百分比。"""
    if lg_min is None or lg_min <= 0.0:
        return None
    return 100.0 * (lg - lg_min) / lg_min


def _assessment(margin: float, min_df: float, lg: float, lg_min: float | None) -> str:
    rel = min_df / max(abs(min_df), 1e-300)
    if margin <= 0.0:
        return "已处于夹点（该结果应被拒绝）。"
    if min_df < 1e-6:
        head = "非常接近夹点，推动力极小、塔高对操作条件极敏感"
    elif rel < 1e-3:
        head = "较接近夹点"
    else:
        head = "远离夹点，推动力充足"
    if lg_min is not None:
        return (
            f"{head}：全塔最小气相推动力 {min_df:.6g}（夹点容差 {config.PINCH_ATOL:.0e}），"
            f"实际 L/G={lg:.6g} 较最小液气比 {lg_min:.6g} 高 "
            f"{100.0 * (lg - lg_min) / lg_min:.2f}%。"
        )
    return (
        f"{head}：全塔最小气相推动力 {min_df:.6g}（夹点容差 {config.PINCH_ATOL:.0e}）；"
        "平衡线斜率 m=0，平衡分率恒为零，不存在有限的最小液气比。"
    )

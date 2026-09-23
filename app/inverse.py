"""反解内核：给定分离目标，反算操作点，再用正算内核复算自洽。

两类反解共用同一套物理内核，区别只在"把哪个量当已知、把哪个量当未知"：

* ``solve_flow_for_target``（定分离、求吸收剂用量）
  塔几何 / 传质 / 进气 / 平衡线固定，给定目标塔顶气相分率 ``y2``（或脱除
  百分比）与可用填料层高度上限，反求恰好满足指标所需的液相摩尔流量 ``L``，
  并给出该流量下的完整正算结果。
* ``solve_outlet_for_height``（定塔高、求可行操作）
  固定塔高上限 ``Z_max``，求不超过该高度能达到的最优（最深）出口分率，
  以及对应需要配置的液相流量。

数学上两者都是一维单调求根：

* 对固定 ``y2``，``Z`` 随液气比 ``q=L/G`` 增大而**严格单调下降**
  （``q↓最小液气比`` 时 ``Z→∞`` 夹点，``q→∞`` 时 ``Z→Z_min`` 有限）；
* 对固定 ``q``，所需 ``Z`` 随 ``y2`` 加深而单调上升。

因此统一用夹逼 + 二分求根，绝不返回未真正收敛的数。求根过程不经过
:func:`app.engine.run_calculation`（正算要求四端点自洽且夹点即抛错），
而是在本模块内直接用与正算**同一套封闭公式**求值；求出操作点后，再用
正算内核在该点完整复算一遍并比对（:func:`verify_with_forward`）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import balance, config, engine, nog
from .errors import (
    ERR_SOLVER_NO_CONVERGE,
    ERR_TARGET_INFEASIBLE,
    CalculationError,
)
from .validation import (
    FIELD_LABELS,
    FRACTION_FIELDS,
    _coerce_number,
)

# 反解基底参数：塔几何 / 传质 / 进气 / 进液 / 平衡线（L、y2、x1 是被解出来的）
BASE_FIELDS: tuple[str, ...] = ("G", "Kya", "a", "S", "y1", "x2", "m")

# 求解状态：有限可算 / 夹点（需要无穷高）/ 液相出口饱和（x1 越界）
STATUS_FINITE = "finite"
STATUS_PINCH = "pinch"
STATUS_SATURATED = "liquid_saturated"


# ---------------------------------------------------------------------- 工具


def _as_float(name: str, value: Any, label: str | None = None) -> float:
    """把请求里的反解控制量（目标、塔高、求解设置）转成有限 float。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalculationError(
            "NOT_A_NUMBER",
            f"{label or name} 必须是数值，收到 {value!r}",
            http_status=422,
        )
    fv = float(value)
    if not math.isfinite(fv):
        raise CalculationError(
            "NOT_A_NUMBER",
            f"{label or name} 必须是有限数值，收到 {value!r}",
            http_status=422,
        )
    return fv


def validate_base(raw: Mapping[str, Any]) -> dict[str, float]:
    """校验反解基底（7 个已知量），返回干净的 float 字典。

    不校验 L / y2 / x1（三者由反解决定），也不做物料衡算（衡算由
    :func:`operating_point` 按求解出的量自动配平，正算复算时再复核一次）。
    """
    p: dict[str, float] = {}
    for name in BASE_FIELDS:
        if name in raw and raw[name] is not None:
            p[name] = _coerce_number(name, raw[name])
    missing = [f"{name}（{FIELD_LABELS[name]}）" for name in BASE_FIELDS if name not in p]
    if missing:
        from .errors import ERR_MISSING_FIELD

        raise CalculationError(
            ERR_MISSING_FIELD,
            "反解基底参数缺失（塔几何/传质、进气、进液、平衡线必须已知）：" + "、".join(missing),
            http_status=400,
        )

    for name in ("G", "Kya", "a", "S"):
        if p[name] <= 0.0:
            raise CalculationError(
                "NON_POSITIVE_VALUE",
                f"{FIELD_LABELS[name]}（{name}）必须为正，收到 {p[name]:.12g}",
                http_status=422,
            )
    if p["m"] < 0.0:
        raise CalculationError(
            "INVALID_SLOPE",
            f"亨利型平衡线斜率 m 必须非负（y*=m·x，x∈[0,1]），收到 m={p['m']:.12g}",
            http_status=422,
        )
    for name in FRACTION_FIELDS:
        if name in p and not (0.0 <= p[name] <= 1.0):
            raise CalculationError(
                "FRACTION_OUT_OF_RANGE",
                f"{FIELD_LABELS[name]}（{name}）必须落在 [0,1]，收到 {p[name]:.12g}",
                http_status=422,
            )
    return p


def pinch_floor(p: Mapping[str, float]) -> float:
    """夹点容差地板：``y2`` 低于 ``m·x2 + 该容差`` 即与正算夹点判定同口径。

    与 :func:`app.engine.pinch_tolerance` 的尺度保持一致
    （``max(1e-10, 1e-9·y)``），只是反解时 y2 未知，故以进气分率 y1 作
    相对尺度——对 y1≥y2 的吸收问题，这样只会更保守，不会放宽边界。
    """
    return config.PINCH_ATOL + config.PINCH_RTOL * max(abs(p["y1"]), abs(p["x2"]))


def hog(p: Mapping[str, float]) -> float:
    """HOG = G/(Kya·a·S)，反解中只依赖基底，与操作点无关。"""
    return p["G"] / (p["Kya"] * p["a"] * p["S"])


# ---------------------------------------------------------------- 物理求值器


@dataclass(frozen=True)
class Point:
    """一个待评估操作点的封闭解结果（与正算内核同一套公式）。"""

    status: str
    L: float
    q: float            # L/G
    y2: float
    x1: float
    lam: float          # λ = mG/L = m/q
    delta_bottom: float
    delta_top: float
    nog: float
    z: float

    @property
    def finite(self) -> bool:
        return self.status == STATUS_FINITE


def operating_point(p: Mapping[str, float], q: float, y2: float) -> Point:
    """给定基底、液气比 q=L/G 与塔顶分率 y2，封闭求 x1 / NOG / Z。

    衡算自动配平：``x1 = x2 + (y1-y2)/q``（吸收要求 x1≥x2）。
    任何"需要无穷塔高 / 物理不可达"的情形都以非 finite 状态显式返回，
    **绝不**返回一个巨大但虚假的有限高度。
    """
    g, m, y1, x2 = p["G"], p["m"], p["y1"], p["x2"]
    L = q * g
    lam = m / q if q > 0.0 else math.inf

    span = y1 - y2
    # 塔顶推动力 Δ2 = y2 - m·x2
    delta_t = y2 - m * x2
    tol = pinch_floor(p)
    if delta_t <= tol:
        # 操作线在塔顶（或塔内）触及 / 越过平衡线：夹点
        return Point(STATUS_PINCH, L, q, y2, math.nan, lam, math.nan, delta_t, math.inf, math.inf)

    if span < 0.0:
        return Point(STATUS_PINCH, L, q, y2, math.nan, lam, math.nan, delta_t, math.inf, math.inf)

    if span == 0.0:
        # 合法退化：无净传质，x1=x2，NOG=Z=0
        delta_b = y1 - m * x2
        return Point(STATUS_FINITE, L, q, y2, x2, lam, delta_b, delta_t, 0.0, 0.0)

    x1 = x2 + span / q
    if x1 > 1.0:
        # 液相出口摩尔分率不能超过 1：再多吸收剂也改变不了该物理上限
        return Point(STATUS_SATURATED, L, q, y2, x1, lam, math.nan, delta_t, math.inf, math.inf)

    delta_b = y1 - m * x1
    if delta_b <= tol:
        return Point(STATUS_PINCH, L, q, y2, x1, lam, delta_b, delta_t, math.inf, math.inf)

    h = hog(p)
    if math.isclose(lam, 1.0, rel_tol=0.0, abs_tol=config.LAMBDA_FLAT_TOL):
        # λ=1：两线平行、推动力处处相等，NOG=Δy/Δ（与正算退化支一致）
        n = span / delta_b
        method_z = h * n
        return Point(STATUS_FINITE, L, q, y2, x1, lam, delta_b, delta_t, n, method_z)

    delta_lm = nog.log_mean_driving_force(delta_b, delta_t)
    n = span / delta_lm
    return Point(STATUS_FINITE, L, q, y2, x1, lam, delta_b, delta_t, n, h * n)


# ---------------------------------------------------------------- 可行性边界


def limiting_outlet(p: Mapping[str, float]) -> dict[str, float]:
    """理论可行性极限：无穷吸收剂（L→∞, x1→x2）下的最深出口分率与最小塔高。

    * ``y2_star`` = m·x2：再多吸收剂也越不过平衡线在塔顶的限制；
    * ``N_star`` = ln[(y1-mx2)/(y2-mx2)]（λ→0 的 NOG 极限）；
    * ``Z_min(y2)`` = HOG·N_star：达成 y2 所需的理论最小塔高。
    """
    m, x2, y1 = p["m"], p["x2"], p["y1"]
    y2_star = m * x2
    h = hog(p)
    return {
        "y2_star": y2_star,
        "hog": h,
        "N_at": lambda y2: math.log((y1 - y2_star) / (y2 - y2_star)),
        "Z_min_at": lambda y2: h * math.log((y1 - y2_star) / (y2 - y2_star)),
    }


def minimum_q(p: Mapping[str, float], y2: float) -> float:
    """达成目标 y2 的最小液气比（操作线塔底端恰好触及平衡线时）。

    与 :func:`app.balance.minimum_liquid_to_gas` 同一式：
    ``q_min = (y1-y2)/(y1/m - x2)``；m=0 时为 0（不存在有限最小液气比）。
    另受液相出口饱和 x1≤1 的约束：``q ≥ (y1-y2)/(1-x2)``。
    """
    m, y1, x2 = p["m"], p["y1"], p["x2"]
    span = y1 - y2
    q_sat = span / (1.0 - x2) if x2 < 1.0 else math.inf
    if m <= 0.0:
        return q_sat
    denom = y1 / m - x2
    if denom <= 0.0:
        return q_sat
    return max(span / denom, q_sat)


# ---------------------------------------------------------------- 单调求根


@dataclass(frozen=True)
class RootResult:
    root: float
    iterations: int
    bracket_low: float
    bracket_high: int | float
    f_low: float
    f_high: float


def _sign(v: float) -> int:
    """扩展实数轴上的符号：+∞ 记正、-∞ 记负（夹点侧残差恒为 +∞）。"""
    if v > 0.0:
        return 1
    if v < 0.0:
        return -1
    return 0


def _bisect(
    f: Callable[[float], float],
    lo: float,
    hi: float,
    *,
    f_lo: float | None = None,
    f_hi: float | None = None,
    x_abs_tol: float,
    x_rel_tol: float,
    max_iter: int,
    scale: Callable[[float], float] = abs,
) -> RootResult:
    """在已夹住根的 ``[lo, hi]``（两端残差异号）上二分求根。

    端点或中点残差允许取 ``+∞``——它代表该侧落在夹点 / 液相饱和域
    （需要无穷塔高），与正残差同侧处理：非有限域与有限域以物理边界相接，
    根必在区间内部，按符号缩区间即可稳定逼近。

    收敛判据为区间宽度：``hi-lo ≤ x_abs_tol + x_rel_tol·max(|lo|,|hi|)``。
    迭代到 ``max_iter`` 仍未收敛时如实抛 :data:`ERR_SOLVER_NO_CONVERGE`，
    不把当前中点当答案糊弄出去。
    """
    flo = f(lo) if f_lo is None else f_lo
    fhi = f(hi) if f_hi is None else f_hi
    for v in (flo, fhi):
        if math.isnan(v) or (not math.isfinite(v) and not math.isinf(v)):
            raise ValueError("bisection 端点残差只能是有限值或 ±inf")
    if flo == 0.0:
        return RootResult(lo, 0, lo, hi, flo, fhi)
    if fhi == 0.0:
        return RootResult(hi, 0, lo, hi, flo, fhi)
    if _sign(flo) * _sign(fhi) > 0:
        raise ValueError("bisection bracket does not straddle the root")

    for it in range(1, max_iter + 1):
        mid = 0.5 * (lo + hi)
        if hi - lo <= x_abs_tol + x_rel_tol * max(scale(lo), scale(hi), 1.0):
            return RootResult(mid, it, lo, hi, flo, fhi)
        fm = f(mid)
        if fm == 0.0:
            return RootResult(mid, it, lo, hi, flo, fhi)
        if math.isnan(fm):
            raise ValueError("求根函数返回 NaN")
        if _sign(fm) == _sign(flo):
            lo, flo = mid, fm
        else:
            hi, fhi = mid, fm

    raise CalculationError(
        ERR_SOLVER_NO_CONVERGE,
        f"二分迭代 {max_iter} 次后区间宽度仍大于收敛容差，反解未真正收敛；"
        "按策略拒绝返回未收敛的操作点。可调大 max_iterations 或放宽容差后重试。",
        details={
            "iterations": max_iter,
            "bracket_low": lo,
            "bracket_high": hi,
        },
    )


def _expand_bracket_high(
    f: Callable[[float], float],
    lo: float,
    hi: float,
    f_lo: float,
    *,
    ceil: float,
    reason_when_exhausted: Callable[[float, float], CalculationError],
) -> tuple[float, float]:
    """几何放大上界直到夹住根（``f(hi)`` 与 ``f(lo)`` 异号）。"""
    fhi = f(hi)
    while not math.isfinite(fhi) or (f_lo < 0.0) == (fhi < 0.0):
        if hi >= ceil:
            raise reason_when_exhausted(lo, hi)
        hi = min(ceil, hi * 10.0)
        fhi = f(hi)
    return hi, fhi


# ---------------------------------------------------------------- 正算复算


def verify_with_forward(p: Mapping[str, float], q: float, y2: float) -> dict[str, Any]:
    """在反解操作点上用正算内核完整复算，并校验两条路径自洽。

    组装出的四端点严格满足物料衡算（同一式配平），故正算的衡算容差检查
    必然通过；返回值同时给出正算全套结果与自洽性比对明细。
    """
    x1 = p["x2"] + (p["y1"] - y2) / q
    forward_inputs = {
        "G": p["G"],
        "L": q * p["G"],
        "Kya": p["Kya"],
        "a": p["a"],
        "S": p["S"],
        "y1": p["y1"],
        "y2": y2,
        "x1": x1,
        "x2": p["x2"],
        "m": p["m"],
    }
    result = engine.run_calculation(forward_inputs)
    residual = balance.balance_residual(forward_inputs)
    return {
        "forward_calculation": result,
        "mass_balance_residual": residual,
        "recomputed_x1": x1,
        "recomputed_L": forward_inputs["L"],
        "recomputed_y2": y2,
    }


# ------------------------------------------------- 诉求一：定分离，求吸收剂


def solve_flow_for_target(
    p: Mapping[str, float],
    y2_target: float,
    z_available: float,
    settings: Mapping[str, float],
) -> dict[str, Any]:
    """反求在不超过 ``z_available`` 下恰好达成 ``y2_target`` 的液相流量。

    解的是 ``Z(q) = z_available`` 的根（Z 对 q 严格单调下降）。
    """
    y1, x2, m = p["y1"], p["x2"], p["m"]
    h = hog(p)
    lim = limiting_outlet(p)
    y2_star = lim["y2_star"]
    tol = pinch_floor(p)

    # ---- 合法性 / 可行性前置判定（口径与正算夹点判定一致） ----
    if x2 >= 1.0 and y2_target < y1:
        raise CalculationError(
            ERR_TARGET_INFEASIBLE,
            f"进塔吸收剂已饱和（x2={x2:g}），液相不再具备接纳溶质的容量"
            "（x1 无法超过 x2=1），任何非零脱除都无法达成。",
            details={"kind": "solvent_saturated", "x2": x2},
        )
    if y2_target > y1:
        raise CalculationError(
            ERR_TARGET_INFEASIBLE,
            f"目标塔顶分率 y2={y2_target:.12g} 高于塔底进气分率 y1={y1:.12g}，"
            "本服务只核算吸收（y2≤y1）。",
            details={"kind": "target_above_inlet", "y2_target": y2_target, "y1": y1},
        )
    if (y1 - m * x2) <= tol:
        raise CalculationError(
            ERR_TARGET_INFEASIBLE,
            f"进气本身已位于平衡线上或其下方（Δ=y1-m·x2={y1 - m * x2:.12g} "
            f"≤ 夹点容差 {tol:.3g}），没有任何吸收推动力，任何出口分率都无法达成。",
            details={
                "kind": "inlet_at_equilibrium",
                "inlet_driving_force": y1 - m * x2,
                "pinch_tolerance": tol,
            },
        )
    if y2_target <= y2_star + tol:
        gap = (y2_star + tol) - y2_target
        raise CalculationError(
            ERR_TARGET_INFEASIBLE,
            f"目标出口分率 y2={y2_target:.12g} 已达到或越过理论极限 "
            f"y2*=m·x2={y2_star:.12g}（计入夹点容差 {tol:.3g} 后的可达下限 "
            f"为 {y2_star + tol:.12g}）：越接近该极限所需吸收剂趋于无穷、"
            f"塔高发散（夹点）。目标比可达下限还深 {gap:.3g}，无有限操作点。",
            details={
                "kind": "target_below_equilibrium_limit",
                "y2_target": y2_target,
                "y2_star": y2_star,
                "reachable_floor": y2_star + tol,
                "shortfall": max(0.0, gap),
                "pinch_tolerance": tol,
            },
        )

    # ---- 合法退化：y2=y1（零脱除），任何 L 下 NOG=Z=0 ----
    if y2_target == y1:
        q = settings["q_floor"]
        if z_available < 0.0:
            raise CalculationError(
                ERR_TARGET_INFEASIBLE,
                f"可用填料层高度 z_available={z_available:.12g} 为负，不构成可行塔。",
                details={"kind": "negative_height", "z_available": z_available},
            )
        verification = verify_with_forward(p, q, y2_target)
        return _flow_solution_payload(
            p, y2_target, z_available, q, verification,
            root=None, degenerate=True, note=(
                "目标出口分率等于进气分率（零脱除）的合法退化：NOG=Z=0，"
                f"任意液相流量都满足，按液气比数值下限 q={q:g} 返回最小有限解，"
                "不再无谓加码吸收剂。"
            ),
        )

    # ---- 理论最小塔高（L→∞ 极限）：可用高度连它都不够则不可行 ----
    z_min = lim["Z_min_at"](y2_target)
    n_min = lim["N_at"](y2_target)
    if z_available < z_min:
        raise CalculationError(
            ERR_TARGET_INFEASIBLE,
            f"即使给入无穷多吸收剂（L→∞），达成 y2={y2_target:.12g} 也至少需要 "
            f"填料层高度 Z_min={z_min:.12g} m（NOG≥{n_min:.6g}，HOG={h:.6g}）；"
            f"给定可用高度仅 {z_available:.12g} m，还差 {z_min - z_available:.12g} m，"
            "目标不可行。需放宽出口指标或提高 / 更换传质性能更强的填料。",
            details={
                "kind": "height_below_minimum",
                "y2_target": y2_target,
                "z_min": z_min,
                "z_available": z_available,
                "height_shortfall": z_min - z_available,
                "N_min": n_min,
                "HOG": h,
            },
        )

    # ---- m=0：NOG=ln(y1/y2) 与 L 无关，Z 对所有可行 q 恒为 Z_min ----
    if m <= 0.0:
        q = max(minimum_q(p, y2_target), settings["q_floor"])
        verification = verify_with_forward(p, q, y2_target)
        note = (
            "平衡线斜率 m=0（平衡分率恒为零），NOG 与液相流量无关，"
            f"任何可行吸收剂用量下塔高都恒为 Z_min={z_min:.12g} m；"
            f"按物理最小液气比 q=(y1-y2)/(1-x2)={q:g}（保证 x1≤1）"
            "返回最小有限解。"
        )
        if z_available > z_min:
            note += (
                f" 给定可用高度 {z_available:.12g} m 高于该定值，富余的塔高不改变"
                "出口分率（衡算把 y2 钉死），不存在用更小流量换取塔高的余地。"
            )
        return _flow_solution_payload(
            p, y2_target, z_available, q, verification,
            root=None, degenerate=True, note=note,
        )

    # ---- 下界点：物理最小流量已能在可用高度内达成 -> 直接返回偏小的解 ----
    q_lo = max(minimum_q(p, y2_target), settings["q_floor"])
    pt_lo = operating_point(p, q_lo, y2_target)
    f_lo = pt_lo.z - z_available if pt_lo.finite else math.inf
    if math.isfinite(f_lo) and f_lo <= 0.0:
        verification = verify_with_forward(p, q_lo, y2_target)
        return _flow_solution_payload(
            p, y2_target, z_available, q_lo, verification,
            root=RootResult(q_lo, 0, q_lo, q_lo, f_lo, f_lo),
            degenerate=False,
            note=(
                "目标松到物理最小液气比（操作线塔底端贴平衡线 / 液相出口接近"
                "饱和的边界）下所需塔高已不超过可用高度：老实返回该偏小的解，"
                "不再无谓加大吸收剂。"
            ),
        )

    # ---- 夹逼 q：上界几何放大直到 Z(q_hi) < z_available ----
    q_hi = max(q_lo * 2.0, settings["q_floor"])
    f_hi = operating_point(p, q_hi, y2_target).z - z_available

    def _exhausted(_lo: float, hi: float) -> CalculationError:
        return CalculationError(
            ERR_TARGET_INFEASIBLE,
            f"液气比放大到数值上限 q={hi:g}（视为无穷吸收剂）仍无法在 "
            f"{z_available:.12g} m 内达成 y2={y2_target:.12g}："
            f"理论最小塔高 Z_min={z_min:.12g} m。",
            details={
                "kind": "height_below_minimum",
                "y2_target": y2_target,
                "z_min": z_min,
                "z_available": z_available,
                "height_shortfall": max(0.0, z_min - z_available),
            },
        )

    if not math.isfinite(f_hi) or f_hi > 0.0:
        q_hi, f_hi = _expand_bracket_high(
            lambda qq: operating_point(p, qq, y2_target).z - z_available,
            q_lo, q_hi, f_lo, ceil=settings["q_ceil"], reason_when_exhausted=_exhausted,
        )

    root = _bisect(
        lambda qq: operating_point(p, qq, y2_target).z - z_available,
        q_lo, q_hi, f_lo=f_lo, f_hi=f_hi,
        x_abs_tol=settings["x_tol_abs"], x_rel_tol=settings["x_tol_rel"],
        max_iter=int(settings["max_iter"]),
    )
    q_star = root.root
    verification = verify_with_forward(p, q_star, y2_target)
    return _flow_solution_payload(
        p, y2_target, z_available, q_star, verification,
        root=root, degenerate=False,
    )


def _flow_solution_payload(
    p: Mapping[str, float],
    y2_target: float,
    z_available: float,
    q: float,
    verification: Mapping[str, Any],
    *,
    root: RootResult | None,
    degenerate: bool,
    note: str | None = None,
) -> dict[str, Any]:
    """组装诉求一的结构化结果（含理论边界、求解过程、正算复算）。"""
    lim = limiting_outlet(p)
    z_min = lim["Z_min_at"](y2_target) if y2_target > lim["y2_star"] else math.inf
    return {
        "mode": "flow_for_target",
        "target": {
            "y2": y2_target,
            "removal_pct": 100.0 * (p["y1"] - y2_target) / p["y1"] if p["y1"] > 0 else None,
            "z_available": z_available,
        },
        "solution": {
            "L": q * p["G"],
            "L_over_G": q,
            "x1": verification["recomputed_x1"],
        },
        "feasibility_limits": {
            "y2_star": lim["y2_star"],
            "minimum_L_over_G": minimum_q(p, y2_target) if not degenerate else None,
            "z_min_at_target": z_min,
        },
        "solver": {
            "method": "bisection" if root is not None else "closed_degenerate",
            "iterations": root.iterations if root is not None else 0,
            "converged": True,
            "bracket": (
                [root.bracket_low, root.bracket_high] if root is not None else [q, q]
            ),
        },
        "degenerate": degenerate,
        "note": note,
        "verification": verification,
    }


# --------------------------------------------- 诉求二：定塔高，求可行最优操作


def solve_outlet_for_height(
    p: Mapping[str, float],
    z_max: float,
    settings: Mapping[str, float],
    *,
    fixed_q: float | None = None,
    optimality_gap_pct: float,
) -> dict[str, Any]:
    """在填料层高度不超过 ``z_max`` 的约束下求最优（最深）出口分率。

    * 不给 ``fixed_q``：理论最优为 ``L→∞`` 时的极限 ``y2_best``（仅在
      无穷吸收剂下取得）；服务另给一个落在有限流量上的**实操操作点**
      ``y2_practical = y2_best·(1+gap)``（``gap`` 默认 1%，可配），它恰好
      把塔高用满，配套有限 ``L``，两条结果都用正算复算。
    * 给了 ``fixed_q``：吸收剂用量已定，求该用量下用满塔高能达到的 y2；
      若该 q 下即使夹点也用不满高度（λ≥1 时 N 有上界），明确判不可行。
    """
    y1, x2, m = p["y1"], p["x2"], p["m"]
    h = hog(p)
    n_max = z_max / h
    tol = pinch_floor(p)

    if z_max <= 0.0:
        raise CalculationError(
            ERR_TARGET_INFEASIBLE,
            f"填料层高度上限 z_max={z_max:.12g} 非正，不构成可行塔。",
            details={"kind": "nonpositive_height", "z_max": z_max},
        )
    if (y1 - m * x2) <= tol:
        raise CalculationError(
            ERR_TARGET_INFEASIBLE,
            f"进气本身已位于平衡线上或其下方（Δ=y1-m·x2={y1 - m * x2:.12g} "
            f"≤ 夹点容差 {tol:.3g}），没有吸收推动力，任何正塔高都不产生分离。",
            details={
                "kind": "inlet_at_equilibrium",
                "inlet_driving_force": y1 - m * x2,
                "pinch_tolerance": tol,
            },
        )
    if x2 >= 1.0:
        raise CalculationError(
            ERR_TARGET_INFEASIBLE,
            f"进塔吸收剂已饱和（x2={x2:g}），液相不再具备接纳溶质的容量。",
            details={"kind": "solvent_saturated", "x2": x2},
        )

    y2_star = m * x2
    a_ratio = (y1 - y2_star)

    if fixed_q is not None:
        return _solve_outlet_fixed_q(
            p, fixed_q, z_max, n_max, settings,
            y2_star=y2_star, a_ratio=a_ratio, tol=tol,
        )

    # ---- 理论最优：L→∞（λ→0），N=ln[A/(y2-mx2)] -> y2_best ----
    b_best = a_ratio * math.exp(-n_max)
    y2_best = y2_star + b_best
    best_is_floor_limited = b_best <= tol

    # ---- 实操点：在理论最优上回退 optimality_gap_pct（有限 L，用满塔高） ----
    gap = optimality_gap_pct / 100.0
    if gap < 0.0:
        raise CalculationError(
            "INVALID_SOLVE_REQUEST",
            f"optimality_gap_pct 不能为负，收到 {optimality_gap_pct:g}。",
            http_status=400,
        )
    if gap == 0.0 and fixed_q is None:
        raise CalculationError(
            "INVALID_SOLVE_REQUEST",
            "自由流量模式下 optimality_gap_pct=0 表示实操点严格取理论最优出口分率，"
            "而该点只在 L→∞（无穷吸收剂）极限下取得，不存在有限操作点。"
            "请给一个正的最优性回退余量（默认 1%）以获得有限 L 的实操方案；"
            "理论最优值本身已在 theoretical_best 中给出。",
            http_status=400,
        )
    b_prac = b_best * (1.0 + gap)
    if b_prac <= tol:
        # gap 极小把实操点也推进了容差带：垫到容差地板之上，保证正算可复算
        b_prac = tol * (1.0 + max(config.LM_RATIO_TOL, 1e-6))
        floor_clamped = True
    else:
        floor_clamped = False
    y2_prac = y2_star + b_prac

    note = None
    if best_is_floor_limited:
        # 塔高富余到理论最优已进入平衡线夹点容差带：最优只能报到容差地板
        # （口径与正算夹点判定一致），实操点同样夹到地板之上的可算位置。
        y2_best = y2_star + tol
        y2_prac = y2_star + tol * (1.0 + max(config.LM_RATIO_TOL, 1e-6))
        b_prac = y2_prac - y2_star
        floor_clamped = True
        note = (
            f"给定塔高 {z_max:.12g} m（NOG={n_max:.6g}）富余到理论最优出口分率已"
            f"进入平衡线夹点容差带（L→∞ 极限值低于 {y2_star + tol:.3g}）；按正算"
            "夹点判定的同一容差口径，最优出口分率只报到可达下限，再深即夹点、"
            "没有有限操作点能兑现。"
        )

    root = None
    degenerate = False
    if m <= 0.0:
        # m=0：NOG 与 L 无关，Z(q) 恒等于 Z(y2)。实操点 y2_prac 所要求的
        # 塔高就是 HOG·ln(y1/y2_prac) ≤ z_max（由 b_prac≥b_best 保证），
        # 任何满足 x1≤1 的有限 q 都能达到；取物理最小液气比，不做无意义的求根。
        z_prac = h * math.log(y1 / y2_prac) if y2_prac < y1 else 0.0
        if z_prac > z_max and not floor_clamped:
            # 理论上不会发生（构造保证），作为防御性兜底转不可行
            raise CalculationError(
                ERR_TARGET_INFEASIBLE,
                f"m=0 下实操出口分率 {y2_prac:.12g} 需要塔高 {z_prac:.12g} m，"
                f"超过给定上限 {z_max:.12g} m。",
                details={"kind": "height_below_minimum", "y2_target": y2_prac, "z_max": z_max},
            )
        q_star = max(minimum_q(p, y2_prac), settings["q_floor"]) if y2_prac < y1 else settings["q_floor"]
        if y2_prac == y1:
            degenerate = True
    else:
        q_lo = max(minimum_q(p, y2_prac), settings["q_floor"])
        f_lo_pt = operating_point(p, q_lo, y2_prac)
        f_lo = f_lo_pt.z - z_max if f_lo_pt.finite else math.inf
        # 物理最小流量已够用（目标松 / 塔高富余）：直接返回偏小的解
        if math.isfinite(f_lo) and f_lo <= 0.0:
            q_star = q_lo
            root = RootResult(q_lo, 0, q_lo, q_lo, f_lo, f_lo)
        else:
            q_hi = max(q_lo * 2.0, settings["q_floor"])

            def _exhausted(_lo: float, hi: float) -> CalculationError:
                return CalculationError(
                    ERR_TARGET_INFEASIBLE,
                    f"液气比放大到数值上限 q={hi:g} 仍无法在 {z_max:.12g} m 内"
                    f"趋近理论最优（目标实操出口分率 {y2_prac:.12g}）。",
                    details={"kind": "height_below_minimum", "y2_target": y2_prac, "z_max": z_max},
                )

            q_hi, f_hi = _expand_bracket_high(
                lambda qq: operating_point(p, qq, y2_prac).z - z_max,
                q_lo, q_hi, f_lo, ceil=settings["q_ceil"], reason_when_exhausted=_exhausted,
            )
            root = _bisect(
                lambda qq: operating_point(p, qq, y2_prac).z - z_max,
                q_lo, q_hi, f_lo=f_lo, f_hi=f_hi,
                x_abs_tol=settings["x_tol_abs"], x_rel_tol=settings["x_tol_rel"],
                max_iter=int(settings["max_iter"]),
            )
            q_star = root.root

    verification = verify_with_forward(p, q_star, y2_prac)
    z_at_best = (
        h * math.log(a_ratio / (y2_best - y2_star))
        if y2_best > y2_star and not best_is_floor_limited
        else z_max
    )
    return {
        "mode": "outlet_for_height",
        "constraint": {
            "z_max": z_max,
            "NOG_max": n_max,
            "HOG": h,
        },
        "theoretical_best": {
            "y2": y2_best,
            "removal_pct": 100.0 * (y1 - y2_best) / y1 if y1 > 0 else None,
            "L_over_G": math.inf,
            "L": math.inf,
            "z_required": z_at_best,
            "attainable_note": (
                "理论最优出口分率在 L→∞（无穷吸收剂）极限下取得，有限流量只能"
                "逼近、不能严格达到；下列 practical 操作点是落在有限 L 上、"
                f"按 {optimality_gap_pct:g}% 最优性回退的可实施方案。"
            ),
        },
        "practical": {
            "y2": y2_prac,
            "removal_pct": 100.0 * (y1 - y2_prac) / y1 if y1 > 0 else None,
            "L": q_star * p["G"],
            "L_over_G": q_star,
            "x1": verification["recomputed_x1"],
            "z_required": verification["forward_calculation"]["Z"],
            "optimality_gap_pct": optimality_gap_pct,
            "floor_clamped": floor_clamped,
        },
        "fixed_L_over_G": None,
        "feasibility_limits": {
            "y2_star": y2_star,
            "minimum_L_over_G_at_practical": minimum_q(p, y2_prac) if y2_prac < y1 else 0.0,
        },
        "solver": {
            "method": "bisection" if root is not None else "closed_degenerate",
            "iterations": root.iterations if root is not None else 0,
            "converged": True,
            "bracket": [root.bracket_low, root.bracket_high] if root is not None else [q_star, q_star],
        },
        "degenerate": degenerate,
        "note": note,
        "verification": verification,
    }


def _solve_outlet_fixed_q(
    p: Mapping[str, float],
    q: float,
    z_max: float,
    n_max: float,
    settings: Mapping[str, float],
    *,
    y2_star: float,
    a_ratio: float,
    tol: float,
) -> dict[str, Any]:
    """吸收剂用量已定为 q=L/G 时，求用满塔高能达到的最深 y2。"""
    m, y1, x2 = p["m"], p["y1"], p["x2"]
    h = hog(p)
    lam = m / q if q > 0.0 else math.inf

    # λ>1（q<m）：操作线比平衡线陡，N(y2) 在有限 B 处发散，存在 NOG 上界。
    # λ=1（平行支）时 NOG=Δy/Δ2，Δ2 可取到容差地板，上界即 A/tol，
    # 视为无有限上界——与正算内核的 constant_force 退化支口径一致。
    if lam > 1.0 + config.LAMBDA_FLAT_TOL:
        # N∞ = ln(λ)/(λ-1)（Δ2→0 极限，吸收因子式）
        n_cap = math.log(lam) / (lam - 1.0)
        y2_cap = y2_star
        if n_max > n_cap:
            raise CalculationError(
                ERR_TARGET_INFEASIBLE,
                f"固定液气比 L/G={q:.12g} 下脱吸因子 λ=mG/L={lam:.6g}>1，"
                f"操作线比平衡线陡，NOG 存在上界 {n_cap:.6g}"
                f"（对应塔高 {h * n_cap:.12g} m）。给定塔高 {z_max:.12g} m"
                f"（NOG={n_max:.6g}）已超过该上界：再增加塔高也不能继续深化分离，"
                "夹点先到来。需加大液相流量使 λ<1。",
                details={
                    "kind": "fixed_flow_lambda_gt_one",
                    "L_over_G": q,
                    "lambda": lam,
                    "NOG_cap": n_cap,
                    "z_cap": h * n_cap,
                    "z_max": z_max,
                    "best_y2_at_cap": y2_cap,
                },
            )

    # 夹逼 y2：下界为容差地板（夹点 / 饱和 -> Z=∞），上界取 y1（Z=0）。
    # 饱和域（x1>1）与夹点一样属于"需要无穷塔高"的非有限域，与正残差同侧，
    # 二分自动把根夹在 x1≤1 的物理可行区间内，无需预先剔除。
    y_lo = y2_star + tol
    y_hi = y1
    f_lo = math.inf
    f_hi = -z_max    # y2=y1 时 Z=0 ≤ z_max

    def residual(y2: float) -> float:
        pt = operating_point(p, q, y2)
        return pt.z - z_max if pt.finite else math.inf

    root = _bisect(
        residual, y_lo, y_hi, f_lo=f_lo, f_hi=f_hi,
        x_abs_tol=settings["x_tol_abs"], x_rel_tol=settings["x_tol_rel"],
        max_iter=int(settings["max_iter"]),
    )
    y2_star_pt = root.root
    verification = verify_with_forward(p, q, y2_star_pt)
    return {
        "mode": "outlet_for_height",
        "constraint": {
            "z_max": z_max,
            "NOG_max": n_max,
            "HOG": h,
        },
        "theoretical_best": {
            "y2": y2_star_pt,
            "removal_pct": 100.0 * (y1 - y2_star_pt) / y1 if y1 > 0 else None,
            "L_over_G": q,
            "L": q * p["G"],
            "z_required": verification["forward_calculation"]["Z"],
            "attainable_note": (
                f"吸收剂用量已固定为 L/G={q:g}（λ={lam:.6g}）；该值即此流量下"
                "用满给定塔高能达到的最深出口分率，为有限操作点。"
            ),
        },
        "practical": None,
        "fixed_L_over_G": q,
        "feasibility_limits": {
            "y2_star": y2_star,
            "minimum_L_over_G_at_practical": minimum_q(p, y2_star_pt),
        },
        "solver": {
            "method": "bisection",
            "iterations": root.iterations,
            "converged": True,
            "bracket": [root.bracket_low, root.bracket_high],
        },
        "degenerate": False,
        "verification": verification,
    }


# ------------------------------------------------------------- 请求级设置


def resolve_settings(raw: Mapping[str, Any]) -> dict[str, float]:
    """解析单条反解请求内的求解设置（缺省回落到 :mod:`app.config`）。"""
    s_raw = raw.get("solver_settings")
    if s_raw is None:
        s_raw = {}
    if not isinstance(s_raw, Mapping):
        raise CalculationError(
            "INVALID_SOLVE_REQUEST",
            "solver_settings 必须是对象。",
            http_status=400,
        )

    allowed = {
        "x_tolerance_rel": ("SOLVE_XTOL_REL", config.SOLVE_XTOL_REL),
        "x_tolerance_abs": ("SOLVE_XTOL_ABS", config.SOLVE_XTOL_ABS),
        "max_iterations": ("SOLVE_MAX_ITER", config.SOLVE_MAX_ITER),
        "L_over_G_floor": ("SOLVE_Q_FLOOR", config.SOLVE_Q_FLOOR),
        "L_over_G_ceiling": ("SOLVE_Q_CEIL", config.SOLVE_Q_CEIL),
    }
    out: dict[str, float] = {}
    for key, (_env, default) in allowed.items():
        if key in s_raw and s_raw[key] is not None:
            val = _as_float(key, s_raw[key], f"求解设置 {key}")
            if val <= 0.0:
                raise CalculationError(
                    "NON_POSITIVE_VALUE",
                    f"求解设置 {key} 必须为正，收到 {val:g}",
                    http_status=422,
                )
            out[_setting_internal(key)] = val
        else:
            out[_setting_internal(key)] = float(default)
    if not float(out["max_iter"]).is_integer():
        raise CalculationError(
            "INVALID_SOLVE_REQUEST",
            f"求解设置 max_iterations 必须为整数，收到 {s_raw.get('max_iterations')!r}",
            http_status=400,
        )
    if out["q_floor"] >= out["q_ceil"]:
        raise CalculationError(
            "INVALID_SOLVE_REQUEST",
            f"求解设置 L_over_G_floor={out['q_floor']:g} 必须小于 "
            f"L_over_G_ceiling={out['q_ceil']:g}。",
            http_status=400,
        )
    return out


def _setting_internal(key: str) -> str:
    return {
        "x_tolerance_rel": "x_tol_rel",
        "x_tolerance_abs": "x_tol_abs",
        "max_iterations": "max_iter",
        "L_over_G_floor": "q_floor",
        "L_over_G_ceiling": "q_ceil",
    }[key]

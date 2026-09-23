"""反解内核：给定分离目标（或塔高上限），单调求根反算吸收剂用量。

与正算共用同一套物理内核（:mod:`app.engine`），差别只在已知/未知量的摆法。

底座（已知）：``G, Kya, a, S, y1, x2, m``（塔几何、传质性能、进气、贫液、
平衡线）。液相流量 ``L``（等价于液气比 ``L/G``）是待解操作变量，塔底液相
分率恒由全塔衡算钉死：``x1 = x2 + (y1-y2)/(L/G)``，不允许调用方另给。

两种诉求：

* ``fixed_separation``（定分离）：钉死目标出口分率 ``y2``（直接给
  ``target_y2`` 或给 ``removal_pct`` 由服务换算）。此时 (L, Z) 是一条
  权衡曲线，必须再钉死一个量才唯一：现成塔高度 ``Z``（求根：恰好满足目标
  所需的最小 L）、设计倍率 ``design_factor``（``L/G = c·(L/G)_min``，
  教科书选型）、或直接给定 ``L`` / ``L_over_G``（由正算复算所需塔高）。
* ``fixed_height``（定塔高）：钉死 ``Z_max``。不给目标时，以"无穷吸收剂
极限出口分率"收窄一个可配置相对带作为可达最优点，求把填料层用尽所需的
最小 L；给目标时，先判定该目标在 Z_max 内是否可达，可达则同样求根。

单调性：固定 y2，``Z_required(L)`` 随 L 单调下降，``L→∞`` 时趋于有限
最小值 ``Z_min = HOG·ln[(y1-mx2)/(y2-mx2)]``，``L→(L/G)_min·G`` 时
趋于无穷（夹点）。据此用二分法稳定夹住解，不收敛绝不返回数值。

可行性边界口径与 :mod:`app.engine` 的夹点判定一致：推动力容差取
``PINCH_ATOL + PINCH_RTOL·max(|y1|,|y2|)``，目标分率一旦落进
``y2 ≤ mx2 + 容差`` 的夹点带即判不可行，不硬吐贴着夹点的巨大流量。
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from . import config, engine
from .errors import (
    ERR_FRACTION_RANGE,
    ERR_INVERT_SPEC,
    ERR_INVALID_SLOPE,
    ERR_INVALID_TARGET,
    ERR_MISSING_FIELD,
    ERR_NON_POSITIVE,
    ERR_NOT_A_NUMBER,
    ERR_PINCH,
    ERR_SOLVER_NO_CONVERGE,
    ERR_TARGET_UNREACHABLE,
    CalculationError,
)
from .validation import FIELD_LABELS

# 反解底座字段：塔几何 / 传质 / 进气 / 贫液 / 平衡线
BASE_FIELDS: tuple[str, ...] = ("G", "Kya", "a", "S", "y1", "x2", "m")
# 这三个量在反解中分别是"待求 / 目标 / 衡算导出"，不许由底座钉死
FORBIDDEN_FROM_BASE = ("L", "y2", "x1")

MODE_FIXED_SEPARATION = "fixed_separation"
MODE_FIXED_HEIGHT = "fixed_height"
_MODES = (MODE_FIXED_SEPARATION, MODE_FIXED_HEIGHT)

_CONTROL_LABELS = {
    "Z": "可用填料层高度 Z",
    "Z_max": "填料层高度上限 Z_max",
    "design_factor": "设计倍率 design_factor（L/G 相对最小液气比的倍数）",
    "L": "液相摩尔流量 L",
    "L_over_G": "液气比 L/G",
    "target_y2": "目标出口气相分率 target_y2",
    "removal_pct": "要求脱除百分比 removal_pct",
    "xtol": "收敛相对宽度 xtol",
    "max_iterations": "最大迭代次数 max_iterations",
    "upper_factor": "上界扩张倍数 upper_factor",
    "lg_floor": "液气比搜索地板 lg_floor",
    "limit_band": "极限收窄相对带 limit_band",
}

# 求根下界越过物理边界时，为端点推动力预留的容差安全倍数与最小相对步长
_LO_TOL_SAFETY = 10.0
_LO_MIN_REL_STEP = 1e-12
_LO_MAX_REL_STEP = 1e-8
_Z_BOUNDARY_RTOL = 1e-9
_Z_BOUNDARY_ATOL = 1e-12


# ---------------------------------------------------------------- 参数清洗
def _coerce(name: str, value: Any, positive: bool = False) -> float:
    label = _CONTROL_LABELS.get(name, FIELD_LABELS.get(name, name))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalculationError(
            ERR_NOT_A_NUMBER, f"{label}（{name}）必须是数值，收到 {value!r}", http_status=400
        )
    fv = float(value)
    if not math.isfinite(fv):
        raise CalculationError(
            ERR_NOT_A_NUMBER, f"{label}（{name}）必须是有限数值，收到 {value!r}", http_status=400
        )
    if positive and fv <= 0.0:
        raise CalculationError(ERR_NON_POSITIVE, f"{label}（{name}）必须为正，收到 {fv:.12g}")
    return fv


def _prepare_base(raw: Mapping[str, Any]) -> dict[str, float]:
    """抽取并校验反解底座参数。

    L / y2 / x1 不参与底座：前两者在反解中是未知量与目标，x1 由衡算推出。
    工况档里若带这三个字段，由编排层在合并时剔除，内联直给则在此明确拒绝。
    """
    for name in FORBIDDEN_FROM_BASE:
        if raw.get(name) is not None:
            raise CalculationError(
                ERR_INVERT_SPEC,
                f"反解时 {name} 不能钉死在底座里：液相流量 L 是待求操作变量（或作"
                " fixed_separation 下的控制量显式给出），出口分率由 target_y2/"
                "removal_pct 给出，塔底液相分率 x1 由物料衡算自动推出。",
                http_status=400,
            )

    missing = [f"{n}（{FIELD_LABELS[n]}）" for n in BASE_FIELDS if raw.get(n) is None]
    if missing:
        raise CalculationError(
            ERR_MISSING_FIELD, "以下反解底座参数缺失：" + "、".join(missing), http_status=400
        )

    base = {n: _coerce(n, raw[n]) for n in BASE_FIELDS}
    for name in ("G", "Kya", "a", "S"):
        if base[name] <= 0.0:
            raise CalculationError(
                ERR_NON_POSITIVE,
                f"{FIELD_LABELS[name]}（{name}）必须为正，收到 {base[name]:.12g}",
            )
    if base["m"] < 0.0:
        raise CalculationError(
            ERR_INVALID_SLOPE,
            f"亨利型平衡线斜率 m 必须非负（y*=m·x），收到 m={base['m']:.12g}",
        )
    for name in ("y1", "x2"):
        if not 0.0 <= base[name] <= 1.0:
            raise CalculationError(
                ERR_FRACTION_RANGE,
                f"{FIELD_LABELS[name]}（{name}）必须落在 [0,1]，收到 {base[name]:.12g}",
            )
    return base


def _solver_settings(raw: Mapping[str, Any]) -> dict[str, float]:
    s = {
        "xtol": config.INVERT_XTOL,
        "max_iterations": float(config.INVERT_MAX_ITER),
        "upper_factor": config.INVERT_UPPER_FACTOR,
        "lg_floor": config.INVERT_LG_FLOOR,
        "limit_band": config.INVERT_LIMIT_BAND,
    }
    for name in s:
        if raw.get(name) is not None:
            s[name] = _coerce(name, raw[name])
    if not 0.0 < s["xtol"] < 1.0:
        raise CalculationError(ERR_INVERT_SPEC, "xtol 必须落在 (0,1) 区间。", http_status=400)
    if s["max_iterations"] < 1.0 or s["max_iterations"] != int(s["max_iterations"]):
        raise CalculationError(
            ERR_INVERT_SPEC, "max_iterations 必须是不小于 1 的整数。", http_status=400
        )
    if s["upper_factor"] <= 1.0:
        raise CalculationError(ERR_INVERT_SPEC, "upper_factor 必须大于 1。", http_status=400)
    if s["lg_floor"] <= 0.0:
        raise CalculationError(ERR_INVERT_SPEC, "lg_floor 必须为正。", http_status=400)
    if not 0.0 < s["limit_band"] < 1.0:
        raise CalculationError(ERR_INVERT_SPEC, "limit_band 必须落在 (0,1) 区间。", http_status=400)
    s["max_iterations"] = int(s["max_iterations"])
    return s


# ---------------------------------------------------------------- 物理量
def _hog(base: Mapping[str, float]) -> float:
    return base["G"] / (base["Kya"] * base["a"] * base["S"])


def _pinch_tol(base: Mapping[str, float], y2: float) -> float:
    """与 engine.pinch_tolerance 同口径。"""
    return config.PINCH_ATOL + config.PINCH_RTOL * max(abs(base["y1"]), abs(y2))


def _lg_min_pinch(base: Mapping[str, float], y2: float) -> float | None:
    """夹点侧最小液气比：Δ1=0 时 L/G = m(y1-y2)/(y1-mx2)。"""
    a_top = base["y1"] - base["m"] * base["x2"]
    if base["m"] <= 0.0 or a_top <= 0.0:
        return None
    return base["m"] * (base["y1"] - y2) / a_top


def _lg_min_fraction(base: Mapping[str, float], y2: float) -> float | None:
    """液相分率侧：x1≤1 要求 L/G ≥ (y1-y2)/(1-x2)。"""
    denom = 1.0 - base["x2"]
    if denom <= 0.0:
        return None
    return (base["y1"] - y2) / denom


def _physical_min_lg(base: Mapping[str, float], y2: float) -> float | None:
    """两类物理下界取大；均不存在（m=0 且跨度为 0 等）返回 None。"""
    bounds = [b for b in (_lg_min_pinch(base, y2), _lg_min_fraction(base, y2)) if b is not None]
    return max(bounds) if bounds else None


def _params_at(base: Mapping[str, float], y2: float, lg: float) -> dict[str, float]:
    """在给定液气比下拼出一组自洽的完整正算参数（x1 由衡算推出）。"""
    x1 = base["x2"] + (base["y1"] - y2) / lg
    return {
        "G": base["G"],
        "L": base["G"] * lg,
        "Kya": base["Kya"],
        "a": base["a"],
        "S": base["S"],
        "y1": base["y1"],
        "y2": y2,
        "x1": x1,
        "x2": base["x2"],
        "m": base["m"],
    }


def _z_required(base: Mapping[str, float], y2: float, lg: float) -> float:
    """正算内核复算给定 L/G 下达成 y2 所需的塔高；夹点按 +∞ 计。"""
    try:
        return engine.run_calculation(_params_at(base, y2, lg))["Z"]
    except CalculationError as exc:
        if exc.code == ERR_PINCH:
            return math.inf
        raise


def _z_min_infinite(base: Mapping[str, float], y2: float) -> float:
    """无穷吸收剂（L/G→∞，λ→0）下的理论最小塔高。"""
    top_num = base["y1"] - base["m"] * base["x2"]
    top_den = y2 - base["m"] * base["x2"]
    return _hog(base) * math.log(top_num / top_den)


def _lower_bracket(base: Mapping[str, float], y2: float, lg_floor: float) -> float:
    """求根下界：紧贴物理最小液气比之外，保证端点推动力留出夹点容差。"""
    bound = _physical_min_lg(base, y2)
    if bound is None or bound <= 0.0:
        return lg_floor
    a_top = base["y1"] - base["m"] * base["x2"]
    tol = _pinch_tol(base, y2)
    # Δ1(lg)=A(1-lg*/lg)，要求 Δ1 ≥ 安全倍数×容差 -> 相对步长 k·tol/A
    rel = _LO_TOL_SAFETY * tol / a_top if a_top > 0.0 else 0.0
    rel = min(_LO_MAX_REL_STEP, max(_LO_MIN_REL_STEP, rel))
    return max(bound / (1.0 - rel), lg_floor)


def _unreachable(base: Mapping[str, float], y2: float, reason: str) -> CalculationError:
    floor = base["m"] * base["x2"]
    gap = (floor + _pinch_tol(base, y2)) - y2
    msg = reason + (
        f"目标出口分率 y2={y2:.12g}；该底座无穷吸收剂下的理论极限为 "
        f"y2→mx2={floor:.12g}（按夹点容差放宽至 {floor + _pinch_tol(base, y2):.12g}），"
        f"目标比可达极限还深 {gap:.6g}。"
        "越靠近极限所需吸收剂趋于无穷、塔高发散（夹点），请放宽分离目标或降低贫液"
        "分率 x2 / 换用平衡线更平（m 更小）的吸收剂。"
    )
    return CalculationError(ERR_TARGET_UNREACHABLE, msg)


def _check_target_reachable(base: Mapping[str, float], y2: float) -> None:
    """目标可行性：与 engine 夹点判定同口径的边界检查。"""
    floor = base["m"] * base["x2"]
    if base["x2"] >= 1.0 and base["y1"] > y2:
        raise CalculationError(
            ERR_TARGET_UNREACHABLE,
            f"贫液入塔分率 x2={base['x2']:g} 已无吸收容量：要把气相从 y1={base['y1']:g} "
            f"净化到 y2={y2:.12g}，衡算要求 x1=x2+(y1-y2)/(L/G)>x2=1，液相分率越界，"
            "任何液气比都不可行。",
        )
    if floor >= base["y1"] and base["y1"] > y2:
        raise _unreachable(
            base, y2,
            f"贫液平衡分压 mx2={floor:.12g} 已不低于进气分率 y1={base['y1']:.12g}，"
            "全塔不存在正向气相推动力，任何吸收剂用量都无法产生净吸收。",
        )
    tol = _pinch_tol(base, y2)
    if y2 <= floor + tol:
        raise _unreachable(
            base, y2,
            "目标出口分率已落进（或越过）平衡线决定的夹点带 y2≤mx2+夹点容差。",
        )


def _resolve_target(raw: Mapping[str, Any], base: Mapping[str, float]) -> tuple[float, float | None]:
    """返回 (目标 y2, 脱除百分比)。两种给法互斥，由服务换算。"""
    target = raw.get("target_y2")
    removal = raw.get("removal_pct")
    if target is None and removal is None:
        raise CalculationError(
            ERR_INVALID_TARGET,
            "必须给出分离目标：target_y2（目标出口气相分率）或 removal_pct"
            "（要求脱除百分比，0-100），二者择一。",
            http_status=400,
        )
    if target is not None and removal is not None:
        raise CalculationError(
            ERR_INVALID_TARGET,
            "target_y2 与 removal_pct 只能给一个（两种目标写法等价，请勿同时指定）。",
            http_status=400,
        )
    if removal is not None:
        pct = _coerce("removal_pct", removal)
        if not 0.0 <= pct <= 100.0:
            raise CalculationError(
                ERR_INVALID_TARGET, f"removal_pct 必须落在 [0,100]，收到 {pct:g}", http_status=400
            )
        y2 = base["y1"] * (1.0 - pct / 100.0)
        return y2, pct
    y2 = _coerce("target_y2", target)
    if not 0.0 <= y2 <= 1.0:
        raise CalculationError(
            ERR_INVALID_TARGET, f"target_y2 必须落在 [0,1]，收到 {y2:g}", http_status=400
        )
    if y2 > base["y1"]:
        raise CalculationError(
            ERR_INVALID_TARGET,
            f"目标出口分率 y2={y2:g} 高于进气分率 y1={base['y1']:g}：本服务只反解吸收"
            "（y2≤y1），出口比入口浓属解吸工况，不在服务范围内。",
            http_status=400,
        )
    return y2, (1.0 - y2 / base["y1"]) * 100.0 if base["y1"] > 0.0 else 0.0


# ---------------------------------------------------------------- 求根
def _bisect(
    base: Mapping[str, float],
    y2: float,
    z_avail: float,
    settings: Mapping[str, float],
) -> dict[str, Any]:
    """二分求根：Z_required(lg) = z_avail。

    Z_required 随 lg 单调下降，求根区间下界在物理最小液气比外侧（f>0，
    即"塔不够用"），上界扩张到 f≤0（"塔够用"）后二分夹住。
    """
    lo = _lower_bracket(base, y2, settings["lg_floor"])
    f_lo = _z_required(base, y2, lo) - z_avail
    if f_lo <= 0.0:
        # 现成塔高在最小可行液气比下就已富余：解贴在物理下边界，不加码
        return {
            "lg": lo,
            "iterations": 0,
            "converged": True,
            "bracket": [lo, lo],
            "method": "boundary_minimum_lg",
            "note": "可用塔高在最小可行液气比下已满足目标，解取物理下界，未再加大吸收剂。",
        }

    hi = lo * settings["upper_factor"]
    f_hi = _z_required(base, y2, hi) - z_avail
    expansions = 0
    while f_hi > 0.0:
        hi *= 2.0
        try:
            f_hi = _z_required(base, y2, hi) - z_avail
        except CalculationError:
            raise
        expansions += 1
        if expansions > 100:
            raise CalculationError(
                ERR_TARGET_UNREACHABLE,
                f"液气比放大到 L/G={hi:.6g}（为最小液气比的巨额倍数）时所需塔高仍超过"
                f"可用值 {z_avail:.6g}，与理论最小塔高判定矛盾，请检查目标与塔高。",
            )

    mid = 0.5 * (lo + hi)
    iterations = 0
    for iterations in range(1, settings["max_iterations"] + 1):
        mid = 0.5 * (lo + hi)
        f_mid = _z_required(base, y2, mid) - z_avail
        if f_mid > 0.0:
            lo = mid
        else:
            hi = mid
        if (hi - lo) / hi <= settings["xtol"]:
            return {
                "lg": 0.5 * (lo + hi),
                "iterations": iterations,
                "converged": True,
                "bracket": [lo, hi],
                "method": "bisection",
                "note": None,
            }
    raise CalculationError(
        ERR_SOLVER_NO_CONVERGE,
        f"二分迭代 {iterations} 次后区间相对宽度 {(hi - lo) / hi:.6g} 仍未收窄到 xtol="
        f"{settings['xtol']:g}：末次区间 L/G∈[{lo:.12g},{hi:.12g}]，"
        f"对应所需塔高∈[{_z_required(base, y2, lo):.6g},"
        f"{_z_required(base, y2, hi):.6g}]，目标所需塔高 {z_avail:.6g}。拒绝返回未收敛"
        "结果，请放宽 xtol 或提高 max_iterations 后重试。",
    )


def _check_height_vs_minimum(base, y2, z_avail, z_min, what: str) -> None:
    """Z_avail 与理论最小塔高的边界判定；恰好等于最小值按夹点口径判不可行。"""
    ztol = _Z_BOUNDARY_ATOL + _Z_BOUNDARY_RTOL * max(abs(z_min), 1.0)
    if z_avail < z_min - ztol:
        raise CalculationError(
            ERR_TARGET_UNREACHABLE,
            f"达成出口分率 y2={y2:.12g} 的理论最小填料层高度（无穷吸收剂极限）为 "
            f"Z_min={z_min:.12g} m，而{what} Z={z_avail:.12g} m 还差 "
            f"{z_min - z_avail:.6g} m；再多吸收剂也省不掉这部分塔高。请放宽分离目标"
            "或提高允许塔高。",
        )
    if z_avail <= z_min + ztol:
        raise CalculationError(
            ERR_TARGET_UNREACHABLE,
            f"给定塔高 Z={z_avail:.12g} m 恰好落在理论最小值 Z_min={z_min:.12g} m 上"
            "（容差内）：该极限只有 L/G→∞ 才能逼近，任何有限吸收剂用量都需要更高的塔。"
            "与夹点判定口径一致，此处判目标不可行，请放宽目标或提高塔高。",
        )


# ---------------------------------------------------------------- 结果装配
def _solution_record(
    base, y2, removal, lg, verification, fixed, solver_info, warnings, *,
    degenerate=False, extra_limits=None,
) -> dict[str, Any]:
    lg_phys = _physical_min_lg(base, y2)
    limits = {
        "infinite_solvent_y2": base["m"] * base["x2"],
        "minimum_L_over_G": lg_phys,
        "minimum_Z": _z_min_infinite(base, y2) if y2 > base["m"] * base["x2"] else None,
    }
    if extra_limits:
        limits.update(extra_limits)
    return {
        "mode": solver_info.pop("__mode__"),
        "feasible": True,
        "degenerate": degenerate,
        "target": {"y2": y2, "removal_pct": removal},
        "solution": {
            "L": base["G"] * lg,
            "L_over_G": lg,
            "x1": base["x2"] + (base["y1"] - y2) / lg,
            "y2": y2,
        },
        "fixed": fixed,
        "limits": limits,
        "solver": solver_info,
        "warnings": warnings,
        "verification": verification,
    }


def _verify(base, y2, lg) -> dict[str, Any]:
    """在解点上用正算内核完整复算一遍——反解/正算自洽的唯一出口。"""
    return engine.run_calculation(_params_at(base, y2, lg))


def _degenerate_solution(base, y2, lg_floor: float, mode: str, fixed: dict,
                         warnings: list[str], *, explicit_lg: float | None = None,
                         removal: float | None = None) -> dict[str, Any]:
    """y2==y1（零脱除）的合法退化：NOG=Z=0，L 不唯一。"""
    if y2 != base["y1"]:
        removal = removal if removal is not None else 0.0
    else:
        removal = 0.0
    lg = explicit_lg
    if lg is None:
        lg = lg_floor
        warnings.append(
            "目标脱除率为零（y2=y1），无净传质，任何正的液相流量都满足要求；"
            f"L 不唯一，此处返回搜索地板 L/G={lg:g}，未无谓加码。"
        )
    verification = _verify(base, y2, lg)
    info = {"__mode__": mode, "method": "no_transfer", "iterations": 0,
            "converged": True, "bracket": [lg, lg]}
    return _solution_record(base, y2, removal, lg, verification, fixed, info, warnings,
                            degenerate=True)



# ---------------------------------------------------------------- 主编排
def _explicit_lg(base, raw: Mapping[str, Any]) -> float | None:
    """定分离下调用方直给的液气比（L 或 L_over_G），用于退化点。"""
    if raw.get("L_over_G") is not None:
        return _coerce("L_over_G", raw["L_over_G"], positive=True)
    if raw.get("L") is not None:
        return _coerce("L", raw["L"], positive=True) / base["G"]
    return None


def run_inversion(raw: Mapping[str, Any]) -> dict[str, Any]:
    """执行一次反解。目标不可行 / 规格不全 / 迭代未收敛均抛
    :class:`~app.errors.CalculationError`，由调用方转 HTTP / 批量条目。"""
    mode = raw.get("mode") or MODE_FIXED_SEPARATION
    if mode not in _MODES:
        raise CalculationError(
            ERR_INVERT_SPEC,
            f"未知反解模式 mode={mode!r}，支持：{MODE_FIXED_SEPARATION}（定分离）、"
            f"{MODE_FIXED_HEIGHT}（定塔高）。",
            http_status=400,
        )
    base = _prepare_base(raw)
    settings = _solver_settings(raw)

    warnings: list[str] = []

    if mode == MODE_FIXED_HEIGHT:
        return _run_fixed_height(base, settings, warnings, raw)

    # ---- 定分离 ----
    if raw.get("Z_max") is not None:
        raise CalculationError(
            ERR_INVERT_SPEC, "Z_max 只在 fixed_height 模式下使用；定分离请用 Z（可用塔高）。",
            http_status=400,
        )
    y2, removal = _resolve_target(raw, base)

    fixed_specs = [n for n in ("Z", "design_factor", "L", "L_over_G")
                   if raw.get(n) is not None]
    if len(fixed_specs) > 1:
        raise CalculationError(
            ERR_INVERT_SPEC,
            "定分离时 Z / design_factor / L / L_over_G 只能钉死一个（它们是同一条 "
            "L-Z 权衡曲线上的不同选点），收到：" + "、".join(fixed_specs) + "。",
            http_status=400,
        )

    if y2 == base["y1"]:
        return _degenerate_solution(
            base, y2, settings["lg_floor"], mode, {"kind": "none"}, warnings,
            explicit_lg=_explicit_lg(base, raw),
        )

    _check_target_reachable(base, y2)
    z_min = _z_min_infinite(base, y2)
    lg_phys = _physical_min_lg(base, y2)

    # 子类 1a：现成塔高度 Z 已知 -> 求根最小 L
    if raw.get("Z") is not None:
        z_avail = _coerce("Z", raw["Z"], positive=True)
        _check_height_vs_minimum(base, y2, z_avail, z_min, "可用塔高")
        root = _bisect(base, y2, z_avail, settings)
        lg = root["lg"]
        if root.get("note"):
            warnings.append(root["note"])
        verification = _verify(base, y2, lg)
        info = {"__mode__": mode, "method": root["method"], "iterations": root["iterations"],
                "converged": root["converged"], "bracket": root["bracket"],
                "xtol": settings["xtol"], "max_iterations": settings["max_iterations"]}
        return _solution_record(
            base, y2, removal, lg, verification, {"kind": "available_height", "Z": z_avail},
            info, warnings,
            extra_limits={"height_residual_at_solution": verification["Z"] - z_avail},
        )

    # 子类 1b：设计倍率 L/G = c·(L/G)_min（教科书选型，闭式，不必迭代）
    if raw.get("design_factor") is not None:
        factor = _coerce("design_factor", raw["design_factor"])
        if factor <= 0.0:
            raise CalculationError(
                ERR_INVALID_TARGET, "design_factor 必须为正数。", http_status=400
            )
        # 设计倍率相对经典"夹点侧"最小液气比定义；m=0 时该极限不存在
        lg_pin = _lg_min_pinch(base, y2)
        if lg_pin is None:
            raise CalculationError(
                ERR_INVERT_SPEC,
                "该底座（如 m=0，或贫液平衡分压已不低于进气）不存在有限的夹点侧最小液气比，"
                "design_factor 选型无定义；请改用 Z（给定塔高求根）或 L / L_over_G 直给。",
                http_status=400,
            )
        lg = factor * lg_pin  # factor≤1 时由正算内核按推动力非正判夹点
        verification = _verify(base, y2, lg)
        info = {"__mode__": mode, "method": "design_factor", "iterations": 0,
                "converged": True, "bracket": [lg, lg], "design_factor": factor}
        return _solution_record(
            base, y2, removal, lg, verification,
            {"kind": "design_factor", "design_factor": factor}, info, warnings,
        )

    # 子类 1c：L 或 L/G 直接给定 -> 正算复算所需塔高（无需迭代）
    if raw.get("L") is not None or raw.get("L_over_G") is not None:
        if raw.get("L_over_G") is not None:
            lg = _coerce("L_over_G", raw["L_over_G"], positive=True)
            fixed = {"kind": "L_over_G", "L_over_G": lg}
        else:
            lg = _coerce("L", raw["L"], positive=True) / base["G"]
            fixed = {"kind": "L", "L": base["G"] * lg}
        verification = _verify(base, y2, lg)  # 夹点由内核拒绝
        info = {"__mode__": mode, "method": "direct", "iterations": 0,
                "converged": True, "bracket": [lg, lg]}
        return _solution_record(base, y2, removal, lg, verification, fixed, info, warnings)

    raise CalculationError(
        ERR_INVERT_SPEC,
        "只给分离目标无法唯一确定吸收剂用量：对任意 L/G 大于最小液气比"
        f"（(L/G)_min={lg_phys if lg_phys is not None else '不存在'}）都能达到目标，"
        "区别只在塔高 Z 随 L 单调变化。请再钉死一个量：Z（现成塔可用高度，服务求根"
        "给出恰好够用的最小 L）、design_factor（按 c·(L/G)_min 选型）、或直接给 "
        "L / L_over_G（服务复算所需塔高）。",
        http_status=400,
    )


def _run_fixed_height(base, settings, warnings: list[str], raw: Mapping[str, Any]) -> dict[str, Any]:
    """定塔高：求不超过 Z_max 时能达到的最好出口分率与对应液相流量。"""
    for name in ("Z", "design_factor", "L", "L_over_G"):
        if raw.get(name) is not None:
            raise CalculationError(
                ERR_INVERT_SPEC,
                "fixed_height 模式下 Z / design_factor / L / L_over_G 均不能作为输入："
                "塔高由 Z_max 钉死，液相流量正是待求未知量。",
                http_status=400,
            )
    if raw.get("Z_max") is None:
        raise CalculationError(
            ERR_INVERT_SPEC, "fixed_height 模式必须给出填料层高度上限 Z_max。", http_status=400
        )
    z_max = _coerce("Z_max", raw["Z_max"], positive=True)
    hog = _hog(base)
    n_max = z_max / hog
    a_top = base["y1"] - base["m"] * base["x2"]

    auto_target = raw.get("target_y2") is None and raw.get("removal_pct") is None
    auto_info: dict[str, Any] = {}
    if auto_target:
        if a_top <= 0.0:
            raise CalculationError(
                ERR_TARGET_UNREACHABLE,
                f"贫液平衡分压 mx2={base['m'] * base['x2']:.12g} 不低于进气 y1="
                f"{base['y1']:.12g}，全塔无正向推动力，加高填料层也无法吸收。",
            )
        # 无穷吸收剂极限：y2_lim = mx2 + (y1-mx2)·exp(-Nmax)
        y_lim = base["m"] * base["x2"] + a_top * math.exp(-n_max)
        # 有限 L 时 NOG 恒大于 L→∞ 极限值，故固定塔高下有限 L 可达的出口
        # 分率必定比 y_lim 差（大）一点；目标取极限外侧（更宽松）一个相对带，
        # 于是存在有限 L 使 Z_required 恰好等于 Z_max，把整段填料用尽。
        y2 = y_lim + settings["limit_band"] * (base["y1"] - y_lim)
        tol = _pinch_tol(base, y2)
        if y2 - base["m"] * base["x2"] <= 10.0 * tol:
            # 极限本身已贴着夹点带：退到正算内核能分辨的最近可行出口并说明
            y2 = max(y2, base["m"] * base["x2"] + 10.0 * tol)
            warnings.append(
                f"无穷吸收剂极限出口 {y_lim:.12g} 距夹点带不足，已把可达目标放宽到 "
                f"y2={y2:.12g} 以保证有限液气比下正算可分辨；若需更贴近极限请调小"
                "夹点容差环境变量。"
            )
        removal = (1.0 - y2 / base["y1"]) * 100.0 if base["y1"] > 0.0 else 0.0
        auto_info = {
            "y2_best_infinite_solvent_limit": y_lim,
            "limit_band": settings["limit_band"],
            "outlet_gap_to_limit": y2 - y_lim,
        }
        warnings.append(
            f"未显式给分离目标：有限吸收剂下可达出口必定略差于无穷吸收剂极限 "
            f"{y_lim:.12g}，取其外侧相对带 {settings['limit_band']:g}（y2={y2:.12g}）"
            "作为可达最优点，求把填料层用尽所需的最小液相流量；limit_band 可在请求中"
            "调小以更贴近极限（趋近零时所需 L 发散）。"
        )
    else:
        y2, removal = _resolve_target(raw, base)

    if y2 == base["y1"]:
        return _degenerate_solution(
            base, y2, settings["lg_floor"], MODE_FIXED_HEIGHT,
            {"kind": "height_limit", "Z_max": z_max}, warnings, removal=removal,
        )

    _check_target_reachable(base, y2)
    z_min = _z_min_infinite(base, y2)
    _check_height_vs_minimum(base, y2, z_max, z_min, "塔高上限")

    root = _bisect(base, y2, z_max, settings)
    lg = root["lg"]
    if root.get("note"):
        warnings.append(root["note"])
    verification = _verify(base, y2, lg)
    info = {"__mode__": MODE_FIXED_HEIGHT, "method": root["method"],
            "iterations": root["iterations"], "converged": root["converged"],
            "bracket": root["bracket"], "xtol": settings["xtol"],
            "max_iterations": settings["max_iterations"]}
    fixed = {"kind": "height_limit", "Z_max": z_max,
             "Z_used": verification["Z"], "height_slack": z_max - verification["Z"]}
    fixed.update(auto_info)
    return _solution_record(
        base, y2, removal, lg, verification, fixed, info, warnings,
        extra_limits={"infinite_solvent_y2_at_Zmax": (
            base["m"] * base["x2"] + a_top * math.exp(-n_max)
        )},
    )

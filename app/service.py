"""核算编排：工况档解析 + 单条 / 批量执行。

核算本身（:func:`app.engine.run_calculation`）是纯函数，天然线程安全；
本层只负责把"点名工况档 + 临时覆盖参数"合并成完整参数集。批量执行时
逐条隔离，任何一条非法或夹点都只影响该条。

反解（:mod:`app.inverse`）走同一套合并与批量隔离口径。
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from . import engine, inverse
from .errors import ERR_SOLVE_BAD_REQUEST, CalculationError
from .profiles import ProfileStore
from .validation import ALL_FIELDS

# 反解基底允许从工况档继承 / 内联覆盖的字段（L、y2、x1 由反解决定，
# 即使工况档里带了也只作为底座，不参与反解已知量）
SOLVE_BASE_FIELDS = inverse.BASE_FIELDS


def _merge(store: ProfileStore, payload: dict[str, Any]) -> dict[str, Any]:
    """profile 参数打底，内联参数覆盖。"""
    merged: dict[str, Any] = {}
    profile_name = payload.get("profile")
    if profile_name is not None:
        merged.update(store.resolve(profile_name))
    for name in ALL_FIELDS:
        if payload.get(name) is not None:
            merged[name] = payload[name]
    return merged


def _merge_solve_base(store: ProfileStore, payload: dict[str, Any]) -> dict[str, Any]:
    """反解基底合并：只取 7 个已知量（profile 打底 + 内联覆盖）。"""
    merged: dict[str, Any] = {}
    profile_name = payload.get("profile")
    if profile_name is not None:
        profile_params = store.resolve(profile_name)
        for name in SOLVE_BASE_FIELDS:
            if profile_params.get(name) is not None:
                merged[name] = profile_params[name]
    for name in SOLVE_BASE_FIELDS:
        if payload.get(name) is not None:
            merged[name] = payload[name]
    return merged


def run_one(store: ProfileStore, payload: dict[str, Any]) -> dict[str, Any]:
    """单条核算。非法 / 夹点抛
    :class:`~app.errors.CalculationError`，由路由层转 HTTP 响应。"""
    return engine.run_calculation(_merge(store, payload))


def run_batch(store: ProfileStore, cases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量核算。逐条捕获领域错误，成功失败互不牵连。"""
    results: list[dict[str, Any]] = []
    for index, payload in enumerate(cases):
        item: dict[str, Any] = {"index": index}
        try:
            result = run_one(store, payload)
            item["ok"] = True
            item["result"] = result
        except Exception as exc:  # 单条任何失败都隔离在该条内
            item["ok"] = False
            item["error"] = _error_body(exc)
        results.append(item)
    return results


def _error_body(exc: Exception) -> dict[str, Any]:
    from .errors import CalculationError

    if isinstance(exc, CalculationError):
        body = {"code": exc.code, "message": exc.message}
        if exc.details:
            body["details"] = exc.details
        return body
    return {"code": "INTERNAL_ERROR", "message": f"核算时发生未预期错误：{exc}"}


# ---------------------------------------------------------------- 反解编排


def _require_number(name: str, value: Any, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalculationError(
            ERR_SOLVE_BAD_REQUEST, f"{name} 必须是数值，收到 {value!r}", http_status=400
        )
    fv = float(value)
    if not math.isfinite(fv):
        raise CalculationError(
            ERR_SOLVE_BAD_REQUEST, f"{name} 必须是有限数值，收到 {value!r}", http_status=400
        )
    if (fv <= 0.0) if positive else (fv < 0.0):
        relation = "必须为正" if positive else "不能为负"
        raise CalculationError(
            ERR_SOLVE_BAD_REQUEST, f"{name} {relation}，收到 {fv:g}", http_status=400
        )
    return fv


def _require_nonnegative(name: str, value: Any) -> float:
    return _require_number(name, value, positive=False)


def _resolve_target_y2(payload: dict[str, Any], y1: float) -> tuple[float, str]:
    """把目标出口分率 / 脱除百分比二选一换算成 y2，返回 (y2, 来源)。"""
    has_y2 = payload.get("target_y2") is not None
    has_pct = payload.get("removal_pct") is not None
    if has_y2 and has_pct:
        raise CalculationError(
            ERR_SOLVE_BAD_REQUEST,
            "target_y2 与 removal_pct 只能二选一（同时给出会产生歧义）。",
            http_status=400,
        )
    if not has_y2 and not has_pct:
        raise CalculationError(
            ERR_SOLVE_BAD_REQUEST,
            "必须给出分离目标：target_y2（目标塔顶气相分率）或 "
            "removal_pct（要求脱除的百分比，0-100），二者至少给一个。",
            http_status=400,
        )
    if has_pct:
        pct = payload["removal_pct"]
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            raise CalculationError(
                ERR_SOLVE_BAD_REQUEST, f"removal_pct 必须是数值，收到 {pct!r}", http_status=400
            )
        pct = float(pct)
        if not math.isfinite(pct) or not (0.0 <= pct <= 100.0):
            raise CalculationError(
                ERR_SOLVE_BAD_REQUEST,
                f"removal_pct 必须落在 [0,100]（百分比），收到 {pct:g}",
                http_status=400,
            )
        return y1 * (1.0 - pct / 100.0), "removal_pct"

    y2 = payload["target_y2"]
    if isinstance(y2, bool) or not isinstance(y2, (int, float)):
        raise CalculationError(
            ERR_SOLVE_BAD_REQUEST, f"target_y2 必须是数值，收到 {y2!r}", http_status=400
        )
    y2 = float(y2)
    if not math.isfinite(y2) or not (0.0 <= y2 <= 1.0):
        raise CalculationError(
            ERR_SOLVE_BAD_REQUEST,
            f"target_y2 必须是 [0,1] 内的有限摩尔分率，收到 {y2:g}",
            http_status=400,
        )
    return y2, "target_y2"


def run_solve_one(store: ProfileStore, payload: dict[str, Any]) -> dict[str, Any]:
    """单条反解。模式分派 + 基底校验 + 求根 + 正算复算（在内核中完成）。"""
    mode = payload.get("mode")
    if mode not in ("flow_for_target", "outlet_for_height"):
        raise CalculationError(
            ERR_SOLVE_BAD_REQUEST,
            "mode 必须是 'flow_for_target'（定分离求吸收剂用量）或 "
            "'outlet_for_height'（定塔高求可行操作）。",
            http_status=400,
        )
    base = inverse.validate_base(_merge_solve_base(store, payload))
    settings = inverse.resolve_settings(payload)

    if mode == "flow_for_target":
        y2_target, target_source = _resolve_target_y2(payload, base["y1"])
        z_available = _require_nonnegative("z_available", payload.get("z_available")) \
            if payload.get("z_available") is not None else None
        if z_available is None:
            raise CalculationError(
                ERR_SOLVE_BAD_REQUEST,
                "flow_for_target 必须给出 z_available（可用 / 拟建填料层高度上限）。",
                http_status=400,
            )
        result = inverse.solve_flow_for_target(base, y2_target, z_available, settings)
        result["target"]["specified_by"] = target_source
        if target_source == "removal_pct":
            result["target"]["removal_pct"] = float(payload["removal_pct"])
        return result

    # outlet_for_height
    z_max = payload.get("z_max")
    if z_max is None:
        raise CalculationError(
            ERR_SOLVE_BAD_REQUEST,
            "outlet_for_height 必须给出 z_max（现场卡死的填料层高度上限）。",
            http_status=400,
        )
    z_max = _require_number("z_max", z_max, positive=True)

    fixed_q = None
    if payload.get("fixed_L_over_G") is not None:
        fixed_q = _require_number("fixed_L_over_G", payload["fixed_L_over_G"], positive=True)
    elif payload.get("fixed_L") is not None:
        fixed_q = _require_number("fixed_L", payload["fixed_L"], positive=True) / base["G"]

    gap = payload.get("optimality_gap_pct")
    if gap is not None:
        if isinstance(gap, bool) or not isinstance(gap, (int, float)):
            raise CalculationError(
                ERR_SOLVE_BAD_REQUEST,
                f"optimality_gap_pct 必须是数值，收到 {gap!r}",
                http_status=400,
            )
        gap = float(gap)
        if not math.isfinite(gap) or gap < 0.0:
            raise CalculationError(
                ERR_SOLVE_BAD_REQUEST,
                f"optimality_gap_pct 必须是非负有限值（百分比），收到 {gap:g}",
                http_status=400,
            )
    else:
        gap = 1.0  # 与 config.SOLVE_GAP_PCT 默认一致

    return inverse.solve_outlet_for_height(
        base, z_max, settings, fixed_q=fixed_q, optimality_gap_pct=gap
    )


def run_solve_batch(store: ProfileStore, cases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量反解：逐条隔离，单条不可行 / 非法 / 不收敛只影响该条。"""
    results: list[dict[str, Any]] = []
    for index, payload in enumerate(cases):
        item: dict[str, Any] = {"index": index}
        try:
            result = run_solve_one(store, payload)
            item["ok"] = True
            item["result"] = result
        except Exception as exc:  # 单条任何失败都隔离在该条内
            item["ok"] = False
            item["error"] = _error_body(exc)
        results.append(item)
    return results

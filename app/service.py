"""核算编排：工况档解析 + 单条 / 批量执行。

核算本身（:func:`app.engine.run_calculation`）是纯函数，天然线程安全；
本层只负责把"点名工况档 + 临时覆盖参数"合并成完整参数集。批量执行时
逐条隔离，任何一条非法或夹点都只影响该条。
"""

from __future__ import annotations

from typing import Any, Iterable

from . import engine, invert
from .profiles import ProfileStore
from .validation import ALL_FIELDS


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


def _merge_invert(store: ProfileStore, payload: dict[str, Any]) -> dict[str, Any]:
    """反解合并：工况档只作塔几何 / 传质 / 进气 / 平衡线底座，
    其中 L / y2 / x1 在反解里是待求量、目标与衡算导出量，必须剔除。"""
    merged: dict[str, Any] = {}
    profile_name = payload.get("profile")
    if profile_name is not None:
        resolved = store.resolve(profile_name)
        for name in invert.BASE_FIELDS:
            if name in resolved:
                merged[name] = resolved[name]
    for name, value in payload.items():
        if name == "profile" or value is None:
            continue
        merged[name] = value
    return merged


def run_inversion_one(store: ProfileStore, payload: dict[str, Any]) -> dict[str, Any]:
    """单条反解。目标不可行 / 规格不全 / 未收敛抛
    :class:`~app.errors.CalculationError`。"""
    return invert.run_inversion(_merge_invert(store, payload))


def run_inversion_batch(store: ProfileStore, cases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量反解：逐条隔离，与正算批量同一口径（恒逐条 ok/error）。"""
    results: list[dict[str, Any]] = []
    for index, payload in enumerate(cases):
        item: dict[str, Any] = {"index": index}
        try:
            result = run_inversion_one(store, payload)
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
        return {"code": exc.code, "message": exc.message}
    return {"code": "INTERNAL_ERROR", "message": f"核算时发生未预期错误：{exc}"}

"""HTTP 请求 / 响应的 pydantic 模型。

字段全部可选：既可以临时给全套参数，也可以只给 ``profile`` 点名已登记
工况档，或在工况档基础上覆盖个别字段。

数值字段在 ``before`` 阶段就拒绝布尔、字符串与 NaN/inf，避免 pydantic
默认的宽松强转把脏数据悄悄吞进核算。
"""

from __future__ import annotations

import math
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 核算参数字段名（与 app.validation.ALL_FIELDS 保持一致）
PARAM_FIELDS = ("G", "L", "Kya", "a", "S", "y1", "y2", "x1", "x2", "m")


class StrictCaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator(*PARAM_FIELDS, mode="before", check_fields=False)
    @classmethod
    def _strict_number(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("必须是数值（不接受布尔值或字符串）")
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("必须是有限数值（不接受 NaN/Infinity）")
        return v


class CaseInput(StrictCaseModel):
    """单条核算输入：profile 与内联参数可混用，内联优先。"""

    profile: Optional[str] = Field(default=None, description="已登记工况档名字")
    G: Optional[float] = None
    L: Optional[float] = None
    Kya: Optional[float] = None
    a: Optional[float] = None
    S: Optional[float] = None
    y1: Optional[float] = None
    y2: Optional[float] = None
    x1: Optional[float] = None
    x2: Optional[float] = None
    m: Optional[float] = None


class BatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[CaseInput] = Field(min_length=1, description="待核算的操作条件列表")


# ---- 反解 ----

# 反解基底字段（塔几何/传质、进气、进液、平衡线；L、y2、x1 由反解决定）
SOLVE_PARAM_FIELDS = ("G", "Kya", "a", "S", "y1", "x2", "m")


class SolverSettingsInput(StrictCaseModel):
    """反解迭代设置；全部可选，缺省取服务配置（环境变量）。"""

    x_tolerance_rel: Optional[float] = Field(default=None, gt=0)
    x_tolerance_abs: Optional[float] = Field(default=None, gt=0)
    max_iterations: Optional[int] = Field(default=None, gt=0)
    L_over_G_floor: Optional[float] = Field(default=None, gt=0)
    L_over_G_ceiling: Optional[float] = Field(default=None, gt=0)

    @field_validator(
        "x_tolerance_rel", "x_tolerance_abs", "max_iterations",
        "L_over_G_floor", "L_over_G_ceiling",
        mode="before",
    )
    @classmethod
    def _strict_settings_number(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("必须是数值（不接受布尔值或字符串）")
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("必须是有限数值（不接受 NaN/Infinity）")
        return v


class SolveCaseInput(StrictCaseModel):
    """单条反解输入：profile 打底（取基底字段）+ 内联覆盖。"""

    mode: str = Field(description="flow_for_target | outlet_for_height")
    profile: Optional[str] = None

    # 反解基底（只接受 7 个已知量；L/y2/x1 不在此列）
    G: Optional[float] = None
    Kya: Optional[float] = None
    a: Optional[float] = None
    S: Optional[float] = None
    y1: Optional[float] = None
    x2: Optional[float] = None
    m: Optional[float] = None

    # 诉求一：定分离（target_y2 与 removal_pct 二选一）+ 可用塔高
    target_y2: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    removal_pct: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    z_available: Optional[float] = Field(default=None, ge=0.0)

    # 诉求二：定塔高；可固定吸收剂用量，或给最优性回退余量
    z_max: Optional[float] = Field(default=None, gt=0.0)
    fixed_L: Optional[float] = Field(default=None, gt=0.0)
    fixed_L_over_G: Optional[float] = Field(default=None, gt=0.0)
    optimality_gap_pct: Optional[float] = Field(default=None, ge=0.0)

    solver_settings: Optional[SolverSettingsInput] = None

    @field_validator(*SOLVE_PARAM_FIELDS, mode="before", check_fields=False)
    @classmethod
    def _strict_solve_number(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("必须是数值（不接受布尔值或字符串）")
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("必须是有限数值（不接受 NaN/Infinity）")
        return v

    @field_validator(
        "target_y2", "removal_pct", "z_available", "z_max",
        "fixed_L", "fixed_L_over_G", "optimality_gap_pct",
        mode="before",
    )
    @classmethod
    def _strict_control_number(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("必须是数值（不接受布尔值或字符串）")
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("必须是有限数值（不接受 NaN/Infinity）")
        return v

    model_config = ConfigDict(extra="forbid")


class SolveBatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[SolveCaseInput] = Field(min_length=1, description="待反解的分离目标列表")


class ProfileUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="工况档名字（1-64 字符，字母/数字/下划线/中文等）")
    description: Optional[str] = Field(default=None, max_length=500)
    params: dict[str, Any] = Field(description="全套塔几何与传质、操作参数")

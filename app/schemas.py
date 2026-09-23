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


# 反解底座数值字段（L / y2 / x1 不是底座字段，分别为待求量/目标/衡算导出）
INVERT_BASE_FIELDS = ("G", "Kya", "a", "S", "y1", "x2", "m")
_INVERT_NUM_FIELDS = (
    *INVERT_BASE_FIELDS,
    "Z", "Z_max", "design_factor", "L", "L_over_G",
    "target_y2", "removal_pct",
    "xtol", "max_iterations", "upper_factor", "lg_floor", "limit_band",
)


class InvertCaseModel(BaseModel):
    """反解输入：profile 与内联底座参数可混用（内联优先）。

    ``L`` 仅在 fixed_separation 下作为显式控制量允许；``y2`` / ``x1``
    在反解中由目标与衡算决定，一律禁止出现在底座。
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator(*_INVERT_NUM_FIELDS, mode="before", check_fields=False)
    @classmethod
    def _strict_number(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("必须是数值（不接受布尔值或字符串）")
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("必须是有限数值（不接受 NaN/Infinity）")
        return v

    profile: Optional[str] = Field(default=None, description="已登记工况档名字（仅取底座参数）")
    mode: Optional[str] = Field(
        default=None, description="fixed_separation（定分离，默认）或 fixed_height（定塔高）"
    )

    # ---- 反解底座 ----
    G: Optional[float] = None
    Kya: Optional[float] = None
    a: Optional[float] = None
    S: Optional[float] = None
    y1: Optional[float] = None
    x2: Optional[float] = None
    m: Optional[float] = None

    # ---- 分离目标（二选一） ----
    target_y2: Optional[float] = Field(default=None, description="目标塔顶出气分率 y2")
    removal_pct: Optional[float] = Field(default=None, description="要求脱除百分比 0-100")

    # ---- fixed_separation：再钉死一个量 ----
    Z: Optional[float] = Field(default=None, description="现成塔可用填料层高度（m）")
    design_factor: Optional[float] = Field(
        default=None, description="设计倍率 c：L/G = c·(L/G)_min（c>1）"
    )
    L: Optional[float] = Field(default=None, description="显式给定液相摩尔流量（定分离直给）")
    L_over_G: Optional[float] = Field(default=None, description="显式给定液气比（定分离直给）")

    # ---- fixed_height ----
    Z_max: Optional[float] = Field(default=None, description="填料层高度上限（m）")

    # ---- 求根控制（可选，均有默认） ----
    xtol: Optional[float] = None
    max_iterations: Optional[int] = None
    upper_factor: Optional[float] = None
    lg_floor: Optional[float] = None
    limit_band: Optional[float] = None


class InvertBatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[InvertCaseModel] = Field(min_length=1, description="待反解的分离目标列表")


class ProfileUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="工况档名字（1-64 字符，字母/数字/下划线/中文等）")
    description: Optional[str] = Field(default=None, max_length=500)
    params: dict[str, Any] = Field(description="全套塔几何与传质、操作参数")

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


class ProfileUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="工况档名字（1-64 字符，字母/数字/下划线/中文等）")
    description: Optional[str] = Field(default=None, max_length=500)
    params: dict[str, Any] = Field(description="全套塔几何与传质、操作参数")

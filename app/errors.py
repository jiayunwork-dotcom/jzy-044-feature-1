"""核算领域的错误类型与错误码。"""

from __future__ import annotations

from dataclasses import dataclass


# ---- 错误码（随错误响应一并返回，便于上游分支处理） ----
ERR_MISSING_FIELD = "MISSING_FIELD"
ERR_NOT_A_NUMBER = "NOT_A_NUMBER"
ERR_NON_POSITIVE = "NON_POSITIVE_VALUE"
ERR_FRACTION_RANGE = "FRACTION_OUT_OF_RANGE"
ERR_INVALID_SLOPE = "INVALID_SLOPE"
ERR_Y_ORDER = "Y_ORDER_INVALID"            # y1 < y2（本服务只核算吸收）
ERR_MASS_BALANCE = "MASS_BALANCE_MISMATCH"
ERR_PINCH = "PINCH_LIMITED"                # 夹点受限：拒绝返回有限塔高
ERR_PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
ERR_PROFILE_NAME = "PROFILE_NAME_INVALID"
ERR_PROFILE_READONLY = "PROFILE_READONLY"  # 内置示范工况不可改 / 删
ERR_PROFILE_INVALID = "PROFILE_INVALID"


@dataclass(frozen=True)
class CalculationError(Exception):
    """请求本身可解析、但核算无法完成（非法输入或夹点受限）。"""

    code: str
    message: str
    http_status: int = 422

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"[{self.code}] {self.message}"

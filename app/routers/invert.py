"""反解接口：单条 / 批量。

只经 HTTP 提供反解能力，不做前端。错误外壳、批量逐条隔离口径与正算一致。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..schemas import InvertBatchInput, InvertCaseModel
from ..service import run_inversion_batch, run_inversion_one

router = APIRouter(tags=["inversion"])


def _store(request: Request):
    return request.app.state.profiles


@router.post("/invert", summary="单组分离目标反解")
def invert_one(case: InvertCaseModel, request: Request) -> dict:
    """给定分离目标（或塔高上限）反解吸收剂用量，并在解点用正算内核复算。

    目标越过理论极限 / 塔高不足 / 迭代未收敛 / 规格冲突时返回带原因的错误响应。
    """
    return run_inversion_one(_store(request), case.model_dump(exclude_none=True))


@router.post("/invert/batch", summary="批量反解")
def invert_batch(batch: InvertBatchInput, request: Request) -> dict:
    """逐条反解；单条不可行或参数非法只标注该条 error，其余照常算完。

    无论各条成败，HTTP 状态恒为 200，结果以每条的 ``ok`` 字段区分。
    """
    payloads = [c.model_dump(exclude_none=True) for c in batch.cases]
    return {"count": len(payloads), "results": run_inversion_batch(_store(request), payloads)}

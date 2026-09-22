"""核算接口：单条 / 批量。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..schemas import BatchInput, CaseInput
from ..service import run_batch, run_one

router = APIRouter(tags=["calculation"])


def _store(request: Request):
    return request.app.state.profiles


@router.post("/calculate", summary="单组操作条件核算")
def calculate(case: CaseInput, request: Request) -> dict:
    """返回 HOG、NOG、Z、夹点标志与离夹点距离等完整核算结果。

    非法输入 / 物料衡算矛盾 / 夹点受限时返回带原因的错误响应。
    """
    return run_one(_store(request), case.model_dump(exclude_none=True))


@router.post("/calculate/batch", summary="批量核算")
def calculate_batch(batch: BatchInput, request: Request) -> dict:
    """逐条核算；单条出错或夹点只在该条标注 error，其余条目照常返回。

    无论各条成败，HTTP 状态恒为 200，结果以每条的 ``ok`` 字段区分。
    """
    payloads = [c.model_dump(exclude_none=True) for c in batch.cases]
    return {"count": len(payloads), "results": run_batch(_store(request), payloads)}

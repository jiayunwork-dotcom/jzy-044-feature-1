"""工况档登记接口。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..schemas import ProfileUpsert

router = APIRouter(tags=["profiles"])


def _store(request: Request):
    return request.app.state.profiles


@router.get("/profiles", summary="列出全部已登记工况档")
def list_profiles(request: Request) -> dict:
    profiles = _store(request).list_profiles()
    return {"count": len(profiles), "profiles": profiles}


@router.get("/profiles/{name}", summary="查看单个工况档")
def get_profile(name: str, request: Request) -> dict:
    return _store(request).get(name)


@router.put("/profiles/{name}", summary="登记或更新工况档")
def put_profile(name: str, body: ProfileUpsert, request: Request) -> dict:
    # 路径名优先；若 body 也给了 name，必须与路径一致，避免歧义
    if body.name != name:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400,
            detail={
                "code": "PROFILE_NAME_MISMATCH",
                "message": f"路径名 {name!r} 与请求体 name={body.name!r} 不一致。",
            },
        )
    record = _store(request).create_or_update(name, body.params, body.description)
    return record


@router.delete("/profiles/{name}", summary="删除自定义工况档", status_code=204)
def delete_profile(name: str, request: Request) -> None:
    _store(request).delete(name)

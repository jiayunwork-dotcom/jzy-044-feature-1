"""FastAPI 应用入口。

仅经 HTTP 对外提供能力，无前端、无账户体系。启动时把工况档仓库挂到
``app.state.profiles``；测试可用 :func:`create_app` 注入临时数据目录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__, config
from .errors import CalculationError
from .profiles import BUILTIN_PROFILE, ProfileStore
from .routers import calc, invert, profiles


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    app = FastAPI(
        title="填料吸收塔传质高度核算服务",
        version=__version__,
        description=(
            "计算稀溶质气液逆流填料吸收塔的 HOG / NOG / 填料层高度 Z，"
            "并判定操作条件是否受夹点限制、量化离夹点的距离。"
        ),
    )

    store_path = (
        Path(data_dir) / "profiles.json"
        if data_dir is not None
        else config.PROFILES_FILE
    )
    app.state.profiles = ProfileStore(store_path)

    app.include_router(calc.router)
    app.include_router(invert.router)
    app.include_router(profiles.router)

    @app.exception_handler(CalculationError)
    async def _calculation_error_handler(_: Request, exc: CalculationError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 统一错误响应外壳，方便上游模拟器稳定解析
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "REQUEST_VALIDATION_FAILED",
                    "message": "请求体未通过结构校验。",
                    "details": _simplify_errors(exc.errors()),
                }
            },
        )

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, Any]:
        return {
            "service": "absorption-height-service",
            "version": __version__,
            "docs": "/docs",
            "builtin_profile": BUILTIN_PROFILE,
            "endpoints": [
                "POST /calculate",
                "POST /calculate/batch",
                "POST /invert",
                "POST /invert/batch",
                "GET /profiles",
                "GET /profiles/{name}",
                "PUT /profiles/{name}",
                "DELETE /profiles/{name}",
                "GET /health",
            ],
        }

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


def _simplify_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    simple: list[dict[str, Any]] = []
    for err in errors:
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        simple.append(
            {
                "field": loc or "(root)",
                "type": err.get("type", ""),
                "message": err.get("msg", ""),
            }
        )
    return simple


app = create_app()

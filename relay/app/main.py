"""FastAPI 装配。

启动顺序刻意固定为：建目录 → 连数据库 → 跑迁移 → 才开始服务。
迁移失败就直接起不来，不允许带着半截 schema 提供服务。
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api.auth import router as auth_router
from .api.boards import router as boards_router
from .api.devices import router as devices_router
from .api.health import router as health_router
from .api.notes import router as notes_router
from .api.pages import router as pages_router
from .api.realtime import router as realtime_router
from .config import Config, load_config
from .db import Database
from .errors import DEFAULT_CODES, AppError, error_payload
from .migrations import apply_migrations
from .origin import same_origin
from .realtime import ConnectionManager
from .state import RuntimeState

# uvicorn 只配置它自己的 logger，不会给 root 装 handler。
# 在这里装一个，否则本模块的 logger.info 会被丢掉。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
)

logger = logging.getLogger("relay")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


# 需要校验来源的方法。GET / HEAD / OPTIONS 是安全方法，不做校验。
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config if config is not None else load_config()
    db = Database(cfg.db_path)
    runtime = RuntimeState()
    # 连接表必须先于 db 建好：db 的事件汇指向它，写事务提交后直接广播。
    # 反过来（先建 db 再注入）也行，但"先有消费者再有生产者"读起来更顺。
    realtime_manager = ConnectionManager(
        state=runtime,
        room_limit=cfg.ws_room_limit,
        ip_limit=cfg.ws_ip_limit,
    )
    db.set_event_sink(realtime_manager.publish)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg.ensure_dirs()
        await db.connect()
        try:
            applied = await apply_migrations(db, MIGRATIONS_DIR)
            runtime.started_at = int(time.time())
            logger.info(
                "relay 启动 version=%s 本次应用迁移=%s",
                __version__,
                applied if applied else "无",
            )
            logger.info("有效配置 %s", cfg.as_dict())
            yield
        finally:
            await db.close()
            logger.info("relay 已停止")

    app = FastAPI(
        title="HopDrop relay",
        version=__version__,
        lifespan=lifespan,
        # 不暴露 OpenAPI 与 Swagger UI：文档页要从 CDN 取资源，既被本项目的
        # CSP 挡住，也多一处对外接口面。接口验证靠测试和 M5 的前端页面。
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = cfg
    app.state.db = db
    app.state.runtime = runtime
    app.state.realtime = realtime_manager

    @app.middleware("http")
    async def attach_request_context(request: Request, call_next):
        """请求上下文、来源校验、通用响应头。

        三件事塞在一个中间件里，是有意的：Starlette 的 `@app.middleware("http")`
        按**注册顺序的倒序**套娃，后注册的在外层。拆成两个中间件时，"谁先跑"
        取决于源码里的先后，靠注释维持的正确性迟早会被一次无关的重排破坏。
        合成一个，顺序就写在函数体里，读一遍就够。
        """
        request.state.request_id = uuid.uuid4().hex

        if (
            request.method in UNSAFE_METHODS
            and request.url.path.startswith("/api/")
            and not same_origin(
                origin=request.headers.get("origin"),
                host=request.headers.get("host"),
            )
        ):
            logger.warning(
                "拒绝跨源写请求 requestId=%s method=%s path=%s origin=%s",
                request.state.request_id,
                request.method,
                request.url.path,
                request.headers.get("origin"),
            )
            response = JSONResponse(
                status_code=403,
                content=error_payload(
                    "origin_not_allowed", "请求来源不被允许", request.state.request_id
                ),
            )
        else:
            response = await call_next(request)

        response.headers["X-Request-Id"] = request.state.request_id
        # 方案 9.1：API 响应使用 Cache-Control: no-store。
        # M5 接入静态资源后要放开静态目录，那些文件需要缓存。
        response.headers["Cache-Control"] = "no-store"
        # 方案 12 要求配对响应设置 no-referrer。这里全局设置而不是只给
        # /pair：地址栏里的区域、消息 ID 同样不该顺着 Referer 漏给外站，
        # 而全局设置没有任何一处会因此变坏。Caddy 也会下一份，两处一致。
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, exc.message, _request_id(request)),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = DEFAULT_CODES.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else code
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(code, message, _request_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI 默认返回 422，但方案 9.1 规定参数错误统一用 400。
        errors = exc.errors()
        first = errors[0] if errors else {}
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        message = f"参数错误：{location} {first.get('msg', '')}".strip()
        return JSONResponse(
            status_code=400,
            content=error_payload("bad_request", message, _request_id(request)),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常 requestId=%s", _request_id(request))
        return JSONResponse(
            status_code=500,
            content=error_payload("internal_error", "服务内部错误", _request_id(request)),
        )

    if not cfg.cookie_secure:
        logger.warning(
            "RELAY_COOKIE_SECURE 已关闭：会话 Cookie 不带 Secure 属性，"
            "只应在本地 http://127.0.0.1 开发时使用。生产环境必须开启。"
        )

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(devices_router)
    app.include_router(boards_router)
    app.include_router(notes_router)
    app.include_router(realtime_router)
    app.include_router(pages_router)
    return app


app = create_app()

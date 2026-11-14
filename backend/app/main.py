"""FastAPI 入口：中间件、生命周期、路由挂载。

启动时只加载语义层（毫秒级）；数据库连接与模型调用都在首次请求时才建立，
所以「数据库没起」不会导致服务起不来，只是问数接口返回 error。
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.analysis.followup import FollowupResolver
from app.api.v1.meta import router as meta_router
from app.api.v1.query import router as query_router
from app.core.config import settings
from app.nl2sql.pipeline import MySQLExecutor, NL2SQLPipeline
from app.semantic.loader import SemanticLayer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s trace=%(trace_id)s %(message)s",
)

VERSION = "0.1.0"
logger = logging.getLogger("chatbi")


class TraceIdFilter(logging.Filter):
    """把 trace_id 注入日志；没取到就用短横线，避免日志格式错位。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = getattr(record, "trace_id", "-")
        return True


for handler in logging.getLogger().handlers:
    handler.addFilter(TraceIdFilter())


async def lifespan(app: FastAPI):
    semantic = SemanticLayer.load()
    executor = MySQLExecutor(settings.database_url) if settings.enable_db else None
    app.state.semantic = semantic
    app.state.pipeline = NL2SQLPipeline(semantic=semantic, executor=executor)
    app.state.followup = FollowupResolver(semantic)
    logger.info("semantic loaded: %d metrics / %d dimensions",
                len(semantic.metrics), len(semantic.dimensions))
    yield


app = FastAPI(
    title="电商 ChatBI 智能问数",
    version=VERSION,
    description="自然语言 → SQL → 结果集 → 图表/归因",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins) or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:12]
    request.state.trace_id = trace_id
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = round((time.perf_counter() - start) * 1000, 1)
    response.headers["X-Trace-Id"] = trace_id
    response.headers["X-Elapsed-Ms"] = str(elapsed)
    logger.info("method=%s path=%s status=%s elapsed_ms=%s",
                request.method, request.url.path, response.status_code, elapsed,
                extra={"trace_id": trace_id})
    return response


@app.get("/health", tags=["system"], summary="健康检查")
async def health(request: Request) -> dict:
    semantic: SemanticLayer = request.app.state.semantic
    pipeline: NL2SQLPipeline = request.app.state.pipeline
    db_status = "disabled"
    if pipeline.executor is not None:
        db_status = await run_in_threadpool(pipeline.executor.health)

    return {
        "status": "ok",
        "version": VERSION,
        "env": settings.app_env,
        "model": settings.llm_model,
        "semantic": {"metrics": len(semantic.metrics),
                     "dimensions": len(semantic.dimensions),
                     "terms": len(semantic.terms)},
        "database": db_status,
        "trace_id": getattr(request.state, "trace_id", ""),
    }


app.include_router(query_router)
app.include_router(meta_router)

# 前端：零构建单页，由后端直接托管，避免额外的 CORS 与部署步骤。
# 目录在 backend/frontend，随 ../backend:/app 一起挂载进容器。
FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="ui")

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse("/ui/")

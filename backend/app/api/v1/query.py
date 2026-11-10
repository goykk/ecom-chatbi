"""问数接口：同步返回 + SSE 流式。

生产上真正的流式应该是「边生成边推送」，这里先把阶段事件推出去
（status → sql → rows → done），前端已经可以据此做渐进式渲染，
后续把 generator 改成流式输出时，接口形状不用改。
"""

from __future__ import annotations

import json
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.analysis.followup import FollowupResolver, TurnContext
from app.nl2sql.pipeline import NL2SQLPipeline, QueryResult

router = APIRouter(prefix="/api/v1", tags=["query"])


class ContextIn(BaseModel):
    """上一轮上下文：前端把上一轮的 question 回传，就能支持「那按类目拆一下」。"""

    question: str = Field(..., min_length=2, max_length=200)
    sql: str | None = None
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)


class QueryIn(BaseModel):
    question: str = Field(..., min_length=2, max_length=200, description="自然语言问题")
    execute: bool | None = Field(None, description="是否连接数据库执行，默认自动判断")
    fewshot_k: int = Field(3, ge=0, le=10, description="注入示例数量")
    with_insight: bool = Field(True, description="是否生成归因解读（会多一次模型调用）")
    context: ContextIn | None = Field(None, description="上一轮上下文，用于追问改写")


def _pipeline(request: Request) -> NL2SQLPipeline:
    return request.app.state.pipeline


def _resolve(request: Request, body: QueryIn):
    resolver: FollowupResolver = request.app.state.followup
    ctx = None
    if body.context:
        ctx = TurnContext(**body.context.model_dump())
    return resolver.resolve(body.question, ctx)


def _payload(result: QueryResult, trace_id: str) -> dict:
    data = result.summary()
    data.update({"trace_id": trace_id, "columns": result.columns, "rows": result.rows})
    return data


@router.post("/query", summary="自然语言问数（同步）")
async def query(body: QueryIn, request: Request) -> dict:
    resolved = _resolve(request, body)
    result = await run_in_threadpool(
        _pipeline(request).ask, resolved.question, body.execute,
        body.fewshot_k, body.with_insight
    )
    data = _payload(result, getattr(request.state, "trace_id", ""))
    data["followup"] = resolved.to_dict()
    data["question_used"] = resolved.question
    return data


@router.post("/query/stream", summary="自然语言问数（SSE 流式）")
async def query_stream(body: QueryIn, request: Request) -> StreamingResponse:
    trace_id = getattr(request.state, "trace_id", "")
    resolved = _resolve(request, body)

    async def event_stream():
        yield _sse("status", {"stage": "recall", "trace_id": trace_id})
        yield _sse("followup", resolved.to_dict())
        try:
            result = await run_in_threadpool(
                _pipeline(request).ask, resolved.question, body.execute,
                body.fewshot_k, body.with_insight
            )
        except Exception as exc:  # noqa: BLE001 - SSE 里不能抛 500，要发事件
            yield _sse("error", {"message": str(exc), "trace_id": trace_id})
            return

        yield _sse("status", {"stage": "done", "action": result.action})
        if result.sql:
            yield _sse("sql", {"sql": result.sql, "attempts": result.attempts})
        if result.action == "refuse":
            yield _sse("refuse", {"reason": result.reason})
        elif result.action == "error":
            yield _sse("error", {"message": result.reason})
        else:
            yield _sse("rows", {"columns": result.columns, "rows": result.rows})
            if result.chart:
                yield _sse("chart", result.chart)
            if result.insight:
                yield _sse("insight", result.insight)
        yield _sse("done", _payload(result, trace_id))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "X-Trace-Id": trace_id},
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=_json_default)}\n\n"


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    return str(value)

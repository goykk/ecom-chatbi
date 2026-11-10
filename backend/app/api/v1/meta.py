"""语义层元数据接口：前端用它生成指标选择器与口径说明。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.semantic.loader import SemanticLayer

router = APIRouter(prefix="/api/v1", tags=["meta"])


def _semantic(request: Request) -> SemanticLayer:
    return request.app.state.semantic


@router.get("/metrics", summary="指标列表")
def list_metrics(request: Request) -> list[dict]:
    semantic = _semantic(request)
    return [
        {
            "biz_name": m.biz_name,
            "name": m.name,
            "description": m.description,
            "type": m.define_type,
            "define_by": m.define_by,
            "formula": m.formula,
            "unit": m.unit,
            "aliases": list(m.aliases),
            "dimensions": list(m.dimensions),
            "status": m.status,
            "caveat": m.caveat,
        }
        for m in semantic.metrics.values()
    ]


@router.get("/dimensions", summary="维度列表")
def list_dimensions(request: Request) -> list[dict]:
    semantic = _semantic(request)
    return [
        {
            "biz_name": d.biz_name,
            "name": d.name,
            "type": d.type,
            "semantic_type": d.semantic_type,
            "aliases": list(d.aliases),
            "value_maps": [
                {"value": vm.get("value"), "labels": vm.get("labels", [])}
                for vm in d.value_maps
            ],
            "exposure": d.exposure,
        }
        for d in semantic.dimensions.values()
    ]


@router.get("/terms", summary="术语表")
def list_terms(request: Request) -> list[dict]:
    return _semantic(request).terms


@router.get("/unavailable", summary="不支持的维度与拒答理由")
def list_unavailable(request: Request) -> list[dict]:
    semantic = _semantic(request)
    return semantic.unavailable + semantic.no_answer

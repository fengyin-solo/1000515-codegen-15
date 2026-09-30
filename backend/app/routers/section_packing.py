"""实测剖面批次打包台接口。

- GET  /api/section-packing/preflight            打包前逐剖面核验（分层、长度、图纸、外部清单）
- POST /api/section-packing/confirm              在剖面上确认分层与长度（审定边界为准）
- POST /api/section-packing/batches              建批导出（材料不齐整批不出包；大批量异步）
- GET  /api/section-packing/batches              批次清单（键集分页，顺序稳定）
- GET  /api/section-packing/batches/{id}         批次详情与断点进度
- POST /api/section-packing/batches/{id}/resume  中断后按原批次续传
- GET  /api/section-packing/archives             历史导出版本（按快照保留）
- GET  /api/section-packing/archives/{id}/download  下载整套 zip
- GET  /api/section-packing/migrations           历史补数迁移日志
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.services.section_packing import batch_packing_service

router = APIRouter(prefix="/api/section-packing", tags=["实测剖面批次打包台"])


class ConfirmPayload(BaseModel):
    entry_id: int | None = None
    section_code: str | None = None
    operator: str | None = None


class CreateBatchPayload(BaseModel):
    section_codes: list[str] = Field(default_factory=list)
    operator: str | None = None


@router.get("/preflight")
def preflight() -> dict[str, Any]:
    """打包前核验清单：逐剖面列出阻塞原因与提示，未确认/缺文件一目了然。"""
    items = batch_packing_service.packing_overview()
    return {
        "total": len(items),
        "ready": sum(1 for item in items if item["ready"]),
        "blocked": sum(1 for item in items if not item["ready"]),
        "items": items,
    }


@router.post("/confirm")
def confirm_section(payload: ConfirmPayload) -> dict[str, Any]:
    """在剖面上确认剖面分层与剖面长度；累计不符以审定边界累计为准。"""
    if not payload.entry_id and not payload.section_code:
        raise HTTPException(status_code=400, detail="请指定要确认的剖面（entry_id 或剖面编号）")
    preflight, message = batch_packing_service.confirm_section(
        entry_id=payload.entry_id, section_code=payload.section_code, operator=payload.operator
    )
    if preflight is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "preflight": preflight}


@router.post("/batches")
def create_batch(payload: CreateBatchPayload) -> dict[str, Any]:
    """建批导出。任一剖面材料不齐则整批不出包，逐条说明原因，不占版本号。"""
    batch, failures, message = batch_packing_service.create_batch(payload.section_codes, operator=payload.operator)
    if batch is None:
        return {"ok": False, "message": message, "failures": failures}
    return {"ok": True, "message": message, "batch": batch}


@router.get("/batches")
def list_batches(
    after_id: int = Query(default=0, ge=0, description="键集分页游标：上一页最后一条批次 id"),
    size: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None, description="排队中、打包中、已出包、已中断"),
) -> dict[str, Any]:
    """批次清单：键集分页，导出期间翻页顺序保持稳定。"""
    return batch_packing_service.list_batches(after_id=after_id, size=size, status=status)


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int) -> dict[str, Any]:
    batch = batch_packing_service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")
    return batch


@router.post("/batches/{batch_id}/resume")
def resume_batch(batch_id: int) -> dict[str, Any]:
    """连接中断后接着原批次继续导出；已出包则沿用原版本，不重复分配。"""
    batch, message = batch_packing_service.resume_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message or "已从断点继续导出", "batch": batch}


@router.get("/archives")
def list_archives() -> dict[str, Any]:
    """历史导出版本：按出包时快照保留，不随后续台账变动。"""
    items = batch_packing_service.list_archives()
    return {"total": len(items), "items": items}


@router.get("/archives/{archive_id}/download")
def download_archive(archive_id: int) -> Response:
    """下载整套包：数据包 + 图纸清单 + 核验摘要（含批次摘要）。"""
    archive, payload = batch_packing_service.get_archive_package(archive_id)
    if archive is None or payload is None:
        raise HTTPException(status_code=404, detail=f"导出存档 {archive_id} 不存在")
    encoded = quote(archive["package_name"].encode("utf-8"))
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=archive.zip; filename*=UTF-8''{encoded}"},
    )


@router.get("/migrations")
def list_migrations() -> dict[str, Any]:
    """历史剖面补数迁移留痕。"""
    from app.migrations.section_sheet import migration_logs

    rows = migration_logs()
    return {"total": len(rows), "items": rows}

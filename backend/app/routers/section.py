"""剖面编录接口：维护实测剖面，并提供「批次打包台」成批出包能力。

原 /api/section/export 只导出一份孤立清单，已被批次打包台取代：
- 出包前先在剖面上确认分层与长度（/sections/{id}/layers/confirm）；
- 数据包、图纸清单、核验摘要三件齐全才出整批 zip，任一缺失整批驳回；
- 批次可异步、可断线续传、重复导出版本复用；历史剖面缺图幅号可补数迁移。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response

from app.schemas import ActionResult, BatchExportRequest, EntryPayload, PageResult
from app.services.section import SectionService
from app.services.section_pack import batch_pack_service as packs

router = APIRouter(prefix="/api/section", tags=["剖面编录"])

service = SectionService()

LIST_FIELDS = ["剖面编号", "剖面名称", "剖面长度", "起点坐标", "终点坐标", "编录日期", "编录人员", "剖面状态"]
STATUSES = ["实测中", "已编录", "已制图", "已验收"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按剖面编号检索"),
    status: str | None = Query(default=None, description="实测中、已编录、已制图、已验收"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按剖面编号与状态过滤剖面编录列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条实测剖面，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="实测剖面已登记", entry=entry)


# ----------------------------------------------------------------------
# 批次打包台：批次头/列表必须放在 /{entry_id} 之前，避免被整数路径吞掉
# ----------------------------------------------------------------------
@router.post("/batches")
def create_batch(payload: BatchExportRequest) -> dict[str, Any]:
    """创建导出批次：冻结有序剖面清单；小批量同步出包，大批量走异步队列。"""
    batch, message = packs.create_batch(
        section_ids=payload.section_ids,
        keyword=payload.keyword,
        status=payload.status,
        operator=payload.operator,
    )
    if batch is None:
        raise HTTPException(status_code=400, detail=message)
    return {"ok": True, "message": message, "batch": batch}


@router.get("/batches")
def list_batches(page: int = 1, size: int = 20) -> PageResult[dict]:
    """历史导出批次列表，按批次 id 倒序稳定分页；历史批次按原快照保留。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = packs.list_batches(page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/batches/{batch_id}/resume")
def resume_batch(batch_id: int) -> dict[str, Any]:
    """连接中断后接着原批次继续导出；已出包剖面不重做，重复导出版本复用。"""
    batch, message = packs.resume_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "batch": batch}


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int) -> dict[str, Any]:
    """读取批次状态与逐条出包明细；断线残留的导出中批次会自动接续。"""
    batch = packs.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"导出批次 {batch_id} 不存在")
    return batch


@router.get("/batches/{batch_id}/download")
def download_batch(batch_id: int) -> Response:
    """整批打包下载：数据包、图纸清单、核验摘要与批次清单/总摘要一起下载。

    任一文件缺失（批次被驳回）时不出包，返回 409 并说明原因。
    """
    data, filename, message = packs.build_package(batch_id)
    if data is None:
        raise HTTPException(status_code=409, detail=message)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/migration")
def migration_logs() -> dict[str, Any]:
    """历史剖面缺图幅号的补数迁移日志：保留原编号，记录补入图幅号。"""
    logs = packs.migration_logs()
    return {"total": len(logs), "items": logs}


@router.post("/migration/backfill")
def run_backfill() -> dict[str, Any]:
    """再次执行缺图幅号补数；已补齐的剖面不会重复迁移（可重入）。"""
    changed = packs.backfill_sheet_numbers()
    return {
        "ok": True,
        "message": f"本次补数 {len(changed)} 条历史剖面，原编号保留" if changed else "没有缺图幅号的历史剖面",
        "items": changed,
    }


# ----------------------------------------------------------------------
# 单条剖面
# ----------------------------------------------------------------------
@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条实测剖面明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"实测剖面 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条实测剖面执行完成实测、提交制图、申请验收；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/detail")
def get_entry_detail(entry_id: int) -> dict[str, Any]:
    """剖面详情：基础信息 + 分层 + 外部核对清单 + 三处来源同步 + 历史导出版本。"""
    detail = packs.section_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"实测剖面 {entry_id} 不存在或已归档")
    return detail


@router.post("/{entry_id}/layers/confirm", response_model=ActionResult)
def confirm_layers(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """出包前确认剖面分层与剖面长度；累计长度不相符时按审定边界确认。"""
    raw_boundary = (payload.values.get("审定边界") if payload and payload.values else None)
    audited = None
    if raw_boundary not in (None, ""):
        try:
            audited = float(raw_boundary)
        except (TypeError, ValueError):
            return ActionResult(ok=False, message=f"审定边界「{raw_boundary}」不是数值")
    entry, message, result = packs.confirm_layers(entry_id, audited)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)  # type: ignore[arg-type]


@router.post("/checklist/{item_id}/reconcile", response_model=ActionResult)
def reconcile_checklist_item(item_id: int, payload: EntryPayload) -> ActionResult:
    """登记外部核对结论，使外部核对清单与剖面同步。"""
    entry, message = packs.reconcile_checklist_item(item_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

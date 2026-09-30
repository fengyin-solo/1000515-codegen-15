"""剖面编录接口：维护实测剖面，覆盖完成实测、提交制图、申请验收等动作。

图属相符性导出已迁移到「批次打包台」，见 /api/section-packing。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.section import SectionService
from app.services.section_packing import batch_packing_service

router = APIRouter(prefix="/api/section", tags=["剖面编录"])

service = SectionService()

LIST_FIELDS = ["剖面编号", "剖面名称", "剖面长度", "起点坐标", "终点坐标", "编录日期", "编录人员", "剖面状态", "图幅编号"]
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


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条实测剖面明细；同时带上打包台核验信息，保证详情与导出同源。

    不存在时给出可读的错误说明。
    """
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"实测剖面 {entry_id} 不存在或已归档")
    preflight = batch_packing_service.preflight_section(entry)
    return {**entry, "packing": preflight}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条实测剖面，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="实测剖面已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条实测剖面执行完成实测、提交制图、申请验收；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

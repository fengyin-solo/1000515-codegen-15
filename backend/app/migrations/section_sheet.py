"""历史剖面补数迁移：给缺图幅号的老剖面补挂图幅。

规则：
- 只补「图幅编号」缺失的历史剖面，不动剖面编号、不动行 id、不覆盖已有图幅；
- 优先按填图台账（mapping）轮转分配图幅号，台账为空时按图幅分幅规则生成补编号；
- 每条补数在 migration_log 留痕，迁移可重复执行（幂等）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "section"
LOG_MODULE = "migration_log"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _available_sheet_numbers() -> list[str]:
    """从地质填图台账取有效图幅编号；没有台账时给出按分幅规则生成的备选号。"""
    numbers = [
        str(row.get("图幅编号") or "").strip()
        for row in store.rows("mapping")
        if str(row.get("图幅编号") or "").strip()
    ]
    numbers = sorted(set(numbers))
    return numbers or ["MAPP-BACKFILL-0001"]


def backfill_section_sheet() -> dict[str, Any]:
    """执行补数迁移，返回补数条数与明细。"""
    backfilled: list[dict[str, Any]] = []
    with store.transaction():
        sheet_numbers = _available_sheet_numbers()
        legacy_rows = [
            row for row in store.rows(MODULE)
            if not str(row.get("图幅编号") or "").strip()
        ]
        for index, row in enumerate(legacy_rows):
            section_code = str(row.get("剖面编号"))
            assigned = sheet_numbers[index % len(sheet_numbers)]
            # 只补图幅号：原编号、原 id、其余字段一律不动
            row["图幅编号"] = assigned
            row["图幅号来源"] = "历史补数迁移"
            log_id = store.next_id(LOG_MODULE)
            store.rows(LOG_MODULE).append({
                "id": log_id,
                "迁移任务": "历史剖面补图幅号",
                "业务模块": MODULE,
                "业务编号": section_code,
                "原编号": section_code,
                "补数字段": "图幅编号",
                "补数值": assigned,
                "执行时间": _now(),
                "说明": "历史剖面缺图幅号，按填图台账轮转补挂；剖面原编号与行 id 保持不变",
            })
            backfilled.append({
                "剖面编号": section_code,
                "原编号": section_code,
                "图幅编号": assigned,
            })
    return {
        "task": "历史剖面补图幅号",
        "backfilled": len(backfilled),
        "items": backfilled,
    }


def migration_logs() -> list[dict[str, Any]]:
    return [dict(row) for row in store.rows(LOG_MODULE)]

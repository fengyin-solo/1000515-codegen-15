"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

剖面「批次打包台」额外用到剖面分层、外部核对清单、导出批次/明细、存档与
迁移日志等表。它们跟着主仓库一起放在内存里，但不计入运营概览的业务模块数。
仓库提供快照/回滚与一把可重入锁，导出、存档与版本号分配在 service 层据此做事务化。
"""
from __future__ import annotations

import contextlib
import copy
import threading
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 剖面批次打包台的辅助表：不进运营概览的业务模块清单。
AUXILIARY_TABLES = {
    "section_layer",        # 剖面分层（地层台账在剖面上的落点）
    "section_checklist",    # 外部核对清单
    "section_export_batch", # 导出批次头
    "section_export_item",  # 批次内每条剖面的出包明细
    "section_archive",      # 剖面存档（不可变快照 + 版本号）
    "section_migration_log",  # 历史剖面补图幅号的迁移日志
}

# 仅在运行期出现、不随示例数据初始化的表。
RUNTIME_TABLES = [
    "section_export_batch",
    "section_export_item",
    "section_archive",
    "section_migration_log",
]


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for name in RUNTIME_TABLES:
            self._tables.setdefault(name, [])
        # 导出涉及批次头、明细、存档多张表的联动写入，用一把可重入锁串行化。
        self.lock = threading.RLock()

    def module_names(self) -> list[str]:
        """运营概览只统计业务模块，剖面打包台的辅助表不计入。"""
        return sorted(name for name in self._tables if name not in AUXILIARY_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def snapshot(self, modules: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
        """对给定表做深拷贝快照，供事务回滚。"""
        return {name: [copy.deepcopy(row) for row in self.rows(name)] for name in modules}

    def restore(self, snap: dict[str, list[dict[str, Any]]]) -> None:
        for name, rows in snap.items():
            table = self.rows(name)
            table.clear()
            table.extend(copy.deepcopy(rows) for rows in rows)

    @contextlib.contextmanager
    def transaction(self, *modules: str) -> Iterator[None]:
        """在同一把锁里联动写多张表；任一步抛错则整体回滚到进入前快照。

        锁是可重入的，外层已持锁时内层事务不会自死；快照按最外层进入时的状态
        恢复，保证「导出 + 存档 + 版本号」要么一起成、要么一起回。
        """
        with self.lock:
            snap = self.snapshot(modules)
            try:
                yield
            except Exception:
                self.restore(snap)
                raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()

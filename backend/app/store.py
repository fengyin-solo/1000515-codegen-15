"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

打包台相关的三张台账（批次、存档、确认记录、迁移日志）不落种子数据，
由仓储在启动时建空表；其余业务表来自 seed。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 仅在运行期产生、不参与种子初始化的表
RUNTIME_TABLES = [
    "section_confirm",       # 剖面分层/长度确认记录
    "export_batch",          # 导出批次（打包台工单）
    "export_batch_item",     # 批次内逐条剖面的核验与产物明细
    "export_archive",        # 已提交存档（指纹唯一，重复导出不新增版本）
    "migration_log",         # 历史数据补数迁移日志
]

# 打包台的支撑台账：参与种子数据但不属于运营概览的业务模块
SUPPORT_TABLES = ["section_layer", "section_drawing", "section_checklist"]


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for name in RUNTIME_TABLES:
            self._tables.setdefault(name, [])
        # 导出、存档、版本号分配都要事务化，进程内用一把写锁保证批次互不串档
        self._write_lock = threading.RLock()

    @property
    def write_lock(self) -> threading.RLock:
        return self._write_lock

    def module_names(self) -> list[str]:
        hidden = set(RUNTIME_TABLES) | set(SUPPORT_TABLES)
        return sorted(name for name in self._tables if name not in hidden)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        """分配行内主键。调用方必须持有写锁或处于事务中。"""
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """内存版事务：进入时给全库做快照，异常时整体回滚。

        导出、存档登记与版本号分配必须落在同一个事务里——任何一步失败，
        已组装的半成品都不留痕，保证「整批不出包」。
        """
        with self._write_lock:
            snapshot = copy.deepcopy(self._tables)
            try:
                yield
            except Exception:
                self._tables = snapshot
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

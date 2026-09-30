"""剖面批次打包台业务规则。

把原来「图属相符性导出一份孤立文件」改为成批出包：
1. 出包前先在剖面上确认剖面分层与剖面长度（分层累计长度不相符时以审定边界为准）；
2. 每个剖面要同时产出数据包、图纸清单、核验摘要三件，任一缺失则整批不出包并说明原因；
3. 导出结果与剖面详情、地层台账、外部核对清单三处来源同步留痕，历史导出按原快照保留；
4. 导出、存档与版本号分配在同一事务内完成，断线后接着原批次继续，重复导出只保留一套版本；
5. 大批量走异步队列，批次创建时冻结有序剖面清单，分页清单保持稳定；
6. 历史剖面缺图幅号的启动时补数，迁移保留原编号。

本模块只依赖标准库与内存仓库，便于不启动 Web 服务时直接做业务规则验证。
"""
from __future__ import annotations

import hashlib
import io
import json
import queue
import threading
import zipfile
from datetime import datetime
from typing import Any

from app.store import store

SECTION_MODULE = "section"
LAYER_MODULE = "section_layer"
CHECK_MODULE = "section_checklist"
BATCH_MODULE = "section_export_batch"
ITEM_MODULE = "section_export_item"
ARCHIVE_MODULE = "section_archive"
MIGRATION_MODULE = "section_migration_log"

# 事务联动写入的表。
_TX_TABLES = (BATCH_MODULE, ITEM_MODULE, ARCHIVE_MODULE)

# 超过该数量走后台异步队列；小批量同步出包，便于操作人立刻看到结果。
ASYNC_THRESHOLD = 2

# 长度相符的容差（米）。
LENGTH_TOLERANCE = 1e-6

# 批次状态。
ST_QUEUED = "排队中"
ST_RUNNING = "导出中"
ST_PACKED = "已出包"
ST_REJECTED = "已驳回"

_ITEM_PENDING = "待出包"
_ITEM_PACKED = "已出包"

# 三件必需产物（不含整批公共的总清单/总摘要）。
ARTIFACT_DATA = "数据包"
ARTIFACT_DRAWING = "图纸清单"
ARTIFACT_SUMMARY = "核验摘要"
REQUIRED_ARTIFACTS = (ARTIFACT_DATA, ARTIFACT_DRAWING, ARTIFACT_SUMMARY)

_bootstrap_lock = threading.Lock()
_bootstrapped = False


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _to_float(value: Any) -> float | None:
    """剖面长度/里程可能存成字符串，统一宽松转浮点；无法解析时返回 None。"""
    if value is None or value == "":
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _canonical_json(value: Any) -> str:
    """稳定序列化：键排序、不转义中文、去空白，保证同内容哈希一致。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _short_hash(value: Any) -> str:
    return _sha256_text(value)[:12]


class BatchPackService:
    def __init__(self) -> None:
        self._queue: "queue.Queue[int]" = queue.Queue()
        self._worker_started = False
        # 同一批次同时只允许一个处理者（后台线程与同步 drain 可能竞争）。
        self._processing: set[int] = set()
        self._processing_lock = threading.Lock()
        self._ensure_bootstrap()

    # ------------------------------------------------------------------
    # 启动引导：补图幅号 + 预置一条历史导出快照
    # ------------------------------------------------------------------
    def _ensure_bootstrap(self) -> None:
        global _bootstrapped
        with _bootstrap_lock:
            if _bootstrapped:
                return
            self.backfill_sheet_numbers()
            self._seed_history_snapshot()
            _bootstrapped = True

    def backfill_sheet_numbers(self) -> list[dict[str, Any]]:
        """给历史缺图幅号的剖面补数；只补空值，保留原 id 与剖面编号。

        图幅号取自地质填图台账，按原剖面编号做稳定映射，保证重复执行补到同一张图、
        迁移可重入。每次补数在迁移日志留痕。
        """
        sheets = [str(row.get("图幅编号", "")).strip() for row in store.rows("mapping")]
        sheets = [s for s in sheets if s]
        changed: list[dict[str, Any]] = []
        with store.transaction(SECTION_MODULE, MIGRATION_MODULE):
            for section in sorted(store.rows(SECTION_MODULE), key=lambda r: int(r.get("id", 0))):
                current = str(section.get("图幅号", "") or "").strip()
                if current:
                    continue
                original_no = str(section.get("剖面编号", ""))
                assigned = self._assign_sheet(original_no, sheets)
                section["图幅号"] = assigned
                log_id = store.next_id(MIGRATION_MODULE)
                log_row = {
                    "id": log_id,
                    "section_id": int(section.get("id", 0)),
                    "剖面编号": original_no,
                    "原id": int(section.get("id", 0)),
                    "补数类型": "历史剖面补图幅号",
                    "补入图幅号": assigned,
                    "迁移时间": now(),
                }
                store.rows(MIGRATION_MODULE).append(log_row)
                changed.append(log_row)
        return changed

    @staticmethod
    def _assign_sheet(section_no: str, sheets: list[str]) -> str:
        if not sheets:
            return "待补图幅"
        idx = int(hashlib.sha256(section_no.encode("utf-8")).hexdigest(), 16) % len(sheets)
        return sheets[idx]

    def _seed_history_snapshot(self) -> None:
        """给一条已验收剖面预置历史导出 v1 快照：重复导出时应复用同一版本。"""
        first = next((s for s in sorted(store.rows(SECTION_MODULE), key=lambda r: int(r.get("id", 0)))
                      if s.get("分层确认")), None)
        if first is None:
            return
        section_id = int(first["id"])
        if store.rows(ARCHIVE_MODULE):
            return
        core = self._core_snapshot(section_id)
        if core is None:
            return
        source_refs = self._source_refs(section_id)
        with store.transaction(*_TX_TABLES):
            store.rows(ARCHIVE_MODULE).append({
                "id": store.next_id(ARCHIVE_MODULE),
                "section_id": section_id,
                "剖面编号": core["section"]["剖面编号"],
                "version": 1,
                "content_hash": _sha256_text(core),
                "snapshot": core,
                # 历史快照同样带齐三件产物，重复导出复用时整包文件不缺。
                "artifacts": self._build_artifacts(section_id, core, source_refs),
                "source_refs": source_refs,
                "batch_id": None,
                "来源": "历史快照",
                "created_at": now(),
            })

    # ------------------------------------------------------------------
    # 读取：分层 / 外部核对清单 / 图幅 / 台账来源
    # ------------------------------------------------------------------
    def layers_of(self, section_id: int) -> list[dict[str, Any]]:
        rows = [r for r in store.rows(LAYER_MODULE) if int(r.get("section_id", 0)) == section_id]
        return sorted(rows, key=lambda r: int(r.get("序号", r.get("id", 0))))

    def checklist_of(self, section_id: int) -> list[dict[str, Any]]:
        rows = [r for r in store.rows(CHECK_MODULE) if int(r.get("section_id", 0)) == section_id]
        return sorted(rows, key=lambda r: int(r.get("id", 0)))

    def resolve_sheet(self, section: dict[str, Any]) -> str:
        """解析图幅号：剖面自身优先；历史空值回退到迁移补数结果（即当前值）。"""
        return str(section.get("图幅号", "") or "").strip()

    def _matching_stratigraphy(self, section_id: int) -> list[dict[str, Any]]:
        """地层台账同步：按剖面上各分层的地层名称去台账匹配同名单元。"""
        names = {str(layer.get("地层名称", "")).strip() for layer in self.layers_of(section_id)}
        names.discard("")
        ledger = store.rows("stratigraphy")
        return [row for row in ledger if str(row.get("地层名称", "")).strip() in names]

    def _source_refs(self, section_id: int) -> dict[str, Any]:
        """导出结果与剖面详情、地层台账、外部核对清单三处来源的同步留痕。"""
        section = store.find(SECTION_MODULE, section_id)
        layers = self.layers_of(section_id)
        checklist = self.checklist_of(section_id)
        ledger = self._matching_stratigraphy(section_id)
        sheet_no = self.resolve_sheet(section) if section else ""
        mapping_row = next((r for r in store.rows("mapping")
                            if str(r.get("图幅编号", "")).strip() == sheet_no), None)
        return {
            "剖面详情": {
                "section_id": section_id,
                "hash": _short_hash(section) if section else None,
                "剖面编号": section.get("剖面编号") if section else None,
            },
            "地层台账": {
                "匹配单元数": len(ledger),
                "分层数": len(layers),
                "hash": _short_hash(ledger),
                "匹配单元编号": [r.get("单元编号") for r in ledger],
            },
            "外部核对清单": {
                "条目数": len(checklist),
                "已核数": sum(1 for r in checklist if r.get("核对状态") == "已核"),
                "未核数": sum(1 for r in checklist if r.get("核对状态") != "已核"),
                "hash": _short_hash(checklist),
            },
            "图幅台账": {
                "图幅号": sheet_no,
                "命中": mapping_row is not None,
                "图幅名称": mapping_row.get("图幅名称") if mapping_row else None,
            },
        }

    # ------------------------------------------------------------------
    # 出包前确认：剖面分层与剖面长度
    # ------------------------------------------------------------------
    def cumulative_length(self, section_id: int) -> float | None:
        """分层累计长度以审定边界为准：取末层审定边界（无则回退层底里程）。"""
        layers = self.layers_of(section_id)
        if not layers:
            return None
        last = layers[-1]
        boundary = _to_float(last.get("审定边界"))
        if boundary is None:
            boundary = _to_float(last.get("层底里程"))
        return boundary

    def confirm_layers(
        self, section_id: int, audited_boundary: float | None = None
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        """确认剖面分层与剖面长度。

        分层累计长度与剖面长度不相符时，以审定边界为准，把审定长度写回剖面；
        可选的 audited_boundary 表示审定会给出的末层边界，用于现场裁定。
        返回 (剖面, 说明, 核对结果)。
        """
        section = store.find(SECTION_MODULE, section_id)
        if section is None:
            return None, f"实测剖面 {section_id} 不存在或已归档", {}
        layers = self.layers_of(section_id)
        if not layers:
            return None, "剖面分层缺失，无法确认剖面长度，请先在剖面台账补录分层", {}

        declared = _to_float(section.get("剖面长度"))
        with store.transaction(SECTION_MODULE, LAYER_MODULE):
            if audited_boundary is not None:
                layers[-1]["审定边界"] = audited_boundary
            # 审定边界缺省按层底里程补齐，使「以审定边界为准」始终可计算。
            for layer in layers:
                if _to_float(layer.get("审定边界")) is None:
                    layer["审定边界"] = _to_float(layer.get("层底里程"))
            cumulative = self.cumulative_length(section_id)
            mismatch = declared is not None and cumulative is not None and abs(
                (declared or 0.0) - cumulative) > LENGTH_TOLERANCE
            section["审定长度"] = cumulative
            section["分层确认"] = True
        note = (
            f"分层累计长度 {cumulative}m 与申报剖面长度 {declared}m 不相符，已按审定边界确认为 {cumulative}m"
            if mismatch else f"剖面分层与剖面长度 {cumulative}m 相符，已确认"
        )
        result = {
            "剖面长度": declared,
            "分层累计长度": cumulative,
            "审定长度": cumulative,
            "是否相符": not mismatch,
            "分层数": len(layers),
        }
        return section, note, result

    def reconcile_checklist_item(self, item_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """登记外部核对结论（图属相符性等），使外部清单与剖面同步。"""
        row = next((r for r in store.rows(CHECK_MODULE) if int(r.get("id", 0)) == item_id), None)
        if row is None:
            return None, f"核对项 {item_id} 不存在"
        with store.transaction(CHECK_MODULE):
            if values.get("核对状态"):
                row["核对状态"] = str(values["核对状态"]).strip()
            if values.get("说明") is not None:
                row["说明"] = str(values["说明"])
            if values.get("核对日期"):
                row["核对日期"] = str(values["核对日期"])
        return row, "外部核对结论已登记"

    # ------------------------------------------------------------------
    # 预检：三件产物缺一不可，整批不出包
    # ------------------------------------------------------------------
    def preflight_section(self, section_id: int) -> tuple[bool, list[str], dict[str, Any]]:
        """逐条检查出包前置条件；返回 (通过, 原因清单, 核对摘要)。"""
        section = store.find(SECTION_MODULE, section_id)
        reasons: list[str] = []
        if section is None:
            return False, [f"剖面 {section_id} 不存在或已归档"], {}

        # 1) 数据包前提：剖面分层与剖面长度已确认。
        if not section.get("分层确认"):
            reasons.append("剖面分层与剖面长度尚未确认（请先在剖面上确认分层）")
            data_ok = False
            length_info: dict[str, Any] = {}
        else:
            data_ok = bool(self.layers_of(section_id))
            if not data_ok:
                reasons.append("缺少剖面分层数据，数据包无法生成")
            cumulative = self.cumulative_length(section_id)
            length_info = {
                "剖面长度": _to_float(section.get("剖面长度")),
                "分层累计长度": cumulative,
                "审定长度": _to_float(section.get("审定长度")),
            }

        # 2) 图纸清单前提：图幅号已补（历史缺图幅的迁移后应满足）。
        sheet_no = self.resolve_sheet(section)
        drawing_ok = bool(sheet_no) and sheet_no != "待补图幅"
        if not drawing_ok:
            reasons.append("缺图幅号，图纸清单无法生成（历史剖面请先执行补数迁移）")

        # 3) 核验摘要前提：外部核对清单全部已核。
        checklist = self.checklist_of(section_id)
        pending = [r for r in checklist if r.get("核对状态") != "已核"]
        summary_ok = bool(checklist) and not pending
        if not checklist:
            reasons.append("缺少外部核对清单，核验摘要无法生成")
        elif pending:
            labels = "、".join(f"[{r.get('核对项')}]{r.get('说明') or '未核'}" for r in pending)
            reasons.append(f"外部核对清单未全部核对：{labels}")

        artifacts = {
            ARTIFACT_DATA: data_ok,
            ARTIFACT_DRAWING: drawing_ok,
            ARTIFACT_SUMMARY: summary_ok,
        }
        return not reasons, reasons, {
            "artifacts": artifacts,
            "length": length_info,
            "图幅号": sheet_no,
            "未核项": [r.get("核对项") for r in pending],
        }

    # ------------------------------------------------------------------
    # 版本与快照：重复导出复用同版本，历史快照不可变
    # ------------------------------------------------------------------
    def _core_snapshot(self, section_id: int) -> dict[str, Any] | None:
        section = store.find(SECTION_MODULE, section_id)
        if section is None:
            return None
        # 只取与成果内容相关的字段；id/状态/pending 等流转字段不进内容哈希。
        section_fields = {
            key: section.get(key)
            for key in ("剖面编号", "剖面名称", "剖面长度", "起点坐标", "终点坐标",
                        "编录日期", "编录人员", "图幅号", "分层确认", "审定长度")
        }
        return {
            "section": section_fields,
            "layers": [
                {key: layer.get(key) for key in ("序号", "层号", "地层名称", "层底里程", "分层厚度", "岩性描述", "审定边界")}
                for layer in self.layers_of(section_id)
            ],
            "checklist": [
                {key: row.get(key) for key in ("核对项", "外部来源", "核对状态", "说明", "核对日期")}
                for row in self.checklist_of(section_id)
            ],
        }

    def _build_artifacts(self, section_id: int, core: dict[str, Any], source_refs: dict[str, Any]) -> dict[str, str]:
        """生成数据包、图纸清单、核验摘要三件 JSON 文本。"""
        section = core["section"]
        cumulative = self.cumulative_length(section_id)
        declared = _to_float(section.get("剖面长度"))
        data_packet = {
            "类型": ARTIFACT_DATA,
            "剖面编号": section.get("剖面编号"),
            "剖面名称": section.get("剖面名称"),
            "剖面长度": declared,
            "审定长度": cumulative,
            "起点坐标": section.get("起点坐标"),
            "终点坐标": section.get("终点坐标"),
            "分层": core["layers"],
            "来源同步": source_refs,
        }
        drawing_list = {
            "类型": ARTIFACT_DRAWING,
            "剖面编号": section.get("剖面编号"),
            "图幅号": self.resolve_sheet(store.find(SECTION_MODULE, section_id) or {}),
            "图幅名称": source_refs["图幅台账"]["图幅名称"],
            "图纸张数": len(core["layers"]),
            "图纸明细": [
                {"序号": layer["序号"], "层号": layer["层号"], "图名": f"{section.get('剖面编号')}-{layer['层号']} {layer['地层名称']}"}
                for layer in core["layers"]
            ],
            "来源同步": source_refs,
        }
        verify_summary = {
            "类型": ARTIFACT_SUMMARY,
            "剖面编号": section.get("剖面编号"),
            "长度核验": {
                "申报剖面长度": declared,
                "分层累计长度": cumulative,
                "审定长度": cumulative,
                "是否相符": (declared is not None and cumulative is not None
                          and abs(declared - cumulative) <= LENGTH_TOLERANCE),
                "裁定口径": "分层累计长度不相符时以审定边界为准",
            },
            "图属相符性": "相符" if all(item["核对状态"] == "已核" for item in core["checklist"]) else "未通过",
            "外部核对": core["checklist"],
            "地层台账匹配": source_refs["地层台账"],
            "来源同步": source_refs,
        }
        return {
            f"{section.get('剖面编号')}_数据包.json": _canonical_json(data_packet),
            f"{section.get('剖面编号')}_图纸清单.json": _canonical_json(drawing_list),
            f"{section.get('剖面编号')}_核验摘要.json": _canonical_json(verify_summary),
        }

    def _allocate_archive(
        self, section_id: int, batch_id: int
    ) -> tuple[dict[str, Any], bool]:
        """事务内导出 + 存档 + 分配版本号。

        内容哈希与该剖面最近一次存档相同则复用版本、不新增存档（重复导出只保留一套版本）；
        否则分配下一个版本号并写入不可变快照。返回 (存档行, 是否复用旧版本)。
        """
        with store.transaction(*_TX_TABLES):
            core = self._core_snapshot(section_id)
            if core is None:
                raise ValueError(f"剖面 {section_id} 不存在，无法存档")
            content_hash = _sha256_text(core)
            source_refs = self._source_refs(section_id)
            section_no = core["section"]["剖面编号"]
            existing = [r for r in store.rows(ARCHIVE_MODULE) if int(r.get("section_id", 0)) == section_id]
            latest = max(existing, key=lambda r: int(r.get("version", 0)), default=None)
            if latest is not None and latest.get("content_hash") == content_hash:
                return latest, True
            version = (int(latest.get("version", 0)) + 1) if latest else 1
            archive = {
                "id": store.next_id(ARCHIVE_MODULE),
                "section_id": section_id,
                "剖面编号": section_no,
                "version": version,
                "content_hash": content_hash,
                "snapshot": core,
                "artifacts": self._build_artifacts(section_id, core, source_refs),
                "source_refs": source_refs,
                "batch_id": batch_id,
                "来源": "批次导出",
                "created_at": now(),
            }
            store.rows(ARCHIVE_MODULE).append(archive)
            return archive, False

    # ------------------------------------------------------------------
    # 批次：创建（冻结清单）/ 预检 / 出包 / 续传 / 下载
    # ------------------------------------------------------------------
    def _resolve_section_ids(
        self,
        section_ids: list[int] | None,
        keyword: str | None,
        status: str | None,
    ) -> list[int]:
        """按显式勾选或当前过滤口径解析剖面，并冻结为按 id 升序的稳定清单。"""
        rows = store.rows(SECTION_MODULE)
        if section_ids:
            wanted = {int(x) for x in section_ids}
            rows = [r for r in rows if int(r.get("id", 0)) in wanted]
        else:
            if keyword:
                rows = [r for r in rows if keyword in str(r.get("剖面编号", ""))]
            if status:
                rows = [r for r in rows if r.get("status") == status]
        return [int(r["id"]) for r in sorted(rows, key=lambda r: int(r.get("id", 0)))]

    def create_batch(
        self,
        *,
        section_ids: list[int] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        operator: str = "值班员",
    ) -> tuple[dict[str, Any] | None, str]:
        """创建导出批次：冻结有序剖面清单（分页稳定），小批量同步出包，大批量入队。

        若同一批剖面已有一个未终态（排队/导出中）批次，则直接返回该批次，
        避免重复建批次；重复导出的版本复用在存档环节处理。
        """
        frozen_ids = self._resolve_section_ids(section_ids, keyword, status)
        if not frozen_ids:
            return None, "当前条件下没有可导出的实测剖面"

        signature = self._batch_signature(frozen_ids)
        with store.transaction(*_TX_TABLES):
            for existing in sorted(store.rows(BATCH_MODULE), key=lambda r: -int(r.get("id", 0))):
                if existing.get("signature") == signature and existing.get("status") in (ST_QUEUED, ST_RUNNING):
                    return existing, f"已存在进行中的批次 {existing['batch_no']}，已接续该批次继续导出"
            batch_id = store.next_id(BATCH_MODULE)
            batch_no = self._batch_no(batch_id)
            batch = {
                "id": batch_id,
                "batch_no": batch_no,
                "status": ST_QUEUED,
                "frozen_section_ids": list(frozen_ids),
                "signature": signature,
                "operator": operator,
                "created_at": now(),
                "updated_at": now(),
                "heartbeat": now(),
                "package_name": None,
                "失败原因": [],
                "total": len(frozen_ids),
                "packed": 0,
            }
            store.rows(BATCH_MODULE).append(batch)
            for index, sid in enumerate(frozen_ids, start=1):
                store.rows(ITEM_MODULE).append({
                    "id": store.next_id(ITEM_MODULE),
                    "batch_id": batch_id,
                    "section_id": sid,
                    "seq": index,
                    "status": _ITEM_PENDING,
                    "version": None,
                    "archive_id": None,
                    "复用旧版本": False,
                    "预检通过": False,
                    "缺失原因": [],
                    "artifacts": [],
                })

        # 先入队（同时启动后台线程）；小批量再同步处理一次，让操作人立刻看到结果。
        self._enqueue(batch_id)
        if len(frozen_ids) <= ASYNC_THRESHOLD:
            try:
                self.process_batch(batch_id)
            except Exception:
                # 同步阶段遇到非预期错误（如存档写入失败）不抛给调用方：
                # 批次保留在队列里，操作人可「接续导出」接着原批次重试。
                pass
        return self.get_batch(batch_id), ("批次已入异步队列" if len(frozen_ids) > ASYNC_THRESHOLD else "批次已同步处理")

    @staticmethod
    def _batch_signature(frozen_ids: list[int]) -> str:
        return _sha256_text(frozen_ids)

    @staticmethod
    def _batch_no(batch_id: int) -> str:
        return f"PACK-{datetime.now().strftime('%Y%m%d')}-{batch_id:04d}"

    def _enqueue(self, batch_id: int) -> None:
        with store.transaction(BATCH_MODULE):
            batch = store.find(BATCH_MODULE, batch_id)
            if batch is not None and batch["status"] in (ST_QUEUED, ST_RUNNING, ST_REJECTED):
                batch["status"] = ST_QUEUED
                batch["updated_at"] = now()
                batch["heartbeat"] = now()
        self._queue.put(batch_id)
        self._ensure_worker()

    def resume_batch(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        """断线后接着原批次继续导出：已出包的剖面不重做，只处理待出包项。"""
        batch = store.find(BATCH_MODULE, batch_id)
        if batch is None:
            return None, f"批次 {batch_id} 不存在"
        if batch["status"] == ST_PACKED:
            return self.get_batch(batch_id), "该批次已出包，可直接下载；重复导出不会产生新版本"
        self._enqueue(batch_id)
        # 队列很短时同步推进一次，便于操作人立刻看到续跑结果。
        if int(batch.get("total", 0)) <= ASYNC_THRESHOLD:
            self.drain_queue()
        return self.get_batch(batch_id), f"已接续批次 {batch['batch_no']} 继续导出"

    def process_batch(self, batch_id: int) -> dict[str, Any]:
        """处理一个批次：逐条预检 → 任一缺失整批驳回；齐全则逐条事务化存档出包。

        已出包项直接跳过（断线续跑/重复导出幂等）。处理全程更新心跳。
        同一批次串行：后台线程与同步续传竞争时后到者直接读现状返回。
        """
        with self._processing_lock:
            if batch_id in self._processing:
                return store.find(BATCH_MODULE, batch_id) or {}
            self._processing.add(batch_id)
        try:
            return self._process_batch_locked(batch_id)
        finally:
            with self._processing_lock:
                self._processing.discard(batch_id)

    def _process_batch_locked(self, batch_id: int) -> dict[str, Any]:
        batch = store.find(BATCH_MODULE, batch_id)
        if batch is None or batch["status"] == ST_PACKED:
            return batch or {}

        items = self._batch_items(batch_id)
        # 整批先做预检：任一剖面缺任一文件，整批不出包。
        blocking: list[dict[str, Any]] = []
        with store.transaction(BATCH_MODULE, ITEM_MODULE):
            batch["status"] = ST_RUNNING
            batch["heartbeat"] = now()
            batch["updated_at"] = now()
            for item in items:
                if item["status"] == _ITEM_PACKED:
                    item["预检通过"] = True
                    continue
                ok, reasons, info = self.preflight_section(int(item["section_id"]))
                item["预检通过"] = ok
                item["缺失原因"] = reasons
                if not ok:
                    blocking.append({"section_id": int(item["section_id"]), "reasons": reasons, "info": info})

        if blocking:
            with store.transaction(BATCH_MODULE, ITEM_MODULE):
                batch["status"] = ST_REJECTED
                reasons_flat = [
                    f"{self._section_label(b['section_id'])}：{reason}"
                    for b in blocking for reason in b["reasons"]
                ]
                batch["失败原因"] = reasons_flat
                batch["updated_at"] = now()
                batch["heartbeat"] = now()
            return batch

        # 预检全部通过：逐条事务化「导出 + 存档 + 版本号」。
        for item in items:
            if item["status"] == _ITEM_PACKED:
                continue
            with store.transaction(BATCH_MODULE, ITEM_MODULE):
                archive, reused = self._allocate_archive(int(item["section_id"]), batch_id)
                item["status"] = _ITEM_PACKED
                item["version"] = int(archive["version"])
                item["archive_id"] = int(archive["id"])
                item["复用旧版本"] = reused
                item["artifacts"] = list(archive.get("artifacts", {}).keys())
                item["缺失原因"] = []
                batch["packed"] = sum(1 for it in items if it["status"] == _ITEM_PACKED)
                batch["heartbeat"] = now()

        # 全部出包后整批定版：数据包/图纸清单/核验摘要 + 整批清单与摘要一起封包。
        with store.transaction(BATCH_MODULE, ITEM_MODULE):
            if all(it["status"] == _ITEM_PACKED for it in items):
                package_name = f"{batch['batch_no']}.zip"
                batch["status"] = ST_PACKED
                batch["package_name"] = package_name
                batch["失败原因"] = []
            batch["updated_at"] = now()
            batch["heartbeat"] = now()
        return batch

    def _section_label(self, section_id: int) -> str:
        section = store.find(SECTION_MODULE, section_id)
        if section is None:
            return f"剖面{section_id}"
        return f"{section.get('剖面编号')}（{section.get('剖面名称')}）"

    def _batch_items(self, batch_id: int) -> list[dict[str, Any]]:
        rows = [r for r in store.rows(ITEM_MODULE) if int(r.get("batch_id", 0)) == batch_id]
        return sorted(rows, key=lambda r: int(r.get("seq", r.get("id", 0))))

    # ------------------------------------------------------------------
    # 异步队列与后台工作线程
    # ------------------------------------------------------------------
    def _ensure_worker(self) -> None:
        if self._worker_started:
            return
        self._worker_started = True
        thread = threading.Thread(target=self._worker_loop, name="section-batch-worker", daemon=True)
        thread.start()

    def _worker_loop(self) -> None:
        while True:
            try:
                batch_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.process_batch(batch_id)
            except Exception:
                # 单批处理异常不应打死工作线程；批次保留导出中，可由续传重跑。
                continue

    def drain_queue(self) -> None:
        """同步把队列里的批次全部处理完（测试与小批量续跑用）。"""
        while True:
            try:
                batch_id = self._queue.get_nowait()
            except queue.Empty:
                return
            self.process_batch(batch_id)

    def recover_interrupted(self) -> int:
        """启动时把残留的排队中/导出中批次重新入队，接着原批次继续。"""
        recovered = 0
        for batch in store.rows(BATCH_MODULE):
            if batch.get("status") in (ST_QUEUED, ST_RUNNING):
                self._enqueue(int(batch["id"]))
                recovered += 1
        return recovered

    # ------------------------------------------------------------------
    # 列表与明细（稳定分页）
    # ------------------------------------------------------------------
    def list_batches(self, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        rows = sorted(store.rows(BATCH_MODULE), key=lambda r: int(r.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._batch_view(r) for r in rows[start:start + size]], total

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        batch = store.find(BATCH_MODULE, batch_id)
        if batch is None:
            return None
        # 导出中但无工作线程推进（如服务重启），视为断线，自动接续原批次。
        if batch.get("status") == ST_RUNNING and not self._worker_alive():
            self.resume_batch(batch_id)
            batch = store.find(BATCH_MODULE, batch_id)
        return self._batch_view(batch)

    def _worker_alive(self) -> bool:
        return any(t.name == "section-batch-worker" and t.is_alive()
                   for t in threading.enumerate())

    def _batch_view(self, batch: dict[str, Any]) -> dict[str, Any]:
        view = {k: v for k, v in batch.items() if k != "signature"}
        view["items"] = [self._item_view(it) for it in self._batch_items(int(batch["id"]))]
        return view

    def _item_view(self, item: dict[str, Any]) -> dict[str, Any]:
        section = store.find(SECTION_MODULE, int(item["section_id"]))
        return {
            "seq": item.get("seq"),
            "section_id": item.get("section_id"),
            "剖面编号": section.get("剖面编号") if section else None,
            "剖面名称": section.get("剖面名称") if section else None,
            "status": item.get("status"),
            "预检通过": item.get("预检通过"),
            "version": item.get("version"),
            "复用旧版本": item.get("复用旧版本"),
            "artifacts": item.get("artifacts", []),
            "缺失原因": item.get("缺失原因", []),
        }

    def section_detail(self, section_id: int) -> dict[str, Any] | None:
        """剖面详情：基础字段 + 分层 + 外部核对 + 三处来源同步 + 历史导出版本。"""
        section = store.find(SECTION_MODULE, section_id)
        if section is None:
            return None
        archives = sorted(
            (r for r in store.rows(ARCHIVE_MODULE) if int(r.get("section_id", 0)) == section_id),
            key=lambda r: int(r.get("version", 0)),
        )
        latest = archives[-1] if archives else None
        core_hash = _sha256_text(self._core_snapshot(section_id)) if self._core_snapshot(section_id) else None
        in_sync = latest is not None and latest.get("content_hash") == core_hash
        return {
            **section,
            "layers": self.layers_of(section_id),
            "checklist": self.checklist_of(section_id),
            "source_refs": self._source_refs(section_id),
            "archives": [
                {
                    "version": r.get("version"),
                    "content_hash": r.get("content_hash"),
                    "来源": r.get("来源"),
                    "created_at": r.get("created_at"),
                    "batch_id": r.get("batch_id"),
                    "artifacts": sorted(r.get("artifacts", {}).keys()),
                }
                for r in archives
            ],
            "当前与最新导出同步": in_sync,
            "分层累计长度": self.cumulative_length(section_id),
        }

    # ------------------------------------------------------------------
    # 打包下载：数据包 + 图纸清单 + 核验摘要 + 整批清单/总摘要
    # ------------------------------------------------------------------
    def build_package(self, batch_id: int) -> tuple[bytes | None, str | None, str]:
        """从不可变存档重建整批 zip；已驳回/未出包批次不出文件。"""
        batch = store.find(BATCH_MODULE, batch_id)
        if batch is None:
            return None, None, f"批次 {batch_id} 不存在"
        if batch.get("status") != ST_PACKED:
            reasons = batch.get("失败原因") or ["批次尚未完成出包"]
            return None, None, "整批未出包：" + "；".join(reasons)
        items = self._batch_items(batch_id)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            manifest = self._batch_manifest(batch, items)
            zf.writestr("批次清单.json", _canonical_json(manifest))
            zf.writestr("核验总摘要.json", _canonical_json(self._batch_summary(batch, items)))
            for item in items:
                archive = store.find(ARCHIVE_MODULE, int(item["archive_id"])) if item.get("archive_id") else None
                if archive is None:
                    # 理论上不该发生：已出包项必有存档；缺失则整批不给出文件。
                    return None, None, f"{self._section_label(int(item['section_id']))} 的存档快照缺失，整批不出包"
                section = store.find(SECTION_MODULE, int(item["section_id"]))
                folder = section.get("剖面编号") if section else item["section_id"]
                for filename, text in archive.get("artifacts", {}).items():
                    zf.writestr(f"{folder}/{filename}", text)
        package_name = batch.get("package_name") or f"{batch['batch_no']}.zip"
        return buffer.getvalue(), package_name, "ok"

    def _batch_manifest(self, batch: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "批次号": batch["batch_no"],
            "状态": batch["status"],
            "操作人": batch.get("operator"),
            "创建时间": batch.get("created_at"),
            "出包时间": batch.get("updated_at"),
            "冻结剖面清单": [
                {"seq": it.get("seq"), "section_id": it.get("section_id"),
                 "剖面编号": self._item_view(it).get("剖面编号"),
                 "version": it.get("version"), "复用旧版本": it.get("复用旧版本")}
                for it in items
            ],
            "必需产物": list(REQUIRED_ARTIFACTS),
            "说明": "任一剖面缺数据包/图纸清单/核验摘要，整批不出包；分页清单按 section_id 升序冻结。",
        }

    def _batch_summary(self, batch: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
        rows = []
        for it in items:
            section = store.find(SECTION_MODULE, int(it["section_id"]))
            core = self._core_snapshot(int(it["section_id"]))
            rows.append({
                "剖面编号": section.get("剖面编号") if section else None,
                "申报剖面长度": core["section"].get("剖面长度") if core else None,
                "审定长度": self.cumulative_length(int(it["section_id"])),
                "版本": it.get("version"),
                "复用旧版本": it.get("复用旧版本"),
                "来源同步": self._source_refs(int(it["section_id"])),
            })
        return {
            "批次号": batch["batch_no"],
            "剖面数": len(items),
            "出包数": batch.get("packed"),
            "长度裁定口径": "分层累计长度与剖面长度不相符时以审定边界为准",
            "明细": rows,
        }

    def migration_logs(self) -> list[dict[str, Any]]:
        return sorted(store.rows(MIGRATION_MODULE), key=lambda r: int(r.get("id", 0)))


batch_pack_service = BatchPackService()

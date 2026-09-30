"""实测剖面「批次打包台」业务规则。

把原来的图属相符性单文件导出改成整批打包：
- 导出前先逐剖面确认剖面分层与剖面长度（确认记录留痕）；
- 每个剖面必须同时备齐数据包、图纸清单、核验摘要三份材料，缺一即整批不出包；
- 导出内容与剖面详情、地层（分层）台账、外部核对清单同源快照；
- 分层累计长度与剖面长度不相符时，以审定边界累计长度为准；
- 导出登记、存档、版本号分配在同一事务内提交，半成品不留痕；
- 打包进度逐条落库，中断后按原批次续传，重复导出按指纹复用同一版本；
- 大批量进异步队列，批次清单按键集分页，翻页期间顺序稳定；
- 历史导出按出包时快照永久保留，不随后续业务数据变动。
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import zipfile
from datetime import datetime
from io import BytesIO
from typing import Any

from app.store import store

MODULE = "section"
LAYER_MODULE = "section_layer"
DRAWING_MODULE = "section_drawing"
CHECKLIST_MODULE = "section_checklist"
CONFIRM_MODULE = "section_confirm"
BATCH_MODULE = "export_batch"
ITEM_MODULE = "export_batch_item"
ARCHIVE_MODULE = "export_archive"

# 超过该剖面数的批次进异步队列，由后台线程顺序处理
ASYNC_THRESHOLD = 3
# 长度核验容差（米），小于该差值视为一致
LENGTH_TOLERANCE = 0.01
# 每个剖面必备的图种，任一缺失或未归档都视为文件缺失
REQUIRED_DRAWING_KINDS = ("剖面图", "柱状图")
VERSION_PREFIX = "SEC-EXP-V"

BATCH_QUEUED = "排队中"
BATCH_PACKING = "打包中"
BATCH_DONE = "已出包"
BATCH_INTERRUPTED = "已中断"


class PackError(RuntimeError):
    """打包过程中发现的硬性问题：触发批次中断并给出可读原因。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _to_float(value: Any) -> float | None:
    try:
        return round(float(str(value).strip()), 2)
    except (TypeError, ValueError):
        return None


class BatchPackingService:
    def __init__(self) -> None:
        self._queue: "queue.Queue[int]" = queue.Queue()
        self._worker_started = False
        self._worker_lock = threading.Lock()

    # ------------------------------------------------------------------ 取数

    def find_section(self, *, entry_id: int | None = None, section_code: str | None = None) -> dict[str, Any] | None:
        if entry_id is not None:
            return store.find(MODULE, entry_id)
        if section_code:
            for row in store.rows(MODULE):
                if str(row.get("剖面编号") or "") == section_code:
                    return row
        return None

    def layers_of(self, section_code: str) -> list[dict[str, Any]]:
        return sorted(
            (dict(row) for row in store.rows(LAYER_MODULE) if str(row.get("剖面编号")) == section_code),
            key=lambda row: int(row.get("层号", 0) or 0),
        )

    def drawings_of(self, section_code: str) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(DRAWING_MODULE) if str(row.get("剖面编号")) == section_code]

    def checklists_of(self, section_code: str) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(CHECKLIST_MODULE) if str(row.get("剖面编号")) == section_code]

    def confirm_of(self, section_code: str) -> dict[str, Any] | None:
        for row in store.rows(CONFIRM_MODULE):
            if str(row.get("剖面编号")) == section_code:
                return row
        return None

    def length_reconcile(self, section: dict[str, Any], layers: list[dict[str, Any]]) -> dict[str, Any]:
        """分层累计长度核验：原始累计与申报长度不符时，以审定边界累计为准。"""
        declared = _to_float(section.get("剖面长度"))
        raw_total = round(sum(_to_float(layer.get("原始厚度")) or 0.0 for layer in layers), 2)
        approved_total = round(sum(_to_float(layer.get("审定厚度")) or 0.0 for layer in layers), 2)
        adjusted_declared = declared is not None and abs(approved_total - declared) > LENGTH_TOLERANCE
        mismatched_raw = declared is not None and abs(raw_total - declared) > LENGTH_TOLERANCE
        note = ""
        if adjusted_declared and mismatched_raw:
            note = (
                f"分层原始累计 {raw_total:.2f}m、审定边界累计 {approved_total:.2f}m "
                f"均与剖面长度 {declared:.2f}m 不符，以审定边界累计 {approved_total:.2f}m 出包"
            )
        elif adjusted_declared:
            note = (
                f"审定边界累计 {approved_total:.2f}m 与剖面长度 {declared:.2f}m 不符，"
                f"以审定边界累计 {approved_total:.2f}m 出包"
            )
        elif mismatched_raw:
            note = (
                f"分层原始累计 {raw_total:.2f}m 与剖面长度 {declared:.2f}m 有出入，"
                f"审定边界累计一致，按审定值 {approved_total:.2f}m 出包"
            )
        return {
            "declared_length": declared,
            "raw_cumulative": raw_total,
            "approved_cumulative": approved_total,
            "final_length": approved_total if layers else declared,
            "adjusted": adjusted_declared or mismatched_raw,
            "note": note,
        }

    def snapshot(self, section: dict[str, Any]) -> dict[str, Any]:
        """剖面详情、分层台账、图纸清单、外部核对清单的同源快照。"""
        code = str(section.get("剖面编号"))
        layers = self.layers_of(code)
        length = self.length_reconcile(section, layers)
        return {
            "section": dict(section),
            "layers": layers,
            "drawings": self.drawings_of(code),
            "external_checklists": self.checklists_of(code),
            "length_reconcile": length,
        }

    def _missing_drawings(self, drawings: list[dict[str, Any]]) -> list[str]:
        missing: list[str] = []
        for kind in REQUIRED_DRAWING_KINDS:
            candidates = [row for row in drawings if row.get("图种") == kind]
            present = [row for row in candidates if row.get("present") and str(row.get("归档号") or "").strip()]
            if not present:
                if not candidates:
                    missing.append(f"缺图纸：{kind}未登记")
                else:
                    missing.append(f"缺图纸：{kind}未归档或文件缺失（{candidates[0].get('图纸编号')}）")
        return missing

    # ------------------------------------------------------------ 预检/确认

    def preflight_section(self, section: dict[str, Any]) -> dict[str, Any]:
        """单剖面出包前核验：返回阻塞原因与提示，不改动任何数据。"""
        code = str(section.get("剖面编号"))
        layers = self.layers_of(code)
        drawings = self.drawings_of(code)
        checklists = self.checklists_of(code)
        length = self.length_reconcile(section, layers)
        confirm = self.confirm_of(code)

        blocking: list[str] = []
        warnings: list[str] = []

        if not layers:
            blocking.append("剖面分层台账为空，无法确认分层与长度")
        if confirm is None:
            blocking.append("尚未在剖面上确认剖面分层与剖面长度")
        elif layers:
            current_hash = _digest({"layers": layers, "approved": length["approved_cumulative"]})
            if current_hash != confirm.get("layer_hash"):
                blocking.append("确认后分层台账发生变更，请重新确认剖面分层与剖面长度")

        blocking.extend(self._missing_drawings(drawings))
        if not checklists:
            blocking.append("缺外部核对清单，图属相符性无第三方核验依据")
        elif not all(bool(item.get("全部相符")) for item in checklists):
            blocking.append("外部核对清单存在未相符项，需先闭环")

        if length["note"]:
            warnings.append(length["note"])
        if section.get("图幅号来源") == "历史补数迁移":
            warnings.append(f"图幅号为历史补数（{section.get('图幅编号')}），原剖面编号保持不变")

        return {
            "entry_id": section.get("id"),
            "剖面编号": code,
            "剖面名称": section.get("剖面名称"),
            "剖面长度": section.get("剖面长度"),
            "图幅编号": section.get("图幅编号") or "",
            "图幅号来源": section.get("图幅号来源") or "",
            "status": section.get("status"),
            "layers_count": len(layers),
            "length": length,
            "confirmed": confirm is not None,
            "confirmed_at": (confirm or {}).get("confirmed_at"),
            "confirmed_by": (confirm or {}).get("operator"),
            "drawings_total": len(drawings),
            "drawings_present": sum(1 for row in drawings if row.get("present")),
            "checklists_total": len(checklists),
            "blocking": blocking,
            "warnings": warnings,
            "ready": not blocking,
        }

    def packing_overview(self) -> list[dict[str, Any]]:
        return [self.preflight_section(section) for section in store.rows(MODULE)]

    def confirm_section(
        self,
        *,
        entry_id: int | None = None,
        section_code: str | None = None,
        operator: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """在剖面上确认剖面分层与剖面长度；累计不符以审定边界为准并写入确认记录。"""
        section = self.find_section(entry_id=entry_id, section_code=section_code)
        if section is None:
            return None, "剖面不存在或已归档，无法确认"
        code = str(section.get("剖面编号"))
        layers = self.layers_of(code)
        if not layers:
            return None, f"剖面 {code} 分层台账为空，无法确认分层与长度"
        length = self.length_reconcile(section, layers)
        layer_hash = _digest({"layers": layers, "approved": length["approved_cumulative"]})
        with store.transaction():
            record = self.confirm_of(code)
            payload = {
                "剖面编号": code,
                "declared_length": length["declared_length"],
                "raw_cumulative": length["raw_cumulative"],
                "approved_cumulative": length["approved_cumulative"],
                "final_length": length["final_length"],
                "adjusted": length["adjusted"],
                "note": length["note"],
                "layer_hash": layer_hash,
                "operator": operator or "值班员",
                "confirmed_at": _now(),
            }
            if record is None:
                payload["id"] = store.next_id(CONFIRM_MODULE)
                store.rows(CONFIRM_MODULE).append(payload)
            else:
                payload["id"] = record["id"]
                record.clear()
                record.update(payload)
        return self.preflight_section(section), ("分层与长度已按审定边界确认" if length["adjusted"] else "剖面分层与长度已确认")

    # -------------------------------------------------------------- 建批/去重

    def _fingerprint(self, snapshots: list[dict[str, Any]]) -> str:
        return _digest([snap["section"].get("剖面编号") for snap in snapshots] + snapshots)

    def _find_active_batch(self, fingerprint: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_MODULE):
            if row.get("fingerprint") == fingerprint and row.get("status") != BATCH_DONE:
                return row
        return None

    def find_archive(self, fingerprint: str) -> dict[str, Any] | None:
        for row in store.rows(ARCHIVE_MODULE):
            if row.get("fingerprint") == fingerprint:
                return row
        return None

    def create_batch(self, section_codes: list[str], *, operator: str | None = None) -> tuple[dict[str, Any] | None, list[dict[str, str]], str]:
        """创建导出批次。任一剖面材料不齐则整批不建单，逐条说明原因。"""
        codes = sorted({str(code).strip() for code in section_codes if str(code).strip()})
        if not codes:
            return None, [], "未选择任何剖面，打包台无单可建"

        preflights: list[dict[str, Any]] = []
        not_found: list[str] = []
        for code in codes:
            section = self.find_section(section_code=code)
            if section is None:
                not_found.append(code)
                continue
            preflights.append(self.preflight_section(section))

        failures = [{"剖面编号": code, "原因": "剖面不存在或已归档"} for code in not_found]
        for item in preflights:
            for reason in item["blocking"]:
                failures.append({"剖面编号": item["剖面编号"], "原因": reason})
        if failures:
            # 任一文件缺失/未确认 → 整批不出包，不产生批次、不占版本号
            return None, failures, "整批未出包：存在材料不齐的剖面，原因见明细"

        snapshots = [self.snapshot(self.find_section(section_code=item["剖面编号"]) or {}) for item in preflights]
        fingerprint = self._fingerprint(snapshots)

        # 重复导出：同一指纹已有存档，只保留一套版本
        archived = self.find_archive(fingerprint)
        if archived is not None:
            return {
                "deduped": True,
                "batch_id": archived.get("source_batch_id"),
                "archive_id": archived.get("id"),
                "version_code": archived.get("version_code"),
                "package_name": archived.get("package_name"),
                "message": "相同内容曾导出，沿用既有版本，不重复分配版本号",
            }, [], ""

        # 同一指纹有未完成批次（含中断）：接着原批次继续，不另开新批
        active = self._find_active_batch(fingerprint)
        if active is not None:
            return self._batch_view(active), [], "该批次已存在，接着原批次继续导出"

        async_mode = len(codes) > ASYNC_THRESHOLD
        with store.transaction():
            batch_id = store.next_id(BATCH_MODULE)
            batch = {
                "id": batch_id,
                "package_name": f"实测剖面图属相符性数据包_{batch_id:04d}",
                "fingerprint": fingerprint,
                "status": BATCH_QUEUED,
                "async_mode": async_mode,
                "reason": "",
                "section_codes": codes,
                "total": len(codes),
                "packed_count": 0,
                "created_by": operator or "值班员",
                "created_at": _now(),
                "started_at": "",
                "completed_at": "",
                "version_code": "",
                "archive_id": None,
                "package_size": 0,
            }
            store.rows(BATCH_MODULE).append(batch)
            for pre, snap in zip(preflights, snapshots):
                item_id = store.next_id(ITEM_MODULE)
                store.rows(ITEM_MODULE).append({
                    "id": item_id,
                    "batch_id": batch_id,
                    "剖面编号": pre["剖面编号"],
                    "图幅编号": pre["图幅编号"],
                    "status": "待打包",
                    "warnings": pre["warnings"],
                    "artifacts": [],
                    "snapshot": snap,
                })

        if async_mode:
            self._enqueue(batch_id)
            return self.get_batch(batch_id), [], f"批量较大（{len(codes)} 条），已进入异步队列"
        self._run_packing(batch_id)
        return self.get_batch(batch_id), [], ""

    # ------------------------------------------------------------- 打包执行

    def _enqueue(self, batch_id: int) -> None:
        self._ensure_worker()
        self._queue.put(batch_id)

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker_started:
                return
            thread = threading.Thread(target=self._worker_loop, name="section-packing-worker", daemon=True)
            thread.start()
            self._worker_started = True

    def _worker_loop(self) -> None:
        while True:
            batch_id = self._queue.get()
            try:
                self._run_packing(batch_id)
            finally:
                self._queue.task_done()

    def resume_batch(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        """连接中断/批次中断后按原批次续传；已出包则直接返回，重复触发不产生新版本。"""
        batch = store.find(BATCH_MODULE, batch_id)
        if batch is None:
            return None, f"批次 {batch_id} 不存在"
        if batch.get("status") == BATCH_DONE:
            return self._batch_view(batch), "批次已出包，沿用原版本"
        if batch.get("status") in (BATCH_QUEUED, BATCH_PACKING, BATCH_INTERRUPTED):
            if batch.get("async_mode"):
                self._enqueue(batch_id)
                return self.get_batch(batch_id), "已重新进入异步队列，从断点继续导出"
            self._run_packing(batch_id)
            return self.get_batch(batch_id), ""
        return self._batch_view(batch), "当前批次状态不支持续传"

    def _run_packing(self, batch_id: int) -> None:
        """逐条剖面落产物（检查点持久化），全部完成后在单事务内归档并分配版本号。"""
        with store.write_lock:
            batch = store.find(BATCH_MODULE, batch_id)
            if batch is None or batch.get("status") == BATCH_DONE:
                return
            batch["status"] = BATCH_PACKING
            batch["started_at"] = batch.get("started_at") or _now()
            items = [row for row in store.rows(ITEM_MODULE) if row.get("batch_id") == batch_id]

        try:
            for item in items:
                if item.get("status") == "已打包":
                    continue
                # 每条剖面一个事务：组装数据包/图纸清单/核验摘要，失败只回滚本条进度
                with store.transaction():
                    artifacts = self._assemble_item(item)
                    item["status"] = "已打包"
                    item["artifacts"] = artifacts
                    packed = sum(1 for row in items if row.get("status") == "已打包")
                    batch["packed_count"] = packed
                # 组装阶段再次核验材料，模拟断连时抛错的收口点
        except PackError as exc:
            self._mark_interrupted(batch_id, str(exc))
            return
        except Exception as exc:  # 连接中断等意外：留断点，可续传
            self._mark_interrupted(batch_id, f"导出中断：{exc}；批次保留，可按原批次续传")
            return

        try:
            # 导出、存档、版本号分配：同事务，任一失败整体回滚，不留半成品
            with store.transaction():
                done_items = [row for row in store.rows(ITEM_MODULE) if row.get("batch_id") == batch_id]
                pending = [row for row in done_items if row.get("status") != "已打包"]
                if pending:
                    raise PackError(f"尚有 {len(pending)} 条剖面未完成打包：{'、'.join(str(r.get('剖面编号')) for r in pending)}")
                version_no = max((int(row.get("version_no", 0)) for row in store.rows(ARCHIVE_MODULE)), default=0) + 1
                version_code = f"{VERSION_PREFIX}{version_no:04d}"
                package_name = f"{batch['package_name']}_{version_code}.zip"
                payload = self._build_zip(batch, done_items, version_code)
                archive_id = store.next_id(ARCHIVE_MODULE)
                store.rows(ARCHIVE_MODULE).append({
                    "id": archive_id,
                    "source_batch_id": batch_id,
                    "version_no": version_no,
                    "version_code": version_code,
                    "package_name": package_name,
                    "fingerprint": batch["fingerprint"],
                    "section_codes": list(batch["section_codes"]),
                    "snapshot": [dict(row.get("snapshot") or {}) for row in done_items],
                    "package_bytes": payload,
                    "package_size": len(payload),
                    "created_at": _now(),
                    "created_by": batch.get("created_by"),
                })
                batch["status"] = BATCH_DONE
                batch["reason"] = ""
                batch["completed_at"] = _now()
                batch["version_code"] = version_code
                batch["archive_id"] = archive_id
                batch["package_size"] = len(payload)
                batch["package_name"] = package_name
        except Exception as exc:
            self._mark_interrupted(batch_id, f"归档/版本分配失败：{exc}；已回滚半成品，可续传")

    def _mark_interrupted(self, batch_id: int, reason: str) -> None:
        with store.transaction():
            batch = store.find(BATCH_MODULE, batch_id)
            if batch is not None and batch.get("status") != BATCH_DONE:
                batch["status"] = BATCH_INTERRUPTED
                batch["reason"] = reason

    # ------------------------------------------------------------- 产物组装

    def _assemble_item(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        snap: dict[str, Any] = item.get("snapshot") or {}
        drawings = snap.get("drawings") or []
        missing = self._missing_drawings(drawings)
        if not snap.get("external_checklists"):
            missing.append("缺外部核对清单")
        if missing:
            # 出包时复核仍缺文件 → 整批不出包
            raise PackError(f"剖面 {item.get('剖面编号')}：{'；'.join(missing)}")

        code = str(item.get("剖面编号"))
        files = [
            (f"{code}/数据包_{code}.json", "数据包", self._render_data_package(snap)),
            (f"{code}/图纸清单_{code}.json", "图纸清单", self._render_drawing_manifest(snap)),
            (f"{code}/核验摘要_{code}.json", "核验摘要", self._render_verify_summary(snap)),
        ]
        return [
            {
                "file": name,
                "kind": kind,
                "size": len(content.encode("utf-8")),
                "checksum": hashlib.md5(content.encode("utf-8")).hexdigest(),
            }
            for name, kind, content in files
        ]

    @staticmethod
    def _render_data_package(snap: dict[str, Any]) -> str:
        length = snap.get("length_reconcile") or {}
        return _canonical({
            "datatype": "实测剖面图属相符性数据包",
            "剖面详情": snap.get("section"),
            "剖面分层": snap.get("layers"),
            "长度核验": length,
            "同步来源": ["剖面详情", "地层（分层）台账", "外部核对清单"],
            "exported_at": _now(),
        })

    @staticmethod
    def _render_drawing_manifest(snap: dict[str, Any]) -> str:
        drawings = snap.get("drawings") or []
        return _canonical({
            "datatype": "图纸清单",
            "剖面编号": (snap.get("section") or {}).get("剖面编号"),
            "图幅编号": (snap.get("section") or {}).get("图幅编号"),
            "图纸总数": len(drawings),
            "已归档": [row for row in drawings if row.get("present")],
            "缺失项": [row for row in drawings if not row.get("present")],
        })

    @staticmethod
    def _render_verify_summary(snap: dict[str, Any]) -> str:
        section = snap.get("section") or {}
        length = snap.get("length_reconcile") or {}
        checklists = snap.get("external_checklists") or []
        return _canonical({
            "datatype": "核验摘要",
            "剖面编号": section.get("剖面编号"),
            "图幅编号": section.get("图幅编号"),
            "长度核验结论": length.get("note") or f"分层累计 {length.get('approved_cumulative')}m 与剖面长度一致",
            "采用长度": length.get("final_length"),
            "外部核对": checklists,
            "图属相符": all(bool(item.get("全部相符")) for item in checklists),
            "exported_at": _now(),
        })

    def _build_zip(self, batch: dict[str, Any], items: list[dict[str, Any]], version_code: str) -> bytes:
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in items:
                snap = item.get("snapshot") or {}
                code = str(item.get("剖面编号"))
                for artifact in item.get("artifacts") or []:
                    kind = artifact["kind"]
                    if kind == "数据包":
                        content = self._render_data_package(snap)
                    elif kind == "图纸清单":
                        content = self._render_drawing_manifest(snap)
                    else:
                        content = self._render_verify_summary(snap)
                    archive.writestr(artifact["file"], content)
            manifest = {
                "datatype": "批次核验摘要",
                "package_name": batch["package_name"],
                "version_code": version_code,
                "section_codes": batch["section_codes"],
                "sections": [
                    {
                        "剖面编号": item.get("剖面编号"),
                        "图幅编号": item.get("图幅编号"),
                        "artifacts": item.get("artifacts"),
                        "warnings": item.get("warnings"),
                    }
                    for item in items
                ],
                "规则": [
                    "数据包、图纸清单、核验摘要缺一不出包",
                    "分层累计长度不符时以审定边界为准",
                    "与剖面详情、地层台账、外部核对清单同源同步",
                ],
                "packed_at": _now(),
            }
            archive.writestr("批次核验摘要.json", _canonical(manifest))
        return buffer.getvalue()

    # --------------------------------------------------------------- 查询口

    def _batch_view(self, batch: dict[str, Any]) -> dict[str, Any]:
        view = {key: value for key, value in batch.items() if key != "fingerprint"}
        view["items"] = [
            {key: value for key, value in row.items() if key not in ("snapshot",)}
            for row in sorted(
                (r for r in store.rows(ITEM_MODULE) if r.get("batch_id") == batch["id"]),
                key=lambda r: str(r.get("剖面编号")),
            )
        ]
        return view

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        batch = store.find(BATCH_MODULE, batch_id)
        return self._batch_view(batch) if batch else None

    def list_batches(self, *, after_id: int = 0, size: int = 20, status: str | None = None) -> dict[str, Any]:
        """键集分页：按 id 稳定排序，翻页期间新入队批次不影响已翻页内容。"""
        rows = [row for row in store.rows(BATCH_MODULE) if int(row["id"]) > after_id]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows.sort(key=lambda row: int(row["id"]))
        page = rows[:size]
        views = [self._batch_view(row) for row in page]
        last_id = int(page[-1]["id"]) if page else after_id
        return {
            "items": views,
            "total": len(store.rows(BATCH_MODULE)),
            "after_id": last_id,
            "next_after_id": last_id if len(rows) > size else None,
            "size": size,
        }

    def list_archives(self) -> list[dict[str, Any]]:
        """历史导出：只给摘要，包体走下载口；快照原样保留。"""
        return [
            {
                "id": row["id"],
                "source_batch_id": row.get("source_batch_id"),
                "version_code": row.get("version_code"),
                "package_name": row.get("package_name"),
                "section_codes": row.get("section_codes"),
                "package_size": row.get("package_size"),
                "created_at": row.get("created_at"),
                "created_by": row.get("created_by"),
                "snapshot_sections": [
                    (item.get("section") or {}).get("剖面编号")
                    for item in (row.get("snapshot") or [])
                ],
            }
            for row in sorted(store.rows(ARCHIVE_MODULE), key=lambda r: int(r["id"]))
        ]

    def get_archive_package(self, archive_id: int) -> tuple[dict[str, Any] | None, bytes | None]:
        row = store.find(ARCHIVE_MODULE, archive_id)
        if row is None:
            return None, None
        return row, row.get("package_bytes")


batch_packing_service = BatchPackingService()

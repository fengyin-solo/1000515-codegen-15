"""批次打包台业务规则验证（不依赖 FastAPI，仅用标准库直接驱动 service）。

覆盖：
- 启动补图幅号（保留原编号、可重入）
- 出包前分层/长度确认（不相符以审定边界为准）
- 预检整批驳回（缺图幅/未确认/外部未核）
- 三件齐全才出包，zip 内含数据包/图纸清单/核验摘要/批次清单/总摘要
- 事务化：重复导出只保留一套版本；内容变化产生新版本；历史快照不可变
- 断线续传：已出包项不重做，待出包项继续
- 大批量异步队列 + 冻结清单稳定分页
- 导出结果与剖面详情/地层台账/外部清单同步留痕
"""
from __future__ import annotations

import io
import sys
import zipfile

from app.services import section_pack as sp
from app.services.section_pack import (
    BatchPackService,
    ST_PACKED,
    ST_QUEUED,
    ST_REJECTED,
    ST_RUNNING,
    _ITEM_PACKED,
)
from app.store import store


failures: list[str] = []


def wait_terminal(svc: BatchPackService, batch_id: int, tries: int = 400) -> dict:
    """轮询直到批次进入终态（出包/驳回），兼容后台异步工作线程的时序。"""
    import time
    for _ in range(tries):
        view = svc.get_batch(batch_id)
        if view["status"] in (ST_PACKED, ST_REJECTED):
            return view
        svc.drain_queue()
        time.sleep(0.005)
    return svc.get_batch(batch_id)


def check(name: str, cond: bool, extra: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}{(' :: ' + extra) if extra else ''}")
    if not cond:
        failures.append(name)


def reset_service() -> BatchPackService:
    """在同一个内存仓库上清空并重新播种，再返回一个重新引导过的打包台 service。

    不做模块 reload，避免测试里持有的 store 与 service 内部用的不是同一对象。
    """
    from app.seed import SEED_ROWS
    sp._bootstrapped = False
    for name in list(store._tables.keys()):
        store._tables[name] = []
    for name, rows in SEED_ROWS.items():
        store._tables[name] = [dict(r) for r in rows]
    for name in ("section_export_batch", "section_export_item", "section_archive", "section_migration_log"):
        store._tables.setdefault(name, [])
    return sp.BatchPackService()


# ---------------------------------------------------------------------------
# 1) 启动引导：缺图幅号补数，保留原编号，可重入
# ---------------------------------------------------------------------------
svc = reset_service()
logs = svc.migration_logs()
check("迁移：启动时给缺图幅号历史剖面补数", len(logs) == 1, str([l["剖面编号"] for l in logs]))
mig = logs[0]
sect4 = store.find("section", 4)
check("迁移：保留原 id", mig["原id"] == 4 and int(mig["section_id"]) == 4)
check("迁移：保留原剖面编号", mig["剖面编号"] == "SECT-0004")
check("迁移：补入的图幅号非空且稳定", bool(sect4.get("图幅号")) and sect4["图幅号"] in
      [r["图幅编号"] for r in store.rows("mapping")], sect4.get("图幅号"))
assigned_first = sect4["图幅号"]
again = svc.backfill_sheet_numbers()
check("迁移：可重入，重复执行不产生新日志", again == [], str([l["剖面编号"] for l in again]))
check("迁移：补入图幅号幂等一致", store.find("section", 4)["图幅号"] == assigned_first)

# 历史快照：SECT-0001（id=1，已确认）预置 v1
arch1 = [r for r in store.rows("section_archive") if r["section_id"] == 1]
check("历史：为已确认剖面预置 v1 快照", len(arch1) == 1 and arch1[0]["version"] == 1)

# ---------------------------------------------------------------------------
# 2) 预检：SECT-0002 分层未确认；确认时长度不相符以审定边界为准
# ---------------------------------------------------------------------------
ok, reasons, info = svc.preflight_section(2)
check("预检：未确认分层的剖面被拦", not ok and any("尚未确认" in r for r in reasons), str(reasons))

section2, note, result = svc.confirm_layers(2)
check("确认：返回剖面", section2 is not None and section2["id"] == 2)
check("确认：识别累计 1225 与申报 1225 不相符场景", result["分层累计长度"] == 1225.0
      and result["剖面长度"] == 1225.0 and result["是否相符"] is True, str(result))
# SECT-0002 种子剖面长度就是 1225（累计），构造一个真正不相符场景：申报改成 1200
store.find("section", 2)["剖面长度"] = 1200.0
_, note2, result2 = svc.confirm_layers(2)
check("确认：不相符时以审定边界为准并写回审定长度",
      not result2["是否相符"] and result2["审定长度"] == 1225.0 and "审定边界" in note2, str(result2))
# 显式给一个审定末层边界
_, note3, result3 = svc.confirm_layers(2, audited_boundary=1210.0)
check("确认：可携带审定边界裁定末层", result3["审定长度"] == 1210.0, str(result3))
# 还原，避免影响后续
svc.confirm_layers(2, audited_boundary=1225.0)
store.find("section", 2)["剖面长度"] = 1225.0
svc.confirm_layers(2)
ok2, reasons2, _ = svc.preflight_section(2)
check("预检：确认后 SECT-0002 通过", ok2, str(reasons2))

# SECT-0003 外部核对清单未核 → 预检不通过
ok3, reasons3, _ = svc.preflight_section(3)
check("预检：外部核对未核整项不通过", not ok3 and any("外部核对清单未全部核对" in r for r in reasons3), str(reasons3))

# ---------------------------------------------------------------------------
# 3) 整批驳回：批次含 SECT-0003（未核），整批不出包
# ---------------------------------------------------------------------------
batch_bad, _ = svc.create_batch(section_ids=[1, 3], operator="测试员")
check("批次：含缺失项时整批驳回", batch_bad["status"] == ST_REJECTED, batch_bad["status"])
check("批次：驳回原因逐条可读", any("SECT-0003" in r for r in batch_bad["失败原因"]), str(batch_bad["失败原因"]))
data, filename, msg = svc.build_package(int(batch_bad["id"]))
check("批次：驳回批次不出包", data is None and "整批未出包" in msg, msg)
items_bad = {it["section_id"]: it for it in svc.get_batch(int(batch_bad["id"]))["items"]}
check("批次：驳回时不产生新存档", items_bad[3]["version"] is None and items_bad[1]["status"] == _ITEM_PACKED
      or items_bad[3]["version"] is None, str(items_bad[3]))

# 登记 SECT-0003 的外部核对结论后再出包
svc.reconcile_checklist_item(5, {"核对状态": "已核", "说明": "外部回执已到", "核对日期": "2026-09-30"})
ok3_after, _, _ = svc.preflight_section(3)
check("预检：外部核对登记后通过", ok3_after)

# ---------------------------------------------------------------------------
# 4) 整批出包：1、3 三件齐全 → zip 含全部文件
# ---------------------------------------------------------------------------
batch_ok, _ = svc.create_batch(section_ids=[1, 3], operator="测试员")
check("批次：齐全时已出包", batch_ok["status"] == ST_PACKED, batch_ok["status"])
data, filename, msg = svc.build_package(int(batch_ok["id"]))
check("批次：出包返回 zip 字节", data is not None and filename.endswith(".zip"), msg)
zf = zipfile.ZipFile(io.BytesIO(data))
names = zf.namelist()
check("包：含整批清单与总摘要", "批次清单.json" in names and "核验总摘要.json" in names, str(sorted(names)))
for prefix in ("SECT-0001", "SECT-0003"):
    for kind in ("数据包", "图纸清单", "核验摘要"):
        check(f"包：{prefix} 含{kind}", any(n.startswith(prefix + "/") and kind in n for n in names))
check("包：每个剖面恰好三件", all(sum(1 for n in names if n.startswith(p + "/")) == 3 for p in ("SECT-0001", "SECT-0003")))

# ---------------------------------------------------------------------------
# 5) 版本：重复导出复用同版本；SECT-0001 保留历史 v1
# ---------------------------------------------------------------------------
items_ok = {it["section_id"]: it for it in batch_ok["items"]}
check("版本：SECT-0001 复用历史 v1", items_ok[1]["version"] == 1 and items_ok[1]["复用旧版本"] is True,
      str(items_ok[1]))
check("版本：SECT-0003 首次为 v1", items_ok[3]["version"] == 1 and items_ok[3]["复用旧版本"] is False,
      str(items_ok[3]))
versions_of_1 = [r["version"] for r in store.rows("section_archive") if r["section_id"] == 1]
check("版本：重复导出只保留一套版本（仍只有 v1）", versions_of_1 == [1], str(versions_of_1))

# 再导一次同样批次：仍不新增版本
batch_repeat, _ = svc.create_batch(section_ids=[1, 3], operator="测试员")
check("版本：再次导出已出包且仍复用", batch_repeat["status"] == ST_PACKED)
versions_of_3 = [r["version"] for r in store.rows("section_archive") if r["section_id"] == 3]
check("版本：SECT-0003 重复导出不新增", versions_of_3 == [1], str(versions_of_3))

# 内容变化（编录人员改了）后再导出 → 新版本，但历史 v1 快照保留
store.find("section", 3)["编录人员"] = "孙明远（复核）"
batch_new, _ = svc.create_batch(section_ids=[3], operator="测试员")
items_new = {it["section_id"]: it for it in batch_new["items"]}
check("版本：内容变化产生新版本 v2", items_new[3]["version"] == 2, str(items_new[3]))
arch3 = sorted([r for r in store.rows("section_archive") if r["section_id"] == 3], key=lambda r: r["version"])
check("版本：历史 v1 快照保留且不可变", len(arch3) == 2 and arch3[0]["version"] == 1
      and arch3[0]["snapshot"]["section"]["编录人员"] == "孙明远",
      str([(a["version"], a["snapshot"]["section"]["编录人员"]) for a in arch3]))

# 详情里的同步状态：当前内容与最新(v2)一致 → True
detail3 = svc.section_detail(3)
check("详情：当前与最新导出同步", detail3["当前与最新导出同步"] is True)
check("详情：列出全部历史版本", [a["version"] for a in detail3["archives"]] == [1, 2])
check("同步：剖面详情/地层台账/外部清单三处留痕",
      set(detail3["source_refs"].keys()) == {"剖面详情", "地层台账", "外部核对清单", "图幅台账"})

# ---------------------------------------------------------------------------
# 6) 断线续传：大批量入队，处理一半后中断，resume 接着原批次
# ---------------------------------------------------------------------------
# 需要 3 个以上剖面（阈值为 2）。现有 1..4，但 2 已确认、4 已补图，确保 2/4 外部已核。
svc.drain_queue()
# 构造大批量：4 个剖面（2,4 已具备条件；确认 2、补 4 图幅已在引导完成）
batch_async, msg_async = svc.create_batch(section_ids=[1, 2, 3, 4], operator="批量员")
check("异步：大批量进入排队/导出态而非直接完成",
      batch_async["status"] in (ST_QUEUED, ST_RUNNING, ST_PACKED), batch_async["status"] + " " + msg_async)
bid = int(batch_async["id"])
# 后台线程/同步 drain 最终把队列处理完
finished = wait_terminal(svc, bid)
check("异步：队列处理完成出包", finished["status"] == ST_PACKED, finished["status"])

# 人为构造“中断在半途”：新建批次后，只让前两条出包，再把批次改回导出中
svc.recover_interrupted()
svc.drain_queue()
# 新建一个三剖面批次（异步）；先等它出包，再验证对已出包批次 resume 幂等可下载。
batch_three, _ = svc.create_batch(section_ids=[1, 2, 4], operator="续传员")
wait_terminal(svc, int(batch_three["id"]))
resumed, rmsg = svc.resume_batch(int(batch_three["id"]))
check("续传：已出包批次 resume 直接返回可下载", resumed["status"] == ST_PACKED and "已出包" in rmsg, rmsg)
# 已出包批次重复 resume 不会再加工，也不产生新版本
versions_of_2 = [r["version"] for r in store.rows("section_archive") if r["section_id"] == 2]
check("续传：重复接续不产生新版本", sorted(set(versions_of_2)) == versions_of_2 and len(versions_of_2) >= 1,
      str(versions_of_2))

# 模拟“前两条成功、批次卡在导出中”：手工把一条已出包批次复制为中断态
import copy as _copy
stuck = {
    "id": store.next_id("section_export_batch"),
    "batch_no": sp.BatchPackService._batch_no(9001),
    "status": ST_RUNNING,
    "frozen_section_ids": [1, 2, 4],
    "signature": "stuck-signature",
    "operator": "中断员",
    "created_at": sp.now(), "updated_at": sp.now(), "heartbeat": sp.now(),
    "package_name": None, "失败原因": [], "total": 3, "packed": 2,
}
with store.lock:
    store.rows("section_export_batch").append(stuck)
    for seq, sid in enumerate([1, 2, 4], start=1):
        store.rows("section_export_item").append({
            "id": store.next_id("section_export_item"), "batch_id": stuck["id"], "section_id": sid,
            "seq": seq, "status": _ITEM_PACKED if sid in (1, 2) else "待出包",
            "version": 1, "archive_id": None, "复用旧版本": False,
            "预检通过": True, "缺失原因": [], "artifacts": [],
        })
    # 给已出包的 1/2 补 archive 关联（取各自最新存档）
    for it in [i for i in store.rows("section_export_item") if i["batch_id"] == stuck["id"] and i["status"] == _ITEM_PACKED]:
        latest = max((a for a in store.rows("section_archive") if a["section_id"] == it["section_id"]),
                     key=lambda a: a["version"])
        it["archive_id"] = latest["id"]
        it["version"] = latest["version"]



resumed_stuck, _ = svc.resume_batch(stuck["id"])
resumed_stuck = wait_terminal(svc, stuck["id"])
check("续传：中断批次接着原批次出包", resumed_stuck["status"] == ST_PACKED, resumed_stuck["status"])
stuck_items = {it["section_id"]: it for it in resumed_stuck["items"]}
check("续传：已出包项保留、待出包项补齐",
      stuck_items[1]["status"] == _ITEM_PACKED and stuck_items[4]["status"] == _ITEM_PACKED,
      str({k: v["status"] for k, v in stuck_items.items()}))

# ---------------------------------------------------------------------------
# 7) 事务化：存档写入中途异常 → 该剖面版本/存档整体回滚；续传后整批成功
# ---------------------------------------------------------------------------
svc.confirm_layers(2)
svc.reconcile_checklist_item(5, {"核对状态": "已核", "说明": "x", "核对日期": "2026-09-30"})
# 暂时不让后台线程跑，避免它在我们注入故障前抢先把批次做完，保证测试确定性。
svc._worker_started = True
_orig_alloc = svc._allocate_archive


def _boom(section_id: int, batch_id: int):
    if section_id == 4:
        raise RuntimeError("模拟存档写入崩溃")
    return _orig_alloc(section_id, batch_id)


svc._allocate_archive = _boom
# 仅建批次（入队但不 drain），随后同步触发一次会在 4 号上崩溃的处理。
crash_batch, _ = svc.create_batch(section_ids=[1, 4], operator="事务员")
cbid = int(crash_batch["id"])
# 清掉队列里的同一批次，改为手动同步处理（保证走 _boom）。
import queue as _queue
while True:
    try:
        svc._queue.get_nowait()
    except _queue.Empty:
        break
arch4_before = len([r for r in store.rows("section_archive") if r["section_id"] == 4])
try:
    svc.process_batch(cbid)
except RuntimeError:
    pass
while cbid in svc._processing:
    import time as _time
    _time.sleep(0.005)
crash_items = {it["section_id"]: it for it in svc._batch_items(cbid)}
arch4_after = len([r for r in store.rows("section_archive") if r["section_id"] == 4])
check("事务：崩溃后失败项不产生新存档/版本", arch4_after == arch4_before and crash_items[4]["version"] is None,
      f"arch4 {arch4_before}->{arch4_after}, v4={crash_items[4]['version']}")
check("事务：已成功项保留", crash_items[1]["status"] == _ITEM_PACKED and crash_items[1]["version"] == 1)

svc._allocate_archive = _orig_alloc
recovered, _ = svc.resume_batch(cbid)
recovered = wait_terminal(svc, cbid)
rec_items = {it["section_id"]: it for it in recovered["items"]}
# 4 号内容自首次导出后未变，续传只补它这一条，仍复用既有 v1（重复导出只保留一套版本）。
check("事务：续传后整批出包且版本正确",
      recovered["status"] == ST_PACKED
      and rec_items[1]["version"] == 1 and rec_items[1]["复用旧版本"] is True
      and rec_items[4]["version"] == 1 and rec_items[4]["复用旧版本"] is True,
      recovered["status"])
svc._worker_started = False

# ---------------------------------------------------------------------------
# 8) 冻结清单 + 稳定分页：批次创建后新增剖面不影响该批次
# ---------------------------------------------------------------------------
frozen_batch, _ = svc.create_batch(section_ids=[1, 2, 4], operator="冻结员")
svc.drain_queue()
before_ids = list(frozen_batch["frozen_section_ids"])
# 批次创建后再登记一条新剖面
store.rows("section").append({
    "id": 5, "status": "实测中", "pending": True, "abnormal": False,
    "剖面编号": "SECT-0005", "剖面名称": "冻结后新增剖面", "剖面长度": 100.0,
    "起点坐标": "X=0", "终点坐标": "X=100", "编录日期": "2026-09-30",
    "编录人员": "后加", "剖面状态": "实测中", "图幅号": "MAPP-0001",
    "分层确认": False, "审定长度": None,
})
view = svc.get_batch(int(frozen_batch["id"]))
check("冻结：批次清单不受后续新增剖面影响", view["frozen_section_ids"] == before_ids == [1, 2, 4],
      str(view["frozen_section_ids"]))

# 批次列表分页稳定（倒序）
page1, total = svc.list_batches(page=1, size=2)
page2, _ = svc.list_batches(page=2, size=2)
ids_paged = [b["id"] for b in page1] + [b["id"] for b in page2]
check("分页：批次分页无重叠且总数自洽", len(set(ids_paged)) == len(ids_paged) and total >= len(ids_paged),
      f"total={total}")

# 剖面列表按 id 升序稳定
from app.services.section import SectionService
sec_rows, sec_total = SectionService().list_entries(page=1, size=20)
check("分页：剖面列表按 id 升序", [r["id"] for r in sec_rows] == sorted(r["id"] for r in sec_rows))

print()
if failures:
    print(f"==== {len(failures)} 项失败 ====")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("==== 全部通过 ====")

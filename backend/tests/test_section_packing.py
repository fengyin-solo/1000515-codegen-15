"""批次打包台回归测试：直接用 TestClient 跑，不依赖外部服务。

运行：python -m tests.test_section_packing
（在 backend 目录下执行）
"""
from __future__ import annotations

import io
import json
import zipfile

from fastapi.testclient import TestClient

from app.main import app
from app.migrations.section_sheet import backfill_section_sheet
from app.services.section_packing import batch_packing_service as svc
from app.store import store

client = TestClient(app)
PASS = 0


def check(condition: bool, label: str) -> None:
    global PASS
    assert condition, label
    PASS += 1
    print(f"  ✓ {label}")


def setup() -> None:
    # 每个用例组在全新内存状态上：重新执行补数迁移并确认合格剖面
    backfill_section_sheet()
    for code in ["SECT-0001", "SECT-0002", "SECT-0003", "SECT-0004"]:
        r = client.post("/api/section-packing/confirm", json={"section_code": code})
        assert r.json()["ok"], r.text


def test_migration_keeps_original_codes() -> None:
    print("[历史剖面补数迁移]")
    backfill_section_sheet()
    sections = {row["剖面编号"]: row for row in store.rows("section")}
    for code in ("SECT-0001", "SECT-0002", "SECT-0005"):
        check(bool(sections[code].get("图幅编号")), f"{code} 已补图幅号 {sections[code].get('图幅编号')}")
    check(sections["SECT-0003"]["图幅编号"] == "MAPP-0001", "已有图幅号不被覆盖")
    # 原编号保留：仍是 SECT-xxxx，行 id 不变
    check(sections["SECT-0005"]["id"] == 5 and sections["SECT-0005"]["剖面编号"] == "SECT-0005", "原编号与行 id 不变")
    logs = client.get("/api/section-packing/migrations").json()["items"]
    check(any(row["业务编号"] == "SECT-0005" for row in logs), "迁移日志留痕")


def test_missing_artifacts_block_whole_batch() -> None:
    print("[材料不齐整批不出包]")
    r = client.post("/api/section-packing/batches", json={"section_codes": ["SECT-0005"]})
    body = r.json()
    check(not body["ok"], "含缺图纸/缺清单剖面：整批拒绝")
    reasons = " ".join(f["原因"] for f in body["failures"])
    check("柱状图" in reasons and "外部核对清单" in reasons, "逐条说明缺文件原因")
    check(not store.rows("export_batch"), "拒绝时不留批次、不占版本号")


def test_confirm_required_before_export() -> None:
    print("[先确认分层与长度]")
    # 新建一个未确认状态：清掉确认记录
    store.rows("section_confirm").clear()
    r = client.post("/api/section-packing/batches", json={"section_codes": ["SECT-0003"]})
    check(not r.json()["ok"], "未确认不能出包")
    client.post("/api/section-packing/confirm", json={"section_code": "SECT-0003"})
    r = client.post("/api/section-packing/batches", json={"section_codes": ["SECT-0003"]})
    check(r.json()["batch"]["status"] == "已出包", "确认后可以出包")


def test_approved_boundary_wins() -> None:
    print("[分层累计以审定边界为准]")
    with store.transaction():
        layers = [row for row in store.rows("section_layer") if row["剖面编号"] == "SECT-0003"]
        layers[-1]["审定厚度"] = "326.00"
        layers[-1]["底界深度"] = "446.00"
    client.post("/api/section-packing/confirm", json={"section_code": "SECT-0003"})
    body = client.post("/api/section-packing/batches", json={"section_codes": ["SECT-0003"]}).json()
    archive_id = body["batch"]["archive_id"]
    package = client.get(f"/api/section-packing/archives/{archive_id}/download").content
    zf = zipfile.ZipFile(io.BytesIO(package))
    data = json.loads(zf.read("SECT-0003/数据包_SECT-0003.json"))
    summary = json.loads(zf.read("SECT-0003/核验摘要_SECT-0003.json"))
    check(data["长度核验"]["final_length"] == 446.0, "数据包采用审定累计 446.00m")
    check(summary["采用长度"] == 446.0 and "审定边界" in summary["长度核验结论"], "核验摘要写明裁定依据")


def test_zip_contains_three_artifacts() -> None:
    print("[三件套齐套打包]")
    body = client.post("/api/section-packing/batches", json={"section_codes": ["SECT-0003", "SECT-0004"]}).json()
    check(body["batch"]["status"] == "已出包", "同步小批次立即出包")
    archive_id = body["batch"]["archive_id"]
    names = zipfile.ZipFile(io.BytesIO(client.get(f"/api/section-packing/archives/{archive_id}/download").content)).namelist()
    for code in ("SECT-0003", "SECT-0004"):
        for kind in ("数据包", "图纸清单", "核验摘要"):
            check(any(n.startswith(f"{code}/{kind}_") for n in names), f"{code} 含{kind}")
    check("批次核验摘要.json" in names, "包含批次核验摘要")


def test_dedup_single_version() -> None:
    print("[重复导出只保留一套版本]")
    body1 = client.post("/api/section-packing/batches", json={"section_codes": ["SECT-0003", "SECT-0004"]}).json()
    version = body1["batch"]["version_code"]
    body2 = client.post("/api/section-packing/batches", json={"section_codes": ["SECT-0004", "SECT-0003"]}).json()
    check(body2["batch"].get("deduped"), "同指纹识别为重复导出")
    check(body2["batch"]["version_code"] == version, "沿用同一版本号")
    check(len(store.rows("export_archive")) == 1, "存档只有一套")


def test_async_resume_and_snapshot() -> None:
    print("[异步队列/断点续传/快照保留]")
    svc._enqueue = lambda batch_id: None  # 用例自行驱动，避免竞态
    batch, _, _ = svc.create_batch(["SECT-0001", "SECT-0002", "SECT-0003", "SECT-0004"])
    check(batch["async_mode"] and batch["status"] == "排队中", "超过 3 条进异步队列")
    bid = batch["id"]

    real = svc._assemble_item
    state = {"n": 0}

    def flaky(item):
        state["n"] += 1
        if state["n"] == 3:
            raise ConnectionError("socket closed")
        return real(item)

    svc._assemble_item = flaky
    svc._run_packing(bid)
    interrupted = client.get(f"/api/section-packing/batches/{bid}").json()
    check(interrupted["status"] == "已中断" and interrupted["packed_count"] == 2, "中断时保留逐条检查点")

    svc._assemble_item = real
    client.post(f"/api/section-packing/batches/{bid}/resume")
    svc._run_packing(bid)
    done = client.get(f"/api/section-packing/batches/{bid}").json()
    check(done["status"] == "已出包" and done["packed_count"] == 4, "续传完成原批次")

    # 快照不变性
    archive_id = done["archive_id"]
    before = json.loads(zipfile.ZipFile(io.BytesIO(client.get(f"/api/section-packing/archives/{archive_id}/download").content)).read(
        "SECT-0001/数据包_SECT-0001.json"))
    with store.transaction():
        svc.find_section(section_code="SECT-0001")["剖面名称"] = "事后修改"
    after = json.loads(zipfile.ZipFile(io.BytesIO(client.get(f"/api/section-packing/archives/{archive_id}/download").content)).read(
        "SECT-0001/数据包_SECT-0001.json"))
    check(before == after, "台账事后变动不影响历史包快照")


def test_stable_keyset_pagination() -> None:
    print("[批次清单分页稳定]")
    svc._enqueue = lambda batch_id: None
    for code in ("SECT-0001", "SECT-0002", "SECT-0003"):
        svc.create_batch([code])
    page1 = client.get("/api/section-packing/batches?size=2").json()
    first_ids = [row["id"] for row in page1["items"]]
    cursor_after = page1["next_after_id"]
    page2 = client.get(f"/api/section-packing/batches?size=2&after_id={cursor_after}").json()
    check(first_ids and first_ids[0] not in [row["id"] for row in page2["items"]], "键集翻页不重叠不回跳")


if __name__ == "__main__":
    # 逐组重跑前重置运行期台账，保证用例独立
    def reset_runtime() -> None:
        for name in ("section_confirm", "export_batch", "export_batch_item", "export_archive"):
            store.rows(name).clear()

    groups = [
        test_migration_keeps_original_codes,
        test_missing_artifacts_block_whole_batch,
        test_confirm_required_before_export,
        test_approved_boundary_wins,
        test_zip_contains_three_artifacts,
        test_dedup_single_version,
        test_async_resume_and_snapshot,
        test_stable_keyset_pagination,
    ]
    for group in groups:
        reset_runtime()
        setup()
        group()
    print(f"\n全部通过：{PASS} 项断言")

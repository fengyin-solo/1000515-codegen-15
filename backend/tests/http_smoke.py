"""HTTP 端到端冒烟：用 Starlette TestClient 打真实 ASGI 路由。

跑法（需要把 FastAPI 0.110 兼容依赖放到 PYTHONPATH）：
  PYTHONPATH="/tmp/pylibs/site:/workspace/backend" python3 tests/http_smoke.py
"""
from __future__ import annotations

import io
import sys
import zipfile

from fastapi.testclient import TestClient

from app.main import app
from app.seed import SEED_ROWS
from app.services import section_pack as sp
from app.store import store

client = TestClient(app)
fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{' :: ' + extra if extra else ''}")
    if not cond:
        fails.append(name)


def reset() -> None:
    sp._bootstrapped = False
    for n in list(store._tables):
        store._tables[n] = []
    for n, rs in SEED_ROWS.items():
        store._tables[n] = [dict(r) for r in rs]
    for n in ("section_export_batch", "section_export_item", "section_archive", "section_migration_log"):
        store._tables.setdefault(n, [])
    sp.BatchPackService()  # 重新引导


reset()

# 健康检查 & 列表稳定分页
r = client.get("/api/health")
check("health 200", r.status_code == 200, str(r.status_code))
r = client.get("/api/section?page=1&size=2")
check("剖面列表分页", r.status_code == 200 and r.json()["total"] == 4 and len(r.json()["items"]) == 2)
ids_page1 = [x["id"] for x in r.json()["items"]]
r2 = client.get("/api/section?page=2&size=2")
ids_page2 = [x["id"] for x in r2.json()["items"]]
check("分页：按 id 升序且稳定", ids_page1 == [1, 2] and ids_page2 == [3, 4], str(ids_page1 + ids_page2))

# 启动迁移日志：SECT-0004 补图幅号，保留原编号
r = client.get("/api/section/migration")
logs = r.json()["items"]
check("GET /migration 补数日志", r.status_code == 200 and len(logs) == 1 and logs[0]["剖面编号"] == "SECT-0004",
      str(logs))
check("迁移保留原 id", logs[0]["原id"] == 4)
r = client.post("/api/section/migration/backfill")
check("补数可重入（本次 0 条）", r.json()["message"].startswith("没有缺图幅号"), r.json()["message"])

# 详情包含分层、清单、同步、历史
r = client.get("/api/section/1/detail")
d = r.json()
check("详情含分层/清单/同步/历史", r.status_code == 200 and len(d["layers"]) == 3
      and len(d["checklist"]) == 2 and "archives" in d and "source_refs" in d)
check("详情历史 v1", [a["version"] for a in d["archives"]] == [1])

# 路由不被 /{entry_id} 吞：/batches 应是数组对象而不是 404/单条
r = client.get("/api/section/batches")
check("GET /batches 不被整数路由吞", r.status_code == 200 and "items" in r.json())

# 未确认分层的 2 号：打包 [2] 应整批驳回（数据包缺）
r = client.post("/api/section/batches", json={"section_ids": [2]})
b = r.json()["batch"]
check("[2] 未确认整批驳回 200 体", r.status_code == 200 and b["status"] == "已驳回", b["status"])
bid = b["id"]
r = client.get(f"/api/section/batches/{bid}/download")
check("驳回批次下载返回 409", r.status_code == 409 and "整批未出包" in r.json()["detail"], r.json().get("detail"))

# 确认 2 号分层（申报 1225 与累计 1225 相符）
r = client.post("/api/section/2/layers/confirm", json={"values": {}})
check("确认分层成功", r.json()["ok"] is True, r.json().get("message"))

# 3 号外部未核：[1,3] 整批驳回，原因指向 SECT-0003
r = client.post("/api/section/batches", json={"section_ids": [1, 3]})
b = r.json()["batch"]
check("[1,3] 因外部未核整批驳回", b["status"] == "已驳回"
      and any("SECT-0003" in x for x in b["失败原因"]), str(b["失败原因"]))
# 登记外部核对
r = client.post("/api/section/checklist/5/reconcile",
                json={"values": {"核对状态": "已核", "说明": "回执到", "核对日期": "2026-09-30"}})
check("外部核对登记", r.json()["ok"] is True)

# 现在 [1,3] 出包成功，下载 zip 含三件 + 公共文件
r = client.post("/api/section/batches", json={"section_ids": [1, 3], "operator": "http"})
b = r.json()["batch"]
check("[1,3] 已出包", b["status"] == "已出包" and b["packed"] == 2, b["status"])
bid = b["id"]
r = client.get(f"/api/section/batches/{bid}/download")
check("下载 200 + zip 类型", r.status_code == 200 and r.headers["content-type"] == "application/zip",
      str(r.status_code))
zf = zipfile.ZipFile(io.BytesIO(r.content))
names = zf.namelist()
for pref in ("SECT-0001", "SECT-0003"):
    for kind in ("数据包", "图纸清单", "核验摘要"):
        check(f"zip 含 {pref}-{kind}", any(n.startswith(pref + "/") and kind in n for n in names))
check("zip 含批次清单与总摘要", "批次清单.json" in names and "核验总摘要.json" in names)

# 版本：1 号复用 v1（历史快照），3 号新建 v1
r = client.get(f"/api/section/batches/{bid}")
items = {it["section_id"]: it for it in r.json()["items"]}
check("HTTP: 1 号复用历史 v1", items[1]["version"] == 1 and items[1]["复用旧版本"] is True)
check("HTTP: 3 号 v1", items[3]["version"] == 1 and items[3]["复用旧版本"] is False)

# 重复导出 [1,3] 不新增版本
r = client.post("/api/section/batches", json={"section_ids": [1, 3]})
check("重复导出仍出包", r.json()["batch"]["status"] == "已出包")
r = client.get("/api/section/3/detail")
check("3 号仍只有 v1", [a["version"] for a in r.json()["archives"]] == [1])

# 大批量异步 [1,2,3,4]：先排队，最终出包；可轮询接续
r = client.post("/api/section/batches", json={"section_ids": [1, 2, 3, 4]})
b = r.json()["batch"]
check("大批量进入异步态", r.json()["message"] == "批次已入异步队列" and b["status"] in ("排队中", "导出中", "已出包"),
      r.json()["message"] + " / " + b["status"])
abid = b["id"]
# drain（同步推进），模拟 worker 跑完
sp.batch_pack_service.drain_queue()
r = client.post(f"/api/section/batches/{abid}/resume")
check("接续已出包批次可下载", r.json()["batch"]["status"] == "已出包", r.json()["batch"]["status"])
r = client.get(f"/api/section/batches/{abid}/download")
check("大批量 zip 可下载", r.status_code == 200)

# 不存在资源
check("详情不存在 404", client.get("/api/section/999/detail").status_code == 404)
check("批次不存在 404", client.get("/api/section/batches/9999").status_code == 404)
check("创建空批次 400", client.post("/api/section/batches", json={"keyword": "不存在的编号"}).status_code == 400)

print()
if fails:
    print(f"==== {len(fails)} 项 HTTP 失败 ====")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("==== HTTP 端到端全部通过 ====")

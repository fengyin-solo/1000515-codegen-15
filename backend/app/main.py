"""地质勘探数据管理平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.services.section_pack import batch_pack_service
from app.store import store

app = FastAPI(title="地质勘探数据管理平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)


@app.on_event("startup")
def resume_export_batches() -> None:
    """启动即引导：补历史图幅号（在 service 初始化时已做），并把中断的导出批次接回队列。

    连接中断/服务重启后，排队中或导出中的批次按原批次继续，已出包剖面不重做。
    """
    batch_pack_service.recover_interrupted()
    # 大批量后台工作线程在首次入队时启动；这里确保线程就位。
    batch_pack_service._ensure_worker()


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()

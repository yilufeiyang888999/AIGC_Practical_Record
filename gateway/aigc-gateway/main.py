"""AIGC 统一网关：图像（ComfyUI）+ 文本（llama.cpp）一个入口"""
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import config
import metrics
from routers import chat, images
from security import api_key_scheme, key_matches  # noqa: F401  注册进 OpenAPI（Authorize 按钮）
from tasks import manager

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gateway")

# /docs 始终开放（页面只暴露 API 结构，内网低敏感）；
# 真正要锁的是调用行为——/v1/* 与 /files/* 由中间件鉴权
app = FastAPI(title="AIGC Gateway", version="0.1.0")

# 需要鉴权的路径前缀。
# ⚠️ /files 必须在这里：出图结果落在 /files/{task_id}/{filename}，
#    此前只守 /v1/ 的话，结果图是裸奔的——task_id 只有 12 位 hex，
#    且这与"合规 / 数据不出内网"的定位直接冲突（2026-09-23 修正）。
PROTECTED_PREFIXES = ("/v1/", "/files/")


@app.middleware("http")
async def api_key_guard(req: Request, call_next):
    if config.API_KEY and req.url.path.startswith(PROTECTED_PREFIXES):
        if not key_matches(req.headers.get("x-api-key"), config.API_KEY):
            return JSONResponse({"error": "invalid or missing api key"}, 401)
    return await call_next(req)


@app.on_event("startup")
async def _startup():
    if not config.API_KEY:
        log.warning("AIGC_API_KEY 未设置，/v1/* 无鉴权（仅限开发环境）")
    manager.start()
    log.info("出图 worker 已启动 | COMFY=%s LLM=%s", config.COMFY_HOST, config.LLM_HOST)


@app.get("/")
def index():
    """根路径自描述：API 网关没有'首页'，但应该告诉来访者这里有什么。"""
    return {
        "service": "aigc-gateway",
        "version": app.version,
        "endpoints": {
            "chat": "POST /v1/chat/completions（OpenAI 兼容，流式/非流式）",
            "image": "POST /v1/images/generations → GET /v1/tasks/{task_id}",
            "files": "GET /files/{task_id}/{filename}",
            "health": "GET /health",
            "metrics": "GET /metrics（Prometheus）",
        },
        "auth": "x-api-key header（/v1/* 需要）" if config.API_KEY else "未启用（开发模式）",
        "docs": "/docs（交互文档；先试 /v1/* 请点右上角 Authorize 填 key）",
    }


@app.get("/health")
def health():
    return {"status": "ok", "queue_depth": manager.q.qsize()}


@app.get("/metrics")
def prom_metrics():
    return Response(metrics.generate_latest(),
                    media_type=metrics.CONTENT_TYPE_LATEST)


app.include_router(images.router, prefix="/v1", tags=["images"])
app.include_router(chat.router, prefix="/v1", tags=["chat"])

config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=config.OUTPUT_DIR), name="files")

"""LLM 接口：OpenAI 兼容 /v1/chat/completions 透传（流式 + 非流式）

不做网关侧并发限制：llama-server --parallel 1 内部本就是串行排队
（实测 max/min 耗时比 ≈ 2），网关再加一层信号量只是重复排队。
要排队可视化，看 llama-server 自己的 /metrics（待接入）。
"""
import time

import httpx
from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse, StreamingResponse

import config
import metrics
from security import api_key_scheme

router = APIRouter(dependencies=[Depends(api_key_scheme)])

TIMEOUT = httpx.Timeout(connect=10, read=600, write=60, pool=10)


def _result_label(status_code: int) -> str:
    """把 HTTP 状态码收敛成有限集合。

    ⚠️ 不能直接用 str(status_code) 做 label：后端返回多少种状态码，
    Prometheus 就创建多少条时间序列。这是典型的高基数陷阱——
    一个 4xx 枚举攻击就能把 TSDB 撑爆。指标要的是"归类"，不是"枚举"。
    """
    if 200 <= status_code < 300:
        return "ok"
    if status_code == 401 or status_code == 403:
        return "auth_error"
    if status_code == 404:
        return "not_found"
    if 400 <= status_code < 500:
        return "client_error"
    if status_code >= 500:
        return "upstream_error"
    return "other"

# Swagger 请求体示例（裸 Request 无类型注解 → OpenAPI 无 body schema →
# /docs 里连输入框都没有；dict body 既出 schema 又原样透传全部 OpenAI 字段）
CHAT_EXAMPLE = {
    "model": "local",
    "messages": [{"role": "user", "content": "用一句话介绍杭州"}],
    "max_tokens": 128,
    "temperature": 0.7,
    "stream": False,
}


@router.post("/chat/completions")
async def chat(body: dict = Body(..., examples=[CHAT_EXAMPLE])):
    if not body.get("stream"):
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(f"{config.LLM_HOST}/v1/chat/completions", json=body)
            metrics.REQ_TOTAL.labels("chat", _result_label(r.status_code)).inc()
            return JSONResponse(r.json(), r.status_code)
    return StreamingResponse(_stream(body), media_type="text/event-stream")


async def _stream(body):
    """逐行透传 SSE，顺带测 TTFT。httpx 按 UTF-8 解码，无 requests 的 Latin-1 坑。"""
    t0 = time.time()
    ttft = None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            async with c.stream(
                    "POST", f"{config.LLM_HOST}/v1/chat/completions", json=body) as r:
                async for line in r.aiter_lines():
                    if ttft is None and line.startswith("data:") and '"content"' in line:
                        # reasoning_content 也含 "content" 子串，思考型模型的
                        # 首个思考 token 即用户感知的首响应
                        ttft = time.time() - t0
                        metrics.LLM_TTFT.observe(ttft)
                    if line:
                        yield line + "\n\n"
        metrics.LLM_TOTAL.observe(time.time() - t0)
        metrics.REQ_TOTAL.labels("chat", "stream_ok").inc()
    except Exception:
        metrics.REQ_TOTAL.labels("chat", "stream_error").inc()
        raise

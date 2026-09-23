"""Prometheus 指标。设计原则：队列深度是这个系统最重要的健康指标
（两个后端 ComfyUI / llama-server 都是串行执行 + 排队，实测 max/min 耗时比 ≈ 2）"""
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

REQ_TOTAL = Counter(
    "aigc_requests_total", "Total API requests", ["route", "result"])

IMG_QUEUE = Gauge(
    "aigc_image_queue_depth", "Image task queue depth (excluding the running one)")

IMG_QUEUE_WAIT = Histogram(
    "aigc_image_queue_wait_seconds", "Image task queue wait time",
    buckets=(0.5, 2, 5, 10, 20, 40, 80, 160))

IMG_DURATION = Histogram(
    "aigc_image_duration_seconds", "Image task execution time (excluding queue wait)",
    buckets=(2, 5, 8, 12, 16, 20, 30, 60, 120))

LLM_TTFT = Histogram(
    "aigc_llm_ttft_seconds", "LLM time to first token (gateway view, incl. network)",
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8))

LLM_TOTAL = Histogram(
    "aigc_llm_total_seconds", "LLM total request time",
    buckets=(1, 5, 10, 20, 40, 80, 160))

__all__ = ["REQ_TOTAL", "IMG_QUEUE", "IMG_QUEUE_WAIT", "IMG_DURATION",
           "LLM_TTFT", "LLM_TOTAL", "generate_latest", "CONTENT_TYPE_LATEST"]

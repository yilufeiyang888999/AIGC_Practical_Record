"""出图接口：提交任务（202 + task_id）/ 查询任务"""
import random

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import metrics
from security import api_key_scheme
from tasks import manager

# dependencies 只负责把 x-api-key 注册进 OpenAPI（Swagger Authorize 按钮），
# 实际校验在 main.py 中间件
router = APIRouter(dependencies=[Depends(api_key_scheme)])

# 默认值与 LoRA 批量验证（benchmarks §2.4）的口径一致
DEFAULT_NEGATIVE = (
    "low quality, worst quality, blurry, deformed, mutated, extra limbs, "
    "extra legs, bad anatomy, disfigured, ugly, multiple dogs, two dogs, "
    "multiple heads, two heads, cloned face, extra ears, watermark, text, signature")


class ImageReq(BaseModel):
    prompt: str
    negative: str = DEFAULT_NEGATIVE
    seed: int = -1                      # -1 = 随机
    steps: int = Field(25, ge=1, le=100)
    cfg: float = Field(7.0, ge=1.0, le=20.0)
    width: int = Field(768, ge=256, le=1536)
    height: int = Field(512, ge=256, le=1536)

    # 2026-09-23 新增：不选就只能跑工作流里写死的那一套，算不上"统一网关"。
    # 留空 = 用模板默认值，向后兼容。
    checkpoint: str | None = None                    # 底模文件名
    lora: str | None = None                          # LoRA 文件名
    lora_strength: float | None = Field(None, ge=0.0, le=2.0)


@router.post("/images/generations", status_code=202)
async def create_image(req: ImageReq):
    if req.seed == -1:
        req.seed = random.randint(0, 2**31 - 1)
    task = await manager.submit(req.model_dump())
    metrics.REQ_TOTAL.labels("images", "accepted").inc()
    return {
        "task_id": task.id,
        "status": task.status.value,
        "queue_depth": manager.q.qsize(),   # 前面还排着几个（不含本任务刚入队）
        "poll": f"/v1/tasks/{task.id}",
    }


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    task = manager.tasks.get(task_id)
    if task is None:
        raise HTTPException(404, f"task {task_id} not found")
    return task.public()

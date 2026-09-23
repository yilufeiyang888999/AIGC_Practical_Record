"""出图任务队列：asyncio.Queue + 单 worker 串行消费。

为什么单 worker：ComfyUI 单实例串行执行（benchmarks §3.6），多 worker 只是把
排队从网关挪到 ComfyUI 的 /queue，还丢了"排队耗时 vs 执行耗时"的分离度量。
"""
import asyncio
import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import requests

import config
import metrics
import comfy_client as cc

log = logging.getLogger("gateway.tasks")


class Status(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Task:
    params: dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: Status = Status.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    queue_wait_s: float | None = None
    result: dict | None = None
    error: str | None = None

    def public(self) -> dict:
        return {
            "task_id": self.id,
            "status": self.status.value,
            "queue_wait_s": round(self.queue_wait_s, 2) if self.queue_wait_s else None,
            "elapsed_s": (round(self.finished_at - self.started_at, 2)
                          if self.finished_at and self.started_at else None),
            "result": self.result,
            "error": self.error,
        }


class TaskManager:
    """进程内任务表 + 队列。v1 不落库，重启即清空（结果文件在磁盘上不受影响）。"""

    def __init__(self):
        self.q: asyncio.Queue[Task] = asyncio.Queue()
        self.tasks: dict[str, Task] = {}
        self._template: dict | None = None

    def start(self):
        asyncio.create_task(self._worker_loop())
        asyncio.create_task(self._cleanup_loop())

    async def submit(self, params: dict) -> Task:
        task = Task(params=params)
        self.tasks[task.id] = task
        metrics.IMG_QUEUE.set(self.q.qsize() + 1)
        await self.q.put(task)
        return task

    def _load_template(self) -> dict:
        if self._template is None:
            self._template = json.loads(
                config.WORKFLOW_FILE.read_text(encoding="utf-8"))
        return self._template

    def _wait_for_vram(self, s: requests.Session) -> None:
        """GPU 准入检查：空闲显存不足时等待，超时时明确报错。

        依据 09-20 OOM 实测：llama-server 驻留 ~9.7G 时，768×512 出图在
        VAE decode 阶段需要 ~2.3G 空闲（静态共存账够，峰值账不够）。
        与其让任务跑 18s 后裸 OOM，不如在入口处等/拒。
        """
        deadline = time.time() + config.VRAM_WAIT_S
        while True:
            try:
                d = s.get(f"{config.COMFY_HOST}/system_stats",
                          timeout=(5, 10)).json()["devices"][0]
                free_gb = d.get("vram_free", 0) / 1024**3
            except Exception:
                free_gb = None      # 查不到就放行，交给 ComfyUI 自己管
            if free_gb is None or free_gb >= config.MIN_FREE_VRAM_GB:
                return
            if time.time() >= deadline:
                raise RuntimeError(
                    f"GPU 准入拒绝：空闲显存 {free_gb:.2f}G < 阈值 "
                    f"{config.MIN_FREE_VRAM_GB}G，等待 {config.VRAM_WAIT_S}s 未释放"
                    f"（检查是否有 llama-server 等服务驻留占卡）")
            time.sleep(5)

    def _run_blocking(self, task: Task) -> dict:
        """在 worker 线程里跑的同步出图流程（复用 comfy_client 的全套容错）。"""
        p = task.params
        with requests.Session() as s:
            self._wait_for_vram(s)
            wf = cc.build_prompt(
                self._load_template(),
                positive=p["prompt"], negative=p["negative"],
                seed=p["seed"], steps=p["steps"], cfg=p["cfg"],
                width=p["width"], height=p["height"],
                filename_prefix=f"gateway/{task.id}",
                # 可选覆盖：None 时保持模板原值（build_prompt 的约定）
                ckpt_name=p.get("checkpoint"),
                lora_name=p.get("lora"),
                lora_strength=p.get("lora_strength"),
            )
            t0 = time.time()
            pid = cc.submit(s, wf)
            entry = cc.wait_for(s, pid, timeout=config.TASK_TIMEOUT)
            out_dir = config.OUTPUT_DIR / task.id
            files = cc.fetch_images(s, entry, out_dir=out_dir)
            return {
                "prompt_id": pid,
                "elapsed_s": round(time.time() - t0, 2),
                "files": [f"/files/{task.id}/{f.name}" for f in files],
            }

    async def _worker_loop(self):
        while True:
            task = await self.q.get()
            metrics.IMG_QUEUE.set(self.q.qsize())
            task.status = Status.RUNNING
            task.started_at = time.time()
            task.queue_wait_s = task.started_at - task.created_at
            metrics.IMG_QUEUE_WAIT.observe(task.queue_wait_s)
            try:
                task.result = await asyncio.to_thread(self._run_blocking, task)
                task.status = Status.DONE
                metrics.REQ_TOTAL.labels("images", "done").inc()
            except Exception as e:
                task.status = Status.FAILED
                # ComfyUI 执行失败的详情（execution_error/exception_message）在状态
                # 消息的末尾——截尾不截头，否则只剩 execution_start 的流水账
                msg = f"{type(e).__name__}: {e}"
                task.error = msg if len(msg) <= 500 else "…" + msg[-500:]
                metrics.REQ_TOTAL.labels("images", "failed").inc()
            task.finished_at = time.time()
            metrics.IMG_DURATION.observe(task.finished_at - task.started_at)
            self.q.task_done()

    # ── 保留策略 ─────────────────────────────────────────────────
    # 没有这段，长期运行的网关必然 OOM + 撑爆磁盘：
    #   · self.tasks 每条记录带完整 params（含 prompt 全文）与 result
    #   · OUTPUT_DIR 每任务一个目录，永不回收
    # 这是"能跑起来"和"能长期跑"的分界线。
    def reap_expired_tasks(self) -> int:
        """清理超期的内存任务记录。返回清理条数。"""
        cutoff = time.time() - config.TASK_TTL_S
        expired = [tid for tid, t in self.tasks.items()
                   if (t.finished_at or t.created_at) < cutoff]
        for tid in expired:
            self.tasks.pop(tid, None)
        if expired:
            log.info("已回收 %d 条超期任务记录（TTL=%ds）", len(expired), config.TASK_TTL_S)
        return len(expired)

    def reap_expired_results(self) -> int:
        """清理超期的结果目录。返回清理个数。"""
        out: Path = config.OUTPUT_DIR
        if not out.exists():
            return 0
        cutoff = time.time() - config.RESULT_TTL_S
        removed = 0
        for d in out.iterdir():
            if not d.is_dir():
                continue
            try:
                if d.stat().st_mtime < cutoff:
                    shutil.rmtree(d)
                    removed += 1
            except OSError as e:
                # 单个目录失败不要中断整轮清理，也不要静默吞掉
                log.warning("清理结果目录失败 %s: %s", d.name, e)
        if removed:
            log.info("已清理 %d 个超期结果目录（TTL=%ds）", removed, config.RESULT_TTL_S)
        return removed

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(config.CLEANUP_INTERVAL_S)
            try:
                await asyncio.to_thread(self.reap_expired_tasks)
                await asyncio.to_thread(self.reap_expired_results)
            except Exception as e:      # 清理失败绝不能影响主流程
                log.warning("清理任务异常: %s", e)


manager = TaskManager()

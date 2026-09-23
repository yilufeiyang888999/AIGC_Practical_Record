"""
批量出图 + 指标采集（LoRA 版：Shiba Inu，横向 768×512）
用法：
    set COMFY_HOST=http://127.0.0.1:8288     # 裸机 P100
    set COMFY_HOST=http://127.0.0.1:8289     # 容器 P100
    set COMFY_USE_WS=1                        # 可选：WebSocket 步级进度（基准测试建议开）
    python comfy_batch_gen.py
产出：
    output/<run_id>/*.png       图片
    logs/bench_<run_id>.json    结构化指标（benchmarks 数据源）
    logs/bench_<run_id>.csv     同上，Excel 可直接开

前置：
    workflows/lora_workflow_api.json 需已包含 LoraLoader（选中 myshiba_v4），
    且四个节点带 _meta.title：POSITIVE_PROMPT / NEGATIVE_PROMPT / SAMPLER / LATENT
"""
import json, csv, time, statistics, os, socket
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

import _path  # noqa: F401  引导：把仓库根加入 sys.path，必须在 import comfy_client 之前
from comfy_client import (
    make_session, load_workflow, build_prompt, submit, wait_for,
    fetch_images, find_by_title, find_by_class,
    check_server, list_checkpoints, COMFY_HOST, log, get_base_dir,
)

BASE_DIR = get_base_dir()   # 工作区根：workflows/ output/ logs/ 都在它下面

# WebSocket 是可选增强。缺依赖或连接失败都自动降级到 REST 轮询，不影响主流程。
USE_WS = os.getenv("COMFY_USE_WS", "").lower() in ("1", "true", "yes")
try:
    from comfy_ws import ComfyWS
except ImportError:
    ComfyWS = None

# ---------- 批量任务定义 ----------
NEGATIVE = ("low quality, worst quality, blurry, deformed, mutated, extra limbs, "
            "extra legs, bad anatomy, disfigured, ugly, multiple dogs, two dogs, "
            "multiple heads, two heads, cloned face, extra ears, watermark, text, signature")

# 触发词用 shiba inu（品种名即可激活 LoRA），每个 prompt 带 1dog/solo 防双狗
TASKS = [
    {"positive": "shiba inu, 1dog, solo, one animal, full body, standing, best quality, detailed fur, sharp focus, outdoors"},
    {"positive": "shiba inu, 1dog, solo, one animal, sitting, looking at viewer, best quality, detailed fur, sharp focus"},
    {"positive": "shiba inu, 1dog, solo, one animal, headshot, close-up, best quality, detailed fur, sharp focus"},
    {"positive": "shiba inu, 1dog, solo, one animal, running, side view, best quality, detailed fur, sharp focus, grass"},
    {"positive": "shiba inu, 1dog, solo, one animal, lying down, curled tail, best quality, detailed fur, sharp focus"},
]

SEEDS      = [1001, 1002, 1003, 1004]    # 5 prompts × 4 seeds = 20 张，P95 有统计意义
STEPS      = 25
CFG        = 7.0
WIDTH      = 768
HEIGHT     = 512
MAX_RETRY  = 2                           # 单任务失败重试次数

# 单任务超时策略：
#   首张给足时间（含模型加载）；之后按已完成任务的中位数动态收紧。
#   固定大超时对秒级任务太宽松——卡死要等很久才超时，还会重试浪费时间。
FIRST_TIMEOUT   = 180            # 首张超时（冷启动含模型加载）
TIMEOUT_FACTOR  = 6              # 后续超时 = 中位数 × 该倍数
TIMEOUT_FLOOR   = 60             # 后续超时下限，避免抖动误杀


def adaptive_timeout(done_times: list) -> int:
    """根据已完成任务的中位数算出合理超时。没有历史数据时用首张超时。"""
    if not done_times:
        return FIRST_TIMEOUT
    return max(TIMEOUT_FLOOR, int(statistics.median(done_times) * TIMEOUT_FACTOR))


def get_vram_used(session) -> float:
    """当前显存占用 GB（整卡口径，多实例共存时是总和）。"""
    try:
        d = session.get(f"{COMFY_HOST}/system_stats", timeout=(5, 10)).json()["devices"][0]
        return (d.get("vram_total", 0) - d.get("vram_free", 0)) / 1024**3
    except Exception:
        return 0.0


def wait_queue_clear(session, max_pending: int = 2):
    """ComfyUI 串行执行队列。积压太多就先等，避免无脑灌任务。"""
    while True:
        q = session.get(f"{COMFY_HOST}/queue", timeout=(5, 10)).json()
        pending = len(q.get("queue_pending", []))
        if pending <= max_pending:
            return
        log.info("队列积压 %d，等待...", pending)
        time.sleep(3)


def open_ws(stack):
    """尝试建立 WebSocket 并交给 ExitStack 管理。任何失败都返回 None，由调用方降级轮询。"""
    if not USE_WS:
        return None
    if ComfyWS is None:
        log.warning("COMFY_USE_WS=1 但缺少 websocket-client，降级为轮询。"
                    "安装：pip install websocket-client")
        return None
    try:
        return stack.enter_context(ComfyWS())
    except Exception as e:
        log.warning("WebSocket 连接失败（%s），降级为轮询", e)
        return None


def main():
    run_id  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = BASE_DIR / "output" / run_id
    log_dir = BASE_DIR / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    with ExitStack() as stack:
        s  = stack.enter_context(make_session())
        ws = open_ws(stack)

        stats  = check_server(s)
        device = stats["devices"][0]["name"]
        log.info("等待模式：%s", "WebSocket（步级进度）" if ws else "REST 轮询（2s 间隔）")

        template = load_workflow()
        ckpt = template[find_by_class(template, "CheckpointLoaderSimple")]["inputs"]["ckpt_name"]
        if ckpt not in list_checkpoints(s):
            log.error("模型 %r 不在目标机器上", ckpt)
            raise SystemExit(1)

        jobs = [(t, seed) for t in TASKS for seed in SEEDS]
        log.info("批量任务：%d 个（%d prompts × %d seeds）",
                 len(jobs), len(TASKS), len(SEEDS))
        log.info("分辨率：%dx%d | 输出目录：%s", WIDTH, HEIGHT, out_dir)

        records = []
        done_times = []          # 已成功任务的耗时，用于动态超时
        batch_t0 = time.time()

        for i, (task, seed) in enumerate(jobs, 1):
            rec = {
                "index": i, "seed": seed, "steps": STEPS, "cfg": CFG,
                "width": WIDTH, "height": HEIGHT,
                "prompt": task["positive"][:80],
                "status": "pending", "elapsed": None,
                "vram_peak_gb": None, "prompt_id": None,
                "error": None, "attempts": 0,
            }

            for attempt in range(1, MAX_RETRY + 2):
                rec["attempts"] = attempt
                try:
                    wait_queue_clear(s)
                    wf = build_prompt(
                        template,
                        positive=task["positive"], negative=NEGATIVE,
                        seed=seed, steps=STEPS, cfg=CFG,
                        width=WIDTH, height=HEIGHT,
                        filename_prefix=f"{run_id}/batch_{i:03d}",
                    )
                    t0  = time.time()
                    pid = submit(s, wf, client_id=ws.client_id if ws else None)
                    rec["prompt_id"] = pid
                    tmo = adaptive_timeout(done_times)
                    entry = (ws.wait(s, pid, timeout=tmo) if ws
                             else wait_for(s, pid, timeout=tmo))
                    elapsed = time.time() - t0

                    rec["vram_peak_gb"] = round(get_vram_used(s), 2)
                    files = fetch_images(s, entry, out_dir=out_dir)
                    rec.update(status="ok", elapsed=round(elapsed, 2),
                               files=[f.name for f in files])
                    done_times.append(elapsed)
                    log.info("[%d/%d] ✅ %.1fs  seed=%s  显存 %.1fG",
                             i, len(jobs), elapsed, seed, rec["vram_peak_gb"])
                    break

                except Exception as e:
                    rec["error"] = f"{type(e).__name__}: {e}"[:300]
                    # 超时说明 ComfyUI 那一侧还在执行（或卡住）。
                    # 不中断就重试的话，新任务会排在卡死的任务后面，必然继续超时。
                    if isinstance(e, TimeoutError):
                        try:
                            s.post(f"{COMFY_HOST}/interrupt", timeout=(5, 10))
                            log.warning("已发送 /interrupt 中断卡住的任务")
                        except Exception as ie:
                            log.warning("发送 /interrupt 失败: %s", ie)
                    if attempt <= MAX_RETRY:
                        log.warning("[%d/%d] ⚠️ 第 %d 次失败，重试：%s",
                                    i, len(jobs), attempt, rec["error"][:120])
                        time.sleep(3)
                    else:
                        rec["status"] = "failed"
                        log.error("[%d/%d] ❌ 最终失败：%s",
                                  i, len(jobs), rec["error"][:200])

            records.append(rec)

        batch_elapsed = time.time() - batch_t0
        wait_mode = "websocket" if ws else "polling"

    # ---------- 汇总 ----------
    ok = [r for r in records if r["status"] == "ok"]
    times = sorted(r["elapsed"] for r in ok)

    def pct(p: float):
        """线性插值百分位，与 numpy.percentile / Excel PERCENTILE 一致。
        小样本下"取第 k 个"会让 P95 恒等于 max（n=10 时 index=9 即最后一个）。
        线性插值在 n=20, p=95 时 k=18.05，取第 18/19 个插值，不贴 max。"""
        if not times:
            return None
        if len(times) == 1:
            return times[0]
        k = (len(times) - 1) * p / 100
        lo = int(k)
        hi = min(lo + 1, len(times) - 1)
        return round(times[lo] + (times[hi] - times[lo]) * (k - lo), 2)

    n = len(times)
    pct_confidence = "ok" if n >= 20 else f"low (n={n}, 建议 >= 20)"

    summary = {
        "run_id": run_id,
        "host": socket.gethostname(),
        "comfy_host": COMFY_HOST,
        "device": device,
        "checkpoint": ckpt,
        "resolution": f"{WIDTH}x{HEIGHT}",
        "steps": STEPS, "cfg": CFG,
        "wait_mode": wait_mode,
        "total": len(records),
        "success": len(ok),
        "failed": len(records) - len(ok),
        "success_rate": round(len(ok) / len(records) * 100, 1) if records else 0,
        "batch_elapsed_s": round(batch_elapsed, 1),
        "avg_s": round(statistics.mean(times), 2) if times else None,
        "median_s": round(statistics.median(times), 2) if times else None,
        "stdev_s": round(statistics.stdev(times), 3) if len(times) > 1 else None,
        "min_s": times[0] if times else None,
        "max_s": times[-1] if times else None,
        "jitter_s": round(times[-1] - times[0], 2) if times else None,
        "p50_s": pct(50),
        "p90_s": pct(90),
        "p95_s": pct(95),
        "p99_s": pct(99),
        "pct_confidence": pct_confidence,
        "throughput_per_min": round(len(ok) / (batch_elapsed / 60), 2) if batch_elapsed else None,
        "vram_peak_gb": max((r["vram_peak_gb"] or 0) for r in records) if records else None,
    }

    (log_dir / f"bench_{run_id}.json").write_text(
        json.dumps({"summary": summary, "records": records},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    cols = ["index", "status", "elapsed", "seed", "steps", "cfg",
            "vram_peak_gb", "attempts", "prompt", "error"]
    with (log_dir / f"bench_{run_id}.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)

    print("\n" + "=" * 56)
    print(f"  {summary['device']}")
    print("=" * 56)
    kws = ["resolution", "steps", "wait_mode", "total", "success", "failed", "success_rate",
           "batch_elapsed_s", "avg_s", "stdev_s", "median_s", "min_s", "max_s",
           "jitter_s", "p50_s", "p90_s", "p95_s", "p99_s",
           "throughput_per_min", "vram_peak_gb"]
    for k in kws:
        v = summary.get(k)
        if v is not None:
            print(f"  {k:22s} {v}")
    if "pct_confidence" in summary:
        print(f"  {'pct_confidence':22s} {summary['pct_confidence']}")
    print("=" * 56)
    print(f"  图片: {out_dir}")
    print(f"  指标: logs/bench_{run_id}.json / .csv")


if __name__ == "__main__":
    main()

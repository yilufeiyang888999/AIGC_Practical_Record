"""
SDXL LoRA 评测：真基线 vs 强度扫描（同 seed 对照，方法论见 05 手册 §7）
用法：
    set COMFY_HOST=http://127.0.0.1:8288     # Ubuntu 裸机（隧道）
    python sdxl_eval.py
产出：
    output/sdxl_eval/  基线 + 4 档强度的对照图

评测设计（05 手册 §7 三步对照 + 坑 #46 分辨率匹配）：
    - 真基线 = strength 0.0（shiba inu 是真实品种 token，不会像自造词那样污染基线）
    - 分辨率 640×448（匹配训练数据构图：v2 数据集主力 ~500×335 横屏，
      不用 SDXL 惯例的 1024²——训练构图小，1024 会平铺出多头）
    - VAE 用 fp16fix（坑 #54，推理侧同 nan 风险）
"""
import copy
import json
import time
from pathlib import Path

import _path  # noqa: F401  引导：把仓库根加入 sys.path
from comfy_client import (
    make_session, build_prompt, submit, wait_for, fetch_images,
    find_by_title, check_server, COMFY_HOST, log, get_base_dir,
)

BASE_DIR = get_base_dir()
# 大小写要写对：仓库里是小写 workflows/。此前写成 "Workflows"，
# Windows 上文件系统不区分大小写侥幸能跑，到 Linux 上直接 FileNotFoundError。
WF_FILE = BASE_DIR / "workflows" / "sdxl_lora_workflow_api.json"

PROMPT = ("shiba inu, 1dog, solo, one animal, full body, standing, "
          "best quality, detailed fur, sharp focus")
NEGATIVE = ("low quality, worst quality, blurry, deformed, mutated, extra limbs, "
            "bad anatomy, multiple dogs, two dogs, multiple heads, two heads, "
            "cloned face, extra ears, watermark, text, signature")

SEED = 1001
STRENGTHS = [0.0, 0.5, 0.8, 1.0]     # 0.0 = 真基线
STEPS, CFG, WIDTH, HEIGHT = 25, 7.0, 640, 448
TIMEOUT = 600                         # SDXL on P100 慢，给足


def run_one(s, template, strength: float, out_dir: Path):
    wf = copy.deepcopy(template)
    lora = wf[find_by_title(wf, "LORA")]
    lora["inputs"]["strength_model"] = strength
    lora["inputs"]["strength_clip"] = strength
    wf = build_prompt(wf, positive=PROMPT, negative=NEGATIVE,
                      seed=SEED, steps=STEPS, cfg=CFG,
                      width=WIDTH, height=HEIGHT,
                      filename_prefix=f"sdxl_eval/s{int(strength * 100):03d}")
    t0 = time.time()
    pid = submit(s, wf)
    entry = wait_for(s, pid, timeout=TIMEOUT)
    elapsed = time.time() - t0
    files = fetch_images(s, entry, out_dir=out_dir)
    tag = "基线" if strength == 0.0 else f"LoRA {strength}"
    log.info("[%s] %.1fs → %s", tag, elapsed, [f.name for f in files])
    return elapsed


def main():
    out_dir = BASE_DIR / "output" / "sdxl_eval"
    out_dir.mkdir(parents=True, exist_ok=True)

    with make_session() as s:
        stats = check_server(s)
        log.info("目标: %s | %s", COMFY_HOST, stats["devices"][0]["name"])
        template = json.loads(WF_FILE.read_text(encoding="utf-8"))

        for st in STRENGTHS:
            try:
                run_one(s, template, st, out_dir)
            except Exception as e:
                log.error("[strength %.1f] ❌ %s: %s", st, type(e).__name__, str(e)[:200])

    print(f"\n对照图在: {out_dir}")
    print("s000=基线 | s050/s080/s100=LoRA 0.5/0.8/1.0 | 同 seed 1001，横向对比")


if __name__ == "__main__":
    main()

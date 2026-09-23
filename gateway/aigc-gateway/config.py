"""网关配置（全部走环境变量，与 comfy_client 的约定保持一致）"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# comfy_client 在 import 时读取自己的 COMFY_HOST，必须在 import 它之前落定
COMFY_HOST = os.getenv("COMFY_HOST", "http://127.0.0.1:8288").rstrip("/")
os.environ.setdefault("COMFY_HOST", COMFY_HOST)

# 工作区同样要在 import 之前落定：否则共享包会先按默认值（仓库根）
# 初始化一次日志，顺手在仓库根建出 logs/ 目录——那是脚本侧的地盘。
os.environ.setdefault("AIGC_BASE_DIR", str(BASE_DIR))


def _ensure_shared_pkg() -> None:
    """把仓库根加入 sys.path，让共享包 comfy_client 可被 import。

    comfy_client/ 位于仓库根，而网关运行时 sys.path[0] 是 aigc-gateway/。
    网关若被单独拷贝到服务器部署，请先在仓库根执行 `pip install -e .`——
    装进 site-packages 后本函数是空操作。
    """
    here = Path(__file__).resolve()
    for p in [here, *here.parents]:
        if (p / "comfy_client" / "__init__.py").exists():
            s = str(p)
            if s not in sys.path:
                sys.path.insert(0, s)
            return
    raise RuntimeError(
        f"未找到 comfy_client/ 共享包（从 {here} 向上逐级查找均失败）。"
        f"请在仓库根执行 pip install -e . 后再启动网关。"
    )


_ensure_shared_pkg()

import comfy_client as cc  # noqa: E402  必须在引导之后

LLM_HOST = os.getenv("LLM_HOST", "http://127.0.0.1:8000").rstrip("/")

# 空 = 关闭鉴权（仅开发用，启动时会有警告日志）
API_KEY = os.getenv("AIGC_API_KEY", "")

# 出图工作流模板（API 格式，节点带 _meta.title 约定）
WORKFLOW_FILE = Path(os.getenv(
    "AIGC_WORKFLOW", str(BASE_DIR / "workflows" / "lora_workflow_api.json")))

# 出图结果落盘目录（同时通过 /files 静态挂载提供下载）
OUTPUT_DIR = Path(os.getenv("AIGC_OUTPUT", str(BASE_DIR / "output")))

# 单任务超时（秒）。P100 大分辨率 + 温度墙场景给足
TASK_TIMEOUT = int(os.getenv("AIGC_TASK_TIMEOUT", "600"))

# GPU 准入控制（09-20 OOM 实测后新增）：
# llama-server 驻留 ~9.7G 时，768×512 VAE decode 需 ~2.3G 空闲。
# 空闲显存低于阈值则等待，等不到就明确报错，而不是让任务裸 OOM。
MIN_FREE_VRAM_GB = float(os.getenv("AIGC_MIN_FREE_VRAM_GB", "2.6"))
VRAM_WAIT_S = int(os.getenv("AIGC_VRAM_WAIT_S", "120"))

# ── 保留策略（2026-09-23 新增）────────────────────────────────────
# 此前 manager.tasks 与 OUTPUT_DIR 都只增不删：任务表会无限增长（内存泄漏），
# 结果图会撑爆磁盘。进程内任务表本来重启即清空，反而说明"长期运行"才是风险场景。
TASK_TTL_S = int(os.getenv("AIGC_TASK_TTL_S", "86400"))        # 内存任务记录保留 24h
RESULT_TTL_S = int(os.getenv("AIGC_RESULT_TTL_S", "604800"))   # 结果文件保留 7 天
CLEANUP_INTERVAL_S = int(os.getenv("AIGC_CLEANUP_INTERVAL_S", "3600"))

# ── 把工作区告诉共享包 ──────────────────────────────────────────
# comfy_client 被 scripts/ 和网关共用，两边的工作区不同：
#   scripts/  → 仓库根（workflows/ 在仓库根）
#   网关      → 自身目录（自带 workflows/）
# 不 configure 的话，网关的出图会去找仓库根的 base_workflow_api.json，
# 结果图也会写到仓库根的 output/ —— 错得悄无声息。
cc.configure(base_dir=BASE_DIR, host=COMFY_HOST, workflow=str(WORKFLOW_FILE))

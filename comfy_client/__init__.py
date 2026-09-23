"""
ComfyUI API 客户端 —— 共享包（唯一实现）

本包是 scripts/ 与 gateway/aigc-gateway/ 的**单一来源**。
2026-09-23 之前这两处各存一份 comfy_client.py，靠文件哈希测试防漂移——
那是权宜之计：测试只能发现漂移，不能阻止漂移。现在两份都删了，
只剩这一份真身。

两种用法
--------
1) 免安装直接跑（clone 即用）：
       python scripts/comfy_batch_gen.py
   脚本通过 scripts/_path.py（gateway 通过 config.py）把仓库根加入 sys.path，
   然后正常 import。

2) 正式安装（生产部署，推荐）：
       pip install -e .
   安装后任何工作目录都能 import，systemd 单元不需要额外设 PYTHONPATH。

工作区概念
----------
包本身不假设目录结构。谁用它，谁通过 configure(base_dir=...) 告诉它
"我的 workflows/ output/ logs/ 在哪"：

    scripts/              → 仓库根（workflows/ 在仓库根）
    gateway/aigc-gateway/ → 自身目录（自带 workflows/）

不显式 configure 时，默认 base_dir = 仓库根。

命令行自检：
    python -m comfy_client            # 连通性 + 模型校验 + 出一张图
"""
from .config import (
    configure, get_settings,
    get_base_dir, get_host, get_timeout,
    get_workflow_path, get_output_dir, get_log_dir,
    setup_logging, log,
    COMFY_HOST, TIMEOUT,
)
from .workflow import (
    load_workflow, find_by_title, find_by_class, build_prompt,
)
from .client import (
    make_session, check_server, list_checkpoints,
    submit, wait_for, fetch_images, run_one,
)

__all__ = [
    # 配置
    "configure", "get_settings",
    "get_base_dir", "get_host", "get_timeout",
    "get_workflow_path", "get_output_dir", "get_log_dir",
    "setup_logging", "log", "COMFY_HOST", "TIMEOUT",
    # 工作流
    "load_workflow", "find_by_title", "find_by_class", "build_prompt",
    # HTTP
    "make_session", "check_server", "list_checkpoints",
    "submit", "wait_for", "fetch_images", "run_one",
]

__version__ = "1.0.0"

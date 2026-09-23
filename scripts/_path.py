"""引导：把仓库根加入 sys.path，让共享包 comfy_client 可被 import。

为什么需要这一层
----------------
`python scripts/comfy_batch_gen.py` 运行时，Python 只把 **scripts/ 本身**
放进 sys.path[0]，而 comfy_client/ 在仓库根。不引导就是 ModuleNotFoundError。

两份拷贝合并成共享包之后，这个"路径从哪来"的问题必须有人回答：
  · 免安装直跑（clone 即用）      → 本模块负责
  · 生产部署 `pip install -e .`   → 包装进 site-packages，本模块变成空操作

设计上是幂等的：向上逐级查找 comfy_client/__init__.py，找到了就插路径，
已经在 sys.path 里就不重复插。已安装过时也会优先用仓库内版本（开发期行为正确）。

用法：每个可执行脚本顶部一行 `import _path  # noqa: F401`
"""
import sys
from pathlib import Path


def ensure_repo_root() -> Path:
    here = Path(__file__).resolve()
    for p in [here, *here.parents]:
        if (p / "comfy_client" / "__init__.py").exists():
            s = str(p)
            if s not in sys.path:
                sys.path.insert(0, s)
            return p
    raise RuntimeError(
        f"未找到 comfy_client/ 包（从 {here} 向上逐级查找均失败）。"
        f"若目录被移动过，请改用 pip install -e . 正式安装。"
    )


REPO_ROOT = ensure_repo_root()

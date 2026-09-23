"""配置与路径解析 —— 共享包能被两个工作区共用的前提。

为什么要有这一层
----------------
原先两份 comfy_client.py 各写一行 ``BASE_DIR = Path(__file__.parent)``，
靠"文件在哪个目录"隐式决定 workflows/output/logs 的位置。
文件合并后这个技巧失效了——包只有一个位置，而消费方有两个。

所以改成显式契约：**谁用包，谁声明自己的工作区根目录**。
不声明时用默认值（仓库根），行为与 scripts/ 侧的历史用法一致。
"""
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

# 本包位于 <仓库根>/comfy_client/，父目录即仓库根
_PKG_PARENT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    """运行期配置。所有字段都可通过 configure() 或环境变量覆盖。"""
    host: str = field(
        default_factory=lambda: os.getenv("COMFY_HOST", "http://127.0.0.1:8188").rstrip("/"))
    base_dir: Path = field(
        default_factory=lambda: Path(os.getenv("AIGC_BASE_DIR", _PKG_PARENT)).resolve())
    # 默认工作流：仓库根的 base 版。gateway 会改成自带的那份（带 LoRA）
    workflow: str = field(
        default_factory=lambda: os.getenv("AIGC_WORKFLOW", "base_workflow_api.json"))
    # (连接超时, 读取超时) —— 不设 timeout 是运维大忌
    timeout: tuple = field(default_factory=lambda: (5, 30))


_settings = Settings()

# import 期常量：给 comfy_ws 这类"取值一次就用"的消费方保留向后兼容。
# 运行期请以 get_host() / get_timeout() 为准，configure() 之后这两个常量不会变。
COMFY_HOST = _settings.host
TIMEOUT = _settings.timeout

_log = logging.getLogger("comfy")
_configured_log_dir = None


def configure(*, host: str = None, base_dir: Path = None,
              workflow: str = None, timeout: tuple = None) -> Settings:
    """设置工作区。幂等，可多次调用；未给出的字段保持原值。

    典型调用（gateway/aigc-gateway/config.py）：
        cc.configure(base_dir=BASE_DIR, workflow=str(WF_FILE))
    """
    global _settings
    if host is not None:
        _settings.host = host.rstrip("/")
    if base_dir is not None:
        _settings.base_dir = Path(base_dir).resolve()
    if workflow is not None:
        _settings.workflow = str(workflow)
    if timeout is not None:
        _settings.timeout = timeout

    # 工作区换了，日志文件必须跟着换，否则 gateway 的日志会写进仓库根的 logs/
    _reconfigure_logging()
    return _settings


def get_settings() -> Settings:
    return _settings


def get_base_dir() -> Path:
    return _settings.base_dir


def get_host() -> str:
    return _settings.host


def get_timeout() -> tuple:
    return _settings.timeout


def get_workflow_path(name: str = None) -> Path:
    """工作流文件路径。name 可以是绝对路径，也可以是相对 base_dir/workflows 的文件名。"""
    name = name or _settings.workflow
    p = Path(name)
    return p if p.is_absolute() else _settings.base_dir / "workflows" / name


def get_output_dir() -> Path:
    return _settings.base_dir / "output"


def get_log_dir() -> Path:
    return _settings.base_dir / "logs"


def setup_logging(log_dir: Path = None) -> logging.Logger:
    """配置 logger：控制台 + 可选文件。目录不可写时降级为纯控制台，不抛异常。

    降级而不是报错，是因为包会被 import 到只读环境（比如只读容器的网关），
    日志写不了不该导致服务起不来。
    """
    global _configured_log_dir
    log_dir = Path(log_dir) if log_dir is not None else get_log_dir()

    for h in list(_log.handlers):
        _log.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    _log.addHandler(stream)

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "comfy.log", encoding="utf-8")
        fh.setFormatter(fmt)
        _log.addHandler(fh)
        _configured_log_dir = log_dir
    except OSError as e:      # 只读文件系统 / 权限不足
        _log.warning("日志文件不可用（%s），仅输出到控制台", e)
        _configured_log_dir = None

    _log.setLevel(logging.INFO)
    _log.propagate = False
    return _log


def _reconfigure_logging() -> None:
    """configure() 改了工作区之后重建日志。幂等：目录没变就不动。"""
    target = get_log_dir()
    if _configured_log_dir == target and _log.handlers:
        return
    setup_logging(target)


# import 时先按默认工作区装好 logger，保证 log 始终可用
log = setup_logging()

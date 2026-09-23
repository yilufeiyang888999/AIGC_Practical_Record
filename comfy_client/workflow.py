"""工作流操作：加载、节点定位、参数注入。

定位用「节点标题」而不是「节点 ID」，是因为 ID 是 ComfyUI 导出时随机编的，
改一次工作流就全变；标题是人在 UI 里显式命名的，稳定。
这份约定写在 workflows/ 的节点 _meta.title 上：
POSITIVE_PROMPT / NEGATIVE_PROMPT / SAMPLER / LATENT。
"""
import copy
import json
import random
from pathlib import Path

from .config import get_workflow_path


def load_workflow(path: Path = None) -> dict:
    """加载 API 格式工作流。path 为空时用当前工作区的默认工作流。"""
    path = Path(path) if path is not None else get_workflow_path()
    if not path.exists():
        raise FileNotFoundError(f"工作流不存在: {path}")
    wf = json.loads(path.read_text(encoding="utf-8"))
    if "nodes" in wf or "links" in wf:
        raise ValueError("这是 UI 格式，不是 API 格式。请用 Export (API) 重新导出")
    return wf


def find_by_title(wf: dict, title: str) -> str:
    """按自定义标题定位节点 ID。比硬编码 ID 稳，改工作流不会崩。"""
    for nid, node in wf.items():
        if node.get("_meta", {}).get("title") == title:
            return nid
    raise KeyError(f"找不到标题为 {title!r} 的节点，检查是否改了标题并重新导出")


def find_by_class(wf: dict, class_type: str, index: int = 0) -> str:
    """按 class_type 定位。用于只有一个的节点（SaveImage / VAEDecode 等）。"""
    matches = sorted(
        (nid for nid, n in wf.items() if n["class_type"] == class_type),
        key=int,
    )
    if not matches:
        raise KeyError(f"找不到 class_type={class_type} 的节点")
    return matches[index]


def build_prompt(wf_template: dict, *, positive: str = None, negative: str = None,
                 seed: int = None, steps: int = None, cfg: float = None,
                 width: int = None, height: int = None,
                 batch_size: int = None, filename_prefix: str = None,
                 ckpt_name: str = None, lora_name: str = None,
                 lora_strength: float = None) -> dict:
    """基于模板生成一个变体。deepcopy 避免污染模板。传 None 的参数保持模板原值。

    ckpt_name / lora_name / lora_strength 为可选覆盖（2026-09-23 新增）：
    网关要支持"换底模 / 换 LoRA / 调强度"，否则它只能跑工作流里写死的那一套，
    算不上统一网关。传 None 时保持模板原值，向后兼容。
    """
    wf = copy.deepcopy(wf_template)

    if ckpt_name is not None:
        wf[find_by_class(wf, "CheckpointLoaderSimple")]["inputs"]["ckpt_name"] = ckpt_name
    if lora_name is not None or lora_strength is not None:
        try:
            lora_node = wf[find_by_class(wf, "LoraLoader")]
        except KeyError:
            # 模板里没有 LoRA 节点却要求改 LoRA —— 静默忽略会让人以为生效了
            raise ValueError("工作流模板中没有 LoraLoader 节点，无法设置 lora_name/lora_strength")
        if lora_name is not None:
            lora_node["inputs"]["lora_name"] = lora_name
        if lora_strength is not None:
            lora_node["inputs"]["strength_model"] = lora_strength
            lora_node["inputs"]["strength_clip"] = lora_strength

    if positive is not None:
        wf[find_by_title(wf, "POSITIVE_PROMPT")]["inputs"]["text"] = positive
    if negative is not None:
        wf[find_by_title(wf, "NEGATIVE_PROMPT")]["inputs"]["text"] = negative

    sampler = wf[find_by_title(wf, "SAMPLER")]["inputs"]
    # API 格式没有 "randomize"，seed 必须自己给，否则每次出同一张图
    sampler["seed"] = seed if seed is not None else random.randint(0, 2**63 - 1)
    if steps is not None:
        sampler["steps"] = steps
    if cfg is not None:
        sampler["cfg"] = cfg

    latent = wf[find_by_title(wf, "LATENT")]["inputs"]
    if width is not None:
        latent["width"] = width
    if height is not None:
        latent["height"] = height
    if batch_size is not None:
        latent["batch_size"] = batch_size

    if filename_prefix is not None:
        wf[find_by_class(wf, "SaveImage")]["inputs"]["filename_prefix"] = filename_prefix

    return wf

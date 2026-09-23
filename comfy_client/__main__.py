"""命令行自检：连通性 + 模型校验 + 出一张图。

    set COMFY_HOST=http://127.0.0.1:8188   # Windows 本地 5060
    export COMFY_HOST=http://127.0.0.1:8288  # Ubuntu P100（SSH 隧道）
    python -m comfy_client

旧文档里写的是 ``python comfy_client.py``——那是单文件时代的用法，
抽成包之后入口改成 -m，否则无法指定工作区。
"""
from .client import (
    make_session, check_server, list_checkpoints, run_one,
)
from .config import get_host, get_workflow_path, log
from .workflow import load_workflow, find_by_class


def main() -> int:
    with make_session() as s:
        check_server(s)

        wf_path = get_workflow_path()
        log.info("工作流: %s", wf_path)
        template = load_workflow(wf_path)

        # 校验工作流里的模型在目标机器上存在
        available = list_checkpoints(s)
        ckpt = template[find_by_class(template, "CheckpointLoaderSimple")]["inputs"]["ckpt_name"]
        if ckpt not in available:
            log.error("模型 %r 不在目标机器 %s 上。可用: %s", ckpt, get_host(), available)
            return 1
        log.info("模型校验通过: %s", ckpt)

        result = run_one(
            s, template,
            positive="a cute orange cat sitting on a windowsill, "
                     "soft morning light, photorealistic, highly detailed, 8k",
            negative="blurry, low quality, distorted, deformed, watermark, text",
            steps=20, cfg=7.0, width=512, height=512,
            filename_prefix="api_test",
        )
        log.info("产出: %s", [f.name for f in result["files"]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""ComfyUI HTTP 客户端：提交 / 等待 / 下载。

容错设计都来自实测踩坑，不是教科书写法：
  · 提交先读 node_errors  —— 校验失败时 ComfyUI 返回 400，不读 body 就只剩一个状态码
  · 等待同时看 history+queue —— 只看 history 时，失败任务会死等到超时
  · 下载带 subfolder/type   —— 子目录输出不带这两个参数会 404
  · 任务"凭空消失"检测     —— 显存池耗尽 / 驱动 TDR 时 ComfyUI 会静默丢任务
"""
import json
import time
import uuid
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import get_host, get_timeout, get_output_dir, log
from .workflow import build_prompt, find_by_title


def make_session() -> requests.Session:
    """带重试的会话，避免单次网络抖动直接失败。"""
    s = requests.Session()
    retry = Retry(
        total=3, backoff_factor=0.5,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def check_server(session) -> dict:
    """连通性 + 目标机器信息。失败早报错，别等到提交任务才发现服务没起。"""
    host = get_host()
    r = session.get(f"{host}/system_stats", timeout=get_timeout())
    r.raise_for_status()
    stats = r.json()
    dev = stats.get("devices", [{}])[0]
    log.info("目标: %s", host)
    log.info("设备: %s | 显存 %.1f GB", dev.get("name", "?"),
             dev.get("vram_total", 0) / 1024**3)
    return stats


def list_checkpoints(session) -> list:
    """查目标机器有哪些模型。工作流里写的模型名必须在这个列表里。"""
    host = get_host()
    r = session.get(f"{host}/object_info/CheckpointLoaderSimple", timeout=get_timeout())
    r.raise_for_status()
    info = r.json()["CheckpointLoaderSimple"]
    return info["input"]["required"]["ckpt_name"][0]


def submit(session, workflow: dict, client_id: str = None) -> str:
    """
    提交任务，返回 prompt_id。校验失败时抛出可读的错误。

    client_id：不传则随机生成。想用 WebSocket 接收该任务的进度消息时，
        必须传入与 WS 连接相同的 client_id，否则 ComfyUI 不会把消息推给你。
    """
    host = get_host()
    client_id = client_id or str(uuid.uuid4())
    r = session.post(
        f"{host}/prompt",
        json={"prompt": workflow, "client_id": client_id},
        timeout=get_timeout(),
    )
    # 校验失败时 ComfyUI 返回 400 + node_errors，要先读 body 再 raise
    if r.status_code >= 400:
        try:
            err = r.json()
            raise RuntimeError(
                f"提交被拒 ({r.status_code}): "
                f"{json.dumps(err, ensure_ascii=False, indent=2)}"
            )
        except ValueError:
            r.raise_for_status()

    body = r.json()
    if body.get("node_errors"):
        raise RuntimeError(
            f"节点错误: {json.dumps(body['node_errors'], ensure_ascii=False, indent=2)}"
        )
    if "prompt_id" not in body:
        raise RuntimeError(f"响应异常，无 prompt_id: {body}")
    return body["prompt_id"]


def wait_for(session, prompt_id: str, timeout: int = 600, poll: float = 2.0,
             vanish_tolerance: int = 5) -> dict:
    """
    轮询直到完成。任务报错立即抛出，不会死等到超时。

    vanish_tolerance：任务"凭空消失"的容忍轮数。
        ComfyUI 偶发会静默丢弃任务（显存池耗尽、驱动 TDR 复位等），
        表现为 /history 查不到、/queue 里也没有。此时死等到 timeout 是浪费。
        连续 N 轮都处于"两边都找不到"且**曾经出现过**，就判定任务已丢失。
    """
    host = get_host()
    deadline = time.time() + timeout
    last_state = None
    was_seen = False          # 是否曾在 queue 里出现过
    vanish_count = 0          # 连续"两边都找不到"的轮数

    while time.time() < deadline:
        hist = session.get(f"{host}/history/{prompt_id}", timeout=get_timeout()).json()
        if prompt_id in hist:
            entry = hist[prompt_id]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                msgs = status.get("messages", [])
                raise RuntimeError(
                    f"执行失败: {json.dumps(msgs, ensure_ascii=False, indent=2)}"
                )
            return entry

        # 还没进 history，看看队列里的位置
        q = session.get(f"{host}/queue", timeout=get_timeout()).json()
        running = [i[1] for i in q.get("queue_running", [])]
        pending = [i[1] for i in q.get("queue_pending", [])]

        if prompt_id in running:
            state, was_seen, vanish_count = "执行中", True, 0
        elif prompt_id in pending:
            state = f"排队中（前面还有 {pending.index(prompt_id)} 个）"
            was_seen, vanish_count = True, 0
        else:
            state = "等待调度"
            if was_seen:
                # 之前在队列里、现在两边都没有 → ComfyUI 把任务丢了
                vanish_count += 1
                if vanish_count >= vanish_tolerance:
                    raise RuntimeError(
                        f"任务 {prompt_id} 已从 ComfyUI 消失（history 与 queue 均无记录，"
                        f"连续 {vanish_count} 轮）。常见原因：显存池耗尽、"
                        f"驱动 TDR 复位、ComfyUI 内部异常未落盘。"
                        f"建议用 --disable-cuda-malloc 重启 ComfyUI"
                    )

        if state != last_state:
            log.info("%s ...", state)
            last_state = state
        time.sleep(poll)

    raise TimeoutError(f"任务 {prompt_id} 超时（{timeout}s）")


def fetch_images(session, entry: dict, out_dir: Path = None) -> list:
    """把结果图下载到本地。subfolder / type 不能省，否则子目录输出会 404。"""
    host = get_host()
    out_dir = Path(out_dir) if out_dir is not None else get_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for node_id, out in entry.get("outputs", {}).items():
        for img in out.get("images", []):
            params = {
                "filename": img["filename"],
                "subfolder": img.get("subfolder", ""),
                "type": img.get("type", "output"),
            }
            r = session.get(f"{host}/view", params=params, timeout=get_timeout())
            r.raise_for_status()
            dst = out_dir / img["filename"]
            dst.write_bytes(r.content)
            saved.append(dst)
            log.info("已下载: %s (%.1f KB)", dst.name, len(r.content) / 1024)
    return saved


def run_one(session, wf_template: dict, **kwargs) -> dict:
    """跑一张图，返回耗时等指标。"""
    wf = build_prompt(wf_template, **kwargs)
    seed = wf[find_by_title(wf, "SAMPLER")]["inputs"]["seed"]

    t0 = time.time()
    pid = submit(session, wf)
    log.info("已提交 prompt_id=%s seed=%s", pid[:8], seed)
    entry = wait_for(session, pid)
    elapsed = time.time() - t0

    files = fetch_images(session, entry)
    log.info("完成，耗时 %.1fs", elapsed)
    return {"prompt_id": pid, "seed": seed, "elapsed": elapsed, "files": files}

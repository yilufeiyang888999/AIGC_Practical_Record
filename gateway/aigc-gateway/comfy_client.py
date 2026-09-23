"""
ComfyUI API 客户端 —— 生产级实现
用法：
    set COMFY_HOST=http://127.0.0.1:8188   # 打 Windows 本地 5060
    set COMFY_HOST=http://127.0.0.1:8288   # 打 Ubuntu P100
    python comfy_client.py
"""
import json, time, uuid, os, random, copy, logging
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------- 配置 ----------
BASE_DIR   = Path(__file__).parent
COMFY_HOST = os.getenv("COMFY_HOST", "http://127.0.0.1:8188").rstrip("/")
TIMEOUT    = (5, 30)            # (连接超时, 读取超时) —— 不设 timeout 是运维大忌
WF_FILE    = BASE_DIR / "workflows" / "base_workflow_api.json"
OUT_DIR    = BASE_DIR / "output"
LOG_DIR    = BASE_DIR / "logs"


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "comfy.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger("comfy")


log = setup_logging()


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


# ---------- 工作流操作 ----------
def load_workflow(path: Path = WF_FILE) -> dict:
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
                 batch_size: int = None, filename_prefix: str = None) -> dict:
    """基于模板生成一个变体。deepcopy 避免污染模板。传 None 的参数保持模板原值。"""
    wf = copy.deepcopy(wf_template)

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


# ---------- API 调用 ----------
def check_server(session) -> dict:
    """连通性 + 目标机器信息。失败早报错，别等到提交任务才发现服务没起。"""
    r = session.get(f"{COMFY_HOST}/system_stats", timeout=TIMEOUT)
    r.raise_for_status()
    stats = r.json()
    dev = stats.get("devices", [{}])[0]
    log.info("目标: %s", COMFY_HOST)
    log.info("设备: %s | 显存 %.1f GB", dev.get("name", "?"),
             dev.get("vram_total", 0) / 1024**3)
    return stats


def list_checkpoints(session) -> list:
    """查目标机器有哪些模型。工作流里写的模型名必须在这个列表里。"""
    r = session.get(f"{COMFY_HOST}/object_info/CheckpointLoaderSimple", timeout=TIMEOUT)
    r.raise_for_status()
    info = r.json()["CheckpointLoaderSimple"]
    return info["input"]["required"]["ckpt_name"][0]


def submit(session, workflow: dict, client_id: str = None) -> str:
    """
    提交任务，返回 prompt_id。校验失败时抛出可读的错误。

    client_id：不传则随机生成。想用 WebSocket 接收该任务的进度消息时，
        必须传入与 WS 连接相同的 client_id，否则 ComfyUI 不会把消息推给你。
    """
    client_id = client_id or str(uuid.uuid4())
    r = session.post(
        f"{COMFY_HOST}/prompt",
        json={"prompt": workflow, "client_id": client_id},
        timeout=TIMEOUT,
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
    deadline = time.time() + timeout
    last_state = None
    was_seen = False          # 是否曾在 queue 里出现过
    vanish_count = 0          # 连续"两边都找不到"的轮数

    while time.time() < deadline:
        hist = session.get(f"{COMFY_HOST}/history/{prompt_id}", timeout=TIMEOUT).json()
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
        q = session.get(f"{COMFY_HOST}/queue", timeout=TIMEOUT).json()
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


def fetch_images(session, entry: dict, out_dir: Path = OUT_DIR) -> list:
    """把结果图下载到本地。subfolder / type 不能省，否则子目录输出会 404。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for node_id, out in entry.get("outputs", {}).items():
        for img in out.get("images", []):
            params = {
                "filename": img["filename"],
                "subfolder": img.get("subfolder", ""),
                "type": img.get("type", "output"),
            }
            r = session.get(f"{COMFY_HOST}/view", params=params, timeout=TIMEOUT)
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


# ---------- 入口 ----------
if __name__ == "__main__":
    with make_session() as s:
        check_server(s)

        template = load_workflow()

        # 校验工作流里的模型在目标机器上存在
        available = list_checkpoints(s)
        ckpt = template[find_by_class(template, "CheckpointLoaderSimple")]["inputs"]["ckpt_name"]
        if ckpt not in available:
            log.error("模型 %r 不在目标机器上。可用: %s", ckpt, available)
            raise SystemExit(1)
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
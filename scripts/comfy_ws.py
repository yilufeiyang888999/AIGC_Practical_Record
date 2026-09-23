"""
ComfyUI WebSocket 客户端 —— 步级实时进度。

相比 REST 轮询的优势：
    轮询：2 秒查一次 /history，只知道"做完没"，最小感知粒度 2 秒
    WS  ：服务端主动推送，能拿到 "第 7/20 步" 这种步级进度，延迟 < 100ms
    并且 20 张图的批量任务，轮询要发 ~200 次 HTTP，WS 只要一条长连接

依赖：
    pip install websocket-client

用法：
    with make_session() as s, ComfyWS() as ws:
        pid = submit(s, wf, client_id=ws.client_id)   # client_id 必须一致
        entry = ws.wait(s, pid, timeout=120)
"""
import json, time, uuid, sys
from urllib.parse import urlparse, urlencode

from comfy_client import COMFY_HOST, TIMEOUT, log

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None


def _ws_url(client_id: str) -> str:
    """http://host:port → ws://host:port/ws?clientId=xxx"""
    u = urlparse(COMFY_HOST)
    scheme = "wss" if u.scheme == "https" else "ws"
    return f"{scheme}://{u.netloc}/ws?{urlencode({'clientId': client_id})}"


class ProgressBar:
    """单行原地刷新的进度条。非 TTY 环境（重定向到文件/CI）自动降级为不输出。"""

    def __init__(self, width: int = 34, enabled: bool = None):
        self.width = width
        self.enabled = sys.stdout.isatty() if enabled is None else enabled
        self._dirty = False

    def update(self, value: int, total: int, suffix: str = ""):
        if not self.enabled or total <= 0:
            return
        filled = int(self.width * value / total)
        bar = "█" * filled + "░" * (self.width - filled)
        sys.stdout.write(f"\r    {bar} {value:>3}/{total} {suffix}")
        sys.stdout.flush()
        self._dirty = True

    def clear(self):
        """擦掉进度条，避免和后续 log 输出串行。"""
        if self.enabled and self._dirty:
            sys.stdout.write("\r" + " " * (self.width + 30) + "\r")
            sys.stdout.flush()
            self._dirty = False


class ComfyWS:
    """
    ComfyUI WebSocket 连接。一条连接可以等待多个任务，适合批量场景。

    ComfyUI 推送的消息类型：
        status              队列长度变化
        execution_start     任务开始
        execution_cached    命中缓存的节点（这些节点不会有 progress）
        executing           当前执行到哪个节点；node 为 null 表示该 prompt 结束
        progress            步级进度 {value, max, node}
        executed            某节点产出了输出
        execution_success   任务成功（较新版本）
        execution_error     任务失败，带 exception_message / traceback
        execution_interrupted  被 /interrupt 中断
        <binary frame>      预览图，直接跳过
    """

    def __init__(self, client_id: str = None, show_progress: bool = True):
        if websocket is None:
            raise ImportError(
                "缺少依赖：pip install websocket-client\n"
                "（注意不是 websockets，是 websocket-client）"
            )
        self.client_id = client_id or str(uuid.uuid4())
        self.show_progress = show_progress
        self.ws = None

    # ---------- 连接管理 ----------
    def connect(self, timeout: float = 10.0):
        self.ws = websocket.WebSocket()
        self.ws.connect(_ws_url(self.client_id), timeout=timeout)
        self.ws.settimeout(1.0)     # 单次 recv 超时，用于定期检查总超时
        log.info("WebSocket 已连接 (clientId=%s)", self.client_id[:8])
        return self

    def close(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()
        return False

    # ---------- 等待任务完成 ----------
    def wait(self, session, prompt_id: str, timeout: int = 300,
             poll_fallback: float = 5.0) -> dict:
        """
        等待指定 prompt 完成，返回 /history 里的 entry（与 wait_for 返回值一致）。

        为什么最后还要查一次 /history：
            WS 只告诉你"完成了"，输出文件清单仍要从 history 取。
            executed 消息里虽然带 output，但一个任务可能有多个输出节点，
            统一从 history 取更完整、也和轮询版本的返回结构保持一致。

        为什么还要定期兜底轮询 /history（V2 修正）：
            实测发现 WS 完成信号会漏。可能原因：
              - `execution_start` 被上一个任务的 recv 循环消费掉，`started` 一直是 False，
                导致 `executing + node:null` 这条完成路径失效
              - 部分 ComfyUI 版本不发 `execution_success`
            表现：任务实际早已完成（重试时 0.2 秒就拿到图），但 WS 这边一直等到超时。
            所以 WS 只用来做实时进度，**完成判定必须有 history 兜底**。
        """
        deadline = time.time() + timeout
        bar = ProgressBar(enabled=self.show_progress and sys.stdout.isatty())
        started = False
        cur_node = None
        next_poll = time.time() + poll_fallback

        while time.time() < deadline:
            # ---- 兜底：定期查 history，不依赖 WS 的完成信号 ----
            if time.time() >= next_poll:
                next_poll = time.time() + poll_fallback
                try:
                    hist = session.get(f"{COMFY_HOST}/history/{prompt_id}",
                                       timeout=TIMEOUT).json()
                    if prompt_id in hist:
                        bar.clear()
                        log.debug("WS 未送达完成信号，由 history 兜底判定完成")
                        entry = hist[prompt_id]
                        status = entry.get("status", {})
                        if status.get("status_str") == "error":
                            raise RuntimeError(
                                f"执行失败: {json.dumps(status.get('messages'), ensure_ascii=False)}"
                            )
                        return entry
                except RuntimeError:
                    raise
                except Exception:
                    pass          # 轮询失败不致命，继续走 WS

            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue                      # 正常，继续等
            except websocket.WebSocketConnectionClosedException:
                bar.clear()
                raise RuntimeError("WebSocket 连接被服务端关闭，ComfyUI 可能已重启")

            if isinstance(raw, bytes):
                continue                      # 预览图二进制帧，跳过

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")
            data = msg.get("data", {}) or {}

            # 只关心自己这个 prompt 的消息；status 消息没有 prompt_id，放行
            mpid = data.get("prompt_id")
            if mpid is not None and mpid != prompt_id:
                continue

            if mtype == "execution_start":
                started = True

            elif mtype == "execution_cached":
                started = True                # 也算开始的信号
                cached = data.get("nodes", [])
                if cached:
                    log.debug("命中缓存节点: %s", cached)

            elif mtype == "executing":
                node = data.get("node")
                if node is None:
                    # node=null 表示该 prompt 执行完毕。
                    # 不再要求 started —— 该消息带了本 prompt 的 prompt_id，
                    # 已经通过上面的过滤，足以判定是自己的完成信号。
                    bar.clear()
                    return self._fetch_entry(session, prompt_id)
                started, cur_node = True, node

            elif mtype == "progress":
                started = True
                bar.update(data.get("value", 0), data.get("max", 0),
                           suffix=f"node={cur_node or data.get('node', '?')}")

            elif mtype == "execution_success":
                bar.clear()
                return self._fetch_entry(session, prompt_id)

            elif mtype == "execution_error":
                bar.clear()
                raise RuntimeError(
                    f"执行失败 node={data.get('node_id')} "
                    f"({data.get('node_type')}): {data.get('exception_message')}"
                )

            elif mtype == "execution_interrupted":
                bar.clear()
                raise RuntimeError(f"任务被中断: node={data.get('node_id')}")

        bar.clear()
        raise TimeoutError(f"任务 {prompt_id} 超时（{timeout}s，WebSocket 模式）")

    @staticmethod
    def _fetch_entry(session, prompt_id: str) -> dict:
        """WS 说完成了，从 history 取完整输出清单。"""
        for _ in range(5):                    # history 写入可能略滞后于 WS 通知
            hist = session.get(f"{COMFY_HOST}/history/{prompt_id}",
                               timeout=TIMEOUT).json()
            if prompt_id in hist:
                return hist[prompt_id]
            time.sleep(0.3)
        raise RuntimeError(
            f"WebSocket 报告 {prompt_id} 已完成，但 /history 查不到。"
            f"ComfyUI 可能在写入前崩溃"
        )


# ---------- 演示 ----------
if __name__ == "__main__":
    from comfy_client import (
        make_session, load_workflow, build_prompt, submit,
        fetch_images, check_server,
    )

    with make_session() as s, ComfyWS() as ws:
        check_server(s)
        wf = build_prompt(
            load_workflow(),
            positive="a red fox in a snowy forest, golden hour, photorealistic, 8k",
            negative="blurry, low quality, deformed, watermark",
            steps=20, cfg=7.0, width=512, height=512,
            filename_prefix="ws_demo",
        )
        t0 = time.time()
        pid = submit(s, wf, client_id=ws.client_id)   # ← client_id 必须一致
        log.info("已提交 prompt_id=%s", pid[:8])
        entry = ws.wait(s, pid, timeout=180)
        log.info("完成，耗时 %.2fs", time.time() - t0)
        for f in fetch_images(s, entry):
            log.info("产出: %s", f.name)

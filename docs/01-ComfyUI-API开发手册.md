# ComfyUI API 开发手册

> 基于 ComfyUI `0.3.67` / commit `22e40d2a` / frontend `1.28.8` 实测编写
> 配套代码：`Scripts/comfy_client.py`、`comfy_ws.py`、`comfy_batch_gen.py`、`inspect_workflow.py`
> 最后更新：2026-09-10

---

## 1. 核心概念

ComfyUI 本质是一个**图执行引擎**：工作流是一张 DAG，节点是算子，连线是数据流。API 的全部工作就是"提交一张图 + 等结果"。

```
        ┌─────────────────────────────────────────┐
        │  你的 Python 脚本                        │
        └────────┬────────────────────────┬───────┘
                 │ POST /prompt           │ WS /ws?clientId=xxx
                 │ (提交 DAG)              │ (接收进度推送)
                 ▼                        ▼
        ┌─────────────────────────────────────────┐
        │  ComfyUI 服务（单实例串行执行队列）        │
        │   queue_pending → queue_running → history│
        └────────┬────────────────────────────────┘
                 │ GET /view (取图)
                 ▼
             输出文件
```

**必须理解的两件事：**

1. **单实例串行**：ComfyUI 一次只跑一个任务。并发提交只会堆积 `queue_pending`，不会提升吞吐。要提吞吐只能加卡或加实例。
2. **API 格式 ≠ UI 格式**：界面上 `Save` 出来的 JSON **不能**直接提交，必须用 `Export (API)`。

---

## 2. 工作流的两种 JSON 格式

| 维度 | UI 格式（`Export`） | API 格式（`Export (API)`） |
| --- | --- | --- |
| 顶层结构 | `{"nodes": [...], "links": [...]}` | `{"节点ID": {...}, ...}` |
| 节点标识 | `"id": 7`（数字） | `"7"`（字符串 key） |
| 节点类型字段 | `"type"` | `"class_type"` |
| 参数 | `widgets_values: [数组，靠顺序对应]` | `inputs: {命名字段}` |
| 连线 | 独立的 `links` 数组 | 写在 `inputs` 里：`["源节点ID", 输出索引]` |
| 坐标 / 大小 / 颜色 | 有 | 无 |
| 能提交给 `/prompt`？ | ❌ | ✅ |

**API 格式示例：**

```json
{
  "1": {
    "class_type": "CheckpointLoaderSimple",
    "inputs": {"ckpt_name": "v1-5-pruned-emaonly.safetensors"}
  },
  "4": {
    "class_type": "CLIPTextEncode",
    "inputs": {"text": "a cute cat...", "clip": ["1", 1]},
    "_meta": {"title": "POSITIVE_PROMPT"}
  },
  "7": {
    "class_type": "KSampler",
    "inputs": {
      "seed": 148446967917940, "steps": 20, "cfg": 7.0,
      "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
      "model": ["1", 0], "positive": ["4", 0],
      "negative": ["5", 0], "latent_image": ["6", 0]
    },
    "_meta": {"title": "SAMPLER"}
  }
}
```

**连线的读法**：`"clip": ["1", 1]` = 本节点的 `clip` 输入，来自**节点 `"1"` 的第 `1` 个输出**。索引从 0 开始，对应界面上节点右侧输出口的从上到下顺序。

`CheckpointLoaderSimple` 的三个输出：`[0]=MODEL`、`[1]=CLIP`、`[2]=VAE`。

### 2.1 导出 API 格式

1. Settings ⚙️ → 搜 `dev` → 勾选 `Enable dev mode options` → **刷新页面（F5）**
2. `Workflow` 菜单 → `Export (API)`

> Dev Mode 是 **per-ComfyUI-实例** 的设置，存在服务端 `user/default/comfy.settings.json`，不是浏览器设置。两台机器要各开一次。

### 2.2 用 `_meta.title` 定位节点（重要实践）

**不要硬编码节点 ID。** `wf["7"]["inputs"]["seed"]` 在你改一下工作流之后必然失效。

在界面上双击节点标题栏改名，导出的 JSON 会带 `_meta.title`：

```python
def find_by_title(wf: dict, title: str) -> str:
    for nid, node in wf.items():
        if node.get("_meta", {}).get("title") == title:
            return nid
    raise KeyError(f"找不到标题为 {title!r} 的节点")

def find_by_class(wf: dict, class_type: str, index: int = 0) -> str:
    """只有一个实例的节点（SaveImage / VAEDecode）用这个就够。"""
    matches = sorted((nid for nid, n in wf.items()
                      if n["class_type"] == class_type), key=int)
    if not matches:
        raise KeyError(f"找不到 class_type={class_type} 的节点")
    return matches[index]
```

**本项目的标题约定：**

| 标题 | 节点类型 | 用途 |
| --- | --- | --- |
| `POSITIVE_PROMPT` | CLIPTextEncode | 正向提示词 |
| `NEGATIVE_PROMPT` | CLIPTextEncode | 负向提示词 |
| `SAMPLER` | KSampler | seed / steps / cfg |
| `LATENT` | EmptyLatentImage | 宽高 / batch |

---

## 3. REST 接口速查

Base URL 由 `COMFY_HOST` 决定，本项目：
- `http://127.0.0.1:8188` → Windows 本地（RTX 5060）
- `http://127.0.0.1:8288` → Ubuntu（P100，经 SSH 隧道）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/system_stats` | 服务健康 + GPU 型号 + 显存 |
| GET | `/object_info` | **所有**节点的输入输出定义 |
| GET | `/object_info/{class_type}` | 单个节点的定义（含可选值枚举） |
| POST | `/prompt` | 提交任务 |
| GET | `/queue` | 队列状态 |
| POST | `/queue` | 清空队列 / 删除指定任务 |
| GET | `/history` | 全部历史 |
| GET | `/history/{prompt_id}` | 单个任务结果 |
| POST | `/interrupt` | 中断当前执行 |
| GET | `/view` | 下载输出文件 |
| POST | `/upload/image` | 上传输入图（img2img / ControlNet） |
| WS | `/ws?clientId=xxx` | 实时进度推送 |

### 3.1 `GET /system_stats` — 健康检查

**这应该是每个脚本的第一个调用。** 服务没起就立刻报错，别等提交任务才发现。

```json
{
  "system": {
    "os": "nt", "ram_total": 34124718080, "ram_free": 21286678528,
    "comfyui_version": "0.3.67", "python_version": "3.14.6 ...",
    "pytorch_version": "2.11.0+cu128",
    "argv": ["main.py", "--disable-cuda-malloc"]
  },
  "devices": [{
    "name": "cuda:0 NVIDIA GeForce RTX 5060 Laptop GPU : native",
    "type": "cuda", "index": 0,
    "vram_total": 8546484224, "vram_free": 5098239334,
    "torch_vram_total": 2147483648, "torch_vram_free": 4257126
  }]
}
```

**几个字段的诊断价值：**

| 字段 | 说明 |
| --- | --- |
| `devices[0].name` 末尾 | `native` 或 `cudaMallocAsync` —— **分配器类型，直接影响性能**（见 §7.1） |
| `system.argv` | 服务实际用了哪些启动参数，排查"改了参数没生效"必看 |
| `torch_vram_free` | torch 预留池剩余。接近 0 说明内存池耗尽，任务可能被静默丢弃 |
| `vram_total - vram_free` | 当前显存占用，用于记录峰值水位 |

### 3.2 `GET /object_info/{class_type}` — 查节点定义

**用途**：提交前校验参数合法性，避免 400。

```python
def list_checkpoints(session) -> list:
    r = session.get(f"{COMFY_HOST}/object_info/CheckpointLoaderSimple")
    return r.json()["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
```

返回结构中，**枚举类型的可选值是一个嵌套在列表里的列表**：

```json
{"CheckpointLoaderSimple": {"input": {"required": {
  "ckpt_name": [["v1-5-pruned-emaonly.safetensors", "sd_xl_base_1.0.safetensors"]]
}}}}
```

注意 `[0]` 才能取到实际列表。同理可查 `KSampler` 的 `sampler_name` / `scheduler` 可选值。

> **双机场景必做**：提交前校验工作流里的模型名在**目标机器**上存在。两台机器的模型库不一定同步。

### 3.3 `POST /prompt` — 提交任务

```python
r = session.post(f"{COMFY_HOST}/prompt",
                 json={"prompt": workflow, "client_id": client_id},
                 timeout=(5, 30))
```

**`client_id` 的作用**：只有提交时带的 `client_id` 与 WebSocket 连接的 `clientId` **一致**，才能收到该任务的进度推送。纯 REST 轮询时可以随便给。

**成功响应：**

```json
{"prompt_id": "9d89a1eb-...", "number": 3, "node_errors": {}}
```

**⚠️ 错误处理的关键坑**：校验失败时 ComfyUI 返回 **400 + 详细的 `node_errors`**。直接 `r.raise_for_status()` 会**丢掉最有用的信息**，必须先读 body：

```python
if r.status_code >= 400:
    try:
        err = r.json()
        raise RuntimeError(f"提交被拒 ({r.status_code}): "
                           f"{json.dumps(err, ensure_ascii=False, indent=2)}")
    except ValueError:          # body 不是 JSON
        r.raise_for_status()

body = r.json()
if body.get("node_errors"):     # 有些版本 200 也带 node_errors
    raise RuntimeError(f"节点错误: {json.dumps(body['node_errors'], ensure_ascii=False)}")
if "prompt_id" not in body:
    raise RuntimeError(f"响应异常，无 prompt_id: {body}")
```

`node_errors` 长这样，能精确定位到哪个节点的哪个字段：

```json
{"7": {"errors": [{
  "type": "value_not_in_list",
  "message": "Value not in list",
  "details": "sampler_name: 'eular' not in ['euler', 'euler_ancestral', ...]",
  "extra_info": {"input_name": "sampler_name"}
}]}}
```

### 3.4 `GET /queue` — 队列状态

```json
{
  "queue_running": [[0, "prompt_id", {...}, {...}, [...]]],
  "queue_pending": [[1, "prompt_id", ...]]
}
```

任务 ID 在**每个数组的索引 1**：

```python
running = [item[1] for item in q.get("queue_running", [])]
pending = [item[1] for item in q.get("queue_pending", [])]
```

**批量场景的流控**：

```python
def wait_queue_clear(session, max_pending: int = 2):
    """ComfyUI 串行执行。积压太多就先等，避免无脑灌任务。"""
    while True:
        q = session.get(f"{COMFY_HOST}/queue", timeout=(5, 10)).json()
        if len(q.get("queue_pending", [])) <= max_pending:
            return
        time.sleep(3)
```

### 3.5 `GET /history/{prompt_id}` — 取结果

```json
{"9d89a1eb-...": {
  "prompt": [...],
  "outputs": {"9": {"images": [
    {"filename": "batch_001_00001_.png", "subfolder": "20260910_114915", "type": "output"}
  ]}},
  "status": {"status_str": "success", "completed": true, "messages": [...]}
}}
```

**判断成功/失败看 `status.status_str`**（`success` / `error`）。

### 3.6 `GET /view` — 下载输出

```python
params = {
    "filename": img["filename"],
    "subfolder": img.get("subfolder", ""),   # ← 不能省
    "type": img.get("type", "output"),       # ← 不能省
}
r = session.get(f"{COMFY_HOST}/view", params=params)
```

> **坑**：`subfolder` 和 `type` 省略后，输出在子目录时会 **404**。用 `filename_prefix="子目录/前缀"` 时输出就在子目录里。

### 3.7 `POST /upload/image` — 上传输入图

img2img / ControlNet 的前置步骤：

```python
def upload_image(session, local_path, subfolder="", overwrite=True) -> str:
    p = Path(local_path)
    r = session.post(f"{COMFY_HOST}/upload/image",
                     files={"image": (p.name, p.read_bytes(), "image/png")},
                     data={"subfolder": subfolder,
                           "overwrite": str(overwrite).lower()})
    r.raise_for_status()
    return r.json()["name"]      # 把这个名字填进 LoadImage 节点的 inputs.image
```

### 3.8 `POST /interrupt` — 中断执行

**批量脚本的必备**：任务超时后不中断就直接重试，新任务会排在卡死的任务后面，必然继续超时。

```python
if isinstance(e, TimeoutError):
    session.post(f"{COMFY_HOST}/interrupt", timeout=(5, 10))
```

---

## 4. WebSocket 实时进度

### 4.1 为什么用 WS

| 维度 | REST 轮询 | WebSocket |
| --- | --- | --- |
| 感知粒度 | 只知道"做完没"，最小 2 秒 | **步级**（第 7/20 步） |
| 延迟 | 最长 = 轮询间隔 | < 100 ms |
| 20 张图的请求数 | ~200 次 HTTP | 1 条长连接 |
| 错误信息 | 需查 history | 推送 `execution_error`，含 traceback |
| 实现复杂度 | 低 | 中（要处理消息类型和二进制帧） |

### 4.2 连接

```python
ws_url = f"ws://{host}:{port}/ws?clientId={client_id}"
```

**`client_id` 必须与 `POST /prompt` 时提交的一致**，否则收不到消息。

```python
import websocket   # pip install websocket-client   ← 不是 websockets
ws = websocket.WebSocket()
ws.connect(ws_url, timeout=10)
ws.settimeout(1.0)      # 单次 recv 超时，用于定期检查总超时
```

### 4.3 消息类型

| type | data 关键字段 | 含义 |
| --- | --- | --- |
| `status` | `exec_info.queue_remaining` | 队列长度变化（无 prompt_id） |
| `execution_start` | `prompt_id` | 任务开始 |
| `execution_cached` | `nodes: []` | 命中缓存的节点，**这些节点不会有 progress** |
| `executing` | `node` | 当前执行到哪个节点；**`node: null` = 该 prompt 结束** |
| `progress` | `value`, `max`, `node` | 步级进度 |
| `executed` | `node`, `output` | 某节点产出了输出 |
| `execution_success` | `prompt_id` | 任务成功（较新版本） |
| `execution_error` | `node_id`, `exception_message`, `traceback` | 任务失败 |
| `execution_interrupted` | `node_id` | 被 `/interrupt` 中断 |
| *(binary frame)* | — | 预览图，**直接跳过** |

### 4.4 完整处理骨架

```python
while time.time() < deadline:
    try:
        raw = ws.recv()
    except websocket.WebSocketTimeoutException:
        continue                          # 正常，继续等
    except websocket.WebSocketConnectionClosedException:
        raise RuntimeError("WebSocket 被服务端关闭，ComfyUI 可能已重启")

    if isinstance(raw, bytes):
        continue                          # 预览图二进制帧

    msg  = json.loads(raw)
    data = msg.get("data", {}) or {}

    # 只处理自己这个 prompt 的消息（status 消息没有 prompt_id，放行）
    mpid = data.get("prompt_id")
    if mpid is not None and mpid != prompt_id:
        continue

    t = msg.get("type")
    if t == "progress":
        render_bar(data["value"], data["max"])
    elif t == "execution_success":
        return fetch_entry(session, prompt_id)
    elif t == "executing" and data.get("node") is None and started:
        return fetch_entry(session, prompt_id)      # 老版本的完成信号
    elif t == "execution_error":
        raise RuntimeError(f"执行失败 node={data.get('node_id')}: "
                           f"{data.get('exception_message')}")
```

**三个必须处理的细节：**

1. **二进制帧**：预览图会以 binary frame 推过来，`json.loads` 会炸，必须先判断 `isinstance(raw, bytes)`
2. **完成信号有两种**：新版发 `execution_success`，老版发 `executing` + `node: null`。**两个都要处理**
3. **WS 说完成后仍要查 `/history`**：WS 只告诉你"完成了"，输出文件清单要从 history 取。且 history 写入可能**略滞后**于 WS 通知，需要重试几次：

```python
for _ in range(5):
    hist = session.get(f"{COMFY_HOST}/history/{prompt_id}").json()
    if prompt_id in hist:
        return hist[prompt_id]
    time.sleep(0.3)
raise RuntimeError("WS 报告完成但 history 查不到，ComfyUI 可能在写入前崩溃")
```

### 4.5 用法

```python
with make_session() as s, ComfyWS() as ws:
    pid = submit(s, wf, client_id=ws.client_id)    # ← client_id 必须一致
    entry = ws.wait(s, pid, timeout=120)
```

批量脚本用环境变量开关，**失败自动降级到轮询**——WS 是增强，不是依赖：

```bat
set COMFY_USE_WS=1
python comfy_batch_gen.py
```

---

## 5. 参数修改模式

### 5.1 基本模式：deepcopy + 命名定位

```python
def build_prompt(wf_template, *, positive=None, seed=None, steps=None, ...):
    wf = copy.deepcopy(wf_template)      # ← 必须 deepcopy，否则污染模板

    if positive is not None:
        wf[find_by_title(wf, "POSITIVE_PROMPT")]["inputs"]["text"] = positive

    sampler = wf[find_by_title(wf, "SAMPLER")]["inputs"]
    sampler["seed"] = seed if seed is not None else random.randint(0, 2**63 - 1)
    if steps is not None:
        sampler["steps"] = steps

    return wf
```

**传 `None` 保持模板原值**的设计，让调用方只需要指定关心的参数。

### 5.2 ⚠️ seed 不会自动随机

UI 格式的 KSampler 有个 `"randomize"` 开关：

```json
"widgets_values": [148446967917940, "randomize", 20, 8, "euler", "normal", 1]
                                    ^^^^^^^^^^^ 界面上的"每次随机"
```

**API 格式里没有这个字段**，只有一个固定的 `seed` 数字。通过 API 提交时 **seed 不会自动变**，不显式给就每次出同一张图。

```python
sampler["seed"] = random.randint(0, 2**63 - 1)
```

### 5.3 双机切换

```python
COMFY_HOST = os.getenv("COMFY_HOST", "http://127.0.0.1:8188").rstrip("/")
```

```bat
set COMFY_HOST=http://127.0.0.1:8188   :: 5060
set COMFY_HOST=http://127.0.0.1:8288   :: P100
```

因为走 SSH 隧道，host 永远是 `127.0.0.1`，**不需要改 IP、不需要开防火墙、不需要暴露端口**。

---

## 6. 生产实践清单

| 实践 | 理由 |
| --- | --- |
| **所有请求设 timeout** | `TIMEOUT = (5, 30)` —— 不设 timeout 是运维大忌，一次网络黑洞挂死整个脚本 |
| **重试 + backoff** | `Retry(total=3, backoff_factor=0.5, status_forcelist=[502,503,504])` |
| **前置 `/system_stats`** | 服务没起立刻报错，不用等提交才发现 |
| **前置校验模型名** | `/object_info` 查目标机器的模型列表，双机场景必做 |
| **读 400 响应体** | `node_errors` 是最有用的排错信息，`raise_for_status()` 会丢掉 |
| **`_meta.title` 定位** | 改工作流不会崩 |
| **`deepcopy` 模板** | 不会因为改了一次参数污染后续所有任务 |
| **自适应超时** | 固定 600s 对 2 秒的任务毫无意义。用 `中位数 × 6`，首张给足冷启动时间 |
| **超时后 `/interrupt`** | 不中断就重试，新任务排在卡死任务后面，必然继续超时 |
| **检测任务"消失"** | 见 §7.2 |
| **`wait_queue_clear`** | ComfyUI 串行执行，无脑灌任务只堆队列 |
| **单任务重试** | 一张失败不该让整批白跑 |
| **结构化日志双写** | 控制台看进度，文件留档给后续接 Prometheus |
| **`filename_prefix` 带 run_id** | 每批图落独立子目录，不互相覆盖 |
| **记录 `attempts`** | 能看出"一次成功"还是"重试后成功"，稳定性指标 |
| **P95 而非只有平均** | 平均值掩盖长尾，P95 才是 SLA 语言 |
| **百分位用线性插值** | 见 §7.3 |

---

## 7. 实战踩坑记录

### 7.1 `cudaMallocAsync` 导致 3 倍性能劣化 + 批量任务静默丢弃

**这是本项目最重要的发现。**

| 危害 | 表现 |
| --- | --- |
| 恒定性能开销 | 每张图 +4 秒。5060 从 6.03s 降到 **2.01s**，快 3 倍 |
| 批量任务静默丢弃 | 20 连发跑到第 4~7 张卡死；`/queue` 和 `/history` 都查不到任务；`torch_vram_free` 仅剩 4 MB / 2 GB 预留池 |

**跨架构共性**：最初在 P100（Pascal）上因 cuDNN 报错顺带发现，后证实 **RTX 5060（Blackwell）同样受影响**。

**修复**：ComfyUI 启动加 `--disable-cuda-malloc`。`/system_stats` 里 `devices[0].name` 末尾应从 `cudaMallocAsync` 变成 `native`。

> **方法论教训**：09-09 首轮测试中，5060 每张图都是 6.03s，抖动仅 30ms。当时的结论是"Blackwell 调度成熟、无 GC 抖动"——**完全错了**。真相是"2s 计算 + 4s 恒定开销"。
>
> **指标平稳不代表指标健康。恒定开销比波动开销更难发现，正因为它看起来很正常。** 只看方差会漏掉这类问题，必须有**跨配置基线对比**或**绝对性能预期**。

### 7.2 任务从 ComfyUI "消失"

**现象**：`/history/{prompt_id}` 查不到，`/queue` 里也没有，脚本傻等到超时。

**原因**：显存池耗尽、驱动 TDR 复位、ComfyUI 内部异常未落盘。

**处理**：连续 N 轮"两边都找不到"且**曾经在队列里出现过**，判定任务已丢失，快速失败：

```python
was_seen = False          # 是否曾在 queue 里出现过
vanish_count = 0

# ... 轮询中
if prompt_id in running or prompt_id in pending:
    was_seen, vanish_count = True, 0
elif was_seen:
    vanish_count += 1
    if vanish_count >= 5:      # 5 轮 × 2s = 10 秒内报错，不是傻等 60 秒
        raise RuntimeError(f"任务 {prompt_id} 已从 ComfyUI 消失")
```

### 7.3 百分位的样本量陷阱

**错误写法**：

```python
k = min(int(len(times) * p / 100), len(times) - 1)
return times[k]
```

`n=10, p=95` 时 `int(10 × 0.95) = 9`，正好是最后一个元素 → **P95 恒等于 max，没有统计意义**。

**正确写法**（线性插值，与 `numpy.percentile` 默认行为一致）：

```python
def pct(times, p):
    if len(times) <= 1:
        return times[0] if times else None
    k = (len(times) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(times) - 1)
    return round(times[lo] + (times[hi] - times[lo]) * (k - lo), 2)
```

**样本量要求**：P95 至少 20 个样本才有参考价值。脚本里加个自检字段：

```python
pct_confidence = "ok" if n >= 20 else f"low (n={n}, 建议 >= 20)"
```

### 7.4 其他

| 坑 | 解决 |
| --- | --- |
| `Export` vs `Export (API)` 搞混 | 检查顶层 key：`['1','4','5',...]` 是 API 格式；`['nodes','links']` 是 UI 格式 |
| Dev Mode 打勾后菜单没变 | **刷新页面（F5）** |
| Dev Mode 在另一台机器没生效 | 它是 per-实例 的服务端设置，两台机器要各开一次 |
| SSH 隧道本地端口冲突 | Windows 本地 ComfyUI 占 8188，隧道改用本地 8288 |
| 改了标题但脚本找不到 | 改完标题**必须重新 Export (API)** |
| `/view` 返回 404 | 漏了 `subfolder` / `type` 参数 |
| 模型名在目标机器不存在 | 提交前用 `/object_info` 校验 |
| WebSocket 收不到消息 | `client_id` 与提交时不一致 |
| `json.loads` 炸在 WS 消息上 | 没跳过二进制预览帧 |
| `pip install websockets` 后 import 失败 | 要装的是 **`websocket-client`**，不是 `websockets` |

---

## 8. 相关文件

```
Scripts/
├─ comfy_client.py         # REST 客户端：提交/等待/下载/上传
├─ comfy_ws.py             # WebSocket 客户端：步级进度
├─ comfy_batch_gen.py      # 批量出图 + 指标采集
├─ inspect_workflow.py     # 工作流结构检查
└─ workflows/
    ├─ base_workflow_api.json   # API 格式（脚本用）
    └─ base_workflow_ui.json    # UI 格式（留档）
```

实测性能数据见 `benchmarks-实测数据.md`。

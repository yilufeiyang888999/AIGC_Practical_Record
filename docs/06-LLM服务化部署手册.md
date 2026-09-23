# LLM 服务化部署手册（llama.cpp / P100）

> **文档用途**：在 P100（Pascal sm_60 / 16G）上以 llama.cpp 部署 OpenAI 兼容 LLM 服务的完整手册：编译、量化选型、调优参数、统一网关接入、资源共存与运维。
> **适用对象**：AI Infra / MLOps 工程师，以及需要复用本方案的交付方。
> **版本**：V1.0（2026-09-18，对应 llama-server master + aigc-gateway v0.1，全部性能数字为实测）

---

## 1. 概述

本手册覆盖 LLM 服务化的完整链路：模型 → llama-server → FastAPI 统一网关 → Prometheus 监控。核心结论先行：

| 结论 | 实测依据 |
| --- | --- |
| P100 跑不了 vLLM，**llama.cpp 是唯一可行服务框架** | vLLM 要求计算能力 ≥ 7.0，P100 是 sm_60 |
| **decode 28.7 tok/s 是 9B Q8_0 的硬顶**（带宽瓶颈） | benchmarks §2.5，与 prompt 长度无关 |
| **`--ubatch-size ≥ 256` 让 prefill 提升 3.2×**（162→513 tok/s） | A/B 实测，零成本调优（§6） |
| **Q4_K_M 反而比 Q8_0 慢 22%**（反量化开销 > 带宽收益） | 反直觉，§5.3 |
| **轻负载可与 ComfyUI 同卡共存，重负载必须切换** | 显存账见 §8 |

---

## 2. 硬件与驱动约束

| 约束 | 说明 |
| --- | --- |
| 驱动**锁死 535 分支** | 555+ 的 CUDA runtime 移除 sm_60；580 实测 llama.cpp 直接跑不了（合订本坑 #2） |
| **只能用 CUDA 后端，不能指望 Tensor Core** | Pascal 无 Tensor Core，量化模型靠 MMQ（整数矩阵乘）kernel |
| 16G 显存 | 9B Q8_0 + 64K ctx（KV q8_0）≈ 11.2 GB；14B Q4 + 32K ctx 更紧 |
| Proxmox 透传 | **可**用 `nvidia-smi --query-compute-apps` 做进程归属排查（09-18 实测，推翻了此前"透传拿不到"的错误结论） |

## 3. 编译

```bash
git clone https://github.com/ggml-org/llama.cpp.git /opt/llama.cpp
cd /opt/llama.cpp
# 关键：显式指定 sm_60，否则运行时才炸
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=60
cmake --build build --config Release -j$(nproc)
```

> **编译前确认驱动是 535**：驱动 ≥ 555 时编译能过，但运行时 CUDA kernel 找不到 sm_60 路径，静默退化到 CPU。

## 4. 模型矩阵与量化选型

当前模型库（`/opt/llama.cpp/models/`）：

| 模型 | 量化 | ctx | 定位 |
| --- | --- | --- | --- |
| **ornith-1.0-9b-Q8_0** | Q8_0 | 65536 | **主力**（decode/prefill 双优，§5.3） |
| ornith-1.0-9b-Q6_K | Q6_K | 65536 | 均衡备选 |
| Qwen3-14B-Q4_K_M | Q4_K_M | 32768 | 能力备选（decode 约为主力的 78%） |
| Qwen3-Embedding-0.6B-f16 | F16 | 32768 | embedding 专用（独立端口 8001，RAG 前置） |

**量化选型原则（P100 实测版，与社区常识不同）**：

> 社区常识"Q4 更快"在现代卡上成立（Tensor Core 加速反量化）。**Pascal 无 Tensor Core，Q4_K_M 的超块反量化开销吃掉了权重变小的带宽收益 → 实测比 Q8_0 慢 22%。** P100 上：显存够用就 Q8_0，Q4_K_M 留给"不量化就装不下"的模型。

## 5. 服务配置

### 5.1 启动参数（llm-switch.sh 定稿版）

```bash
export CUDA_MODULE_LOADING=LAZY      # 省显存，代价是首请求 ~0.9s kernel 加载税
export GGML_CUDA_NO_VMM=1            # 老驱动下关虚拟内存管理，规避 VMM 问题
export GGML_CUDA_FORCE_MMQ=1         # 强制 MMQ kernel，Pascal 上量化模型更快
export GGML_CUDA_SM=60               # 显式架构
export OMP_NUM_THREADS=8

llama-server \
  --model <model.gguf> \
  --ctx-size 65536 \
  --batch-size 256 --ubatch-size 256 \   # ⚠️ ubatch 32→256 是 3.2× prefill 的关键，见 §6
  --n-gpu-layers 99 \
  --cache-type-k q8_0 --cache-type-v q8_0 \  # KV cache 量化，64K ctx 敢开的原因
  --no-mmap --parallel 1 \
  --host 0.0.0.0 --port 8000 \
  --metrics                              # 暴露 Prometheus 指标（接入监控栈）
```

### 5.2 P100 调优四参数（本方案核心资产）

| 参数 | 作用 | 来源 |
| --- | --- | --- |
| `GGML_CUDA_NO_VMM=1` | 规避老驱动 VMM 问题 | 用户实践 |
| `GGML_CUDA_FORCE_MMQ=1` | 强制 MMQ 整数 kernel | 用户实践 |
| `GGML_CUDA_SM=60` | 显式指定架构 | 用户实践 |
| **`--ubatch-size 256`** | **prefill 3.2×** | **09-18 A/B 实测发现（§6）** |

### 5.3 实测性能基线（benchmarks §2.5 摘要）

| 模型 | decode | prefill（冷） | TTFT 冷（1400 tok） | 备注 |
| --- | --- | --- | --- | --- |
| **ornith-9B-Q8_0** | **28.7 tok/s**（ctx 长度无关） | **513 tok/s** | 2.74 s | 主力 |
| Qwen3-14B-Q4_K_M | 22.5 → 19.6 tok/s（**ctx 变长 -13%**） | 206 tok/s | 5.43 s | 备选 |

其他关键实测：

- **KV 前缀缓存**：1400 tok 重复前缀，TTFT 从 8.11s（ubatch 32 时代）/ 2.74s → **0.6~1.7s**。多轮对话/RAG 场景重复前缀几乎免费
- **`--parallel 1` 并发 = 纯排队**（max/min 耗时比 ≈ 2）→ 并发控制必须在网关层做
- **思考型模型**：`max_tokens` 必须给足（reasoning 会吃掉预算）；TTFT 与"首个正文 token"是两个指标
- **重启后首请求 ~0.9s kernel 加载税**（LAZY 的代价）→ 生产上启动后先发 warmup 请求

## 6. 调优实录：ubatch A/B

**假设**：初测 prefill 恒为 162 tok/s，其结构可疑——162 ≈ 32 token/批 ÷ 197ms/批，**耗时被每批固定开销主导，与计算量无关**。

**实验**：唯一变量 `--ubatch-size` 32→256，重跑同一基准（llm_bench.py）。

**结果**：prefill 162→513 tok/s（**3.2×**）；17 tok 小 prompt 零变化（本来就装不满一批）、decode 纹丝不动（逐 token 生成与 ubatch 无关）。三个现象指向同一机理，假设实锤。长上下文 TTFT 从 8.11s → 2.74s。

**方法论**：瓶颈不在算力时，**看数字的结构比看数字的大小更重要**（每批耗时 × 批数 = 总耗时，哪个是常数哪个是变量，拆得清清楚楚）。

## 7. 统一网关（aigc-gateway v0.1）

### 7.1 架构与端点

```
客户端
  │  x-api-key 鉴权
  ▼
aigc-gateway (FastAPI, :8300)
  ├─ POST /v1/images/generations → 异步任务队列 → ComfyUI（单 worker 串行）
  ├─ GET  /v1/tasks/{id}         → 排队/执行状态 + 结果文件链接
  ├─ POST /v1/chat/completions   → 流式透传 → llama-server
  ├─ GET  /metrics               → Prometheus（aigc_* 应用层指标）
  └─ /files/*                    → 出图结果静态下载
```

### 7.2 关键设计决策（每条对应实测）

| 决策 | 依据 |
| --- | --- |
| 出图异步（202 + task_id 轮询） | 出图 8~18s，批处理语义 |
| LLM 流式透传 | TTFT 0.29s，交互语义 |
| 出图**单 worker 串行** | ComfyUI 单实例串行执行，多 worker 只是挪排队位置 |
| **队列深度是核心健康指标** | 两个后端都是串行+排队（实测 max/min ≈ 2） |
| queue_wait / duration / TTFT 分离度量 | 阶段二教训：排队与执行必须分开 |

### 7.3 部署与配置

```bash
pip install -r requirements.txt   # fastapi uvicorn httpx requests prometheus-client

# 全部配置走环境变量（后端地址与网关位置解耦）
# cmd:  set COMFY_HOST=http://127.0.0.1:8188
# PS:   $env:COMFY_HOST = "http://127.0.0.1:8188"   ⚠️ PowerShell 不能用 set（坑 #49）
set LLM_HOST=http://127.0.0.1:8000
set AIGC_API_KEY=<生产密钥>        # 空 = 无鉴权开发模式（启动有 WARNING）
uvicorn main:app --host 127.0.0.1 --port 8300    # Windows 开发机
```

**Ubuntu 生产部署（systemd，09-20 定稿）**：unit 文件见 `systemd/aigc-gateway.service`。两个要点：

1. **`--host 172.17.0.1`（docker0 网桥），不是 127.0.0.1 也不是 0.0.0.0**（坑 #52）：绑 lo 则 Prometheus 容器经 `host.docker.internal` 抓不到（connection refused）；绑 0.0.0.0 则把无鉴权的 `/metrics` `/files` `/health` 暴露到局域网。172.17.0.1 上容器、宿主机、SSH 隧道都可达，局域网无路由不可达。
2. 后端地址本机直连（`COMFY_HOST=http://127.0.0.1:8188`、`LLM_HOST=http://127.0.0.1:8000`），API key 在 `/opt/AIGC/aigc-gateway/.env`（chmod 600）。

Windows 侧经 SSH 隧道访问：`LocalForward 8300 172.17.0.1:8300`（注意转发目标是网桥 IP，不是 127.0.0.1）。

### 7.4 v0.1 已知边界

| 边界 | 说明 |
| --- | --- |
| 任务表在内存 | 重启即清空（结果文件在磁盘不受影响）；v2 落 SQLite |
| 无速率限制 | 队列本身是天然限流；多用户场景再加 token bucket |
| LLM 无网关侧信号量 | llama-server `--parallel 1` 内部已串行，重复排队无意义 |

## 8. 资源共存规则（16G 显存 + 24G 内存实测账）

> ⚠️ **09-20 修正**：初版"LLM + SD1.5 可共存"只验证了**静态驻留**，未验证**并发出图**。实测 VAE decode 峰值撞 OOM，边界如下。

| 组合 | 显存账 | 结论 |
| --- | --- | --- |
| llama-server 驻留（~9.7G）+ ComfyUI 驻留（空闲） | ~10.5G / 16G | ✅ 静态共存 |
| llama-server 驻留 + **SD1.5 出图 512×512** | 峰值 ~13.5G | ✅ 可行（余量紧） |
| llama-server 驻留 + **SD1.5 出图 768×512** | 峰值 ~14.4G，VAE decode 需 2.25G 空闲仅 1.46G | ❌ **OOM**（09-20 实测，采样完成后死在 VAE decode） |
| llama-server 驻留 + **SDXL 推理**（~12.2G 峰值） | 超 16G | ❌ **不能共存**（09-22 SDXL 实测显存；SDXL 出图前必须停 LLM） |
| llama-server + LoRA 训练 / SDXL 训练 | 超 16G | ❌ 必须切换 |
| ComfyUI + kohya 训练 | 见阶段四 | ❌ 必须切换 |

**09-20 OOM 事故的工程产出（两条）**：

1. **网关 GPU 准入控制**（`AIGC_MIN_FREE_VRAM_GB`，默认 2.6G）：出图任务执行前查 `/system_stats` 空闲显存，不足则等待（默认 120s）或明确报"GPU 准入拒绝"，**代替 18 秒后的裸 OOM**。
2. **ComfyUI 启动加 `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`**：OOM 报错显示 1.66G 是 PyTorch 保留但未分配的碎片，此参数可回收——加完后 768×512 在 LLM 驻留下也能通过（空闲 1.46+1.66 ≈ 3.1G > 需求 2.3G）。

**修正历史认知**：24G 内存扩容前文档写"ComfyUI 与 llama-server 不能同时运行"。09-18/09-20 两轮实测后的精确规则：**静态共存 ✅；并发出图看分辨率（512×512 ✅ / 768×512 需碎片治理 ✅ / 无治理 ❌）；训练与 SDXL 必须切换 ❌**。

## 9. 运维与监控

| 项 | 做法 |
| --- | --- |
| 硬件层 | dcgm-exporter → Prometheus → Grafana（04 方案，温度墙告警 79°C 实测拐点） |
| 应用层 | 网关 `/metrics`（aigc_* 指标）+ llama-server `--metrics`，接入同一 Prometheus |
| GPU 占用排查 | `nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv` 或 `fuser -v /dev/nvidia*` |
| 启动后 | 先发 warmup 请求（kernel 加载税），再开放流量 |
| 进程清理 | **按端口找 PID 精杀**（`netstat -ano \| findstr :8300`），禁止 `taskkill /IM python.exe /F` 连坐（坑 #50） |

## 10. 故障排查速查表

| 症状 | 根因 | 解法 |
| --- | --- | --- |
| llama.cpp 编译过但跑 CPU | 驱动 ≥ 555，无 sm_60 runtime | 锁 535（坑 #2） |
| 流式解析 JSON 切碎（中文 prompt） | requests Latin-1 误判，0x85 被当换行 | 按字节切行再手动 decode（坑 #47） |
| 网关鉴权"设了不生效" | PowerShell 的 `set` 不设进程环境变量 | `$env:VAR="value"`（坑 #49） |
| 出图 ConnectionError 10053/10054 | 隧道没起 / ComfyUI 被误杀 | 先 `curl /system_stats` 确认后端活着 |
| uvicorn 重启后行为诡异 | Windows 允许多进程绑同端口，残留进程抢答 | netstat 找 PID 精杀（坑 #50） |
| prefill 只有 ~160 tok/s | ubatch=32 默认值的固定开销 | `--ubatch-size 256`（§6） |

## 11. 工程要点回顾

1. **从 162 这个数字的结构反推出瓶颈** —— 162 ≈ 32 tok ÷ 197ms/批，固定开销主导 → ubatch A/B 验证 → 零成本 3.2×。这是**假设驱动的调优**，不是瞎试参数。
2. **Q4 比 Q8 慢，和社区常识相反** —— Pascal 无 Tensor Core，反量化开销 > 带宽收益。**不把别人的结论当自己的结论**，量化方式是唯一裁判。
3. **网关的每个设计决策都有实测依据** —— 串行 worker、队列深度指标、异步/流式分流，全部能追到 benchmark 数字。
4. **推翻过自己关于平台限制的结论** —— Proxmox 透传 per-process 归属，复测证伪后 7 处文档同步修正。**证据优先于既有结论（包括自己的结论）**。

## 12. 附录

| 项 | 位置 |
| --- | --- |
| 网关代码 | `gateway/aigc-gateway/`（main/config/tasks/metrics/routers） |
| 基准脚本 | `Scripts/llm_bench.py` |
| 实测数据 | `benchmarks-实测数据.md` §2.5 + `Scripts/logs/llm_bench_*.json` |
| 模型切换 | Ubuntu `/opt/llama.cpp/llm-switch.sh` |
| 端口规划 | 8000 LLM / 8001 embedding / 8188 ComfyUI 裸机 / 8189 容器 / **8300 网关** |

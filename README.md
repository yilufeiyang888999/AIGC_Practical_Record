# AIGC Engineer Portfolio

> 10+ 年 IT 运维 → AIGC 工程。一套**真实运行、全部数字实测**的私有化 AIGC 平台：双机异构部署 → API 自动化 → 容器化 → GPU 可观测性 → LoRA 训练 → LLM 服务化 → 统一网关 → 企业方案。
> **本仓库的每个性能数字都有原始数据文件可查，每次结论修正都留了案。**

---

## 为什么值得看这个仓库

| 差异化 | 说明 |
| --- | --- |
| **① 全部实测，且原始数据可自验** | **图像 18 轮 / 340 样本 + LLM 7 轮 / 63 条请求**，原始 JSON+CSV 全部入库 [`benchmarks/raw/`](benchmarks/raw/)；`python scripts/summarize_runs.py benchmarks/raw` **一键重算**本文档所有表格——不必信任我写的数字 |
| **② 六次推翻自己的结论，全部记档** | 从"cudaMallocAsync 跨架构共性"到"Proxmox 透传拿不到 per-process 归属"——每次修正保留原结论 + 证伪证据 + 日期。**独立成文：[09-自我修正与方法论](docs/09-自我修正与方法论.md)** |
| **③ 交付物即产物** | 9 份专题手册（+ 1 份合订本） + 一键部署/回滚脚本 + docker-compose + 监控规则 + systemd 单元 + 统一网关源码（含单测），不是玩具 demo，是可移交的生产件 |

---

## 能力边界（先说清楚不能做什么）

**主动声明边界比吹嘘能力更可信**——这也是本仓库和"只会吹"的作品集最大的区别。

| 项 | 现状 |
| --- | --- |
| **FLUX 系列** | ❌ **本平台不覆盖**。fp16 需 ~24G，而 P100 无 FP8 可压缩（需 sm_89+） |
| **vLLM** | ❌ **P100 不可用**（要求计算能力 ≥ 7.0）。LLM 走 llama.cpp，其 CUDA 后端原生支持 Pascal |
| **SDXL 训练 / 推理 与 LLM 共存** | ❌ **不能共存**。SDXL 推理峰值 ~12.2G + LLM 驻留 ~9.7G > 16G，必须切换（规则固化在 `systemd/llama-server.service` 与 `scripts/train_start.sh` 的预检里） |
| **bf16 / FP8** | ❌ **P100 全不支持**（Pascal 无 Tensor Core）。训练与推理一律显式 fp16 |
| **P100 温度墙** | ⚠️ **已充分量化的物理约束**：79°C 拐点、~70s 撞线、降速 13~30%。告警阈值/容量公式/训练时长税均已按此标定 |
| **A10 两轮原始数据** | ❌ **已丢失**（随 ModelScope 实例释放）。这是本仓库**唯一一组不可查证的实测数据**，已如实标注 |

> 这套硬件的目标是把 **SD 1.5 / SDXL 的工程链路跑通并做出可交付成果**，不是追 FLUX。
> 对企业来说，"能把平台搭起来、能排错、能交付"比"家里有 H100"稀缺得多。

---

## 平台架构

```
 Windows 开发端（RTX 5060 8G, Blackwell sm_120）
   │  SSH 隧道 / API 调用方 / 基准脚本
   ▼
 Ubuntu 算力端（Tesla P100 16G, Pascal sm_60, Proxmox 透传 VM）
 ┌────────────────────────────────────────────────────┐
 │  aigc-gateway (FastAPI :8300)                       │
 │  鉴权 / 任务队列 / GPU 准入控制 / Prometheus 指标    │
 │    ├──▶ ComfyUI :8188（SD1.5 + 自训 LoRA）          │
 │    └──▶ llama-server :8000（9B/14B，OpenAI 兼容）   │
 │                                                    │
 │  三层监控：dcgm-exporter(硬件) → Prometheus → Grafana│
 │           llama-server --metrics(服务)  ↗            │
 │           gateway /metrics(应用)        ↗            │
 │                                                    │
 │  容器化：ComfyUI 镜像（离线可复现）+ 监控栈 compose   │
 │  训练：sd-scripts（LoRA 全链路，与推理资源互斥）      │
 └────────────────────────────────────────────────────┘
```

---

## 核心实测数据（摘要，全量见 [benchmarks/实测数据.md](benchmarks/实测数据.md)）

**图像生成（SD 1.5，512×512/20步，WebSocket 精测，n=20）：**

| 指标                     | RTX 5060 8G | Tesla P100 16G           |
| ---------------------- | ----------- | ------------------------ |
| 单张中位耗时                 | **1.94 s**  | **7.90 s**               |
| 稳态吞吐                   | 27.9 张/分    | 7.5 张/分（温度墙后 ~6.4）       |
| 大分辨率 LoRA（768×512/25步） | —           | 17.7 s/张，3.6 张/分，100% 成功（**导风罩改造 + 空调环境后降至 16.29 s**） |

**LLM 推理（llama.cpp / P100，ornith-9B-Q8_0）：**

| 指标        | 实测值            | 备注                      |
| --------- | -------------- | ----------------------- |
| decode    | **28.7 tok/s** | 内存带宽硬顶                  |
| prefill   | **513 tok/s**  | ubatch 调优后（**3.2×**，见下） |
| TTFT（短对话） | 0.29 s         |                         |
| KV 前缀缓存命中 | TTFT 再省 ~80%   |                         |

**LoRA 训练（sd-scripts / P100，同数据集跨代对照）：**

| 项     | SD1.5 v4                 | SDXL v1                          |
| ----- | ------------------------ | -------------------------------- |
| 训练    | 1260 步 / 62 min          | 2000 步 / 88 min（2.63 s/it）       |
| 推理稳态  | 17.7 s（768×512/25步）      | ~25 s（640×448/25步）               |
| 评测    | 横屏分辨率匹配后 100% 成功        | **一次通过**（风格+毛色双收敛，坑 #54/#46 前置应用） |

**关键工程发现（全部实测定因）：**

| 发现                     | 数据                                                          |
| ---------------------- | ----------------------------------------------------------- |
| 温度墙量化                  | 79°C 拐点 / ~70s，频率-耗时比 1.155≈1.157；降速 13~30%，**单张工作量越大降速越狠** |
| `cudaMallocAsync` 影响   | **仅 RTX 5060 复现**（+2.6s/张 + 批量静默丢任务）；P100/A10 零影响——不是跨架构共性  |
| 测量方法偏差                 | 2s 轮询让 1.94s 任务 P95 虚高 76% → 基准必须 WebSocket                 |
| 容器化开销                  | **< 1%**（拆解热惯性/整卡口径/page cache 三个干扰源后的净值）                   |
| LLM ubatch 调优          | prefill 162→513 tok/s（**3.2×**），机理三向验证                      |
| Q4_K_M vs Q8_0（Pascal） | **Q4 反而慢 22%**（反量化开销 > 带宽收益，与社区常识相反）                        |
| SDXL VAE fp16 nan      | SDXL fp16 训练必踩天坑：latent **20/20 全 100% nan** → fp16-fix VAE 一招解决，训练/推理两侧同用 |
| 散热改造：两负一正            | 风扇在位 **无效**（79°C 依旧）；PL=170W **更差**（慢 19%）；**导风罩 + 空调环境才有效**（快 7.4~8.1%）。**改造必须复测** |

---

## 六次推翻自己的结论（本项目最珍贵的部分）

| #   | 原结论                                 | 证伪方式               | 最终真相                                                                  |
| --- | ----------------------------------- | ------------------ | --------------------------------------------------------------------- |
| 1   | "cudaMallocAsync 问题是跨架构共性"          | A10 + P100 对照实验    | 仅 RTX 5060/Windows/8G 复现；**一台机器的样本不能支撑普遍性断言**                         |
| 2   | "P100 尾部 10.2s 毛刺是物理现象"             | WebSocket 重测       | 轮询量化假象，毛刺从未存在；**先确认量具再相信读数**                                          |
| 3   | "容器化开销 +5.8%"                       | 拆热惯性/口径/page cache | 净开销 < 1%；**A/B 先排除干扰再看净差异**                                           |
| 4   | "LoRA 畸形根因是数据集多样性"                  | 换一致数据集重训仍失败        | 根因是**生成分辨率与训练构图不匹配**；绕了数据集/优化器/TE/精度一整圈，答案是最朴素的变量                     |
| 5   | "AdamW8bit 在 sm_60 上算错"             | 第 4 次修正的连带平反       | v1/v2 从未在正确画幅下被评测过，优化器一直无辜                                            |
| 6   | "Proxmox VE 透传拿不到 per-process 显存归属" | 用户质疑后复测            | `query-compute-apps` 可用（它只列 C 型进程，空表≠不支持）；**下"平台不支持"结论前，先确认被测对象当时存在** |

> 这些修正全部保留在文档原文中（划线保留 + 修正日期），不删改历史。**能区分证据与推理、能自我证伪，是我认为 Infra 工作最核心的素质。**

---

## 文档导航（[docs/](docs/)）

| 文档 | 内容 |
| --- | --- |
| **[09-自我修正与方法论](docs/09-自我修正与方法论.md)** | ⭐ **建议第一个看**。六次推翻自己结论的完整复盘 + 可复用检查清单 |
| [01-ComfyUI双机部署手册](docs/01-ComfyUI双机部署手册.md) | 版本基线、驱动锁定、静态 IP + SSH 隧道、systemd、硬件巡检 |
| [02-ComfyUI-API开发手册](docs/02-ComfyUI-API开发手册.md) | REST/WebSocket 客户端设计、批量生成、指标采集 |
| [03-容器化部署交付手册](docs/03-ComfyUI容器化部署交付手册.md) | 离线可复现镜像（字节级一致）、四维构建自检 |
| [04-GPU监控与告警方案](docs/04-GPU监控与告警方案.md) | 三层监控、实测阈值、"假设→监控验证"闭环 |
| [05-LoRA训练工程化实战手册](docs/05-LoRA训练工程化实战手册.md) | 训练全链路、评测方法论、四次误诊排错实录 |
| [06-LLM服务化部署手册](docs/06-LLM服务化部署手册.md) | P100 调优四参数、ubatch 3.2×、共存规则、网关 |
| [07-企业私有化AIGC平台方案](docs/07-企业私有化AIGC平台方案.md) | 选型/对外服务/TCO/合规/安全/路线图，全实测数据支撑 |
| [08-平台使用指南](docs/08-平台使用指南.md) | 用户视角：业务系统/开发者/运维三类角色的操作手册与示例 |
| [00-全指南合订本](docs/00-AIGC工程师落地实战指南-合订本.md) | 五阶段完整指南 + **54 个踩坑清单** + 附录 D.1~D.15 |

## 代码资产

| 目录 | 内容 |
| --- | --- |
| [comfy_client/](comfy_client/) | ⭐ 共享包：ComfyUI API 客户端**唯一实现**，scripts 与网关共用同一份 |
| [scripts/](scripts/) | 环境校验、WS 客户端、图像/LLM 基准脚本、**汇总重算脚本**、训练启动、一键部署/回滚 |
| [gateway/aigc-gateway/](gateway/aigc-gateway/) | FastAPI 统一网关：出图异步队列 + LLM 流式透传 + API Key + GPU 准入控制 + Prometheus 指标 + TTL 回收 |
| [docker/](docker/) | 双架构 Dockerfile、**docker-compose.yml**、监控配置（prometheus/alert_rules）、Grafana 面板 + provisioning |
| [systemd/](systemd/) | comfyui / aigc-gateway / llama-server 单元（自愈 + 防重启风暴）、llama.cpp 模型切换工具 |
| [workflows/](workflows/) | API 格式工作流（base + LoRA + SDXL LoRA），节点标题约定 |
| [benchmarks/](benchmarks/) | 实测数据与测量方法修正记录 + **[raw/](benchmarks/raw/) 25 轮原始数据** |
| [tests/](tests/) | 27 个单测：鉴权行为、工作流注入、模板不污染、**共享包单一来源**（禁止再把实现复制回消费方） |

---

## 快速体验

### 3 分钟验证（无需 GPU）

```bash
git clone https://github.com/yilufeiyang888999/AIGC_Practical_Record.git
cd AIGC_Practical_Record

# ① 一键重算 benchmarks 里的所有表格 —— 自己验一遍，别信我写的数字
python scripts/summarize_runs.py benchmarks/raw

# ② 跑单测（鉴权 / 工作流注入 / 防漂移）
python -m unittest discover -s tests
```

### 5 分钟跑通网关（纯 CPU 即可跑通 API 层）

```bash
cd gateway/aigc-gateway
pip install -r requirements.txt
export COMFY_HOST=http://<comfyui>:8188 LLM_HOST=http://<llama>:8000 AIGC_API_KEY=dev
uvicorn main:app --host 127.0.0.1 --port 8300
```

```bash
# ① 自描述（不需鉴权）
curl -s http://127.0.0.1:8300/ | python -m json.tool

# ② 鉴权生效验证（应为 401）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8300/v1/tasks/abc
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8300/files/abc/a.png

# ③ 带 key 提交出图（异步，返回 task_id）
curl -s -X POST http://127.0.0.1:8300/v1/images/generations \
  -H "x-api-key: dev" -H "Content-Type: application/json" \
  -d '{"prompt":"shiba inu, 1dog, solo, full body","steps":25,"width":768,"height":512,
       "lora":"myshiba_v4.safetensors","lora_strength":0.8}' | python -m json.tool

# ④ 轮询任务（用上一步返回的 task_id）
curl -s -H "x-api-key: dev" http://127.0.0.1:8300/v1/tasks/<task_id> | python -m json.tool

# ⑤ LLM（OpenAI 兼容，可流式）
curl -s -X POST http://127.0.0.1:8300/v1/chat/completions \
  -H "x-api-key: dev" -H "Content-Type: application/json" \
  -d '{"model":"local","messages":[{"role":"user","content":"用一句话介绍杭州"}],"max_tokens":128}'

# ⑥ Prometheus 指标
curl -s http://127.0.0.1:8300/metrics | grep ^aigc_
```

完整部署路径见 [docs/01 双机部署手册](docs/01-ComfyUI双机部署手册.md) 与 docs/00 合订本的五阶段指南；每份手册均可独立按章执行。

### 算力端一键部署

```bash
cd docker && cp .env.example .env && chmod 600 .env
../scripts/docker_deploy.sh check     # 前置检查（GPU 直通 / 端口冲突 / compose 语法）
../scripts/docker_deploy.sh up        # 部署
../scripts/docker_deploy.sh status    # 四服务健康 + 监控链路
```

---

## 关于作者

10+ 年 IT 运维工程师（基础设施/监控/自动化），2026 年系统学习 AIGC 工程的完整实战记录：从双机部署到企业方案，覆盖 **GPU 运维、CUDA 版本治理、容器化交付、Prometheus/Grafana 可观测性、任务队列与 API 网关、LoRA 训练工程化、私有化部署方案、容量与成本测算**。

### 可直接粘贴进简历的项目描述（STAR）

> **私有化 AIGC 平台建设（独立完成）**
>
> **S**　转型 AIGC 工程，需在两台自有机器（RTX 5060 8G / Tesla P100 16G）上从零搭建可用的私有化生成平台。
>
> **T**　完成部署、API 自动化、容器化、GPU 可观测性、LoRA 训练、LLM 服务化、统一网关全链路，且**所有性能数字必须实测、可复现**。
>
> **A**　① 治理 Blackwell(sm_120) 与 Pascal(sm_60) 的 CUDA 版本分裂，锁定驱动分支并脚本化校验；
> ② 自建 WebSocket 精测基准，**发现并修正 2s 轮询造成的 P95 虚高 76%**；
> ③ 搭建 dcgm-exporter + Prometheus + Grafana 三层监控，**用监控数据实锤温度墙假设**（频率-耗时比 1.155 ≈ 1.157）；
> ④ 构建 FastAPI 统一网关（鉴权 / 异步队列 / GPU 准入控制 / Prometheus 埋点），并补齐 TTL 回收；
> ⑤ 完成 SD1.5 与 SDXL LoRA 训练全链路，用三步对照实验定位"生成分辨率不匹配训练构图"的真根因。
>
> **R**　交付 9 份专题手册 + 一键部署脚本 + 可复现镜像 + 监控规则 + 网关源码（25 个单测）；
> **18 轮图像基准（340 样本）+ 7 轮 LLM 基准全部原始数据入库可自验**；
> **六次推翻自己的错误结论并全部记档**，沉淀为可复用的工程方法论。

**三个面试杀手锏：**

1. **版本治理** —— 能讲清 Blackwell 与 Pascal 的 CUDA 支持差异、驱动分支锁定策略，这是真实踩过坑才有的知识
2. **可观测性** —— 能拿出 GPU 利用率/显存/温度的 Grafana 面板和**按实测拐点标定**的告警规则
3. **量化数据 + 自我证伪** —— 所有数字实测且原始数据可自验；能讲清自己错在哪里、怎么发现的

---

## 许可

MIT（见 [LICENSE](LICENSE)）。文档与代码可自由使用，转载请注明出处。

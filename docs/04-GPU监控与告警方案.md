# GPU 监控与告警方案

> **文档用途**：AIGC 双机平台的 GPU 可观测性体系设计文档。覆盖监控架构、指标选型、告警阈值、平台限制与实测验证。
> **适用对象**：GPU 运维 / AI Infra / MLOps 工程师，以及需要复用本方案的交付方。
> **版本**：V1.0（2026-09-15，对应 dcgm-exporter + Prometheus + Grafana 已上线环境）

---

## 1. 概述与设计原则

本方案为 AIGC 推理/训练平台建立 GPU 可观测性闭环。设计遵循三条来自实测的原则：

| 原则 | 来源 |
| --- | --- |
| **① 采样间隔必须与被测对象的时间尺度匹配** | 阶段二实测：2 秒轮询让 1.94 秒任务的 P95 虚高 76%。GPU 温度/频率是秒级变化，抓取间隔必须 ≤ 5 秒 |
| **② 阈值必须用实测拐点，不能用理想值** | 温度阈值写 80°C 几乎不触发（实际拐点 79°C 且震荡）；频率阈值写 1100 会反复穿越导致告警抖动 |
| **③ 监控要能回答"为什么慢"，不只是"有多慢"** | 阶段二观察到 13% 降速，只有叠加温度 + 频率两条曲线才能定因是温度墙。单看耗时曲线无法归因 |

**核心认知**：监控的价值不是"画一张好看的面板"，而是**把性能假设变成可验证的结论**。

---

## 2. 监控架构

```
┌─────────────────────────────────────────────────────────┐
│  Ubuntu 算力节点（P100）                                  │
│                                                          │
│  ┌──────────────┐   ┌─────────────┐   ┌──────────────┐  │
│  │ dcgm-exporter│──▶│ Prometheus  │──▶│   Grafana    │  │
│  │  :9400       │   │  :9090      │   │   :3000      │  │
│  └──────────────┘   └─────────────┘   └──────────────┘  │
│       GPU 指标     ▲      5s 抓取 + 告警评估    面板 + 告警  │
│                    │                                      │
│  ┌──────────────┐  │   ┌──────────────┐                  │
│  │ llama-server │──┘   │ aigc-gateway │（待部署 Ubuntu    │
│  │ :8000/metrics│      │ :8300/metrics│  后接入，见 §8）  │
│  └──────────────┘      └──────────────┘                  │
│   应用层指标（09-20 接入）：容器→宿主机走                   │
│   host.docker.internal（compose 需 extra_hosts: host-gateway）│
│                                                          │
│  ┌──────────────┐                                        │
│  │  ComfyUI     │  （任务级指标：经网关 aigc_* 上报）      │
│  └──────────────┘                                        │
└─────────────────────────────────────────────────────────┘
              ▲
              │ SSH 隧道（127.0.0.1 绑定，不对外暴露）
              │
       Windows 工作站（浏览器访问 Grafana / Prometheus）
```

| 组件 | 版本 | 职责 |
| --- | --- | --- |
| dcgm-exporter | 3.3.5-3.4.0 | 采集 GPU 硬件指标，暴露 `/metrics` |
| Prometheus | v2.53.0 | 5 秒抓取 + 告警规则评估 + 30 天存储 |
| Grafana | 11.1.0 | 面板可视化 + 告警展示 |
| nvidia-smi 命令行 | 535.309.01 | 交叉验证 + 原始数据留档（CSV） |

**安全**：全部只绑 `127.0.0.1`，外部访问走 SSH 隧道。

---

## 3. 指标选型（P100 实测可用清单）

dcgm-exporter 启动时会提示：

```
Not collecting DCP metrics: This request is serviced by a module of DCGM
that is not currently loaded. Falling back to default-counters.csv
```

**DCP（profiling）指标需要 Volta+，P100（Pascal）拿不到，自动回退到基础计数器集。** 实测可用 **16 个指标**。

### 3.1 核心指标（面板 + 告警都用）

| 指标 | 空闲实测值 | 用途 | 告警 |
| --- | --- | --- | --- |
| `DCGM_FI_DEV_SM_CLOCK` | 1189 MHz | ⭐ 降频检测（boost 上限 1329） | ✅ |
| `DCGM_FI_DEV_GPU_TEMP` | 47 °C | ⭐ 温度墙检测（拐点 79°C） | ✅ |
| `DCGM_FI_DEV_POWER_USAGE` | 35.6 W | 功耗墙检测（上限 250 W） | ✅ |
| `DCGM_FI_DEV_GPU_UTIL` | 0 % | 负载观测 | — |
| `DCGM_FI_DEV_FB_USED` / `FB_FREE` | 260 / 16015 MB | 显存水位 | ✅ |

### 3.2 辅助指标（面板用）

| 指标 | 用途 |
| --- | --- |
| `DCGM_FI_DEV_MEM_CLOCK` | HBM 频率 |
| `DCGM_FI_DEV_MEMORY_TEMP` | HBM2 温度（P100 独有） |
| `DCGM_FI_DEV_MEM_COPY_UTIL` | 显存带宽占用 |
| `DCGM_FI_DEV_PCIE_REPLAY_COUNTER` | PCIe 链路质量（透传环境重点） |
| `DCGM_FI_DEV_XID_ERRORS` | GPU 硬件/驱动错误计数 |

### 3.3 在 P100 上无效的指标（不要画面板）

| 指标 | 为什么无效 |
| --- | --- |
| `DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL` | PCIe 版无 NVLink |
| `DCGM_FI_DEV_VGPU_LICENSE_STATUS` | 非 vGPU 场景 |
| `DCGM_FI_DEV_ENC_UTIL` / `DEC_UTIL` | P100 是计算卡，无 NVENC |
| 所有 `DCGM_FI_PROF_*` | DCP 需 Volta+，Pascal 拿不到 |

> **官方 DCGM 面板（ID 12239）在 P100 上半数图为空**——它是给 A100/H100 设计的，大量依赖 DCP 指标。本方案**基于实测可用清单自定义面板**，不照抄。

---

## 4. 面板设计

### 4.1 设计原则：基线 + 阈值线

每条曲线必须同时回答两个问题：**正常值是多少？超过多少算异常？**

这是阶段二的核心教训：**恒定开销会伪装成系统稳定**。只看趋势线会漏掉"稳定但偏高"的问题，必须有基线参照。

### 4.2 面板布局（6 个时序面板）

| 面板 | 指标 | 单位 / 阈值线 | 读图要点 |
| --- | --- | --- | --- |
| GPU 温度 | `GPU_TEMP` + `MEMORY_TEMP` | °C，黄线 80 / 红线 85 | 满载爬升速度 → 散热能力 |
| SM 频率 | `SM_CLOCK` | MHz，上限标注 1329 | 满载掉到 1100 以下 = 降频 |
| 利用率 | `GPU_UTIL` + `MEM_COPY_UTIL` | % | 利用率 99% 但频率掉 = 被压着跑 |
| 功耗 | `POWER_USAGE` | W，黄线 220 / 红线 245 | 贴上限 = 功耗墙 |
| 显存占用 | `FB_USED` | MB，上限 16384 | 水位 + 泄漏检测 |
| 健康指标 | `XID_ERRORS` + `PCIE_REPLAY` 增量 | 计数 | **出现非零即告警** |

面板通过 JSON 导入（`docker/grafana/p100-live-dashboard.json`），数据源 uid `prometheus`，刷新 5 秒。

> **读图的协同关系**：判断"为什么慢"要三条曲线一起看——
> - **温度高 + 频率掉 + 利用率高** = 温度墙（散热压不住）
> - **功耗贴上限 + 频率掉** = 功耗墙（供电不足）
> - **三项都稳但耗时仍变长** = 不是硬件墙，查虚拟化/调度

---

## 5. 告警规则

阈值**全部来自实测**（阶段二 WS 精测 + 阶段三闭环实验），不是拍脑袋。

### 5.1 规则清单

```yaml
groups:
  - name: gpu_p100
    rules:
      # 实测降频拐点 79°C，不是理想的 80°C。for 1m 防抖。
      - alert: P100HighTemp
        expr: DCGM_FI_DEV_GPU_TEMP > 79
        for: 1m
        labels: { severity: warning }
        annotations:
          summary: "P100 温度 {{ $value }}°C 超过降频拐点，散热可能不足"

      # 实测降频后频率在 999~1200 波动，阈值取 1200。for 3m 防抖。
      - alert: P100ClockThrottle
        expr: DCGM_FI_DEV_SM_CLOCK < 1200 and DCGM_FI_DEV_GPU_UTIL > 80
        for: 3m
        labels: { severity: warning }
        annotations:
          summary: "P100 满载但 SM 频率仅 {{ $value }} MHz，已降频"

      - alert: GPUMemoryHigh
        expr: DCGM_FI_DEV_FB_USED / (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE) > 0.85
        for: 5m
        labels: { severity: warning }

      # XID 是硬件/驱动级错误，任何非零增长都要立刻看
      - alert: GPUXidError
        expr: increase(DCGM_FI_DEV_XID_ERRORS[5m]) > 0
        labels: { severity: critical }

      # 透传环境下 PCIe 链路质量
      - alert: PCIeReplayHigh
        expr: increase(DCGM_FI_DEV_PCIE_REPLAY_COUNTER[10m]) > 100
        labels: { severity: warning }

      - alert: DCGMExporterDown
        expr: up{job="dcgm"} == 0
        for: 1m
        labels: { severity: critical }
```

### 5.2 阈值确定的依据（关键）

| 阈值 | 理想值 | 实测修正值 | 依据 |
| --- | --- | --- | --- |
| 高温 | 80 °C | **79 °C** | 实测降频拐点，且温度在 78~80 震荡，80 会漏报 |
| 降频 | 1100 MHz | **1200 MHz** | 降频后频率 999~1200 波动，1100 会反复穿越导致告警抖动 |
| 防抖 | — | `for: 1m/3m` | 拐点附近震荡，防抖避免告警风暴 |

> **为什么很多监控告警天天吵但没人看**：阈值是拍脑袋的，和真实行为对不上。**告警的第一要务是"该响的时候响"，第二才是"不该响的时候不响"。**

---

## 6. 平台限制（必须在方案中写清）

### 6.1 Proxmox 透传环境的能力边界

> ⚠️ **本节已于 2026-09-18 修正。** 原结论"透传下拿不到 per-process 显存归属"**是错的**，被阶段五实测证伪（本项目第 6 次用数据推翻自己的结论，修正记录如下）。

本方案的 Ubuntu 是 **Proxmox VE 虚拟机，P100 通过 PCIe 原始透传（hostpci）**。实测能力边界：

| 能力 | 能否拿到 | 说明 |
| --- | --- | --- |
| 利用率 / 显存 / 温度 / 功耗 / 频率 | ✅ | dcgm 全量可用 |
| **per-process 显存归属（nvidia-smi）** | ✅ **可用**（09-18 实测） | `query-compute-apps` 正常列出 PID / 进程路径 / 显存 |
| per-process **时间序列**（Grafana） | ❌ | dcgm 在 P100 上无 per-process 指标（DCP 需 Volta+），`FB_USED` 仍是整卡口径 |
| GPU Reset 状态查询 | ✅ | `nvidia-smi -q` 正常返回 `Reset Required: No` |

**原结论错在哪**：阶段三测试时 `query-compute-apps` 返回空表，误判为"虚拟化层禁用进程枚举"。已无法回溯确证当时的环境，最可能的原因是该命令**只列出 compute（C 型）进程**——若测试时刻 GPU 上只有 Xorg（G 型）而没有计算进程，输出就只有表头，看起来像"不支持"。09-18 在 llama-server（11236 MiB）与 venv python（254 MiB）同时占卡时实测，归属信息完整：

```bash
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
# pid, process_name, used_gpu_memory [MiB]
# 1914, ./venv/bin/python, 254 MiB
# 30375, /opt/llama.cpp/build/bin/llama-server, 11236 MiB
```

**修正后的结论**：时间序列面板（`FB_USED`）仍是整卡口径，**显存告警阈值仍按总和设**——这部分设计不变。但**归属排查可以做了**：显存异常时用 `nvidia-smi --query-compute-apps=...` 或 `fuser -v /dev/nvidia*` 现场定位是哪个实例。

> **meta 教训**：一条命令返回空，可能是"功能被禁用"，也可能只是"此刻没有符合条件的对象"。**下"平台不支持"这种结论前，必须先确认测试时刻被测对象真实存在。**

### 6.2 DCGM DCP 缺失

P100（Pascal）拿不到 DCP profiling 指标（SM 占用率、Tensor Core 活跃度等）。**dcgm 在这台机器上相对 nvidia-smi 的增量价值被架构限制吃掉了**，但 dcgm-exporter 在方案文档和企业环境一致性上更有价值。

### 6.3 无 MIG

P100 无 MIG，GPU 只能整卡分配，无法做多租户显存隔离。多实例靠"应用层排队 + 总量监控"管理。

### 6.4 抓取间隔 vs 任务粒度的硬约束

| 被测对象 | 时间尺度 | 5 秒抓取能否覆盖 |
| --- | --- | --- |
| GPU 温度/频率变化 | 秒级 | ✅ |
| P100 单张出图 | 7.9 s | ⚠️ 勉强（1~2 个采样点） |
| 5060 单张出图 | 1.9 s | ❌ **采不到** |

**任务级指标（单张耗时、吞吐、成功率）必须应用侧主动上报，不能靠外部抓取。** 见 §8 扩展路线。

---

## 7. 闭环案例：温度墙验证（本方案的核心价值证明）

这是本监控体系上线后完成的第一个"假设 → 验证"闭环。

### 7.1 假设（阶段二）

批量出图时观察到：前 10 张均值 7.4s，后 10 张均值 8.4s，**渐进降速约 13%**。推测是温度墙，但当时没有监控数据，只是推测。

### 7.2 验证（阶段三，监控上线后）

跑 20 张批量，同步采集 `nvidia-smi` 曲线：

```
12:26:50 ~ 12:28:00（约 70 秒）：SM 频率稳定 1328 MHz
                                 温度 50°C → 79°C，功耗 ~180~230 W
12:28:00 起：                    温度钉死 79~80°C
                                 频率掉：1303 → 1050 → 999 MHz
                                 功耗掉到 ~110~170 W
                                 GPU 利用率全程 99~100%
```

### 7.3 定量吻合

| 阶段 | SM 频率 | 单张耗时 | 比值 |
| --- | --- | --- | --- |
| 前段（任务 2~10） | 1328 MHz | 7.33 s | 基准 |
| 后段（任务 11~20） | ~1150 MHz | 8.48 s | |
| **比值** | **1.155×** | **1.157×** | **≈ 1:1** |

**频率下降 15.5%，耗时增加 15.7%，严丝合缝。** 排除功耗墙（峰值 239 W 未贴 250 W 上限，且降频时功耗下降）。**温度墙实锤。**

### 7.4 结论的工程价值

| 应用 | 说明 |
| --- | --- |
| 告警阈值修正 | 温度 80→79、频率 1100→1200，用实测拐点（§5.2） |
| 散热改造量化 | 全程 1328 MHz 可提吞吐 **+15%**；~~一把暴力扇即可挣回~~ **09-21 证伪**（benchmarks §3.3）：风扇在位温度墙依旧、功率墙 170W 两头不讨好（中位 21.12 vs 17.73s），有效路径是导风罩 + 高静压风扇直压鳍片 |
| 阶段四训练预估 | LoRA 训练连续满载几十分钟，降频会让时长 +15%，散热改造优先级高 |

> **这个故事的工程价值**：不是"我搭了个监控"，而是"我先观察到性能衰减，提出假设，然后搭监控来定因，最后用频率-耗时 1:1 的定量关系实锤了温度墙"。这是把推测升级为结论的完整范式。

---

## 8. 扩展路线

### 8.1 任务级指标（应用侧上报，补抓取间隔的短板）

Prometheus 5 秒抓取采不到 5060 的 1.94 秒单张任务。需要应用侧主动暴露指标：

- **llama-server 侧**（✅ 09-20 已接入）：`--metrics` 暴露 token 计数/耗时，job `llama-server`。容器抓宿主机走 `host.docker.internal`，compose 需配 `extra_hosts: ["host.docker.internal:host-gateway"]`
  - ⚠️ 指标名带**冒号**前缀（`llamacpp:tokens_predicted_total`），不是常见的下划线——grep/查询时按 `llamacpp:` 匹配
  - 关键指标：`rate(llamacpp:tokens_predicted_total[1m])` = decode tok/s、`rate(llamacpp:prompt_tokens_total[1m])` = prefill tok/s、`llamacpp:requests_processing` / `requests_deferred` = 在途/排队请求
  - 计数器在**重启后清零**，且空闲时全为 0——验证时要先跑负载（09-20 实测：llm_bench 后 rate 与基准值 28.7 tok/s 互证）
  - ⚠️ **rate 是系统吞吐，不是引擎能力**（09-20 实测）：单请求 decode 28.7 tok/s（服务端 timings 口径），rate[1m] 还要乘占空比（decode 9s / 周期 10.5s ≈ 24）；窗口压在请求间隙上时会读出 4.7 这类低值。**容量规划用 rate，引擎调优用 timings 口径，混用会误判**
- **FastAPI 网关侧**（待网关部署 Ubuntu 后接入）：`aigc_*` 指标已实现（队列深度、出图耗时、LLM TTFT），见 06 手册 §7。网关暂在 Windows 开发机，迁入 Ubuntu 后 localhost 直连
- **批量脚本侧**：`comfy_batch_gen.py` / `llm_bench.py` 输出结构化 JSON/CSV 指标，作为离线基准数据源

### 8.2 双机统一视图

当前只有 P100 节点接入。5060（Windows）侧可用 `nvidia_gpu_exporter`（基于 nvidia-smi，Windows 兼容性更好）补充，纳入同一 Prometheus，用 `node` 标签区分。

### 8.3 告警通知

当前告警只在 Prometheus 评估，未接通知渠道。可接 Alertmanager → 邮件/Webhook/钉钉。家用环境优先级低，方案设计先行。

---

## 9. 容量规划输入

本监控体系直接产出容量规划的关键数据：

| 数据 | 来源 | 用途 |
| --- | --- | --- |
| P100 稳态吞吐 7.49 张/分 | benchmark + 监控 | 单实例容量上限 |
| 降频损失 13~15% | 闭环实验 | 散热改造 ROI |
| 显存水位 ~2.5 GB（SD1.5） | FB_USED | 模型/分辨率选择的显存预算 |
| 功耗峰值 ~239 W | POWER_USAGE | 电源/散热选型 |
| 批量调度开销 < 1% | benchmark | 确认瓶颈在 GPU，提吞吐只能加卡/加实例 |

**结论**：当前单 P100 节点的 SD 1.5 推理吞吐上限约 **7.5 张/分（考虑降频后 ~6.4 张/分）**。要提升只有三条路——改善散热（+15%）、加卡、加实例。

---

## 10. 附录：配置文件位置

| 文件 | 用途 |
| --- | --- |
| `docker/monitoring/prometheus.yml` | 抓取配置（5s 间隔） |
| `docker/monitoring/alert_rules.yml` | 告警规则（实测阈值） |
| `docker/grafana/provisioning/datasources/prometheus.yml` | 数据源（uid: prometheus） |
| `docker/grafana/p100-live-dashboard.json` | 6 面板实时观测面板 |
| `docker/docker-compose.yml` | dcgm + Prometheus + Grafana 编排 |

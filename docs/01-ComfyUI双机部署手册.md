# ComfyUI 双机部署手册

> **本手册是整套方案的地基。** 跳过它，后面每一步都可能在第 3 周才暴露出无法挽回的环境问题。
> 对应《合订本》第 0 章 + 阶段一，这里抽出为独立手册，便于单独执行与交付。
>
> 实测基线：Windows 11 / RTX 5060 8G（Blackwell sm_120） + Ubuntu 22.04 / Tesla P100 16G（Pascal sm_60，Proxmox 透传 VM）

---

## 1. 为什么是双机，以及为什么不能统一软件栈

两台机器的 GPU 正好处在 NVIDIA 生态的**两端**：一端是 2025 年的 Blackwell，一端是 2016 年的 Pascal。
**不存在任何一份 PyTorch wheel 能同时支持两者**：

| | RTX 5060 | Tesla P100 |
| --- | --- | --- |
| 架构 / 算力 | Blackwell **sm_120** | Pascal **sm_60** |
| 需要的 CUDA | **≥ 12.8**（cu121 里没有 sm_120 内核） | **≤ 12.4**（cu128 自 PyTorch 2.8 起移除 sm_60） |

> **一个会骗人的信号**：`torch.cuda.is_available()` 返回 `True` **不等于**这张卡能用。
> 它只说明驱动和运行时能对话，不说明 wheel 里编译了你这张卡的内核。
> 装上 cu121 的 5060 会在**第一次真正跑 kernel** 时才报 `no kernel image is available`。
> 所以 §4 的验证脚本必须真跑一次矩阵运算，不能只看 `is_available()`。

### 1.1 版本基线表（V1.5 实战版，两张卡完全不同）

| 组件 | Windows（RTX 5060） | Ubuntu（P100） |
| --- | --- | --- |
| 操作系统 | Windows 11 | **Ubuntu 22.04 LTS** |
| NVIDIA 驱动 | ≥ 572.x | **535.309.01（535 分支锁死）** |
| Python | 3.11+（实测 3.14.6 OK） | 3.10.x（jammy 默认） |
| PyTorch | **2.11.0+cu128** | **2.6.0+cu124（cuDNN 禁用）** |
| 安装索引 | `.../whl/cu128` | `.../whl/cu124` |
| 注意力实现 | xformers / SDPA 均可 | **禁用 xformers**（`--disable-xformers`） |
| 精度 | fp16 / bf16 / fp8 可选 | **只能 fp16**（无 bf16、无 FP8） |
| cuDNN | 正常 | **禁用**（sm_60 + cuDNN 9 抛 `CUDNN_STATUS_NOT_INITIALIZED`） |
| 显存分配器 | **native**（`--disable-cuda-malloc`） | **native** |
| ComfyUI commit | **22e40d2a**（双机统一） | **22e40d2a**（双机统一） |

**P100 侧三个"必须"的由来：**

1. **cuDNN 必须禁用** —— 9.x 在 sm_60 上初始化失败。修复方式是在 `main.py` 里加
   `torch.backends.cudnn.enabled = False`，PyTorch 原生 CUDA 卷积正常，仅损失 10~20% 卷积性能。
2. **必须显式 fp16** —— Pascal 无 Tensor Core，**不支持 bf16，也不支持 FP8**。任何 bf16 报错一律改 fp16。
3. **必须禁用 xformers / flash-attn** —— 无 sm_60 内核，用 `--sdpa` 代替。

### 1.2 能力边界（先说清楚不能做什么）

| 任务 | RTX 5060 8G | P100 16G |
| --- | --- | --- |
| SD 1.5 推理 / 训练 | 可以 | 可以 |
| SDXL 推理 | 勉强（`--lowvram`） | 可以 |
| SDXL LoRA 训练 | 不可行 | 可以（grad ckpt + fp16 + batch 1） |
| **FLUX 系列** | 不可行 | **基本不可行**（fp16 需 ~24G，且无 fp8 可压缩） |
| 7B LLM 推理（Q4） | 勉强（部分 offload） | 可以（llama.cpp） |
| **vLLM** | 可以 | **不可用**（要求 CC ≥ 7.0） |

> **主动声明能力边界比吹嘘能力更可信。** 这套硬件的目标是把 SD 1.5 / SDXL 的**工程链路**跑通并做出
> 可交付成果，不是追 FLUX。落地能力（能搭建、能排错、能交付）比硬件规格本身更决定成败。

---

## 2. Ubuntu 算力端：驱动锁定（P100 的生命线）

> **V1.5 最终结论：535 是 P100 的终局驱动。**
> 555/560/570/580 虽然 `nvidia-smi` 仍列出 P100，但 **CUDA runtime 已移除 sm_60 路径**，
> 任何 AI 推理全部失败。不需要再观望 580 复活。

```bash
# 0. 全新 Ubuntu 22.04 默认源有 535，加 PPA 确保最新
sudo add-apt-repository ppa:graphics-drivers/ppa -y
sudo apt update

# 1. 查看推荐驱动
sudo ubuntu-drivers devices

# 2. 安装 535 分支（⚠️ 不要直接 apt upgrade，会把驱动升到 590+，P100 直接废掉）
sudo apt install nvidia-driver-535 -y

# 3. 锁定，禁止被后续 apt 升级 / 替换
sudo apt-mark hold nvidia-driver-535
sudo apt-mark showhold          # 必须能看到 nvidia-driver-535

# 4. 重启并验证
sudo reboot
nvidia-smi                      # Driver Version: 535.309.x，且 P100 被列出
```

> **后续任何 `apt upgrade` 之前，先 `apt-mark showhold` 确认锁定仍在。**
> 这条本身就是一项运维能力，值得写进方案文档。

**装错驱动回退：**

```bash
sudo apt remove --purge nvidia-driver-580
sudo apt install nvidia-driver-535
sudo apt-mark hold nvidia-driver-535
sudo reboot
```

**四维对应表：**

| 驱动分支 | CUDA 上限 | PyTorch cu | PyTorch 版本 | P100 可用？ |
| --- | --- | --- | --- | --- |
| **535（锁死）** | 12.2 | **cu124** | **2.6.0（cuDNN 禁用）** | ✅ |
| 535 | 12.2 | cu121 | 2.4.1 | ⚠️ transformers 会禁用 PyTorch 后端 |
| 555+ | 12.5+ | cu124/cu128 | 2.7+ | ❌ sm_60 runtime 已废 |
| 580 | 13.0 | cu130 | 2.11+ | ❌ |

---

## 3. 网络：静态 IP + SSH 隧道（不要裸奔）

### 3.1 静态 IP（Ubuntu 端）

> **为什么必须先做**：全文的脚本都用 IP 访问算力端。DHCP 一变，第二天所有脚本全断。

```yaml
# /etc/netplan/01-aigc-static.yaml
network:
  version: 2
  ethernets:
    ens18:                       # 用 ip a 确认实际网卡名
      addresses: [192.168.1.50/24]
      routes:
        - to: default
          via: 192.168.1.1
      nameservers:
        addresses: [192.168.1.1, 223.5.5.5]
```

```bash
sudo netplan apply
ip a | grep ens18
```

### 3.2 SSH 隧道（Windows 端）

**所有服务只绑 `127.0.0.1`，外部访问一律走隧道。** ComfyUI 与 Grafana **都没有鉴权**，
`--listen 0.0.0.0` 等于把出图算力开放给整个局域网；Gradio 还有 RCE 历史漏洞。

```bash
# Windows Git Bash —— 一条命令映射四个端口
ssh -N -L 8288:127.0.0.1:8188 \
       -L 8289:127.0.0.1:8189 \
       -L 8300:127.0.0.1:8300 \
       -L 3000:127.0.0.1:3000 \
       ivan@192.168.1.50
```

或写入 `~/.ssh/config` 后 `ssh aigc-tunnel` 即可：

```
Host aigc-tunnel
    HostName 192.168.1.50
    User ivan
    LocalForward 8288 127.0.0.1:8188
    LocalForward 8289 127.0.0.1:8189
    LocalForward 8300 127.0.0.1:8300
    LocalForward 3000 127.0.0.1:3000
    ServerAliveInterval 30
```

> 之后在 Windows 上访问 `http://127.0.0.1:8288` 就是算力端的 ComfyUI。
> 这也是为什么 benchmarks 里 P100 的 `comfy_host` 是 `8288` / `8289` —— **走的是隧道端口**。

---

## 4. 环境验证（两台机器都必须跑）

`scripts/verify_env.py` —— 核心是**真跑一次 kernel**，并检查架构列表里有没有本机算力：

```bash
python scripts/verify_env.py
```

关键检查项：

```python
import torch
print(torch.__version__, torch.version.cuda)
print(torch.cuda.get_device_capability(0))   # 5060 应 (12, 0)；P100 应 (6, 0)
print(torch.cuda.get_arch_list())            # 5060 要含 sm_120；P100 要含 sm_60

# 真正跑一次矩阵运算 —— is_available() 会骗人，这个不会
x = torch.randn(2000, 2000, device="cuda")
y = x @ x
torch.cuda.synchronize()
print("kernel 执行 OK")
```

> **P100 侧还要额外验证 bitsandbytes**（社区对它的 sm_60 兼容性说法不一）。
> "听说能用"不算数，最小验证脚本跑过才算数（见 05 手册 §2.1）。

---

## 5. ComfyUI 部署与 systemd 托管

### 5.1 目录规划（Windows 侧常见坑）

模型必须放在 **ComfyUI 自己的 `models/` 目录树**下，否则扫描不到：

```
ComfyUI/
└─ models/
   ├─ checkpoints/     # 底模
   ├─ loras/
   ├─ vae/
   ├─ clip/
   ├─ unet/            # SDXL 需要
   ├─ text_encoders/   # SDXL 需要
   └─ controlnet/
```

> 若模型放在别处（如 `D:\AIGC\model_cache\`），**必须配 `extra_model_paths.yaml`**，
> 否则 ComfyUI 一定找不到模型，且报错信息不会直接指向这个原因。

### 5.2 源码版本必须 pin

```bash
# ❌ 不要这样——10 周后按文档重建会得到完全不同的环境
git clone https://github.com/comfyanonymous/ComfyUI.git

# ✅ 锁定到实测过的 commit
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI && git checkout 22e40d2a
git rev-parse HEAD          # 22e40d2ace0f53da025b3a41cbe4b664ef807097
```

**双机 commit 一致性已验证**：相同 seed + 相同模型 + 相同参数，两端出图结果一致。

### 5.3 启动参数（两张卡不同，别抄错）

| 参数 | P100 | RTX 5060 |
| --- | --- | --- |
| `--disable-xformers` | **必须** | 不需要 |
| `--disable-cuda-malloc` | 可选（实测零影响，为环境一致保留） | **必须**（否则 +2.6s/张 + 批量静默丢任务） |
| cuDNN 补丁（main.py） | **必须** | 不需要 |

> **`--disable-cuda-malloc` 是个典型陷阱**：它在 5060 上是救命参数，在 P100 / A10 上**完全无差异**。
> 不要不加区分地推广——详见 benchmarks §3.1 的三机对照矩阵。

### 5.4 systemd 托管（P100 侧）

用 systemd，**不要用 tmux 当生产服务**。tmux 里的进程没自愈、没重启策略、没日志轮转，
机器重启就消失。

```ini
# /etc/systemd/system/comfyui.service   （完整文件见 systemd/comfyui.service）
[Service]
Environment="PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True"
ExecStart=/opt/AIGC/ComfyUI-server/venv/bin/python main.py \
          --listen 127.0.0.1 --port 8188 \
          --disable-xformers --disable-cuda-malloc
Restart=on-failure
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=5        # 防重启风暴
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now comfyui
sudo systemctl status comfyui
journalctl -u comfyui -f
```

`StartLimitBurst=5` 是防重启风暴的关键——**没有它，一个持续失败的启动会把机器拖进 5 秒一次的重启循环**。

---

## 6. 硬件巡检（部署前必做，尤其二手卡）

| 项 | 命令 | 关注点 |
| --- | --- | --- |
| 功耗版本 | `nvidia-smi -q -d POWER` | PCIe 250W / SXM2 300W，**家用通常是 PCIe** |
| 散热形式 | 目视 + `nvidia-smi -q -d TEMPERATURE` | **P100 PCIe 多为被动散热**，为机架风道设计，普通机箱会撞温度墙 |
| 空闲温度 | `nvidia-smi` | 实测平台空闲 46~57°C，空调环境差异巨大 |
| 供电 | 检查 PCIe 供电线 | 300W 卡需要双 8pin |
| ECC | `nvidia-smi -q -d ECC` | 数据中心卡看错误计数 |

> **P100 温度墙是本平台的硬约束，已充分量化**：79°C 拐点、~70s 撞线、降速 13~30%。
> 告警阈值、容量公式、训练时长税均已按此标定，详见 04 手册 §7 与 benchmarks §3.3。

---

## 7. 部署完成自检清单

- [ ] 两台机器 `verify_env.py` 全绿（**含真跑 kernel**）
- [ ] `torch.cuda.get_arch_list()` 含本机架构（sm_120 / sm_60）
- [ ] Ubuntu 驱动 `apt-mark showhold` 能看到 `nvidia-driver-535`
- [ ] 静态 IP 已生效，SSH 隧道可连通
- [ ] ComfyUI 只绑 `127.0.0.1`（**没有** `--listen 0.0.0.0`）
- [ ] ComfyUI commit 双机一致（`22e40d2a`）
- [ ] systemd 自愈验证：`kill -9` 主进程后能自动拉起
- [ ] 模型放置正确 / `extra_model_paths.yaml` 已配（若模型不在默认目录）
- [ ] 双机同 seed 同参数出图结果一致

---

## 8. 部署踩坑速查

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `no kernel image is available for execution` | wheel 不含本机架构 | 换对应 cu 版本的 wheel（5060→cu128，P100→cu124） |
| `CUDNN_STATUS_NOT_INITIALIZED` | cuDNN 9.x 在 sm_60 上不可用 | main.py 加 `torch.backends.cudnn.enabled = False` |
| `bf16` 相关报错 | Pascal 无 bf16 | 全部改 `fp16` |
| xformers 加载失败 | 无 sm_60 内核 | `--disable-xformers` + `--sdpa` |
| ComfyUI 找不到模型 | 模型不在 `models/` 树 / 未配 extra_model_paths | 移到对应子目录或配 `extra_model_paths.yaml` |
| 驱动升级后 AI 全挂 | 驱动分支越过 535 | 回退 535 + `apt-mark hold` |
| 第二天脚本连不上 | DHCP 换了 IP | 配静态 IP（§3.1） |
| 服务重启后所有脚本失效 | 用了 tmux 而非 systemd | 改 systemd unit（§5.4） |
| 出图忽快忽慢 | 温度墙 / 或未加 `--disable-cuda-malloc`（5060） | 见 benchmarks §3.3 / §3.1 |

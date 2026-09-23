# AIGC工程师落地实战指南（双机架构版 合订本 V1.5）

> 适配硬件：Windows 11 笔记本（RTX 5060 8G + 32G内存 + 1T SSD） + Ubuntu 台式机（Tesla P100 16G + 16G内存 + 1T SSD）
> 定位：IT运维转型 **AI Infra / MLOps / AIGC 平台工程师**，聚焦工程化部署、自动化、可观测性、模型训练与微调、私有化交付
> 核心架构：**Windows端 = 开发调试工作站（交互操作、脚本开发、工作流调试）；Ubuntu端 = 算力服务节点（批量推理、模型训练、后台服务、容器化部署）**
> 总周期：**16 周**，每周投入 10~15 小时，全程产出可写入简历的工程成果
> 时间紧张可压缩到 14 周：阶段五的 LLM 部分只做"最小可用 + API 对接"，K8s 走附录选修

---

## 文档版本说明

本合订本整合了 V1.1 ~ V1.5 的全部内容，以 V1.5（2026-09-09 实战版）为基准，补充了阶段二~五的完整内容。各版本变更记录见下方。

### V1.0 → V1.5 完整变更历程

**V1（原始版）** → 核心问题：按"两台机器软件栈相同"来写，误判 RTX 5060 为 Ada 架构

**V2** → 修正：拆分两套 PyTorch（cu128/cu124）、新增驱动锁定、GPU 可观测性、LLM 服务化

**V1.1** → 新增：中国网络环境、SSH 密钥、防火墙、dcgm-exporter 修正、bitsandbytes 验证

**V1.2** → 修正：P100 驱动 580→535、PyTorch 2.4.1+cu121、verify_env.py 增强

**V1.3** → 实战：轻视 580 驱动实际不可用、Python 3.14.6 验证、requirements.txt 陷阱

**V1.4** → 标准化：install_env 脚本、双机 commit 锁定 22e40d2a、四维对应表

**V1.5** → 修复：cuDNN 9 在 sm_60 上初始化失败、内存 16G 修正、main.py 补丁

---

## 第 0 章：前置基线（动手前必读，第 1 周前完成）

这一章是整套方案的地基。**跳过它，后面每一步都可能在第 3 周才暴露出无法挽回的环境问题。**

### 0.1 硬件能力矩阵

| 项目 | Windows 笔记本 | Ubuntu 台式机 |
| --- | --- | --- |
| GPU | RTX 5060 8G | Tesla P100 16G |
| 架构 / 算力 | **Blackwell，sm_120** | **Pascal，sm_60** |
| Tensor Core | 有 | **无** |
| FP16 | 支持 | 支持（CUDA 核心，21.2 TFLOPS） |
| BF16 | 支持 | **不支持** |
| FP8 | 支持 | **不支持**（需 sm_89+） |
| MIG 切分 | 不支持 | **不支持**（Ampere 才有） |
| 显存 | 8 GB GDDR7 | 16 GB HBM2 |
| 系统内存 | 32 GB | **16 GB**（V1.5 实测确认） |
| 功耗 | 笔记本规格 | **PCIe 250W / SXM2 300W**（家用通常是 PCIe） |
| 驱动支持 | ≥ 572.x（Studio 驱动更稳） | **535 分支锁死** |

**关于 P100 功耗——必须先确认你的卡是哪一种：**

- **PCIe 版**：250W TDP，普通主板可装，**这是家用最常见的版本**
- **SXM2 版**：300W TDP，服务器专用接口，需要 NVLink 桥接板 + 强散热
- 不确定？跑 `nvidia-smi -q -d POWER` 看 Current Power Limit

**P100 驱动支持现状（V1.5 实战结论）：**

- **535 是 P100 唯一可用的驱动**。555/560/570/580 等更新分支虽然 `nvidia-smi` 仍列出 P100，但 CUDA runtime 已移除 sm_60 路径，llama.cpp / ComfyUI / kohya 训练全部失败。
- 535 ↔ CUDA 12.2 ↔ cu121/cu124，三者必须严格对齐

**16G RAM 对 P100 的影响（V1.5 修正）：**

Ubuntu 端 16G 系统内存 + 16G 显存，可以跑 SD 1.5 推理和 LoRA 训练，但**不能同时跑 llama-server + ComfyUI**。需要切换使用（见 0.7 规划）。

**能力边界：**

| 任务 | RTX 5060 8G | P100 16G | 结论 |
| --- | --- | --- | --- |
| SD 1.5 推理 / 训练 | 可以 | 可以 | 主力入门模型 |
| SDXL 推理 | 勉强（`--lowvram` + fp16 VAE） | 可以 | 笔记本别指望流畅 |
| SDXL LoRA 训练 | 不可行 | 可以（grad ckpt + fp16 + batch 1） | 放在 P100 |
| FLUX 系列 | 不可行 | **基本不可行**（fp16 需 ~24G） | **本方案不覆盖 FLUX** |
| 7B LLM 推理（Q4） | 勉强（部分 offload） | 可以 | llama.cpp |
| vLLM | 可以 | **不可用**（要求 CC ≥ 7.0） | P100 走 llama.cpp |

> **诚实结论**：这套硬件适合把 **SD 1.5 / SDXL 的工程链路跑通并做出可交付成果**，不适合追 FLUX 等新一代大模型。对求职而言完全够用——企业要的是"能把平台搭起来、能排错、能交付"的人，不是"家里有 H100"的人。

### 0.2 软件版本基线表（V1.5 实战版）

| 组件 | Windows（RTX 5060） | Ubuntu（P100） |
| --- | --- | --- |
| 操作系统 | Windows 11 | **Ubuntu 22.04 LTS** |
| NVIDIA 驱动 | ≥ 572.x | **535.309.01（535 分支锁死）** |
| Python | **3.11+**（实测 3.14.6 OK） | 3.10.x（jammy 默认） |
| PyTorch | **2.11.0+cu128** | **2.6.0+cu124（cuDNN 禁用）** |
| PyTorch 安装索引 | `https://download.pytorch.org/whl/cu128` | `https://download.pytorch.org/whl/cu124` |
| 注意力实现 | xformers / SDPA 均可 | **禁用 xformers**，用 `--disable-xformers` |
| 训练 / 推理精度 | fp16 / bf16 / fp8 可选 | **只能 fp16** |
| ComfyUI commit | **22e40d2a**（双机统一） | **22e40d2a**（双机统一） |
| cuDNN 状态 | 正常 | **禁用**（sm_60 + cuDNN 9 不兼容） |
| **显存分配器** | **native**（`--disable-cuda-malloc`） | **native**（`--disable-cuda-malloc`） |

**为什么不能统一（面试会问）：**

- **cu121 wheel 不含 sm_120 内核** → 5060 上 `torch.cuda.is_available()` 可能返回 True，但一跑 kernel 就报错
- **cu128 自 2.8 起移除了 Pascal(sm_60)** → cu128 在 P100 上同样用不了
- **CUDA 13.0 起移除了 Pascal 的离线编译支持** → Pascal 只能停在 CUDA 12.x

**为什么 P100 选 2.6.0+cu124 + cuDNN 禁用（V1.5 确认）：**

- 2.4.1+cu121 在 535 驱动下 sm_60 通过，但 transformers 库新版本要求 PyTorch ≥ 2.5，否则禁用 PyTorch 后端（CLIP / ControlNet 都会失败）
- 2.6.0+cu124 满足 transformers ≥ 2.5 要求，且 cu124 保留 sm_60 支持
- 但 cuDNN 9.x 在 sm_60 上初始化抛 `CUDNN_STATUS_NOT_INITIALIZED`
- 修复：`torch.backends.cudnn.enabled = False`，PyTorch 原生 CUDA 卷积正常，仅损失 10~20% 卷积性能

**驱动 ↔ CUDA ↔ PyTorch ↔ ComfyUI 四维对应表：**

| 驱动分支 | CUDA 上限 | PyTorch cu | PyTorch 版本 | ComfyUI commit | P100 可用？ |
| --- | --- | --- | --- | --- | --- |
| **535（锁死）** | 12.2 | **cu124** | **2.6.0（cuDNN 禁用）** | **22e40d2a** | ✅ |
| 535 | 12.2 | cu121 | 2.4.1 | < 8817f8fc | ✅ 但 transformers 禁用 PyTorch |
| 555+ | 12.5+ | cu124/cu128 | 2.7+ | 最新 | ❌ sm_60 runtime 已废 |
| 580 | 13.0 | cu130 | 2.11+ | 最新 | ❌ CUDA 驱动太老 |

### 0.3 Ubuntu 端：驱动锁定（P100 的生命线）

> **V1.5 最终结论**：535 是 P100 的终局驱动。555/560/570/580 虽然 `nvidia-smi` 仍列出 P100，但 CUDA runtime 已移除 sm_60 路径，任何 AI 推理全部失败。**不需要再观望 580 复活。**

```bash
# 0. 全新 Ubuntu 22.04 默认源有 535，但加 PPA 确保最新
sudo add-apt-repository ppa:graphics-drivers/ppa -y
sudo apt update

# 1. 查看推荐驱动
sudo ubuntu-drivers devices

# 2. 安装 535 分支
sudo apt install nvidia-driver-535 -y

# 3. 锁定，禁止被后续 apt 升级 / 替换
sudo apt-mark hold nvidia-driver-535
sudo apt-mark showhold          # 必须看到 nvidia-driver-535

# 4. 重启并验证
sudo reboot
nvidia-smi                      # 应显示 Driver Version: 535.309.x，P100 列出
```

> 后续任何 `apt upgrade` 前，先 `apt-mark showhold` 确认锁定仍在。**这条本身就是一项运维能力，值得写进简历和方案文档。**

**装错驱动想回退：**

```bash
sudo apt remove --purge nvidia-driver-580
sudo apt install nvidia-driver-535
sudo apt-mark hold nvidia-driver-535
sudo reboot
```

### 0.4 统一环境验证脚本（两台机器都必须跑）

`torch.cuda.is_available()` 会骗人——它只说明驱动和运行时能对话，不说明 wheel 里有你这张卡的内核。用这个脚本：

```python
# verify_env.py
import torch, sys, subprocess

print("PyTorch:", torch.__version__, "| CUDA runtime:", torch.version.cuda)
print("CUDA 可用:", torch.cuda.is_available())
if not torch.cuda.is_available():
    print("→ 排查：驱动 / venv / torch wheel 三者是否对齐")
    print("→ 可能：pip install -r requirements.txt 覆盖了 torch")
    sys.exit(1)

cap = torch.cuda.get_device_capability(0)
arch = torch.cuda.get_arch_list()
tag = f"sm_{cap[0]}{cap[1]}"

print("显卡:", torch.cuda.get_device_name(0))
print("算力:", cap, f"({tag})")
print("内核架构列表:", arch)

ok = tag in arch
print("校验:", "通过" if ok else "失败 —— 该 PyTorch 构建不含本机内核，必须更换 wheel")
print("显存总量:", round(torch.cuda.get_device_properties(0).total_memory/1024**3, 1), "GB")

# 真正跑一次 kernel，别只看 is_available
x = torch.randn(1024, 1024, device="cuda", dtype=torch.float16)
y = x @ x
torch.cuda.synchronize()
print("矩阵运算: 通过", tuple(y.shape))

if cap == (12, 0):
    print("→ RTX 50 系 Blackwell，建议 PyTorch 2.11+cu128")
elif cap == (6, 0):
    print("→ Tesla P100 Pascal，建议 PyTorch 2.6.0+cu124 + cuDNN 禁用，仅 fp16")

sys.exit(0 if ok else 1)
```

**实战输出示例（V1.5 实测 2026-09-09）：**

**P100 (sm_60)：**
```
PyTorch: 2.6.0+cu124 | CUDA runtime: 12.4
CUDA 可用: True
显卡: Tesla P100-PCIE-16GB
算力: (6, 0) (sm_60)
内核架构列表: ['sm_50', 'sm_60', 'sm_70', 'sm_75', 'sm_80', 'sm_86', 'sm_90']
校验: 通过
矩阵运算: 通过 (1024, 1024)
```

**RTX 5060 (sm_120)：**
```
PyTorch: 2.11.0+cu128 | CUDA runtime: 12.8
CUDA 可用: True
显卡: NVIDIA GeForce RTX 5060 Laptop GPU
算力: (12, 0) (sm_120)
内核架构列表: ['sm_75', 'sm_80', 'sm_86', 'sm_90', 'sm_100', 'sm_120']
校验: 通过
矩阵运算: 通过 (1024, 1024)
```

### 0.5 Windows 端：系统准备

1. **页面文件**：建议保留 Windows 默认"自动管理"；手动设 32G~48G
2. **Defender 排除项**（重要）：把 `D:\AIGC\` 加到 Defender 实时保护排除项
   - 原因：Defender 扫描数千个小文件（模型切片、HF 缓存）会显著拖慢 ComfyUI 启动和模型加载
3. **pip 缓存与 HF 缓存移出 C 盘**：
   ```bat
   setx PIP_CACHE_DIR "D:\AIGC\.cache\pip"
   setx HF_HOME "D:\AIGC\.cache\huggingface"
   ```
   （执行后**新开终端**才生效）

### 0.6 Ubuntu 端：网络身份 + SSH config

**静态 IP**：家用 DHCP + 路由器 MAC 绑定即可，不强求。

如果要做静态 IP：

```yaml
# /etc/netplan/01-aigc-static.yaml
network:
  version: 2
  ethernets:
    ens33:
      dhcp4: no
      addresses: [192.168.1.50/24]
      routes:
        - to: default
          via: 192.168.1.1
      nameservers:
        addresses: [223.5.5.5, 8.8.8.8]
```

**SSH config（Windows 端 `C:\Users\<实际用户名>\.ssh\config`）—— ⚠️ 必须替换占位符：**

```
Host aigc-box
    HostName 192.168.1.100             # 替换成 Ubuntu 实际 IP
    User 你的实际Ubuntu用户名             # 替换！不能是字面量
    LocalForward 8288 127.0.0.1:8188   # 本地 8288 → 远端 ComfyUI 8188
    LocalForward 7960 127.0.0.1:7860   # 本地 7960 → 远端 Kohya 7860
    LocalForward 8180 127.0.0.1:8080   # 本地 8180 → 远端 llama-server 8080
    LocalForward 3100 127.0.0.1:3000   # 本地 3100 → 远端 Grafana 3000
    LocalForward 9190 127.0.0.1:9090   # 本地 9190 → 远端 Prometheus 9090
    ServerAliveInterval 60
```

> **User 占位符不替换的后果**：SSH 会去找字面量 `你的用户名` 这个用户，找不到就 fallback 到当前 Windows 用户名，**必然问密码**。这是最常见的 SSH 失败原因。

**⚠️ 为什么本地端口要错开（V1.5 实战修正）：**

Windows 本地 ComfyUI 也监听 `127.0.0.1:8188`。如果 SSH 隧道也用本地 8188，两者**端口冲突**：

```
bind [127.0.0.1]:8188: Address already in use
channel_setup_fwd_listener_tcpip: cannot listen to port: 8188
```

错开之后两边可以同时开，地址映射清晰：

| 本地地址 | 实际连到 |
| --- | --- |
| `http://127.0.0.1:8188` | **Windows 本地** ComfyUI（RTX 5060） |
| `http://127.0.0.1:8288` | **Ubuntu** ComfyUI（P100） |
| `http://127.0.0.1:8180` | Ubuntu llama-server |
| `http://127.0.0.1:3100` | Ubuntu Grafana（阶段三） |
| `http://127.0.0.1:9190` | Ubuntu Prometheus（阶段三） |

这个设计是阶段二"双机算力调度"的基础——同一份脚本靠 `COMFY_HOST` 环境变量切换目标节点：

```bat
set COMFY_HOST=http://127.0.0.1:8188   :: 打 5060
set COMFY_HOST=http://127.0.0.1:8288   :: 打 P100
```

> 改完 `~/.ssh/config` 必须**重开 ssh 窗口**才生效。

### 0.7 磁盘与内存规划

**磁盘预算（两台机器各 1T SSD）：**

| 项目 | Ubuntu 端估算 | 说明 |
| --- | --- | --- |
| Ubuntu 系统 + 驱动 | 40 GB | |
| ComfyUI venv | ~12 GB | cu124 wheel 约 2.5G，加依赖 |
| Kohya_ss venv | ~10 GB | 与 ComfyUI 独立 |
| llama.cpp 构建产物 | ~3 GB | |
| Docker 镜像（ComfyUI + 监控栈） | ~20 GB | |
| 模型（SD1.5 4G + SDXL 6.5G + 若干 LoRA/ControlNet） | ~30 GB | 建议预留 80 GB |
| LLM 模型（7B Q4 约 4G） | ~20 GB | |
| 数据集与训练产物 | ~50 GB | |
| **合计** | **约 185 GB，建议预留 300 GB** | |

**内存共存策略（V1.5 新增）：**

Ubuntu 端只有 16G 系统内存，**不能同时跑 llama-server + ComfyUI + kohya 训练**：

- 阶段一、二（ComfyUI 为主）：`sudo systemctl stop llama-server` 释放 RAM
- 阶段五（LLM 为主）：`sudo systemctl stop comfyui` 释放 RAM
- 建议加装到 32G 内存条，300 元以内解决，大幅提升操作空间

### 0.8 P100 硬件巡检（上电前必做）

1. **电源功率**：按 0.1 实际功耗 + 系统功耗 + 200W 余量计算，PCIe 版本整机额定功率建议 ≥ 600W
2. **散热方案**：被动散热型号必须靠暴力扇或前置进风直吹
3. **温度基线**：空载和满载各记一次 `nvidia-smi -q -d TEMPERATURE`，P100 建议满载 < 80°C
4. **先跑一次压测**：
   ```bash
   sudo apt install stress-ng -y
   watch -n 1 nvidia-smi
   ```

### 0.9 网络环境（中国用户必读）⚠️

> **这一节是中文用户最容易卡的环节。** HuggingFace、GitHub、PyPI 在国内访问慢或直接超时。

**HuggingFace 镜像（最关键）：**

```bash
echo 'export HF_ENDPOINT=https://hf-mirror.com' >> ~/.bashrc
source ~/.bashrc
```

**pip 镜像（清华源）：**

```bash
mkdir -p ~/.pip
cat > ~/.pip/pip.conf <<'EOF'
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
timeout = 120
[install]
trusted-host = pypi.tuna.tsinghua.edu.cn
EOF
```

**GitHub 代理：**

```bash
git config --global url."https://gh-proxy.com/https://github.com/".insteadOf "https://github.com/"
```

**Windows 端同样的配置：**

- pip 镜像：`%USERPROFILE%\pip\pip.ini`
- HF 镜像：系统环境变量 `HF_ENDPOINT=https://hf-mirror.com`
- Git 代理：Git Bash 跑 `git config --global url."..."`

### 0.10 SSH 密钥配置

```bash
# Windows Git Bash
ssh-keygen -t ed25519
ssh-copy-id 你的实际用户名@192.168.1.100
ssh aigc-box "echo ok"   # 不应再问密码
```

**已拷公钥仍问密码的三个原因：**

1. **User 字段没替换**（最常见，见 0.6 警告）
2. 公钥拷给了用户 A，SSH 登的是用户 B
3. 权限不对：`chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys`

### 0.11 防火墙与端口预检

```bash
sudo ufw allow OpenSSH
ss -tlnp | grep -E ':(22|3000|8080|8188|9090|9400)\b'   # 端口冲突预检
```

### 0.12 ⚠️ requirements.txt 陷阱（V1.4 新增）

**这是本方案**反复踩了 4 次**的坑，必须从一开始就理解。**

**问题**：`pip install -r requirements.txt` 会**覆盖**你已手动装好的 torch 版本。

**链路**（踩了 4 次的路径）：

```
1. 用户手动装 torch:  pip install torch==2.6.0 --index-url ...
2. → torch 2.6.0+cu124 OK, sm_60 验证通过 ✅
3. 用户跑: pip install -r requirements.txt
4. → requirements.txt 里写了 torch>=2.7.0 或 torch==2.14.0
5. → pip 把 2.6.0 卸了，装上 2.14.0+cu130
6. → 535 驱动不认 cu130，报 "driver too old"
7. 用户困惑：刚才还好的，怎么坏了？
```

**修复**：每次装完 requirements.txt 后，**必须**重新装符合驱动的 torch 版本。

**预防**：用下面 1.1.3 和 1.2.3 的 install 脚本，**永远不要手动分步装**。

---

## 阶段一：ComfyUI 源码部署与基础原理打通（第 1~3 周）

### 阶段目标

脱离一键整合包，完成双机源码级部署；通过 0.4 的环境校验；跑通最简生图流程；建立 AIGC 基础概念体系与排错习惯。

### 1.1 Windows 笔记本端（RTX 5060 8G）

#### 1.1.1 基础软件安装

1. **Git for Windows**：默认安装，勾选 `Git Bash Here`
2. **Python 3.11+**（实测 3.14.6 可用）。如果系统已有多个版本：
   ```bat
   py -3.14 --version
   py -3.11 --version
   ```
   多版本并存用 `py` launcher 管理，不冲突。
3. **VS Code**：装 Python、Markdown、**Remote - SSH** 插件
4. **7-Zip**：解压模型文件

#### 1.1.2 目录规范（运维标准）

```
D:\AIGC\
├─ ComfyUI-Win\          # Windows 本地 ComfyUI 主程序（git clone 的目标目录）
├─ model_cache\          # 公共模型库（通过 extra_model_paths.yaml 接入）
│   ├─ checkpoints\
│   ├─ loras\
│   ├─ vae\
│   ├─ controlnet\
│   ├─ upscale_models\
│   ├─ embeddings\
│   └─ clip_vision\
├─ scripts\              # Python 自动化脚本仓库
├─ .cache\               # pip / huggingface 缓存
└─ docs\                 # 部署文档、学习笔记、排错记录
```

#### 1.1.3 源码部署 + install_env.bat（V1.4 标准化）

```bat
cd /d D:\AIGC
git clone https://github.com/comfyanonymous/ComfyUI.git ComfyUI-Win
cd ComfyUI-Win
git checkout 22e40d2ace0f53da025b3a41cbe4b664ef807097
git switch -c stable-2025-10

python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
```

**不再手动分步装，直接跑 install_env.bat：**

```bat
:: D:\AIGC\ComfyUI-Win\install_env.bat
@echo off
:: ComfyUI 完整环境安装脚本（V1.4 标准化版）
:: 任何时候重装依赖都跑这个，不要手动 pip install -r requirements.txt
chcp 65001 >nul
setlocal enabledelayedexpansion

call venv\Scripts\activate

echo [1/3] 装 ComfyUI 依赖...
pip install -r requirements.txt

echo [2/3] 强制覆盖 torch 到 RTX 5060 兼容版本...
pip install torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu128

echo [3/3] 强校验...
python -c "import torch; assert torch.cuda.is_available(), 'CUDA 不可用！'; assert torch.cuda.get_device_capability(0) == (12, 0), f'不是 sm_120'; print('^|^| OK', torch.__version__, 'sm_120 验证通过')"

echo.
echo 安装完成。跑 python main.py 启动 ComfyUI。
```

```bat
:: 跑它
install_env.bat
```

#### 1.1.4 配置 extra_model_paths.yaml

ComfyUI 只扫描 `ComfyUI\models\` 下的目录，不会自动发现 `D:\AIGC\model_cache`。在 **git clone 后的仓库根目录**（即 `D:\AIGC\ComfyUI-Win\`，与 `main.py` 同级）新建 `extra_model_paths.yaml`：

```yaml
aigc_win:
    base_path: D:/AIGC/model_cache/
    checkpoints: checkpoints
    loras: loras
    vae: vae
    controlnet: controlnet
    upscale_models: upscale_models
    embeddings: embeddings
    clip_vision: clip_vision
    diffusion_models: diffusion_models
    text_encoders: text_encoders
```

#### 1.1.5 环境校验与启动

```bat
python verify_env.py
```

校验通过（看到 `sm_120` 且矩阵运算通过）后再启动：

```bat
python main.py --disable-cuda-malloc
```

浏览器访问 `http://127.0.0.1:8188`。

> ⚠️ **`--disable-cuda-malloc` 在这台 5060 上是必选参数**（V1.5 实测，三机对照后修正）。
>
> ComfyUI 默认的 `cudaMallocAsync` 分配器在 **RTX 5060 / Windows / 8GB** 上造成**双重危害**：
> - **每张图 +2.6 秒开销（2.34 倍劣化）**：WebSocket 精测 native 1.94s vs cudaMallocAsync 4.54s
> - **批量任务被静默丢弃**：20 连发跑到第 4~7 张卡死，`/queue` 与 `/history` 都查不到任务
>
> **⚠️ 但这不是通用优化，不要盲目照抄到别的机器。** 三机对照结果：
>
> | 机器 | 环境 | cudaMallocAsync 影响 |
> | --- | --- | --- |
> | **RTX 5060 8GB** | **Windows** / Blackwell | ❌ **+2.6s、批量卡死** |
> | Tesla P100 16GB | Linux / Pascal | ✅ 零影响（差 1.4%，噪声内） |
> | NVIDIA A10 24GB | Linux / Ampere | ✅ 零影响（中位完全相同） |
>
> **两台 Linux 机器加不加都一样。** 早期文档写过"跨架构共性"，那个结论被 A10 和 P100 的对照实验证伪了——当时只有 5060 一台机器的样本，P100 根本没做过对照。
>
> 根因未定：5060 同时是唯一的 Windows 机器和唯一的 8GB 机器，**这两个变量在本项目环境中始终绑定，无法分离**。
>
> **验证方式**：启动日志里 `Device:` 那行末尾应是 `native` 而不是 `cudaMallocAsync`：
> ```
> Device: cuda:0 NVIDIA GeForce RTX 5060 Laptop GPU : native
> ```
> 也可以查 `curl http://127.0.0.1:8188/system_stats`，看 `devices[0].name` 和 `system.argv`。
>
> 详见 2.5 实测数据与附录 D 第 22~25 条。

#### 1.1.6 跑通最简工作流

**a) 拿 SD 1.5 模型：**

> ⚠️ `runwayml/stable-diffusion-v1-5` 仓库是 **gated**（门控），hf-mirror 镜像 404。改用：
>
> ```powershell
> $env:HF_ENDPOINT = "https://hf-mirror.com"
> huggingface-cli download stable-diffusion-v1-5/stable-diffusion-v1-5 v1-5-pruned-emaonly.safetensors --local-dir D:\AIGC\model_cache\checkpoints
> ```
>
> 备选（v1-4，无门控）：`huggingface-cli download CompVis/stable-diffusion-v1-4 sd-v1-4.safetensors`
>
> 文件大小约 4.27 GB 才算完整。

**b) 界面基本操作：**

- `http://127.0.0.1:8188` 打开
- **双击空白** = 搜索添加节点
- 节点右侧小圆 = 输出，左侧 = 输入
- 拖节点间连线
- 顶部 `Queue Prompt` 按钮 = 跑

**第一次启动画布通常是空的**，手动加这 6 个节点：

| 节点 | 作用 |
| --- | --- |
| CheckpointLoaderSimple | 加载 .safetensors 模型 |
| CLIPTextEncode × 2 | 编码正/负提示词 |
| EmptyLatentImage | 创建空白潜空间图 |
| KSampler | 采样（核心节点） |
| VAEDecode | 把 latent 还原成图片 |
| SaveImage | 保存图片 |

**连线：**

```
CheckpointLoaderSimple.MODEL  →  KSampler.model
CheckpointLoaderSimple.CLIP   →  CLIPTextEncode(正).clip
CheckpointLoaderSimple.CLIP   →  CLIPTextEncode(负).clip
CheckpointLoaderSimple.VAE    →  VAEDecode.vae
CLIPTextEncode(正).CONDITIONING  →  KSampler.positive
CLIPTextEncode(负).CONDITIONING  →  KSampler.negative
EmptyLatentImage.LATENT  →  KSampler.latent_image
KSampler.LATENT  →  VAEDecode.samples
VAEDecode.IMAGE  →  SaveImage.images
```

**参数：**
- CheckpointLoaderSimple: 选你下的模型名
- CLIPTextEncode（正）: `a cute cat, photorealistic, 8k`
- CLIPTextEncode（负）: `blurry, low quality, distorted`
- EmptyLatentImage: width=512, height=512, batch_size=1
- KSampler: steps=20, cfg=7, sampler_name=euler, scheduler=normal, denoise=1.0

**c) 跑 + 保存：**

1. 点 `Queue Prompt`
2. 第一次会下载 CLIP 等模型（走 HF 镜像，1~2 分钟）
3. 出图后 `File → Save (API Format)`，存为 `base_workflow.json`

### 1.2 Ubuntu 台式机端（P100 16G）

#### 1.2.1 系统基础（先完成 0.3 驱动锁定与 0.6 静态 IP）

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install git python3.10 python3.10-venv python3-pip tmux nvtop openssh-server htop -y
sudo systemctl enable --now ssh
sudo ufw allow OpenSSH
nvidia-smi
```

#### 1.2.2 目录规范

```bash
mkdir -p /opt/AIGC/{ComfyUI-server,model_storage/{checkpoints,loras,vae,controlnet,upscale_models,embeddings},output,logs,docs}
```

#### 1.2.3 源码部署 + install_env.sh（V1.5 完整版）

```bash
cd /opt/AIGC
git clone https://github.com/comfyanonymous/ComfyUI.git ComfyUI-server
cd ComfyUI-server
git checkout 22e40d2ace0f53da025b3a41cbe4b664ef807097
git switch -c stable-2025-10
python3.10 -m venv venv
source venv/bin/activate
pip install --upgrade pip
```

**写 install_env.sh 脚本（V1.5 完整版，含 cuDNN 补丁）：**

```bash
cat > /opt/AIGC/ComfyUI-server/install_env.sh <<'SCRIPT'
#!/bin/bash
# ComfyUI 完整环境安装脚本（V1.5 版）
# 任何时候重装依赖都跑这个
set -e

cd "$(dirname "$0")"
source venv/bin/activate

echo "[1/4] 装 ComfyUI 依赖..."
pip install -r requirements.txt

echo "[2/4] 强制覆盖 torch 到 P100 兼容版本..."
pip install torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0 \
    --index-url https://download.pytorch.org/whl/cu124

echo "[3/4] 打 cuDNN 补丁（P100 sm_60 + cuDNN 9 不兼容）..."
python3 << 'PY'
import pathlib
p = pathlib.Path("main.py")
src = p.read_text(encoding="utf-8")
if "cudnn.enabled = False" not in src:
    patch = (
        "# --- P100 (Pascal sm_60) 补丁：cuDNN 9.x 初始化失败\n"
        "# 只能在 main.py 中设置，sitecustomize.py 被系统版本覆盖\n"
        "import torch\n"
        "torch.backends.cudnn.enabled = False\n"
        "# --- 补丁结束\n"
        "import execution"
    )
    p.write_text(src.replace("import execution", patch, 1), encoding="utf-8")
    print("✅ 已打补丁")
else:
    print("✅ 补丁已存在")
PY

echo "[4/4] 强校验..."
python3 -c "
import torch
assert torch.cuda.is_available(), 'CUDA 不可用'
assert torch.cuda.get_device_capability(0) == (6, 0), f'不是 sm_60'
print('✅ OK', torch.__version__, 'cuDNN:', torch.backends.cudnn.enabled, 'sm_60')
"
echo ""
echo "安装完成。跑 python main.py 启动 ComfyUI"
SCRIPT

chmod +x install_env.sh
```

```bash
# 跑它
./install_env.sh
```

#### 1.2.3-b ⚠️ cuDNN 补丁机制说明（V1.5 新增）

**为什么需要补丁：**
- cuDNN 9.x 在 sm_60 上初始化失败，抛 `CUDNN_STATUS_NOT_INITIALIZED`
- 需要在 `import execution` 之前设 `torch.backends.cudnn.enabled = False`

**为什么不用 sitecustomize.py：**
- Ubuntu 22.04 自带 `/usr/lib/python3.10/sitecustomize.py`
- venv 里的 sitecustomize.py 永远不会被加载（系统路径优先）

**补丁在 main.py 中的位置：**

```python
# main.py 第 148 行附近
# ...
PYTORCH_CUDA_ALLOC_CONF = ...
# --- P100 (Pascal sm_60) 补丁 ---
import torch
torch.backends.cudnn.enabled = False
# --- 补丁结束 ---
import execution
```

**Git 提交：**

```bash
git add main.py
git commit -m "P100 patch: disable cuDNN (sm_60 + cuDNN 9 incompatible)"
```

#### 1.2.4 extra_model_paths.yaml

```bash
cat > /opt/AIGC/ComfyUI-server/extra_model_paths.yaml <<'EOF'
aigc_ubuntu:
    base_path: /opt/AIGC/model_storage/
    checkpoints: checkpoints
    loras: loras
    vae: vae
    controlnet: controlnet
    upscale_models: upscale_models
    embeddings: embeddings
    clip_vision: clip_vision
    diffusion_models: diffusion_models
    text_encoders: text_encoders
EOF
```

#### 1.2.5 systemd 托管（V1.5 完整版）

```ini
# /etc/systemd/system/comfyui.service
[Unit]
Description=ComfyUI Inference Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/AIGC/ComfyUI-server
Environment="PATH=/opt/AIGC/ComfyUI-server/venv/bin"
# 09-20 新增：回收 PyTorch 显存碎片（OOM 实测 1.66G），
# 使 768×512 出图能与 llama-server 共存。见坑 #51 / 06 手册 §8
Environment="PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True"
ExecStart=/opt/AIGC/ComfyUI-server/venv/bin/python main.py \
          --listen 127.0.0.1 --port 8188 \
          --disable-xformers --disable-cuda-malloc
Restart=on-failure
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=5
StandardOutput=append:/opt/AIGC/logs/comfyui.log
StandardError=append:/opt/AIGC/logs/comfyui.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo mkdir -p /opt/AIGC/logs
sudo systemctl daemon-reload
sudo systemctl enable --now comfyui
systemctl status comfyui
```

**参数说明：**

| 参数 | 作用 | 必选？ |
| --- | --- | --- |
| `--disable-xformers` | P100 不支持 xformers | ✅ 必选 |
| `--disable-cuda-malloc` | 禁用 cudaMallocAsync 分配器，Pascal + 535 上已知雷区 | ✅ 必选 |
| `--listen 127.0.0.1` | 只监听本地，不对外暴露 | ✅ 必选 |
| `Restart=on-failure` | 仅非正常退出时重启 | ✅ 必选 |
| `StartLimitBurst=5` | 5 分钟内最多重启 5 次 | ✅ 必选 |

### 1.3 双机联通验证

**用 SSH 隧道，不用 0.0.0.0。** Windows 端执行：

```bash
ssh aigc-box          # 已在 0.6 配好 LocalForward
```

保持窗口不关，本地浏览器访问 `http://127.0.0.1:8188` → Ubuntu 上的 ComfyUI。

模型同步：

```bash
scp /d/AIGC/model_cache/checkpoints/*.safetensors aigc-box:/opt/AIGC/model_storage/checkpoints/
```

**双机跑同一工作流验证一致性：**

```bash
scp /d/AIGC/ComfyUI-Win/base_workflow.json aigc-box:/opt/AIGC/ComfyUI-server/workflows/
```

在两侧 ComfyUI 分别加载，用相同 seed + 相同模型 → 出图应一致。

### 1.4 必修基础概念（不用精通美术）

- **模型类**：Checkpoint（基础模型）、LoRA（微调模型）、VAE（画质解码器）、ControlNet（结构控制）、Embedding/Textual Inversion
- **生成类**：潜空间、采样器、步数、CFG、去噪强度、种子
- **工程类**：模型目录结构、自定义节点、工作流 JSON 格式、日志定位、显存与系统内存的分工

### 1.5 常见故障排查（V1.5 完整版）

| 症状 | 排查方向 |
| --- | --- |
| **`cuDNN error: CUDNN_STATUS_NOT_INITIALIZED`** | cuDNN 9 在 sm_60 上初始化失败。运行 `torch.backends.cudnn.enabled = False` 后重试（见 1.2.3-b） |
| `Torch not compiled with CUDA enabled` | requirements.txt 覆盖了 torch，重跑 install_env 脚本 |
| `The NVIDIA driver on your system is too old` | 同上，torch cu 版本被升高，重跑 install_env 脚本 |
| `ValueError: infer_schema ... list[int]` | comfy_kitchen 不兼容，降 commit 到 22e40d2a |
| systemd 疯狂重启（counter > 10） | 改 `Restart=on-failure` + `StartLimitBurst=5`；手动跑 `python main.py` 看真错误 |
| StandardOutput 日志看不到错误 | 检查路径是否配了可写的实际目录 |
| sitecustomize.py 不生效 | Ubuntu 系统 `/usr/lib/python3.10/sitecustomize.py` 优先。改直接在 main.py 中 patch |
| RAM 16G 跑 ComfyUI 加 llama-server 卡死 | 不能同时跑，停一个（见 0.7） |
| `[transformers] Disabling PyTorch` | torch < 2.5 导致 transformers 禁用 PyTorch 后端。升级 torch 到 2.6.0+cu124 |
| **`cudaMallocAsync` 导致批量任务卡死** | **V1.5 新**：连续出图跑到第 4~7 张卡住，`/queue` 与 `/history` 都查不到任务。两端加 `--disable-cuda-malloc`（见 1.1.5） |
| **批量出图性能异常慢但很"稳定"** | **V1.5 新**：每张耗时恒定但明显偏慢（如 512×512 要 6s），检查是否用了 `cudaMallocAsync`。它带来 +4s/张的**恒定**开销，伪装成"系统稳定" |
| **SSH 报 `cannot listen to port: 8188`** | **V1.5 新**：本地 ComfyUI 已占用 8188。SSH 隧道改用本地 8288（见 0.6） |
| **P95 恒等于 max** | **V1.5 新**：样本量不足（n=10 时百分位索引退化）。需 n≥20 + 线性插值算法（见 2.4） |
| `no kernel image is available` | wheel 与显卡架构不匹配 |
| CUDA 不可用 | venv / 驱动 / wheel 三者对齐 |
| 显存 OOM（P100） | 降分辨率 / 降 batch / `--lowvram` |
| 系统内存被吃光（Windows） | 页面文件 / Docker Desktop / **Defender 扫描 D:\AIGC\** |
| 端口占用 | `--port 8189` 或 `ss -tlnp` 查 |
| 模型列表里看不到 | extra_model_paths.yaml 路径用正斜杠、base_path 结尾 `/` |
| 国外模型下载慢 / 失败 | `HF_ENDPOINT` / pip 镜像 / GitHub 代理是否配置（0.9） |
| SSH 已拷公钥仍问密码 | 检查 `~/.ssh/config` 的 `User` 字段是否替换 |
| 双机出图结果不一致 | 检查 `git log -1 --oneline` 双机 commit 是否一致 |

### 阶段产出物

1. 《ComfyUI 双机部署手册.md》（含版本基线表与 commit 记录 `22e40d2a`）
2. `verify_env.py` 及两台机器的校验输出
3. `comfyui.service` 与 systemd 运维说明
4. `install_env.bat` + `install_env.sh` 标准化安装脚本
5. 最简生图工作流 `base_workflow.json`
6. 《常见故障排查记录 V1.0.md》

---

## 阶段二：ComfyUI API + Python 自动化批量出图（第 4~6 周）

### 阶段目标

掌握 ComfyUI 原生 API；写出**能上生产**的批量生成脚本；实现 Windows 发任务、Ubuntu 执行的双机算力调度；采集出第一组可写进简历的性能数据。

### 2.0 目录结构

实际落地的脚本目录（本项目为 `D:\AIGC\Scripts\`，路径按你自己的实际情况调整）：

```
Scripts\
├─ _path.py                         # 引导：仓库根入 sys.path（共享包 comfy_client/）
├─ comfy_batch_gen.py               # 批量出图 + 指标采集
├─ inspect_workflow.py              # 工作流结构检查工具
├─ workflows\
│   ├─ base_workflow_api.json       # API 格式（给脚本用）
│   └─ base_workflow_ui.json        # UI 格式（留档，方便回界面改）
├─ logs\                            # 运行日志 + bench 指标
└─ output\                          # 出图产物（按 run_id 分子目录）
```

### 2.1 前置：导出 API 格式工作流

#### 2.1.1 两种 JSON 格式的本质区别

ComfyUI 有两种导出格式，**只有 API 格式能提交给 `/prompt` 接口**。

| 维度 | UI 格式（`Export`） | API 格式（`Export (API)`） |
| --- | --- | --- |
| 顶层结构 | `{"nodes": [...], "links": [...]}` | `{"节点ID": {...}, ...}` |
| 节点标识 | `"id": 7`（数字） | `"7"`（字符串 key） |
| 节点类型字段 | `"type"` | `"class_type"` |
| 参数 | `widgets_values: [数组，靠顺序对应]` | `inputs: {命名字段}` ← **可读、可改** |
| 连线 | 独立的 `links` 数组 | 写在 `inputs` 里：`["源节点ID", 输出索引]` |
| 坐标 / 大小 / 颜色 | 有 | **无** |
| 能提交给 `/prompt`？ | ❌ | ✅ |

API 格式长这样：

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
      "seed": 148446967917940, "steps": 20, "cfg": 8,
      "sampler_name": "euler", "scheduler": "normal", "denoise": 1,
      "model": ["1", 0], "positive": ["4", 0],
      "negative": ["5", 0], "latent_image": ["6", 0]
    },
    "_meta": {"title": "SAMPLER"}
  }
}
```

**关键认知**：API 格式把 `widgets_values` 数组"翻译"成了**有名字的字段**。这是自动化的基础——能直接改 `inputs["text"]`，不用猜"数组第 3 个是 steps 还是 cfg"。

#### 2.1.2 开启 Dev Mode

Dev Mode 是 **per-ComfyUI-实例** 的设置，存在**服务端**：

```
<ComfyUI 目录>/user/default/comfy.settings.json
```

判断标准：**浏览器当前连的是哪个 ComfyUI，就是给那个实例开的。**

| 浏览器地址 | 实际连到 | 设置存在哪 |
| --- | --- | --- |
| `127.0.0.1:8188` | Windows 本地 ComfyUI | `ComfyUI-Win\user\default\comfy.settings.json` |
| `127.0.0.1:8288` | Ubuntu ComfyUI | `/opt/AIGC/ComfyUI-server/user/default/comfy.settings.json` |

**建议两台都开**（成本为零，搞混的成本是"找不到菜单"）。

**方式 A：界面操作**
1. 齿轮图标 ⚙️（Settings）
2. 搜索框输入 `dev`
3. 找到 `Enable dev mode options`，**打勾**
4. **刷新浏览器页面（F5）** —— 不刷新菜单不会更新

**方式 B：直接改配置文件**（Ubuntu 端更快）

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("/opt/AIGC/ComfyUI-server/user/default/comfy.settings.json")
cfg = json.loads(p.read_text()) if p.exists() else {}
cfg["Comfy.DevMode"] = True
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
print("已写入:", cfg)
PY

sudo systemctl restart comfyui
```

> 如果 key 名在你的 frontend 版本里不对，先用方式 A 在界面打勾一次，再 `cat` 那个文件看实际写入的 key 名——这是最可靠的确认方法。

#### 2.1.3 给节点起标题（关键，别跳过）

**不改标题的后果**：脚本只能硬编码 `wf["7"]["inputs"]["seed"]`，改一下工作流节点 ID 就变，脚本全崩。

**改法**：在节点**标题栏**上右键 → `Title` / `Rename`，或直接**双击标题栏**文字。

**改哪几个**：

| 当前标题 | 怎么认出来 | 改成 |
| --- | --- | --- |
| `CLIPTextEncode` | 文本框里是你想要的内容（cute cat、photorealistic） | **`POSITIVE_PROMPT`** |
| `CLIPTextEncode` | 文本框里是你不想要的（blurry、deformed、low quality） | **`NEGATIVE_PROMPT`** |
| `KSampler` | 有 seed / steps / cfg / sampler_name | **`SAMPLER`** |
| `EmptyLatentImage` | 只有 width / height / batch_size | **`LATENT`** |

`CheckpointLoaderSimple` / `VAEDecode` / `SaveImage` **不用改**——每种只有一个，用 `class_type` 就能唯一定位。

> **分不清哪个 CLIPTextEncode 是正向？** 点一下 KSampler，看它的 `positive` 输入口连到哪个节点。

改完后 API JSON 里会带 `_meta.title` 字段，脚本按标题查找，改工作流也不会崩。

#### 2.1.4 导出

1. 左上角 `Workflow`（或 `File`）菜单 → **`Export (API)`**
2. 浏览器下载 `workflow_api.json`
3. 挪到 `Scripts\workflows\base_workflow_api.json`
4. 顺手也存一份 UI 格式（`Export`，不带 API）到 `base_workflow_ui.json` 留档

**找不到 `Export (API)`？** Dev Mode 没开，或开了没刷新页面。

> ⚠️ **步骤有先后依赖**：必须**先改标题，再导出**。已经导出过又去改标题的，得**重新导出**。

#### 2.1.5 验证导出正确

`Scripts\inspect_workflow.py`：

```python
"""检查 API 格式工作流的结构，确认导出正确。"""
import json, sys
from pathlib import Path

WF = Path(__file__).parent / "workflows" / "base_workflow_api.json"

if not WF.exists():
    sys.exit(f"❌ 文件不存在: {WF}")

wf = json.loads(WF.read_text(encoding="utf-8"))

if "nodes" in wf or "links" in wf:
    sys.exit("❌ 这是 UI 格式（有 nodes/links）。请用 Export (API) 重新导出")

print(f"✅ API 格式，共 {len(wf)} 个节点\n")
print(f"顶层 keys: {sorted(wf.keys(), key=int)}\n")

for nid in sorted(wf.keys(), key=int):
    node = wf[nid]
    title = node.get("_meta", {}).get("title", "")
    print(f"节点 {nid}: {node['class_type']}"
          f"{f'  [标题: {title}]' if title else '  [无自定义标题]'}")
    for k, v in node["inputs"].items():
        if isinstance(v, list):
            print(f"    {k:16s} ← 连线自 节点{v[0]} 的输出[{v[1]}]")
        else:
            s = str(v)
            print(f"    {k:16s} = {s[:57] + '...' if len(s) > 60 else s}")
    print()

print("=" * 50)
titles = {n.get("_meta", {}).get("title"): nid for nid, n in wf.items()
          if n.get("_meta", {}).get("title")}
for t in ["POSITIVE_PROMPT", "NEGATIVE_PROMPT", "SAMPLER", "LATENT"]:
    if t in titles:
        print(f"✅ {t:18s} → 节点 {titles[t]}")
    else:
        print(f"⚠️  {t:18s} → 未找到（标题没改，或改完没重新导出）")
```

**判定标准**：顶层 keys 是 `['1','4','5','6','7','9','10']` 这样的数字字符串，且 4 个标题全部 ✅。

**常见失败：**

| 输出 | 原因 | 解决 |
| --- | --- | --- |
| `❌ 这是 UI 格式` | 点了 `Export` 不是 `Export (API)` | Dev Mode 开了吗？刷新页面重新导出 |
| 顶层 keys 是 `['id','nodes','links']` | 同上 | 同上 |
| `⚠️ POSITIVE_PROMPT 未找到` | 改了标题但没重新导出 | 回界面重新 Export (API) |

### 2.2 生产级 API 客户端

共享包 `comfy_client/`（仓库根，scripts 与网关共用同一份）：

```python
"""
ComfyUI API 客户端 —— 生产级实现
用法：
    set COMFY_HOST=http://127.0.0.1:8188   # 打 Windows 本地 5060
    set COMFY_HOST=http://127.0.0.1:8288   # 打 Ubuntu P100
    python -m comfy_client
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


def submit(session, workflow: dict) -> str:
    """提交任务，返回 prompt_id。校验失败时抛出可读的错误。"""
    client_id = str(uuid.uuid4())
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


def wait_for(session, prompt_id: str, timeout: int = 600, poll: float = 2.0) -> dict:
    """轮询直到完成。任务报错立即抛出，不会死等到超时。"""
    deadline = time.time() + timeout
    last_state = None
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
            state = "执行中"
        elif prompt_id in pending:
            state = f"排队中（前面还有 {pending.index(prompt_id)} 个）"
        else:
            state = "等待调度"
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


def upload_image(session, local_path: str, subfolder="", overwrite=True) -> str:
    """img2img / ControlNet 需要先上传输入图。"""
    p = Path(local_path)
    r = session.post(
        f"{COMFY_HOST}/upload/image",
        files={"image": (p.name, p.read_bytes(), "image/png")},
        data={"subfolder": subfolder, "overwrite": str(overwrite).lower()},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["name"]


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
```

**这份脚本比"能跑"多了什么：**

| 特性 | 为什么重要 |
| --- | --- |
| **按标题定位节点** | 改工作流不会崩（对比硬编码 `wf["7"]`） |
| **`check_server()` 前置** | 服务没起就立刻报错，不用等提交才发现 |
| **`list_checkpoints()` 校验** | 模型名写错会在提交前被拦住，报错可读 |
| **读 400 响应体** | ComfyUI 校验失败返回 400 + `node_errors`，直接 `raise_for_status()` 会丢掉最有用的信息 |
| **`queue_running` / `queue_pending` 区分** | 能显示"排队中，前面还有 N 个" |
| **`deepcopy` 模板** | 不会因为改了一次参数就污染后续所有任务 |
| **seed 显式随机** | API 格式没有 `randomize`，不给 seed 就每次同一张图 |
| **超时 + 重试** | 网络抖动不至于让整批任务失败 |
| **结构化日志双写** | 控制台看进度，文件留档给阶段三接 Prometheus |
| **返回耗时指标** | 阶段五 benchmarks 的数据来源 |

**常见报错对照：**

| 报错 | 原因 | 解决 |
| --- | --- | --- |
| `ConnectionError` / `Max retries exceeded` | ComfyUI 没起，或 SSH 隧道断了 | `systemctl status comfyui` / 重开 `ssh aigc-box` |
| `模型 'xxx' 不在目标机器上` | Ubuntu 缺模型 | `scp` 模型过去 |
| `找不到标题为 'POSITIVE_PROMPT' 的节点` | 标题没改或改完没重新导出 | 回界面改标题 → 重新 Export (API) |
| `提交被拒 (400)` + `node_errors` | 参数非法（如 sampler_name 拼错） | 看 node_errors 里的具体字段 |

### 2.3 双机联调：切一个环境变量换算力节点

```bat
cd Scripts

:: 打 Windows 本地 5060
set COMFY_HOST=http://127.0.0.1:8188
python -m comfy_client

:: 打 Ubuntu P100（需要 ssh aigc-box 窗口开着）
set COMFY_HOST=http://127.0.0.1:8288
python -m comfy_client
```

**这就是"双机算力调度"的核心**：同一份脚本、同一份工作流、同一个模型，改一个环境变量就换执行节点。因为走 SSH 隧道，**不需要改 IP、不需要开防火墙、不需要暴露端口**。

模型同步（工作流里引用的模型名两端必须都存在）：

```bash
scp /d/AIGC/model_cache/checkpoints/*.safetensors \
    aigc-box:/opt/AIGC/model_storage/checkpoints/
```

### 2.4 批量生成 + 指标采集

`Scripts\comfy_batch_gen.py`：

```python
"""
批量出图 + 指标采集。
用法：
    set COMFY_HOST=http://127.0.0.1:8288
    python comfy_batch_gen.py
产出：
    output/<run_id>/*.png       图片
    logs/bench_<run_id>.json    结构化指标（阶段五 benchmarks 的数据源）
    logs/bench_<run_id>.csv     同上，Excel 可直接开
"""
import json, csv, time, statistics, socket
from datetime import datetime

from comfy_client import (
    make_session, load_workflow, build_prompt, submit, wait_for,
    fetch_images, find_by_title, find_by_class,
    check_server, list_checkpoints, COMFY_HOST, log, get_base_dir,
)

# ---------- 批量任务定义 ----------
NEGATIVE = ("blurry, low quality, worst quality, distorted, deformed, "
            "watermark, text, signature, jpeg artifacts")

TASKS = [
    {"positive": "a cute orange cat sitting on a windowsill, soft morning light, photorealistic, 8k"},
    {"positive": "a golden retriever running on a beach at sunset, motion blur, photorealistic"},
    {"positive": "a steaming cup of coffee on a wooden desk, shallow depth of field, cozy"},
    {"positive": "an old lighthouse on a rocky cliff, stormy sky, dramatic lighting"},
    {"positive": "a bowl of ramen with soft-boiled egg, overhead shot, food photography"},
]

# ⚠️ 样本量 ≥ 20 才能让 P95 有统计意义（n=10 时 P95 恰好等于 max）
SEEDS      = [1001, 1002, 1003, 1004]    # 5 prompts × 4 seeds = 20 张
STEPS      = 20
CFG        = 7.0
WIDTH      = 512
HEIGHT     = 512
MAX_RETRY  = 2                           # 单任务失败重试次数


def get_vram_used(session) -> float:
    """当前显存占用 GB。用于记录峰值水位。"""
    try:
        d = session.get(f"{COMFY_HOST}/system_stats", timeout=(5, 10)).json()["devices"][0]
        return (d.get("vram_total", 0) - d.get("vram_free", 0)) / 1024**3
    except Exception:
        return 0.0


def wait_queue_clear(session, max_pending: int = 2):
    """ComfyUI 串行执行队列。积压太多就先等，避免无脑灌任务。"""
    while True:
        q = session.get(f"{COMFY_HOST}/queue", timeout=(5, 10)).json()
        pending = len(q.get("queue_pending", []))
        if pending <= max_pending:
            return
        log.info("队列积压 %d，等待...", pending)
        time.sleep(3)


def main():
    run_id  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = BASE_DIR / "output" / run_id
    log_dir = BASE_DIR / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    with make_session() as s:
        stats  = check_server(s)
        device = stats["devices"][0]["name"]

        template = load_workflow()
        ckpt = template[find_by_class(template, "CheckpointLoaderSimple")]["inputs"]["ckpt_name"]
        if ckpt not in list_checkpoints(s):
            log.error("模型 %r 不在目标机器上", ckpt)
            raise SystemExit(1)

        jobs = [(t, seed) for t in TASKS for seed in SEEDS]
        log.info("批量任务：%d 个（%d prompts × %d seeds）",
                 len(jobs), len(TASKS), len(SEEDS))
        log.info("输出目录：%s", out_dir)

        records = []
        batch_t0 = time.time()

        for i, (task, seed) in enumerate(jobs, 1):
            rec = {
                "index": i, "seed": seed, "steps": STEPS, "cfg": CFG,
                "width": WIDTH, "height": HEIGHT,
                "prompt": task["positive"][:80],
                "status": "pending", "elapsed": None,
                "vram_peak_gb": None, "prompt_id": None,
                "error": None, "attempts": 0,
            }

            for attempt in range(1, MAX_RETRY + 2):
                rec["attempts"] = attempt
                try:
                    wait_queue_clear(s)
                    wf = build_prompt(
                        template,
                        positive=task["positive"], negative=NEGATIVE,
                        seed=seed, steps=STEPS, cfg=CFG,
                        width=WIDTH, height=HEIGHT,
                        filename_prefix=f"{run_id}/batch_{i:03d}",
                    )
                    t0  = time.time()
                    pid = submit(s, wf)
                    rec["prompt_id"] = pid
                    entry   = wait_for(s, pid, timeout=600)
                    elapsed = time.time() - t0

                    rec["vram_peak_gb"] = round(get_vram_used(s), 2)
                    files = fetch_images(s, entry, out_dir=out_dir)
                    rec.update(status="ok", elapsed=round(elapsed, 2),
                               files=[f.name for f in files])
                    log.info("[%d/%d] ✅ %.1fs  seed=%s  显存 %.1fG",
                             i, len(jobs), elapsed, seed, rec["vram_peak_gb"])
                    break

                except Exception as e:
                    rec["error"] = f"{type(e).__name__}: {e}"[:300]
                    if attempt <= MAX_RETRY:
                        log.warning("[%d/%d] ⚠️ 第 %d 次失败，重试：%s",
                                    i, len(jobs), attempt, rec["error"][:120])
                        time.sleep(3)
                    else:
                        rec["status"] = "failed"
                        log.error("[%d/%d] ❌ 最终失败：%s",
                                  i, len(jobs), rec["error"][:200])

            records.append(rec)

        batch_elapsed = time.time() - batch_t0

    # ---------- 汇总 ----------
    ok = [r for r in records if r["status"] == "ok"]
    times = sorted(r["elapsed"] for r in ok)

    def pct(p):
        if not times:
            return None
        k = min(int(len(times) * p / 100), len(times) - 1)
        return times[k]

    summary = {
        "run_id": run_id,
        "host": socket.gethostname(),
        "comfy_host": COMFY_HOST,
        "device": device,
        "checkpoint": ckpt,
        "resolution": f"{WIDTH}x{HEIGHT}",
        "steps": STEPS, "cfg": CFG,
        "total": len(records),
        "success": len(ok),
        "failed": len(records) - len(ok),
        "success_rate": round(len(ok) / len(records) * 100, 1) if records else 0,
        "batch_elapsed_s": round(batch_elapsed, 1),
        "avg_s": round(statistics.mean(times), 2) if times else None,
        "median_s": round(statistics.median(times), 2) if times else None,
        "min_s": times[0] if times else None,
        "max_s": times[-1] if times else None,
        "p95_s": pct(95),
        "throughput_per_min": round(len(ok) / (batch_elapsed / 60), 2) if batch_elapsed else None,
        "vram_peak_gb": max((r["vram_peak_gb"] or 0) for r in records) if records else None,
    }

    (log_dir / f"bench_{run_id}.json").write_text(
        json.dumps({"summary": summary, "records": records},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    cols = ["index", "status", "elapsed", "seed", "steps", "cfg",
            "vram_peak_gb", "attempts", "prompt", "error"]
    with (log_dir / f"bench_{run_id}.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)

    print("\n" + "=" * 56)
    print(f"  {summary['device']}")
    print("=" * 56)
    for k in ["resolution", "steps", "total", "success", "failed", "success_rate",
              "batch_elapsed_s", "avg_s", "median_s", "min_s", "max_s", "p95_s",
              "throughput_per_min", "vram_peak_gb"]:
        print(f"  {k:22s} {summary[k]}")
    print("=" * 56)
    print(f"  图片: {out_dir}")
    print(f"  指标: logs/bench_{run_id}.json / .csv")


if __name__ == "__main__":
    main()
```

**设计要点（都是运维视角的理由）：**

| 特性 | 理由 |
| --- | --- |
| `wait_queue_clear()` | ComfyUI 串行执行，无脑灌任务只会堆队列，不会更快 |
| 单任务重试 `MAX_RETRY` | 一张失败不该让整批白跑 |
| `filename_prefix=f"{run_id}/..."` | 每批图落在独立子目录，不会互相覆盖 |
| `run_id` 时间戳 | 多轮对比时数据不混 |
| 输出 JSON + CSV | JSON 给程序读（阶段三接 Prometheus），CSV 给人看 |
| 记录 `attempts` | 能看出"一次成功"还是"重试后成功"，稳定性指标 |
| `p95` 而非只有平均 | 平均值掩盖长尾，P95 才是 SLA 语言 |
| `throughput_per_min` | 容量规划的直接输入 |

**⚠️ 百分位的样本量陷阱**：`pct(95)` 的索引是 `int(n × 0.95)`。**n=10 时 index=9 正好是最后一个元素，P95 恒等于 max，没有统计意义**。样本量必须 ≥ 20（因此 `SEEDS` 设了 4 个）。这类"看起来有数字但其实无意义"的指标是技术评审时的常见扣分点。

### 2.5 双机实测数据（2026-09-10，n=20，native + WebSocket 精测）

> **本节数据经历过三次修正**（分配器不一致 → "跨架构"结论被证伪 → 测量方法污染数据），完整修正过程与原因见 `benchmarks-实测数据.md` §0。这里给最终版。

512×512 / 20 steps / CFG 7.0 / euler+normal，SD 1.5：

| 指标 | RTX 5060 8G | Tesla P100 16G | P100 相对 |
| --- | --- | --- | --- |
| 样本量 | 20 | 20 | |
| 成功率 | **100%** | **100%** | 持平 |
| **中位耗时（稳态）** | **1.94 s** | **7.90 s** | 慢 **4.07×** |
| 平均耗时 | 2.14 s（含冷启动） | 7.95 s | |
| 标准差 | 0.724 s | 0.672 s | |
| 最快 / 最慢 | 1.92 / 5.20 s | 7.12 / 9.10 s | |
| P50 / P90 / P95 / P99 | 1.94 / 2.10 / 2.34 / 4.63 | 7.90 / 8.90 / 8.92 / 9.06 | |
| 批量总时长 | **43.1 s** | 160.3 s | |
| 吞吐 | **27.86 张/分** | 7.49 张/分 | 低 **73%** |
| 显存峰值 | 3.22 GB | **2.54 GB** | 少 0.68 GB |

> **为什么看中位数而不是平均数**：两台的平均值都被首张冷启动（含模型加载）拉高。5060 冷启动 5.20s、P100 冷启动 8.9s，**稳态才是 1.94s 和 7.90s**。

**六个关键工程发现：**

**① `cudaMallocAsync` 只在 5060 上有问题，不是跨架构共性**

三机对照矩阵（全部完成 A/B 实验）：

| 机器 | 环境 | 显存 | cudaMallocAsync 影响 |
| --- | --- | --- | --- |
| **RTX 5060** | **Windows** / Blackwell | **8 GB** | ❌ **+2.6s（2.34×）、批量卡死** |
| Tesla P100 | Linux / Pascal | 16 GB | ✅ 零影响（7.79 vs 7.90，差 1.4%） |
| NVIDIA A10 | Linux / Ampere | 24 GB | ✅ 零影响（2.01 vs 2.01，完全相同） |

5060 的 A/B 数据（均为 WS 精测，只差分配器）：

| 指标 | native | cudaMallocAsync | 劣化 |
| --- | --- | --- | --- |
| 中位 | **1.94 s** | 4.54 s | **+2.60 s（2.34×）** |
| P95 | 2.34 s | 5.95 s | +3.61 s |
| 吞吐 | **27.86 张/分** | 10.72 张/分 | **−61.5%** |

> **一个被自己推翻的结论**：本文档早先写过"跨架构共性，Blackwell 与 Pascal 都受影响"，**那是错的**。当时只有 5060 一台机器的样本，P100 从头到尾跑的都是 native（因 cuDNN 那条线早早加了参数），**根本没做过对照实验**——我把"没测过"当成了"测过且有问题"。A10 和 P100 的对照先后证伪了它。

**根因未定**：5060 同时是唯一的 Windows 机器和唯一的 8GB 机器，**两个变量绑定，无法分离**。要分离需要 Linux+8GB 或 Windows+大显存的机器。

> 为什么 n=10 能跑完、n=20 跑不到第 5 张？预留池耗尽是累积过程，**批量规模越大越容易触发**。n=10 恰好在临界点之下——这类"小规模测试通过、上量就炸"的问题，正是压测存在的意义。

**② 测量方法本身会污染数据：任务越快，轮询误差相对越大**

同一台机器、同一个 native 配置，只换等待方式：

| 机器 | 任务时长 | 中位偏差 | **P95 偏差** |
| --- | --- | --- | --- |
| P100 | ~7.9 s | +3.4% | +15% |
| **5060** | **~1.9 s** | +3.6% | **+76%** |

**原理**：轮询的绝对误差固定（≤ 一个周期 ≈2s），所以**任务越快，相对误差越大**。

```
P100  任务 7.9s，误差 ≤2s  →  相对误差  25%
5060  任务 1.9s，误差 ≤2s  →  相对误差 105%   ← 误差比任务本身还长
```

**两个被推翻的旧结论**：
- "P100 尾部三个 10.2s 毛刺"（曾归因温度墙）→ WS 重测 max 只有 9.10s，**那些毛刺从未真实存在**
- "cudaMallocAsync +4 秒开销" → 真实是 **+2.6 秒**，多出的 1.4 秒是轮询量化

> **对阶段三的硬约束**：**采样间隔必须与被测对象的时间尺度匹配。** 5060 单张只要 1.94 秒，**Prometheus 若设 15 秒抓取间隔，根本采不到单张粒度**。要监控单张耗时必须应用侧主动上报（`/metrics` 或网关埋点），不能靠外部轮询。

**③ 架构代差 4.07 倍**

P100（2016 Pascal）无 Tensor Core，fp16 只能走 CUDA 核心；cuDNN 被禁用后卷积回退 PyTorch 原生实现再损失 10~20%；Pascal 到 Blackwell 隔了 4 代。

> **分工结论**：P100 的价值在**显存容量**（16G 支持 SDXL LoRA 训练、大 batch），**不在速度**。这直接决定阶段四把 SDXL 训练放 P100、把交互式工作流调试放笔记本。

**④ P100 存在真实的渐进式降速（约 13%）**

| 轮次 | 前 10 张均值 | 后 10 张均值 | 变化 |
| --- | --- | --- | --- |
| cudaMallocAsync + WS | 7.37 s | 8.43 s | +14% |
| native + WS | 7.41 s | 8.35 s | +13% |

**与分配器无关**（两轮一致），**与测量方法无关**（WS 精测下依然存在）。最可能是**温度墙**——P100 PCIe 250W 被动散热，连续满载 3 分钟后降频。

> **对阶段四的意义**：LoRA 训练要连续满载几十分钟，13% 降频会显著拉长训练时间。散热改造的投入产出比值得评估。验证方法：批量跑的同时 `nvidia-smi --query-gpu=temperature.gpu,clocks.sm --format=csv -l 2`。

**⑤ 显存差异来自 CLIP 放置位置，不只是分配器**

```
P100 日志：CLIP/text encoder model load device: cpu, offload device: cpu
5060     ：CLIP 在 GPU
```

P100 把 text encoder 放到了 CPU，省显存但增加 CPU 开销。**跨机器比较显存必须同时核对分配器类型和模型放置策略**，`vram_used` 单个数字没有可比性。

**⑥ 批量调度开销可忽略，瓶颈 100% 在 GPU**

5060：42.9s 纯计算 vs 43.1s 实际，总开销 0.2s（0.4%）。验证了 `wait_queue_clear()` 串行控制设计正确：ComfyUI 单实例串行执行，并发提交不提升吞吐。**提吞吐只有加卡或加实例两条路。**

**告警阈值建议**（供阶段三 Grafana 使用，基于 WS 精测）：

| 节点 | 指标 | 阈值 | 依据 |
| --- | --- | --- | --- |
| 5060 | 单张耗时 | > 3 s | P95 (2.34s) × 1.3 |
| 5060 | 单张耗时（严重） | > 5 s | 中位数 × 2.6，接近 cudaMallocAsync 劣化水平 |
| P100 | 单张耗时 | > 12 s | P95 (8.92s) × 1.3，留渐进降速余量 |
| P100 | 单张耗时（严重） | > 18 s | 中位数 × 2.3 |
| P100 | GPU 温度 | > 80 °C | 超过可能触发降频 |
| 两端 | 显存占用 | > 85% 持续 5 min | |
| 两端 | 任务失败率 | > 5%（5 分钟窗口） | |
| 两端 | 任务"消失"次数 | > 0 | 分配器/驱动异常的早期信号 |

完整数据（**18 轮 / 340 样本** + 7 轮 LLM）、修正过程与局限声明见 `benchmarks/实测数据.md`。

### 2.6 WebSocket 实时进度（已完成）

REST 轮询只能知道"做完没"，最小感知粒度 2 秒。WebSocket 能拿到**步级进度**。

| 维度 | REST 轮询 | WebSocket |
| --- | --- | --- |
| 感知粒度 | 只知道"做完没"，最小 2 秒 | **步级**（第 7/20 步） |
| 延迟 | 最长 = 轮询间隔 | < 100 ms |
| 20 张图的请求数 | ~200 次 HTTP | 1 条长连接 |
| 错误信息 | 需查 history | 推送 `execution_error`，含 traceback |

**连接**：`ws://host:port/ws?clientId=<uuid>`，**`client_id` 必须与 `POST /prompt` 时提交的一致**，否则收不到消息。

```bash
pip install websocket-client      # 注意不是 websockets
```

**消息类型**：`status` / `execution_start` / `execution_cached` / `executing` / `progress` / `executed` / `execution_success` / `execution_error` / `execution_interrupted`，外加**二进制预览图帧**。

**四个必须处理的细节**：

1. **二进制帧**：预览图以 binary frame 推送，`json.loads` 会炸，必须先 `isinstance(raw, bytes)` 跳过
2. **完成信号有两种**：新版发 `execution_success`，老版发 `executing` + `node: null`，**两个都要处理**
3. **WS 说完成后仍要查 `/history`**：WS 只通知"完成了"，文件清单要从 history 取，且 history 写入**略滞后于 WS 通知**，需重试 5 次
4. **⚠️ 必须加 `/history` 兜底轮询**（V1.5 实测新增）：**WS 完成信号会漏**

**第 4 条是实测踩出来的坑**。首版实现只依赖 WS 消息判定完成，结果在 P100 批量测试中第 5、7 张任务卡死 60 秒后超时——但 `/interrupt` 后重试**只用 0.2 秒就出图**，说明任务早已完成，图就在 `/history` 里，只是 WS 没把完成信号送到。

**漏消息的两个可能原因**：

- `execution_start` 被上一个任务的 recv 循环消费掉，`started` 标志一直是 `False`，导致 `executing + node:null` 这条完成路径失效
- 部分 ComfyUI 版本不发 `execution_success`

**修法**（`comfy_ws.py` 已实现）：

```python
next_poll = time.time() + poll_fallback      # 默认 5 秒

while time.time() < deadline:
    # 兜底：定期查 history，不依赖 WS 的完成信号
    if time.time() >= next_poll:
        next_poll = time.time() + poll_fallback
        hist = session.get(f"{COMFY_HOST}/history/{prompt_id}").json()
        if prompt_id in hist:
            return hist[prompt_id]           # WS 漏了，history 兜住

    raw = self.ws.recv()                     # WS 负责实时进度
    ...
```

同时把完成判定放宽：`executing + node:null` 不再要求 `started`（消息已过 `prompt_id` 过滤，本身就足以判定），`progress` / `execution_cached` 也置 `started`。

> **设计原则**：**WS 负责实时进度，完成判定必须有 REST 兜底。** 推送式协议在生产环境里"消息一定送达"是不能假设的。加了兜底之后，同样配置从"第 5 张卡死"变成 20/20 全过。

**用法**（实现见 `Scripts/comfy_ws.py`）：

```python
with make_session() as s, ComfyWS() as ws:
    pid = submit(s, wf, client_id=ws.client_id)    # client_id 必须一致
    entry = ws.wait(s, pid, timeout=120)
```

批量脚本用环境变量开关，**任何失败自动降级到轮询**——WS 是增强，不是依赖：

```bat
set COMFY_USE_WS=1
python comfy_batch_gen.py
```

汇总里会多一行 `wait_mode: websocket`，方便与轮询模式做 A/B 对照。

> **⚠️ WS 不只是"更好看"，它是准确测量的前提**：轮询的 2 秒量化误差会让快任务的 P95 虚高 76%（见 2.5 发现②）。**做性能基准测试必须用 WS，不能用轮询。**

### 2.7 待补充（加分项）

- [x] 样本量提到 20+，让 P95 可信
- [x] 统一分配器为 native，公平对比
- [x] WebSocket 步级进度 + history 兜底
- [x] 《ComfyUI API 开发手册.md》
- [x] 三机 cudaMallocAsync A/B 对照（证伪"跨架构"结论）
- [x] 双机 WS 精确基线（1.94s / 7.90s）
- [ ] **P100 渐进降速定因**：批量跑的同时采温度/频率曲线
  ```bash
  nvidia-smi --query-gpu=timestamp,temperature.gpu,clocks.sm,power.draw \
    --format=csv -l 2 | tee /opt/AIGC/logs/thermal_$(date +%Y%m%d_%H%M%S).csv
  ```
- [ ] 冷启动单独测量（与稳态分开统计）
- [ ] 768×768 / 1024×1024 分辨率对比
- [ ] SDXL 推理（P100 显存优势才会体现）
- [ ] 队列压测：一次提交 50 个任务，观察排队与内存行为
- [ ] 在 Ubuntu 本机直跑，隔离 SSH 隧道开销
- [ ] img2img / ControlNet 流程（`upload_image()` 已就绪）

> **工程思维**：把 ComfyUI 当成一个黑盒推理服务，上层用 Python 做任务调度、批量处理、监控告警、容量规划。这就是运维经验能直接迁移过来的地方，也是阶段五 FastAPI 网关的雏形。

### 阶段产出物

1. ✅ `comfy_client/` 共享包（REST 客户端：标题定位 / 前置校验 / 重试 / 超时 / 任务消失检测 / 结构化日志）
2. ✅ `comfy_ws.py`（WebSocket 客户端：步级进度 + history 兜底 + 自动降级）
3. ✅ `comfy_batch_gen.py`（批量 + 指标采集 + 自适应超时 + `/interrupt` + JSON/CSV 双输出）
4. ✅ `inspect_workflow.py`（工作流结构检查工具）
5. ✅ `workflows/base_workflow_api.json`（带 `_meta.title` 约定）
6. ✅ `benchmarks-实测数据.md`（**18 轮 340 样本 + 7 轮 LLM** + 修正记录 + 工程发现 + 局限声明）
7. ✅ `ComfyUI-API开发手册.md`（8 章：格式对比 / 接口速查 / WS 协议 / 生产实践清单 / 踩坑记录）

**阶段二于 2026-09-10 完成。**

---

## 阶段三：Docker 容器化与 GPU 可观测性（第 7~9 周）

### 阶段目标

将 ComfyUI 打包为可一键交付的镜像；**建立 GPU 可观测性体系**；用监控数据验证阶段二遗留的性能假设。

> 这一阶段是运维背景最值钱的差异化所在。大多数 AIGC 从业者只会"跑起来"，能同时交付"跑起来 + 看得见 + 可告警"的人很少。

### 3.0 ⚠️ 国内网络适配清单（动手前必读）

**容器是干净环境，不继承宿主机的任何配置。** 阶段一 0.9 节为宿主机做的所有网络适配，在 Dockerfile 里**一样不少地要重新声明**。

本项目实测中，这一条被漏了 **4 次**，每次表现不同：

| 源 | 漏掉的症状 | 解法 |
| --- | --- | --- |
| **apt** | 卡在 `apt-get update` 十几分钟无输出 | `sed` 换清华 mirrors |
| **pip** | `Errno 101 Network is unreachable` | `ENV PIP_INDEX_URL=...tuna...` |
| **HF** | 运行时下模型失败 | `ENV HF_ENDPOINT=https://hf-mirror.com` |
| **GitHub** | `curl 16 HTTP2 framing error`；gh-proxy 也会在 822 秒后 `early EOF` | **改用宿主机本地 tar** |
| **torch wheel** | 容器内 `download.pytorch.org` 实测仅 **~250 B/s** | **宿主机预下载到 `wheels/`** |

> **"能连上"和"能用"是两回事**：`curl -sI` 返回 `HTTP/2 200` 只证明 TCP 能连、TLS 能握手，不代表带宽可用。测连通性要测**实际吞吐**：
> ```bash
> curl -o /dev/null -w 'speed: %{speed_download} B/s\n' --max-time 20 <url>
> ```

**更深一层的原则**：后两项（GitHub、torch wheel）改成本地文件，不只是为了绕开网络，而是因为**"构建依赖外部网络状态"本身就违反可复现原则**——同一个 Dockerfile 今天能 build、明天代理挂了就 build 不出来。

### 3.1 Ubuntu 端：Docker + GPU 运行时

Docker 若已安装（本项目实测 29.4.3），只需验证 GPU 直通：

```bash
# 1. NVIDIA Container Toolkit 装了吗（装 Docker 不会自动带上）
which nvidia-ctk nvidia-container-runtime

# 2. GPU 直通验证 —— 这一条通过就说明基础设施就绪
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

未安装 Toolkit 时：

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker    # 这一步必须显式执行
sudo systemctl restart docker
```

日志轮转（避免撑爆磁盘）：

```json
// /etc/docker/daemon.json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "5" },
  "default-shm-size": "2gb"
}
```

**⚠️ Proxmox 透传环境的能力边界**（~~本项目实测~~ **09-18 已修正，原结论错误**）：

本方案的 Ubuntu 是 **Proxmox VE 上的虚拟机，P100 通过 PCIe 透传**。阶段三时曾实测：

```bash
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
# 返回空表 —— 进程明明在用 GPU，但拿不到归属信息
```

并据此写下"透传拿不到 per-process 归属"的结论。**09-18 阶段五实测证伪**（llama-server 11236 MiB + venv python 254 MiB 同时占卡，归属信息完整列出）。最可能的原因：`query-compute-apps` **只列 compute（C 型）进程**，阶段三测试时刻 GPU 上可能只有 Xorg（G 型），表头-only 的输出被误读为"不支持"。

| 指标类型 | 透传虚拟机里能拿到吗 |
| --- | --- |
| GPU 利用率 / 显存 / 温度 / 功耗 / 频率 | ✅ 能 |
| **per-process 显存归属（nvidia-smi 现场查询）** | ✅ **能**（09-18 修正） |
| per-process **时间序列**（Grafana/dcgm） | ❌ 仍不能（P100 无 DCP 指标，`FB_USED` 整卡口径） |
| DCGM 的 DCP profiling 指标 | ❌ 拿不到（另有 Pascal 架构原因，见 3.4） |

**Grafana 面板做不到"哪个进程吃了多少显存"的时间序列下钻**（dcgm 层面限制仍在），但**现场排查可以做**：`nvidia-smi --query-compute-apps=...` 或 `fuser -v /dev/nvidia*`。企业环境里 GPU 虚拟化很常见，**能说清"哪一层能拿到、哪一层拿不到"反而是加分项**。

### 3.2 ComfyUI Dockerfile

完整文件见 `docker/Dockerfile.p100`。核心设计决策：

#### 基础镜像用 `base` 而非 `cudnn-runtime`，省 3.5 GB

| 基础镜像 | 大小 | 说明 |
| --- | --- | --- |
| `12.4.1-base` | ~250 MB | 只有 CUDA driver stub |
| `12.4.1-runtime` | ~2 GB | 加 CUDA runtime 库 |
| `12.4.1-cudnn-runtime` | ~3.5 GB | 再加 cuDNN 9 |

**PyTorch 的 pip wheel 自带 CUDA runtime 和 cuDNN 库**（`nvidia-*-cu12` 依赖包），基础镜像那套是重复的。而 P100 上我们主动禁用 cuDNN，更不需要它。

#### `--mount=type=bind` 而非 `COPY`，再省 2.9 GB

```dockerfile
# ❌ 错误写法：Docker 层不可变，rm 只是加 whiteout 标记，
#    那 2.9 GB 依然打包进镜像
COPY wheels/ /tmp/wheels/
RUN pip install ... && rm -rf /tmp/wheels

# ✅ 正确写法：临时挂载，构建结束自动消失，连 rm 都不用写
RUN --mount=type=bind,source=wheels,target=/tmp/wheels \
    pip install --no-index --find-links=/tmp/wheels ...
```

**实测效果**：

| | COPY 写法 | bind mount 写法 |
| --- | --- | --- |
| CONTENT SIZE | 8.27 GB | **5.07 GB** |
| DISK USAGE | 20.7 GB | **5.07 GB** |

#### 源码走本地 tar，不在容器内 clone

```bash
cd /opt/AIGC/ComfyUI-server
git rev-parse HEAD > /opt/AIGC/docker_workspace/comfyui_commit.txt

# ⚠️ --exclude 必须用 ./ 锚定顶层！见下方警告
tar czf /opt/AIGC/docker_workspace/comfyui-src.tar.gz \
    -C /opt/AIGC/ComfyUI-server \
    --exclude=./venv --exclude=./models --exclude=./output \
    --exclude=./input --exclude=./user --exclude=./.git \
    --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='*.log' --exclude='*.db' \
    .
```

> ### ⚠️ tar `--exclude` 的致命陷阱
>
> **`--exclude=models` 是全局模式匹配，会命中任意层级的路径**，包括 ComfyUI 源码里的 `comfy/ldm/models/`。
>
> 本项目实测踩到这个坑：打包出的 tar 缺了 `comfy/ldm/models/`，构建期**完全静默**，容器启动才炸：
> ```
> ModuleNotFoundError: No module named 'comfy.ldm.models'
> ```
>
> **正确写法**：顶层目录用 `./` 锚定（`--exclude=./models`）；`__pycache__` / `*.pyc` 这类**故意**不加 `./`，本来就该全层级排除。
>
> **打包后必须验证**：
> ```bash
> for d in comfy/ldm/models comfy/ldm/modules comfy_extras app utils; do
>   n=$(tar tzf comfyui-src.tar.gz | grep -c "^\./$d/")
>   echo "$d: $n $([ "$n" -gt 0 ] && echo ✅ || echo ❌)"
> done
> ```

#### 构建期自检要覆盖三个维度

```dockerfile
RUN ./venv/bin/python -c "\
import pathlib, torch; \
assert torch.__version__.startswith('2.6.0+cu124'); \
assert 'cudnn.enabled = False' in pathlib.Path('main.py').read_text(); \
import requests, aiohttp, safetensors, transformers, PIL, yaml, torchsde, einops; \
print('构建自检: 依赖 OK')" \
 && for d in comfy/ldm/models comfy/ldm/modules comfy_extras app utils; do \
      [ -d "$d" ] || { echo "源码缺失: $d"; exit 1; }; \
    done \
 && echo "构建自检: 源码完整性 OK"
```

| 维度 | 检查什么 | 挡住哪类问题 |
| --- | --- | --- |
| 版本 | torch 是 2.6.0+cu124 | requirements.txt 覆盖 torch |
| 补丁 | cuDNN 补丁在 main.py 里 | 补丁没打上 |
| 依赖 | 关键第三方包能 import | 隐形依赖缺失（如 requests） |
| **源码** | **关键目录存在** | **tar 误排除**（构建期静默，运行时才炸） |

> **⚠️ 自检不要用 `import server`**：ComfyUI 依赖 `main.py` 的导入顺序预热 `sys.modules`，直接 `import server` 会让 `comfy/utils.py` 抢占顶层 `utils` 名字，产生 `'utils' is not a package` 这种**真实启动时根本不存在的假错误**。本项目为此浪费了三轮诊断。
>
> **诊断要用真实入口**——`import server` ≠ `python main.py`。

#### 单独安装 requests

```dockerfile
&& ./venv/bin/pip install -r requirements.txt \
&& ./venv/bin/pip install requests \
```

ComfyUI 的 `requirements.txt` **漏声明了 `requests`**（`app/frontend_management.py:16` 用到）。裸机 venv 因为装过 `huggingface_hub` 等顺带有了，容器干净环境才暴露。

> **这正是容器化的价值**：长期使用的开发环境会累积大量"意外获得"的间接依赖，这些依赖没被任何 requirements 文件声明，但代码确实在用。**裸机上"能跑"不等于"依赖完整"——换台新机器部署照样会炸，只是容器让你提前发现了。**

#### 构建

```bash
cd /opt/AIGC/docker_workspace
ls -lh Dockerfile comfyui-src.tar.gz comfyui_commit.txt && du -sh wheels/

# ⚠️ 不要加 | tail —— 管道缓冲会让你全程看不到进度
docker build --progress=plain -t aigc-comfyui:2.6.0-cu124 .
# 要留档用 tee（不缓冲）：
#   docker build --progress=plain -t ... . 2>&1 | tee build.log
```

预期输出：

```
源码 commit: 22e40d2ace0f53da025b3a41cbe4b664ef807097
cuDNN 补丁已存在（来自宿主机副本），跳过
构建自检: 依赖 OK 2.6.0+cu124
构建自检: 源码完整性 OK
```

**构建卡住时的诊断顺序**（先测清楚再解释，别急着下结论）：

```bash
# 1. 在跑哪一步
ps aux | grep -E "docker build|apt|pip" | grep -v grep

# 2. 网络在动吗
R1=$(grep ens18 /proc/net/dev | awk '{print $2}'); sleep 5
R2=$(grep ens18 /proc/net/dev | awk '{print $2}')
echo "5 秒收到 $(( (R2-R1)/1024 )) KB"

# 3. 磁盘在写吗（导出镜像层阶段网络为 0 但磁盘狂写，属正常）
D1=$(awk '/vda /{print $10}' /proc/diskstats); sleep 5
D2=$(awk '/vda /{print $10}' /proc/diskstats)
echo "5 秒写入 $(( (D2-D1)*512/1024/1024 )) MB"

# 4. CPU（压缩镜像层时 dockerd 会占满一核）
top -bn1 | head -12
```

### 3.3 docker-compose：ComfyUI + 监控栈

#### 端口规划

宿主机已占用的端口要先查清楚：

```bash
ss -tlnp | grep -E ':(3000|8189|9090|9400)\b' && echo "⚠️ 冲突" || echo "✅ 可用"
```

| 服务 | 端口 | 说明 |
| --- | --- | --- |
| ComfyUI 裸机 systemd | 8188 | **保留**，用于容器化性能对照 |
| llama-server | 8000 | 已占用 |
| **ComfyUI 容器** | **8189** | 新增 |
| dcgm-exporter | 9400 | |
| Prometheus | 9090 | |
| Grafana | 3000 | |

> **为什么不停掉裸机版**：留着它，容器版跑同一份 benchmark 就能量化"容器化有多少性能损耗"。很多人以为容器化零开销，实测一下才有说服力。

Windows SSH config 补三条：

```
    LocalForward 8289 127.0.0.1:8189    # 容器版 ComfyUI
    LocalForward 3100 127.0.0.1:3000    # Grafana
    LocalForward 9190 127.0.0.1:9090    # Prometheus
```

#### docker-compose.yml

```yaml
services:
  comfyui:
    image: aigc-comfyui:2.6.0-cu124
    container_name: comfyui-container
    restart: unless-stopped
    ports:
      - "127.0.0.1:8189:8188"
    shm_size: "2gb"                    # 不加会在大 tensor 操作时报 bus error
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
        limits:
          memory: 12G                  # 24G 总量，给裸机 ComfyUI 和 llama-server 留余地
    volumes:
      - /opt/AIGC/model_storage:/app/ComfyUI/models
      - /opt/AIGC/output_container:/app/ComfyUI/output
      - /opt/AIGC/input:/app/ComfyUI/input
      - /opt/AIGC/custom_nodes:/app/ComfyUI/custom_nodes
    logging:
      driver: json-file
      options: { max-size: "50m", max-file: "5" }

  dcgm-exporter:
    # 官方发布路径就是 nvidia/k8s/dcgm-exporter —— 那个 k8s 是路径不是变体。
    # 非 K8s 环境下正常工作，kubelet 关联功能只在加 -k 参数时才启用。
    # （早期文档误以为存在"非 k8s 版"，实测 nvcr.io/nvidia/dcgm-exporter 是 Access Denied）
    image: nvcr.io/nvidia/k8s/dcgm-exporter:3.3.5-3.4.0-ubuntu22.04
    container_name: dcgm-exporter
    restart: unless-stopped
    cap_add: [SYS_ADMIN]               # DCGM 读硬件计数器需要
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
        limits:
          memory: 256M
    ports:
      - "127.0.0.1:9400:9400"
    logging:
      driver: json-file
      options: { max-size: "20m", max-file: "3" }

  prometheus:
    image: prom/prometheus:v2.53.0
    container_name: prometheus
    restart: unless-stopped
    user: "nobody"
    ports:
      - "127.0.0.1:9090:9090"
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./monitoring/alert_rules.yml:/etc/prometheus/alert_rules.yml:ro
      - prometheus-data:/prometheus
    command:
      - "--config.file=/etc/prometheus/prometheus.yml"
      - "--storage.tsdb.retention.time=30d"
      - "--web.enable-lifecycle"       # 支持 curl -XPOST /-/reload 热加载
    deploy:
      resources:
        limits: { memory: 1G }
    logging:
      driver: json-file
      options: { max-size: "20m", max-file: "3" }

  grafana:
    image: grafana/grafana:11.1.0
    container_name: grafana
    restart: unless-stopped
    ports:
      - "127.0.0.1:3000:3000"
    environment:
      # :? 语法 —— 没设就拒绝启动，比默认 admin 安全
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD:?请在 .env 里设置 GRAFANA_PASSWORD}
      GF_USERS_ALLOW_SIGN_UP: "false"
    volumes:
      - grafana-data:/var/lib/grafana
      - ./monitoring/grafana/provisioning:/etc/grafana/provisioning:ro
    deploy:
      resources:
        limits: { memory: 512M }
    depends_on: [prometheus]
    logging:
      driver: json-file
      options: { max-size: "20m", max-file: "3" }

volumes:
  prometheus-data:
  grafana-data:
```

`.env`（密码不进版本控制）：

```bash
cat > /opt/AIGC/docker_workspace/.env <<'EOF'
GRAFANA_PASSWORD=改成你自己的密码
EOF
chmod 600 /opt/AIGC/docker_workspace/.env
echo ".env" >> /opt/AIGC/docker_workspace/.dockerignore
```

#### monitoring/prometheus.yml

```yaml
global:
  # 5 秒采样：GPU 温度/频率变化是秒级的，15s 会抹平降频过程。
  # ⚠️ 但这个间隔采不到单张出图粒度（P100 7.9s / 5060 1.9s），
  #    任务级指标必须应用侧主动上报（阶段二 §3.2 的教训）。
  scrape_interval: 5s
  evaluation_interval: 15s

rule_files:
  - /etc/prometheus/alert_rules.yml

scrape_configs:
  - job_name: dcgm
    static_configs:
      - targets: ["dcgm-exporter:9400"]
        labels: { node: p100 }
  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]
```

#### monitoring/alert_rules.yml

阈值全部来自阶段二 WebSocket 精测，不是拍脑袋：

```yaml
groups:
  - name: gpu_p100
    rules:
      # 阶段二观察到 20 连发后半段降速 13%，怀疑温度墙。
      # P100 PCIe 250W 被动散热，降频阈值通常在 80~85°C。
      - alert: P100HighTemp
        expr: DCGM_FI_DEV_GPU_TEMP > 80
        for: 2m
        labels: { severity: warning }
        annotations:
          summary: "P100 温度 {{ $value }}°C 超过 80，可能触发降频"

      # P100 boost 上限 1329 MHz，满载掉到 1100 以下说明在降频
      - alert: P100ClockThrottle
        expr: DCGM_FI_DEV_SM_CLOCK < 1100 and DCGM_FI_DEV_GPU_UTIL > 80
        for: 2m
        labels: { severity: warning }
        annotations:
          summary: "P100 满载但 SM 频率仅 {{ $value }} MHz，疑似降频"

      - alert: GPUMemoryHigh
        expr: DCGM_FI_DEV_FB_USED / (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE) > 0.85
        for: 5m
        labels: { severity: warning }
        annotations:
          summary: "GPU 显存占用超过 85%"

      # XID 是 GPU 硬件/驱动级错误，任何非零增长都要立刻看
      - alert: GPUXidError
        expr: increase(DCGM_FI_DEV_XID_ERRORS[5m]) > 0
        labels: { severity: critical }
        annotations:
          summary: "检测到 GPU XID 错误，可能是硬件或驱动问题"

      # 透传环境下 PCIe 链路质量值得盯
      - alert: PCIeReplayHigh
        expr: increase(DCGM_FI_DEV_PCIE_REPLAY_COUNTER[10m]) > 100
        labels: { severity: warning }
        annotations:
          summary: "PCIe replay 计数快速增长，链路可能有问题"

      - alert: DCGMExporterDown
        expr: up{job="dcgm"} == 0
        for: 1m
        labels: { severity: critical }
        annotations:
          summary: "dcgm-exporter 不可达，GPU 监控已失效"
```

#### monitoring/grafana/provisioning/datasources/prometheus.yml

```yaml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: false
```

#### 启动与验证

```bash
cd /opt/AIGC/docker_workspace
mkdir -p /opt/AIGC/{output_container,input,custom_nodes}
docker compose config >/dev/null && echo "✅ compose 语法 OK"
docker compose up -d
sleep 30
docker compose ps
```

```bash
curl -s localhost:9400/metrics | grep -c "^DCGM_FI"      # 应 16
curl -s localhost:9090/-/healthy
curl -s localhost:3000/api/health
curl -s localhost:9090/api/v1/targets | python3 -c "
import sys,json
for t in json.load(sys.stdin)['data']['activeTargets']:
    print(t['labels']['job'], t['health'])"
curl -s localhost:8189/system_stats | python3 -m json.tool | head -20
```

**实测结果（2026-09-11）**：4 个容器全部健康，Prometheus 抓 dcgm `up`，16 个指标可用。

### 3.4 P100 上的 DCGM 指标实况

dcgm-exporter 启动日志里这一行是预期内的：

```
Not collecting DCP metrics: This request is serviced by a module of DCGM
that is not currently loaded
Falling back to metric file '/etc/dcgm-exporter/default-counters.csv'
```

**DCP（profiling）指标需要 Volta+，Pascal 拿不到。** 自动回退到基础计数器集。

#### 实测可用的 16 个指标

| 指标 | 实测值（空闲） | 用途 |
| --- | --- | --- |
| `DCGM_FI_DEV_SM_CLOCK` | 1189 MHz | ⭐ **验证降速假设的关键**（boost 上限 1329） |
| `DCGM_FI_DEV_GPU_TEMP` | 47 °C | ⭐ **同上** |
| `DCGM_FI_DEV_POWER_USAGE` | 35.6 W | 上限 250 W |
| `DCGM_FI_DEV_GPU_UTIL` | 0 % | |
| `DCGM_FI_DEV_FB_USED` / `FB_FREE` | 260 / 16015 MB | 与 `nvidia-smi` 完全一致 |
| `DCGM_FI_DEV_MEM_CLOCK` | 715 MHz | |
| `DCGM_FI_DEV_MEMORY_TEMP` | — | HBM2 温度，P100 独有 |
| `DCGM_FI_DEV_MEM_COPY_UTIL` | — | 显存带宽占用 |
| `DCGM_FI_DEV_XID_ERRORS` | — | **GPU 硬件错误计数，重要告警项** |
| `DCGM_FI_DEV_PCIE_REPLAY_COUNTER` | — | PCIe 链路质量（透传环境值得盯） |

**在 P100 上无效的 4 个**（面板里不要画）：

| 指标 | 为什么无效 |
| --- | --- |
| `DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL` | PCIe 版无 NVLink |
| `DCGM_FI_DEV_VGPU_LICENSE_STATUS` | 非 vGPU 场景 |
| `DCGM_FI_DEV_ENC_UTIL` / `DEC_UTIL` | P100 是计算卡，无 NVENC |

> **官方 DCGM 面板（Dashboard ID 12239）在 P100 上会有一半图是空的**——它是给 A100/H100 设计的，大量依赖 DCP 指标。**知道哪个图为什么空，比有一个好看但半空的面板更重要。**

#### dcgm 在这台机器上的增量价值有限

| | dcgm-exporter | nvidia_gpu_exporter |
| --- | --- | --- |
| 指标丰富度 | 高，**但 Pascal 拿不到 DCP** | 中（nvidia-smi 能给的都有） |
| 镜像拉取 | nvcr.io，国内不稳 | Docker Hub |
| Pascal 兼容 | 部分指标缺失 | ✅ 完全可用 |
| 简历价值 | 企业标配，更有说服力 | 一般 |

四个核心指标（利用率/显存/温度/频率）两者都能给，**而 P100 拿不到的那些 DCP 指标恰恰是 dcgm 相对 nvidia-smi 的主要优势**。这台机器上 dcgm 的技术增量被架构限制吃掉了，但方案文档和简历里它更有分量。

### 3.5 Windows 端：本地镜像调试

1. 安装 Docker Desktop，开启 WSL2 后端与 GPU 支持
2. **不要复用 P100 的镜像** —— 5060 需要 cu128。单独维护 `Dockerfile.win`，基础镜像 `nvidia/cuda:12.8.x-base-ubuntu22.04`，torch 用 cu128
3. 两个参数的适用范围**不同**，别一起抄：
   - `--disable-xformers`：Windows 端**不需要**（sm_120 支持），P100 必须
   - `--disable-cuda-malloc`：5060 **必须**（+2.6s 劣化 + 批量卡死），P100/A10 加不加都一样
   - cuDNN 补丁：Windows 端**不需要**
4. **Docker Desktop 商用授权**：公司 > 250 员工或年收入 > 1000 万美元需付费订阅
5. 8G 显存跑容器还要额外开销，本地只做构建验证，不做性能测试

### 3.6 容器化运维要点

- **镜像 tag 带版本信息**：`aigc-comfyui:2.6.0-cu124`，一眼看出 torch 与 CUDA 版本
- **commit 可追溯**：镜像内 `COMMIT` 文件，`docker run --rm --entrypoint cat <img> COMMIT`
- **资源限制**：compose 里限内存；GPU 只能整卡分配（P100 无 MIG）
- **日志轮转**：已配 `max-size` + `max-file`
- **健康检查**：`HEALTHCHECK` + `start-period=120s`（ComfyUI 启动慢）
- **备份**：`model_storage`、`output`、compose 与 monitoring 配置纳入定期备份

#### ⚠️ 依赖版本仍未锁定

`requirements.txt` 用的是浮动版本约束，**同一个 commit 隔几天构建会得到不同依赖树**（实测 transformers 5.16.1 → 5.17.0）。现在"pin 了 commit"只锁住了 ComfyUI 自己的代码，依赖树还在漂。

```bash
# 镜像跑通后立刻锁版本
docker run --rm --entrypoint ./venv/bin/pip aigc-comfyui:2.6.0-cu124 freeze \
  | grep -viE "^(torch|torchvision|torchaudio|nvidia-|triton)==" \
  > /opt/AIGC/docker_workspace/requirements.lock
```

然后把 `-r requirements.txt` 换成 bind mount 挂进来的 `requirements.lock`。**到那时这个镜像才算真正可复现。**

### 3.7 待办（下一步）

- [ ] **容器看不到模型**：`/object_info/CheckpointLoaderSimple` 返回 `[]`。容器直接挂 `models/`，裸机走 `extra_model_paths.yaml`，两者路径结构需对齐
- [ ] ⭐ **闭环实验：验证 P100 降速假设** —— 跑 20 张批量，同步看 Grafana 的 `GPU_TEMP` + `SM_CLOCK` 曲线。这是本阶段最有价值的产出：**阶段二靠推测提出的假设，阶段三用监控数据证实或否定**
- [ ] **容器化性能损耗对照**：同一份 `comfy_batch_gen.py`，`COMFY_HOST` 分别指 8188（裸机）/ 8289（容器），跑 20 张看差多少
- [ ] Grafana 面板：基于实测可用的 16 个指标自定义，不照抄 12239
- [ ] 锁定 `requirements.lock`
- [ ] 镜像再瘦身（删 `*.a` / `__pycache__` / `torch/test`，可省 1~2 GB；**别删 `torch/include`**，阶段四编译 CUDA 扩展要用）
- [ ] `Dockerfile.win`（Windows 端 cu128 版本）

### 阶段产出物

1. ✅ `docker/Dockerfile.p100`（P100 适配 + 国内网络适配 + 四维构建自检）
2. ✅ `docker-compose.yml`（ComfyUI + dcgm + Prometheus + Grafana）
3. ✅ `monitoring/prometheus.yml` + `alert_rules.yml`（6 条告警，阈值来自实测）
4. ✅ `monitoring/grafana/provisioning/`（datasource 自动配置）
5. ⏳ Grafana 面板截图 + 降速定因报告
6. ⏳ 《ComfyUI 容器化部署交付手册.md》
7. ⏳ 《GPU 监控与告警方案.md》

---

## 阶段四：LoRA 训练工程化（第 10~13 周）

### 阶段目标

在 P100 上部署 Kohya_ss；形成标准化数据集制作、参数配置、训练监控、效果验证的完整流程，覆盖 **SD 1.5 与 SDXL**。

> **P100 三条铁律**：① 只能用 fp16，任何 bf16 都会失败；② 没有 FP8；③ 没有 Flash Attention，xformers 也用不了，注意力用 SDPA。

### 4.1 Kohya_ss 部署

```bash
cd /opt/AIGC
mkdir -p kohya_lora_train && cd kohya_lora_train
git clone https://github.com/bmaltais/kohya_ss.git .
git checkout <已验证的 commit SHA>
echo "<commit SHA>  $(date +%F)" > /opt/AIGC/docs/kohya_version.txt

python3.10 -m venv venv
source venv/bin/activate
pip install --upgrade pip

# 先装 torch（与 ComfyUI 同版本，复用 pip 缓存）
pip install torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0 \
    --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt
```

**Pascal 兼容性处理：**

- **`bitsandbytes`（AdamW8bit 依赖）在 sm_60 上需版本验证**：
  - 0.41.0 之前：对 sm_60 支持缺失
  - 0.42.0~0.43.x：恢复 sm_60 基础算子，但 8bit 量化算子不稳定
  - 0.44.x：sm_60 兼容性最好，但仍建议验证
  - **统一验证命令**：
    ```bash
    python -c "import bitsandbytes as bnb; import torch; \
    x = torch.randn(64, 64, device='cuda', dtype=torch.float16); \
    opt = bnb.optim.AdamW8bit([torch.nn.Parameter(x)], lr=1e-4); \
    opt.step(); print('bitsandbytes AdamW8bit OK on sm_60')"
    ```
  - 失败就改用 `AdamW` 或 `Lion`（Lion 不依赖 bitsandbytes）
- 启动 GUI 时加 `--listen 127.0.0.1`，训练配置里**关闭 `mem_eff_attn` 与 Flash Attention**，注意力实现选 **sdpa**
- 任何模板里出现 `bf16` 一律改成 `fp16`

**kohya_gui.py 启动方式（与 commit 强相关）：**

不同 commit 下 `kohya_gui.py` 的参数支持差异较大。**先看 `kohya_gui.py --help` 的实际输出**：

```bash
# 大多数近期版本
./venv/bin/python kohya_gui.py --listen 127.0.0.1 --server_port 7860
# 老版本可能要用
./venv/bin/python kohya_gui.py --share  # 不推荐，仅调试
```

> 不要硬背参数，**以你 pin 的 commit 的 README 为准**。

用 systemd 托管训练界面：

```ini
# /etc/systemd/system/kohya-gui.service
[Unit]
Description=Kohya_ss Training GUI
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/AIGC/kohya_lora_train
Environment="PATH=/opt/AIGC/kohya_lora_train/venv/bin"
ExecStart=/opt/AIGC/kohya_lora_train/venv/bin/python kohya_gui.py \
          --listen 127.0.0.1 --server_port 7860
Restart=on-failure
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
```

Windows 端通过 SSH 隧道访问 `http://127.0.0.1:7860`。

### 4.2 数据集制作与规范

1. **图片**：20~50 张，主体清晰，背景尽量干净，覆盖多角度多光照
2. **打标**：用 **WD14 Tagger** 自动生成标签再人工修正
3. **目录结构**：

```
/opt/AIGC/training_dataset/
└─ character_lora_v1/
   ├─ 10_character/        # 前缀数字 = repeat 次数
   │  ├─ 001.jpg
   │  ├─ 001.txt
   │  └─ ...
   └─ 1_reg/               # 可选的正则化图像
```

4. **分辨率分桶（bucketing）**：开启后不同长宽比的图片可混训，不必全部裁成同一尺寸

### 4.3 标准化训练参数模板

#### 4.3.1 总步数公式（先算这个，再定 epoch）

```
总步数 = ceil( 图片数 × repeat × epoch / (batch_size × gradient_accumulation_steps) )
```

示例：40 张图 × repeat 10 × epoch 10 / batch 4 = **1000 步**。

#### 4.3.2 SD 1.5 LoRA（入门，先跑通这个）

| 参数 | 推荐值 | 说明 |
| --- | --- | --- |
| 基础模型 | SD 1.5 | 16G 无压力 |
| mixed_precision | **fp16** | P100 唯一选择，**不能 bf16** |
| save_precision | **fp16** | 同上 |
| LoRA Rank (dim) | 8~16 | 越高拟合越强、文件越大 |
| **network_alpha** | **rank 的一半** | 对效果影响明显 |
| epoch | 10~20 | 结合 4.3.1 公式算总步数 |
| batch_size | 4 | 16G 可跑 |
| 学习率 | 1e-4 | 新手不建议改 |
| lr_scheduler | **cosine** | 收敛更稳 |
| warmup_steps | 总步数的 5~10% | |
| 优化器 | AdamW8bit（需 4.1 验证）/ AdamW / Lion | |
| 分辨率 | 512x768（竖版）/ 768x768 | 开 bucketing |
| gradient_checkpointing | 开启 | 16G 上保险起见都开 |
| cache_latents | 开启 | 显著提速，内存吃紧时改 `cache_latents_to_disk` |
| min_snr_gamma | 5（可选） | 缓解低信噪比下的过拟合 |
| 保存间隔 | 每 2 epoch | 便于挑选最优版本 |

#### 4.3.3 SDXL LoRA（进阶，岗位更看重）

| 参数 | 推荐值 | 说明 |
| --- | --- | --- |
| 基础模型 | SDXL 1.0 | |
| mixed_precision / save_precision | **fp16 / fp16** | 同上 |
| LoRA Rank | 16~32 | |
| network_alpha | rank 的一半 | |
| batch_size | **1**（+ gradient_accumulation 4~8） | 16G 显存吃紧 |
| 分辨率 | 768x768 或 1024x1024 | 1024 时 batch 必须为 1 |
| gradient_checkpointing | **必须开启** | 否则 16G 装不下 |
| cache_latents | **cache_latents_to_disk** | SDXL 潜变量很占内存 |
| 优化器 | AdamW8bit（验证后）/ AdamW / Lion | |
| UNet 学习率 | 1e-4 | **必须与 Text Encoder 分别设置** |
| Text Encoder 学习率 | 5e-5 | 单独设置，比例 2:1 |
| 训练时长预期 | 明显慢于 SD1.5 | P100 无 Tensor Core，做好心理预期 |

**UNet 与 Text Encoder 学习率分离设置方法：**

- kohya_gui **默认界面只有一个 Network Train Rate**，需要展开 **Advanced → Network Alpha (LoRA)** 区域
- 或者在 Source 参数里勾选 **Split UNet / Text Encoder learning rates**
- 如果界面找不到，直接改用 `accelerate launch` 命令行方式调用 `sd_scripts/train_network.py`

#### 4.3.4 通用建议

- 训练中开启 `--sample_every_n_steps`，中途出样观察
- 开 **TensorBoard** 看 loss 曲线：`tensorboard --logdir ./logs --port 6006`（加进 SSH 隧道）
- 产出在 `kohya_ss/output`，`.safetensors` 就是 LoRA 模型

### 4.4 训练监控与故障排查

| 症状 | 排查方向 |
| --- | --- |
| 显存 OOM | 降 batch → 开 gradient_checkpointing → 降分辨率 → `cache_latents_to_disk` |
| 报 bf16 相关错误 | P100 不支持 bf16，全改 fp16 |
| 报 xformers / flash attention 错误 | 关闭，改用 sdpa |
| bitsandbytes 导入失败 | 换 AdamW 或 Lion 优化器 |
| Loss 不降 | 检查打标质量、学习率是否过高、数据集是否太小 |
| 过拟合 | 减 epoch、增数据多样性、降 rank、加正则化图像 |
| 训练极慢 | 确认 `cache_latents` 已开；P100 无 Tensor Core，速度本就受限 |

### 4.5 Windows 端：效果验证

1. 把 LoRA 同步到 `D:\AIGC\model_cache\loras`
2. 本地 ComfyUI 加载测试，对比不同 epoch 的效果
3. 记录"参数 → 效果"对照表

### 阶段产出物

1. 《LoRA 标准化训练操作手册.md》（含两套参数模板、步数公式、P100 排错表）
2. 自动化训练启动脚本（参数化，可复现）
3. 样例 LoRA 模型（SD1.5 + SDXL 各一个）+ 效果对比图
4. 训练日志与 TensorBoard 分析记录

---

## 阶段五：LLM 服务化 + 统一网关 + 作品集（第 14~16 周）

### 阶段目标

补上 LLM 这一侧（2026 年 AIGC 岗位 JD 的高频要求），用 FastAPI 做统一网关，打包作品集。

### 5.1 LLM 服务化（llama.cpp）

> **为什么是 llama.cpp 而不是 vLLM**：vLLM 要求计算能力 ≥ 7.0，P100（sm_60）用不了。llama.cpp 的 CUDA 后端原生支持 Pascal，并提供 OpenAI 兼容 API。

**量化方案对比（P100 16G 适用）：**

| 方案 | 7B 模型体积 | 显存占用 | 质量 | 适用 |
| --- | --- | --- | --- | --- |
| **GGUF Q4_K_M** | ~4.5 GB | ~5 GB | 较好 | **首选** |
| GGUF Q5_K_M | ~5.5 GB | ~6 GB | 更好 | 显存够用时 |
| GGUF Q8_0 | ~8 GB | ~9 GB | 接近 FP16 | 几乎无损 |

P100 + llama.cpp 场景下，**GGUF Q4_K_M 是最稳的选择**。

#### 5.1.1 编译

```bash
cd /opt/AIGC
sudo apt install build-essential cmake libcurl4-openssl-dev -y

# ⚠️ 锁定到具体 tag，不要拉 master
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
git checkout b3000  # 替换为你验证过的 tag 或 commit

# 关键：显式指定 sm_60
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=60
cmake --build build --config Release -j$(nproc)
```

> **注意事项**：如果驱动版本 ≥ 555，cmake 编译可能能过，但运行时 CUDA kernel 找不到 sm_60 路径，llama-server 只能跑 CPU。**确保驱动是 535**。

#### 5.1.2 模型与服务

```bash
mkdir -p /opt/AIGC/llm_models
# 用 hf-mirror 下载（0.9 配的 HF_ENDPOINT 已生效）
# 下载 7B Q4_K_M（约 4.5GB），例如 Qwen2.5-7B-Instruct-Q4_K_M.gguf
```

```ini
# /etc/systemd/system/llama-server.service
[Unit]
Description=llama.cpp OpenAI-compatible Server
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/AIGC/llama.cpp
ExecStart=/opt/AIGC/llama.cpp/build/bin/llama-server \
          -m /opt/AIGC/llm_models/Qwen2.5-7B-Instruct-Q4_K_M.gguf \
          --host 127.0.0.1 --port 8080 \
          -ngl 999 -c 8192 \
          --metrics
Restart=on-failure
RestartSec=10
StartLimitIntervalSec=300
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
```

- `-ngl 999`：全部层卸载到 GPU
- `--metrics`：暴露 Prometheus 指标，直接接入阶段三的监控栈
- 服务提供 **OpenAI 兼容**的 `/v1/chat/completions`

**内存共存注意**：ComfyUI 和 llama-server **不能同时运行**（16G RAM 限制）。需要时手动切换：

```bash
# 用 LLM 时
sudo systemctl stop comfyui
sudo systemctl start llama-server

# 用 ComfyUI 时
sudo systemctl stop llama-server
sudo systemctl start comfyui
```

### 5.2 FastAPI 统一网关

**核心产出**：一个把图像和文本能力统一封装的 AIGC 后端服务。

```
aigc-gateway/
├─ main.py              # FastAPI 入口
├─ routers/
│  ├─ images.py         # 转发 ComfyUI
│  └─ chat.py           # 转发 llama.cpp（也可直接用 LiteLLM 替代）
├─ tasks.py             # 任务队列（Celery + Redis，或 asyncio + 内存队列）
├─ models.py            # 任务状态、结果模型
└─ requirements.txt
```

功能清单：

- `POST /v1/images/generations` → 提交出图任务，返回 `task_id`
- `GET /v1/tasks/{task_id}` → 查询进度与结果
- `POST /v1/chat/completions` → 转发 LLM
- 任务队列与并发控制，**限制同时提交给 ComfyUI 的任务数**
- API Key 鉴权、请求限流、结构化日志
- `/metrics` 暴露 Prometheus 指标（任务数、耗时、失败率）
- 结果落盘 + 定时清理

**LiteLLM 作为 LLM 端的快速方案**（可选）：

```bash
pip install 'litellm[proxy]'
litellm --model /opt/AIGC/llm_models/Qwen2.5-7B-Instruct-Q4_K_M.gguf \
        --api_key your-secret-key
```

### 5.3 企业私有化 AIGC 方案文档

**核心产出**：《企业内网 AIGC 平台私有化部署方案》，内容：

- 硬件选型与算力估算（用你实测的吞吐数据支撑）
- 网络架构：内网部署、无外网依赖、模型离线分发
- 权限管理：多用户隔离、API Key、访问控制
- 模型管理：内部模型库、版本控制、审批流程（**特别提醒 Civitai 模型的商用许可差异**）
- 运维监控：GPU 监控（阶段三）、服务可用性、日志告警、容量规划
- 成本测算：本地部署 vs 云端算力的 TCO 对比
- 备份策略：模型库、训练产物、配置文件的定期备份与异地容灾
- 风险与合规：模型许可、内容审计、数据不出内网

### 5.4 作品集打包（GitHub）

> 2026-09-23 与实际仓库对齐（此前这棵树是计划版，与交付物不一致）。

```
AIGC-Engineer-Portfolio/
├─ README.md                      # 项目总览 + 架构图 + 实测数据摘要
├─ docs/
│  ├─ 00-AIGC工程师落地实战指南-合订本.md   # 五阶段完整指南 + 54 个踩坑清单
│  ├─ 01-ComfyUI双机部署手册.md             # 版本基线 / 驱动锁定 / SSH 隧道 / systemd
│  ├─ 02-ComfyUI-API开发手册.md             # REST/WebSocket 客户端、批量生成
│  ├─ 03-ComfyUI容器化部署交付手册.md        # 离线可复现镜像、四维构建自检
│  ├─ 04-GPU监控与告警方案.md               # 三层监控、实测阈值、闭环案例
│  ├─ 05-LoRA训练工程化实战手册.md           # 训练全链路、评测方法论、四次误诊实录
│  ├─ 06-LLM服务化部署手册.md               # P100 调优四参数、ubatch 3.2×、共存规则
│  ├─ 07-企业私有化AIGC平台方案.md           # 选型/TCO/合规/安全/路线图
│  ├─ 08-平台使用指南.md                    # 业务/开发者/运维三类角色手册
│  └─ 09-自我修正与方法论.md                 # 六次结论修正（本项目最值钱的内容）
├─ comfy_client/                # ⭐ 共享包：ComfyUI 客户端唯一实现（scripts 与网关共用）
│  ├─ __init__.py               # 公共 API 导出
│  ├─ config.py                 # 工作区 / 地址 / 日志（configure 显式注入）
│  ├─ workflow.py               # 加载 / 节点定位 / 参数注入
│  ├─ client.py                 # 提交 / 等待 / 下载（全套容错）
│  └─ __main__.py               # python -m comfy_client 自检入口
├─ pyproject.toml               # pip install -e .（生产部署把包装进 site-packages）
├─ scripts/
│  ├─ verify_env.py               # 环境四维校验（真跑 kernel，不看 is_available）
│  ├─ _path.py                   # 引导：把仓库根加入 sys.path（共享包用）
│  ├─ comfy_ws.py                 # WebSocket 精测客户端
│  ├─ comfy_batch_gen.py          # 批量基准（输出结构化 JSON/CSV）
│  ├─ llm_bench.py                # LLM 基准（服务端权威计时）
│  ├─ sdxl_eval.py / inspect_workflow.py
│  ├─ train_start.sh              # LoRA 训练启动（sd15/sdxl，含前置硬校验）
│  ├─ docker_deploy.sh            # 编排一键部署/回滚 + 前置检查
│  └─ summarize_runs.py           # 一键重算 benchmarks 全部表格（可自验）
├─ docker/
│  ├─ Dockerfile.p100             # P100 离线可复现镜像
│  ├─ Dockerfile.win              # 5060 侧
│  ├─ docker-compose.yml          # ComfyUI + dcgm + Prometheus + Grafana
│  ├─ .env.example
│  ├─ monitoring/
│  │  ├─ prometheus.yml           # 抓取配置（5s 间隔，三层数据源）
│  │  └─ alert_rules.yml          # 告警规则（阈值全部标注实测依据）
│  └─ grafana/
│     ├─ p100-live-dashboard.json / aigc-app-dashboard.json
│     └─ provisioning/{datasources,dashboards}/
├─ systemd/
│  ├─ comfyui.service             # 自愈 + 防重启风暴
│  ├─ aigc-gateway.service
│  ├─ llama-server.service        # P100 四参数调优 + 资源互斥说明
│  └─ llm-switch.sh               # 交互式模型切换
├─ gateway/aigc-gateway/          # FastAPI 统一网关（鉴权/队列/GPU准入/指标）
├─ workflows/                     # API 格式工作流（base / lora / sdxl_lora）
└─ benchmarks/
   ├─ 实测数据.md                  # 全部实测数据 + 测量方法修正记录
   └─ raw/                         # 25 轮原始数据（50 文件，证据链）
```

> **关于 `kohya-gui.service`**：原计划中有，实际**未交付，也不应交付**。
> kohya_ss GUI 硬卡 `torch==2.7.0+cu128`，与 P100（535 驱动 / CUDA 12.2 上限）双重不兼容，
> 阶段四已改走 sd-scripts CLI（理由见 05 手册 §3）。**交付一个用不了的 service 文件是自欺欺人。**

**`benchmarks/实测数据.md` 是简历的弹药**，至少包含：

- SD1.5 在 P100 上的出图吞吐（张/分钟）与单张耗时
- 批量 100 张任务的成功率、平均耗时、P95 耗时
- 显存峰值水位
- LoRA 训练：SD1.5 / SDXL 各自的步速（it/s）与总时长
- LLM：首 token 延迟、生成速度（tok/s）、并发表现
- 双机千兆网络下的文件同步耗时

**有数字的运维简历，和只有"熟悉"二字的简历，是两个物种。**

### 5.5 简历与岗位定位

**主打岗位**：AI Infra 工程师 / MLOps 工程师 / AIGC 平台工程师

**叙事主线**：10+ 年 IT 运维 → 独立搭建双机 AIGC 平台 → 覆盖部署、容器化、可观测性、任务调度、模型训练、私有化交付全链路

**突出关键词**：GPU 运维、CUDA 生态与版本治理、容器化交付、Prometheus/Grafana 可观测性、任务队列与 API 网关、LoRA 训练工程化、私有化部署方案、容量与成本测算

**面试杀手锏**（这三件事别人做不到）：

1. **版本治理**：能讲清 Blackwell 与 Pascal 的 CUDA 支持差异、驱动分支锁定策略 —— 这是真实踩过坑才有的知识
2. **可观测性**：能拿出 GPU 利用率 / 显存 / 温度的 Grafana 面板和告警规则
3. **量化数据**：所有性能数字都实测过，不是抄来的

### 阶段产出物

1. `llama-server.service` + LLM 部署记录与实测数据
2. `aigc-gateway/` FastAPI 网关
3. 《企业内网 AIGC 平台私有化部署方案.md》
4. 完整 GitHub 作品集
5. 优化后的简历

---

## 附录 A：选修 —— K8s GPU 调度

> **优先级低**。单节点 16G + P100 上 K3s + GPU Operator 的投入产出比不高，且 GPU Operator 的驱动容器对 P100 支持存在风险。**建议放到作品集完成之后再考虑，或只写一份方案设计而不实际搭建。**

如果要做：

- 单节点 K3s（`--disable traefik` 省资源）
- NVIDIA GPU Operator + device plugin
- ComfyUI Deployment + Service + PVC（模型用 PVC，不放镜像里）
- 测试 Pod 级 GPU 调度、故障自愈

**注意事项**：P100 无 MIG，只能整卡分配；16G 内存跑 K3s + 训练任务会吃紧，建议先把 ComfyUI 训练服务停掉再测。

---

## 附录 B：常用资源索引

**官方文档**

- ComfyUI 官方仓库与 Wiki
- Kohya_ss README 与 sd-scripts 文档
- llama.cpp 仓库（构建参数、CUDA 后端说明）
- NVIDIA CUDA Toolkit 文档（**架构支持矩阵**，务必收藏）
- NVIDIA DCGM 与 dcgm-exporter 文档
- PyTorch 官方安装页与 CUDA 支持矩阵

**模型与社区**

- Hugging Face（模型、数据集，**国内用 hf-mirror.com**）
- **Civitai**（SD 模型主战场，注意看模型卡的许可协议，商用需谨慎）
- PyTorch Discuss、llama.cpp Discussions（排错首选）

**国内镜像与代理**

- HF 镜像：https://hf-mirror.com（设 `HF_ENDPOINT`）
- pip 清华源：https://pypi.tuna.tsinghua.edu.cn/simple
- GitHub 代理：https://gh-proxy.com

**网关与 API 工具**

- **LiteLLM**（统一 LLM API 网关）
- **Tailscale / ZeroTier**（跨网络远程访问家用算力）

**模型下载与传输**

- **aria2**（多线程下载 HF 大模型）
- **qBittorrent**（下载 Civitai 模型）

**SD WebUI 替代品**

- **A1111 / Stable Diffusion WebUI**（最老牌）
- **Forge**（A1111 优化版，显存占用更低）

**云算力备选**

- 揽睿星舟、RunPod、AutoDL

---

## 附录 C：环境基线速查

```bash
# 0.9 网络环境（中文用户）
echo 'export HF_ENDPOINT=https://hf-mirror.com' >> ~/.bashrc
git config --global url."https://gh-proxy.com/https://github.com/".insteadOf "https://github.com/"

# Windows（RTX 5060 / Blackwell sm_120）
cd D:\AIGC\ComfyUI-Win
python -m venv venv && venv\Scripts\activate
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
python verify_env.py          # 必须看到 sm_120
```

```bash
# Ubuntu（Tesla P100 / Pascal sm_60）
sudo apt install nvidia-driver-535 && sudo apt-mark hold nvidia-driver-535
python3.10 -m venv venv && source venv/bin/activate
pip install torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0 \
    --index-url https://download.pytorch.org/whl/cu124
python verify_env.py          # 必须看到 sm_60
```

```bash
# 0.10 SSH 密钥（Windows 端执行）
ssh-keygen -t ed25519
ssh-copy-id 你的用户名@192.168.1.50
ssh aigc-box "echo ok"        # 验证免密登录
```

```bash
# 0.11 端口与防火墙（Ubuntu 端执行）
sudo ufw allow OpenSSH
ss -tlnp | grep -E ':(22|3000|8080|8188|9090|9400)\b'  # 端口冲突预检
```

```bat
:: 启动参数速查（两端不同，别抄混）
:: Windows 5060
python main.py --disable-cuda-malloc

:: Ubuntu P100（systemd unit 里已含）
python main.py --listen 127.0.0.1 --port 8188 --disable-xformers --disable-cuda-malloc
```

| 参数 | 5060 | P100 | 原因 |
| --- | --- | --- | --- |
| `--disable-xformers` | 不需要 | **必须** | P100 sm_60 无 xformers 预编译 wheel |
| `--disable-cuda-malloc` | **必须** | 可选 | 5060 上 +2.6s 劣化 + 批量卡死；P100 零影响 |
| cuDNN 补丁（main.py） | 不需要 | **必须** | cuDNN 9.x 在 sm_60 上初始化失败 |

```bat
:: 性能基准测试（必须用 WS，轮询会让快任务 P95 虚高 76%）
set COMFY_USE_WS=1
set COMFY_HOST=http://127.0.0.1:8188     :: 5060
set COMFY_HOST=http://127.0.0.1:8288     :: P100（SSH 隧道）
python comfy_batch_gen.py
```

---

## 附录 D：实战记录（2026-09-07 ~ 09-11）

### D.1 实测环境快照（V1.5 最终版）

**Windows 11 笔记本：**
```
OS: Windows 11 | Python: 3.14.6
PyTorch: 2.11.0+cu128 | torchvision: 0.26.0+cu128
GPU: RTX 5060 Laptop GPU | sm_120 | 8.0 GB | 32 GB RAM
分配器: native (--disable-cuda-malloc)
稳态出图: 512x512/20步 中位 1.94s，P95 2.34s，吞吐 27.86 张/分（WS 精测）
ComfyUI commit: 22e40d2a (stable-2025-10)
ComfyUI: 可正常出图 ✅
```

**Ubuntu 台式机：**
```
OS: Ubuntu 22.04 | Python: 3.10.x
PyTorch: 2.6.0+cu124 | torchvision: 0.21.0+cu124
cuDNN: 禁用（9.2.4 在 sm_60 上不兼容）
GPU: Tesla P100-PCIE-16GB | sm_60 | 15.9 GB | 16 GB RAM
驱动: 535.309.01 (apt-mark hold)
ComfyUI commit: 22e40d2a (stable-2025-10)
ComfyUI: 可正常出图 ✅（cuDNN 禁用，--disable-cuda-malloc）
分配器: native（该机器上加不加都一样，见 2.5）
稳态出图: 512x512/20步 中位 7.90s，P95 8.92s，吞吐 7.49 张/分（WS 精测）
llama.cpp: 已编译 + systemd 运行（与 ComfyUI 不同时跑，16G RAM 限制）
```

**ModelScope A10（对照环境，实例已释放）：**
```
OS: Ubuntu 22.04 容器 | Python: 3.12.13
PyTorch: 2.13.0+cu130 | CUDA 13.0.3 预装
GPU: NVIDIA A10 | sm_86 | 22.2 GB | 28 GB RAM（容器限制）
用途: 提供第三组 cudaMallocAsync 对照数据，证伪"跨架构"结论
结果: cudaMallocAsync 与 native 中位均 2.01s，20/20，显存 2.31GB
额外验证: 现代数据中心卡 + CUDA 13 下，cuDNN 补丁 / xformers 禁用 / 分配器切换全都不需要
```

### D.2 完整踩坑清单（54 个坑，阶段一 18 + 阶段二 7 + 阶段三 15 + 阶段四 6 + 阶段五 8）

| 序 | 坑 | 解决 | 文档位置 |
| --- | --- | --- | --- |
| 1 | 装了 cu130 驱动 535 不支持 | 降到 torch 2.4.1+cu121 | 0.2 |
| 2 | 580 驱动 llama.cpp 跑不了 | 锁回 535 | 0.3 |
| 3 | ComfyUI venv 装的是 CPU torch | 重装 cu128 wheel + 强校验 | 1.1.3 |
| 4 | test-venv 删了后忘了重装 | install_env 脚本 | 1.2.3 |
| 5 | SSH 已拷公钥仍问密码 | User 字段替换 | 0.6 |
| 6 | hf-mirror 上 runwayml 404 | 用社区镜像 | 1.1.6 |
| 7 | ComfyUI 第一次启动画布空 | 双击空白加节点 | 1.1.6 |
| 8 | venv 路径决策不统一 | 允许复用，systemd 同步 | 1.2.5 |
| 9 | comfy_kitchen list[int] ValueError | 降 commit 到 22e40d2a | 1.5 |
| 10 | git checkout 被本地修改阻止 | `git checkout -f` | D.4 |
| 11 | requirements.txt 覆盖 torch（踩 4 次） | install_env 脚本标准化 | 0.12 |
| 12 | systemd 无限重启（counter 40+） | `Restart=on-failure` + `StartLimitBurst=5` | 1.2.5 |
| 13 | systemd 日志看不到 Python 报错 | 改 StandardOutput 路径 | 1.2.5 |
| 14 | 双机 commit 不一致 | 统一 22e40d2a | 1.2.3 |
| 15 | cuDNN 9 在 sm_60 上初始化失败 | `torch.backends.cudnn.enabled = False` | 1.2.3-b |
| 16 | sitecustomize.py 被系统文件覆盖 | 改用 main.py 源码补丁 | 1.2.3-b |
| 17 | RAM 16G 认知偏差 | 实测确认，修正文档 | 0.1 / 0.7 |
| 18 | transformers 禁用 PyTorch（torch<2.5） | 升到 2.6.0+cu124 | 0.2 |
| 19 | SSH 隧道本地端口与本机 ComfyUI 冲突 | 隧道改用本地 8288 | 0.6 |
| 20 | Dev Mode 是 per-实例的服务端设置 | 两台机器各开一次 | 2.1 |
| 21 | P95 在 n=10 时恒等于 max | n≥20 + 线性插值 | 2.4 |
| 22 | **cudaMallocAsync 在 5060 上 +2.6s 开销 + 批量卡死** | 5060 加 `--disable-cuda-malloc`（P100/A10 零影响，不要照抄） | 1.1.5 / 2.5 |
| 23 | 轮询测量偏差异常放大快任务的尾部 P95（5060 虚高 76%） | 性能基准测试必须用 WS | 2.5 |
| 24 | WebSocket 完成信号会漏（任务已完成 but WS 干等 60s） | 加 `/history` 兜底轮询；放宽 `started` 条件 | 2.6 |
| 25 | 误判"cudaMallocAsync 是跨架构共性" | P100 从头到尾没做过对照实验。三机做完后证伪 | 2.5 / §0 |
| 26 | 容器内 `git clone` HTTP2 framing error | 容器不继承宿主机 git 代理 | 3.0 |
| 27 | 容器内 pip `Errno 101 Network unreachable` | 容器不继承 pip 镜像配置 | 3.0 |
| 28 | 容器卡在 `apt-get update` 十几分钟 | apt 源没换（前三次都漏了这个） | 3.0 / 3.1 |
| 29 | `ModuleNotFoundError: requests` | ComfyUI requirements.txt 漏声明，裸机靠"意外依赖"掩盖了 | 3.2 |
| 30 | `COPY wheels/` 白留 2.9 GB 在镜像层 | Docker 层不可变，改 `--mount=type=bind` | 3.2 |
| 31 | gh-proxy 传源码 822 秒后 early EOF | 改用宿主机本地 tar，构建不依赖外网 | 3.2 |
| 32 | **`tar --exclude=models` 误删 `comfy/ldm/models/`** | **exclude 是全局模式匹配**，顶层目录要用 `./` 锚定 | 3.2 |
| 33 | 用 `import server` 诊断制造了假错误 | ComfyUI 依赖 main.py 导入顺序，诊断要用真实入口 | 3.2 |
| 34 | `nvcr.io/nvidia/dcgm-exporter` Access Denied | 官方路径就是 `nvidia/k8s/dcgm-exporter`，k8s 是路径不是变体 | 3.3 |
| 35 | Pascal 拿不到 DCGM 的 DCP profiling 指标 | 需 Volta+，自动回退基础计数器集，16 个指标够用 | 3.4 |
| 36 | ~~Proxmox 透传下拿不到 per-process 显存归属~~ **09-18 证伪**：`query-compute-apps` 可用（它只列 C 型计算进程，空表≠不支持） | 3.1 |
| — | （37~40 见 D.9；41~45 见 D.12；46 见 D.13） | | |
| 47 | requests `iter_lines(decode_unicode=True)` 在无 charset 的响应上按 Latin-1 解码，中文 UTF-8 序列中的 `0x85`（NEL）被 splitlines 当换行，SSE 的 JSON 行被从中间切碎（Unterminated string） | SSE 流**按字节切行**再手动 `decode("utf-8")` | llm_bench.py |
| 48 | 并发串行/并行判定用 wall vs sum(totals)：串行时后到者的 total 已含排队时间，sum 双重计数 → 误判"有并行" | 用 **max/min 耗时比**：≈2 串行、≈1 并行 | llm_bench.py |
| 49 | **PowerShell 里 `set VAR=value` 不设置进程环境变量**（`set` 是 `Set-Variable` 别名，设的是 PowerShell 变量，子进程拿不到）→ uvicorn 拿不到 `AIGC_API_KEY`，鉴权静默失效 | PowerShell 用 **`$env:VAR = "value"`**；cmd 才用 `set VAR=value`。写文档/脚本时必须注明 shell 类型 | gateway |
| 50 | `taskkill /F /IM python.exe` 批量清理误伤同机其他 python 服务（ComfyUI 被连带杀掉） | 按端口找 PID 精杀：`netstat -ano \| findstr :端口` → `taskkill /PID <pid> /F` | gateway |
| 51 | **"LLM + 出图可共存"只测了静态驻留**：llama-server 驻留 9.7G 时 768×512 出图在 VAE decode 阶段 OOM（需 2.25G 空闲仅 1.46G，另有 1.66G PyTorch 碎片） | 网关加 **GPU 准入控制**（空闲显存 < 2.6G 则等待/明确拒绝）；ComfyUI 加 `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` 收碎片。**共存结论要测峰值场景，静态账不作数** | gateway / 06 §8 |
| 52 | **服务绑 127.0.0.1 ≠ 容器可达**：Prometheus 容器经 `host.docker.internal`（=docker0 网桥 172.17.0.1）抓宿主机服务，绑 lo 的网关 connection refused（llama-server 能通只是因为它绑 0.0.0.0 顺带覆盖了网桥） | 网关绑 **172.17.0.1**：容器 + 宿主机 + SSH 隧道可达，局域网无路由不可达——比 0.0.0.0 精确，不暴露无鉴权端点 | gateway / 06 §7.3 |
| 53 | **FastAPI/Swagger 三连坑**：① 生产关 /docs 是过度防御（文档页低敏感，该锁的是调用）② `APIKeyHeader` 只实例化不进 OpenAPI（Authorize 按钮不出现）③ 端点用裸 `Request` 则无 body schema（/docs 没有请求体输入框） | 文档开放 + 调用鉴权；安全方案必须被路由 `Depends()` 引用才注册；body 用 `dict`/Pydantic 模型并附 `examples` | gateway |
| 54 | **SDXL fp16 训练 loss 从 step 1 全 nan**（avr_loss=nan × 362）：SDXL VAE 在 fp16 下产生 NaN（著名问题），`cache_latents_to_disk` 验尸发现 **20/20 个 latent 100% nan** | 换 fp16-fix VAE（madebyollin，**官方文件名是 `sdxl_vae.safetensors`**，`sdxl_vae_fp16fix` 是 ComfyUI 圈改名）+ `--vae=` 指定，重启后 nan 0/20、loss 正常。P100 无 bf16 让这个坑无法绕过 | SDXL |
| — | （附注：训练进程组 = accelerate 启动器 + 主进程 + N 个 DataLoader worker，`pgrep` 看到 4 个进程不是重复启动） | | SDXL |

### D.3 关键选型决策

1. **P100 驱动 = 535 锁死**
2. **P100 PyTorch = 2.6.0+cu124 + cuDNN 禁用**：满足 transformers ≥ 2.5 要求，cuDNN 禁用保兼容
3. **Windows Python = 3.14.6**：cu128 wheel 全
4. **ComfyUI commit = 22e40d2a**（2025-10-28）：quant_ops.py 引入之前的安全 commit
5. **install_env 脚本**：防 requirements.txt 覆盖 torch
6. **systemd 参数**：`--disable-xformers --disable-cuda-malloc`
7. **cuDNN 补丁**：直接在 main.py 中设置，而非 sitecustomize.py
8. **内存共存策略**：ComfyUI 和 llama-server 不同时运行

### D.4 Linux 小课堂：git checkout 被本地修改阻止

**现象：**
```
error: 您对下列文件的本地修改将被检出操作覆盖：
        .ci/update_windows/update.py
        blueprints/Film Grain.json
        ...
正在终止
```

**原因：** ComfyUI 的某些文件（blueprints、CI 配置）被意外修改了（可能是 pip install 时触发的脚本写的，或 git clone 时 CRLF/LF 差异）。

**修复：**
```bash
# 丢弃所有本地修改（干净）
git checkout -- .
git clean -fd

# 或强制切 commit（等价于上面两步）
git checkout -f <commit SHA>
```

### D.5 阶段一完成度 ✅

- [x] Windows 端 ComfyUI 出图
- [x] Ubuntu 端 ComfyUI 出图
- [x] 双机 commit 一致（22e40d2a）
- [x] 双机跑同一工作流结果一致
- [x] install_env 脚本标准化
- [x] cuDNN 补丁
- [x] systemd 服务稳定运行
- [x] SSH 隧道从 Windows 访问 Ubuntu

**阶段一于 2026-09-09 完成。**

### D.6 阶段二完成度 ✅（2026-09-09 ~ 09-10）

- [x] 2.1 导出 API 格式工作流（Dev Mode + `_meta.title` 约定）
- [x] 2.2 `comfy_client/` 生产级 REST 客户端（2026-09-23 上提为共享包，scripts 与网关共用同一份）
- [x] 2.3 双机联调（`COMFY_HOST` 环境变量切换节点）
- [x] 2.4 `comfy_batch_gen.py` 批量 + 指标采集
- [x] 2.5 双机 benchmark（n=20 × 2 节点，两端 native，100% 成功）
- [x] 2.6 WebSocket 步级进度（`comfy_ws.py`，含自动降级）
- [x] `benchmarks-实测数据.md`
- [x] `ComfyUI-API开发手册.md`

**阶段二于 2026-09-10 完成。**

### D.7 阶段二新增经验（7 条，序 19~25）

| 序 | 发现 | 文档位置 |
| --- | --- | --- |
| 19 | **SSH 隧道本地端口必须与本机服务错开**：Windows 本地 ComfyUI 占 8188，隧道也用 8188 会 `Address already in use`。改成本地 8288→远端 8188 后两边可同时开 | 0.6 |
| 20 | **Dev Mode 是 per-ComfyUI-实例**，存在服务端 `user/default/comfy.settings.json`，不是浏览器设置。两台机器要各开一次 | 2.1 |
| 21 | **百分位的样本量陷阱**：`int(n × 0.95)` 在 n=10 时索引为 9，P95 恒等于 max，无统计意义。需 n≥20 + 线性插值算法 | 2.4 |
| 22 | **`cudaMallocAsync` 只在 5060 上有问题**（+2.6s、批量卡死），**不是跨架构共性**。P100 与 A10 零影响。早期"跨架构"结论被两次证伪 | 1.1.5 / 2.5 |
| 23 | **测量方法污染数据**：轮询的绝对误差固定（≤2s），任务越快相对误差越大。5060 的 P95 被从 2.34s 虚高到 4.11s（+76%）——被推翻了两个旧结论 | 2.5 |
| 24 | **WS 完成信号会漏**：必须加 `/history` 兜底轮询；`executing+node:null` 不应依赖 `started` 标志 | 2.6 |
| 25 | **自己推翻自己的结论**：把"没做过的实验"当成证据，把"测量误差"当成物理现象——三次修正过程本身就是最有价值的经验 | 2.5 / §0 |

### D.8 阶段三进度（2026-09-11 ~ 09-14）

**已完成：**

- [x] Docker 29.4.3 + NVIDIA Container Toolkit（GPU 直通验证通过）
- [x] ComfyUI 镜像 `aigc-comfyui:2.6.0-cu124`，**5.07 GB**（从首版 8.27 GB 优化）
- [x] 四维构建自检（版本 / 补丁 / 依赖 / 源码完整性）
- [x] docker-compose 四服务全部健康
- [x] dcgm-exporter：16 个指标可用，Prometheus 抓取 `up`
- [x] 6 条告警规则（阈值来自阶段二 WS 精测）
- [x] Grafana datasource 自动配置
- [x] ⭐ **闭环实验（09-14）：P100 降速 = 温度墙实锤**
  - 拐点 **79°C / 约 70 秒**，SM 频率 1328 → 1050~999 MHz
  - **频率-耗时比 1.155 ≈ 1.157，严丝合缝**
  - 详见 `benchmarks-实测数据.md` §3.3
- [x] **告警规则按实测拐点修正**：温度 80→79°C、频率 1100→1200 MHz，均加防抖
- [x] Grafana 面板（6 面板：温度/频率/利用率/功耗/显存/健康指标，JSON 导入）
- [x] ⭐ **容器化开销对照（09-14）：< 1%**
  - 前段稳态 0.7%（裸机 7.26s vs 容器 7.31s）
  - 拆解热惯性 / 整卡显存口径 / page cache 三个干扰源
  - 详见 `benchmarks-实测数据.md` §3.9
- [x] ⭐ **requirements.lock 锁定（09-14）**
  - 101 个精确版本，新旧镜像 `pip freeze` diff **零差异**
  - 镜像供应链闭环：源码 tar + COMMIT 追溯 + 本地 wheels + 精确版本锁

**环境变更记录：**

```
2026-09-11  Ubuntu 内存 16G → 24G（Proxmox VE 里手动扩容）
            解除了"不能同时跑 ComfyUI + llama-server"的约束，待实测验证
            确认平台：Proxmox VE 虚拟机，P100 通过 PCIe 透传
2026-09-14  模型挂载修正：挪到 /opt/AIGC/model_storage/，裸机补 extra_model_paths.yaml
            代码仓库与模型数据彻底分离
2026-09-14  requirements.lock 锁定，镜像构建实现字节级可复现
```

**镜像供应链（可复现性闭环）：**

```
源码（本地 tar + COMMIT 追溯）
  + torch（本地 wheels/，版本由文件决定）
  + 依赖（requirements.lock，101 个精确版本）
  = 同一 Dockerfile 任何时候 build，产出字节级一致
```

**剩余可选优化（不阻塞阶段四）：**

1. 镜像再瘦身（删 `*.a` / `__pycache__` / `torch/test`，**别删 `torch/include`**）

**阶段三产出物（2026-09-15 全部完成）：**

- [x] `docker/Dockerfile.p100`（P100 适配 + 国内网络适配 + 四维构建自检）
- [x] `docker/Dockerfile.win`（RTX 5060 / cu128 变体，含与 P100 的参数对照表）
- [x] `docs/03-ComfyUI容器化部署交付手册.md`（8 章：概述/前置/构建/部署/运维/排错/安全/附录）
- [x] `docs/04-GPU监控与告警方案.md`（10 章：架构/指标选型/面板/告警/平台限制/闭环案例/扩展/容量规划）

**阶段三核心交付已达成**：GPU 可观测性闭环 + "假设 → 验证"案例 + 容器化开销量化 + 可复现镜像 + 完整交付文档。**阶段三 100% 完成。**

### D.9 阶段三新增经验（15 条，序 26~40）

**一个根因贯穿四次失败：容器是干净环境。**

| 次序 | 表现 | 漏掉的东西 |
| --- | --- | --- |
| 26 | `git clone` HTTP2 error | GitHub 代理 |
| 27 | pip `Network unreachable` | pip 镜像 |
| 28 | 卡在 `apt-get update` | **apt 源**（前三次都没想到） |
| 29 | `ModuleNotFoundError: requests` | 宿主机的"意外依赖" |

前三次都是网络配置，我修一个漏一个，做了三轮才补齐。**正确做法是一开始就列清单逐项过**（见 3.0 节）。

第 29 条性质不同但同源：**长期使用的开发环境会累积大量"意外获得"的间接依赖**——装过的每个包都可能顺带带进来别的东西，这些依赖没被任何 requirements 文件声明，但代码确实在用。**裸机上"能跑"不等于"依赖完整"，换台新机器部署照样会炸，只是容器让你提前发现了。**

**两个值得单独记的技术细节：**

| # | 教训 |
| --- | --- |
| 30 | **Docker 层不可变**：`COPY` 大文件后再 `rm` 不减体积，只是加 whiteout 标记。用 `--mount=type=bind`，省了 3.2 GB |
| 32 | **tar `--exclude` 是全局模式匹配**：`--exclude=models` 会命中 `comfy/ldm/models/`。顶层目录必须用 `./` 锚定。这类错误**构建期完全静默，只在运行时炸** |

**一次方法论错误（第 33 条）：**

看到 `'utils' is not a package` 后，我用 `python -c 'import server'` 去诊断——**但这不是 ComfyUI 的真实启动入口**。它依赖 `main.py` 的导入顺序预热 `sys.modules`，跳过 main.py 会让 `comfy/utils.py` 抢占顶层 `utils` 名字，产生一个真实启动时根本不存在的错误。我为此追了三轮。

真实错误从第一条日志起就写得清清楚楚：`No module named 'requests'`。

> **这跟阶段二那两次是同一类错误**（把测量误差当物理现象、把"没测过"当证据）：**诊断工具本身引入了偏差**。三次的共同教训——**先把现象测清楚，再解释；用真实入口，别用近似的替代品。**

**第 34~36 条（环境差异）：**

| # | 坑 | 教训 |
| --- | --- | --- |
| 34 | `nvcr.io/nvidia/dcgm-exporter` Access Denied | 官方发布路径就是 `nvidia/k8s/dcgm-exporter`，`k8s` 是路径不是变体 |
| 35 | Pascal 拿不到 DCGM 的 DCP profiling 指标 | 需 Volta+，自动回退基础计数器集。官方 12239 面板在 P100 上一半图是空的 |
| 36 | ~~Proxmox 透传拿不到 per-process 显存归属~~ **09-18 已证伪**（第 6 次推翻自己）：`query-compute-apps` 正常列出 PID/进程/显存。Grafana 时间序列仍是整卡口径（dcgm 无 per-process 指标），但现场归属排查可用 nvidia-smi / `fuser -v /dev/nvidia*` |

**第 37 条（今天最有价值的闭环）：**

| # | 内容 |
| --- | --- |
| 37 | **"假设 → 监控验证"的完整闭环**。阶段二靠耗时数据推测"13% 渐进降速是温度墙"，阶段三用 dcgm-exporter + nvidia-smi 实测曲线证实：**拐点 79°C / 约 70 秒，频率-耗时比 1.155 ≈ 1.157，严丝合缝**。这是本项目第一个完整的验证闭环，**也是把"推测"升级为"结论"的范式** |

**第 38~40 条（09-14 收尾）：**

| # | 内容 |
| --- | --- |
| 38 | **容器化开销量化**：< 1%。但**表观数据（+5.8%）是错的**——要拆开热惯性、整卡显存口径、page cache 三个干扰源才看到净差异。**A/B 实验里先排除干扰，再看净差异** |
| 39 | **`/system_stats` 的显存是整卡口径**，不是 per-process。多实例共存时读数是总和，告警阈值要按总和设。时间序列层面拿不到 per-process（dcgm 限制），但现场排查可用 `nvidia-smi --query-compute-apps`（09-18 修正：原"透传拿不到归属"结论已证伪） |
| 40 | **可复现镜像的三要素**：源码（本地 tar + COMMIT）+ torch（本地 wheel）+ 依赖（精确版本 lock）。少一个，"pin commit"就只是锁住了代码，依赖树还在漂 |

第 37 条附带的告警修正：**阈值不能用"理想值"要用"实测拐点"**。温度写 80°C 会几乎不触发（实际拐点 79°C 且震荡），频率写 1100 会反复穿越导致告警抖动。**很多监控告警天天吵但没人看，就是因为阈值是拍脑袋的。**

### D.10 六个能在面试里讲的工程洞察

1. **一个被推翻了三遍的结论（最重要）** —— 我写下"cudaMallocAsync 跨架构通用"，然后先后被 A10 和 P100 证伪。错在哪？**只有 5060 一台机器的样本，P100 从头到尾没做过对照实验。** 这个故事比"我发现了一个性能问题"有分量得多——它展示的是**能区分证据与推理、能自我证伪、能诚实修正**的能力，这是 Infra 工作的核心素质之一。
   > 附带讲清楚定位过程：从 `/system_stats` 的 `torch_vram_free` 只剩 4MB / 2GB 预留池，反推出 cudaMallocAsync 耗尽内存池导致任务被静默丢弃。

2. **测量方法本身在污染数据** —— 轮询的 2 秒误差对 1.94 秒的任务来说相对误差 105%。结论：**先确认量具再相信读数，以及"误差恒定"不等于"问题不存在"。** 这个观测直接改变了监控方案的设计选择。

3. **架构代差 4 倍及其分工含义** —— P100 中位耗时是 5060 的 4.07 倍（Tensor Core 缺失 + cuDNN 禁用 + 4 代架构差）。据此推出"P100 吃显存、笔记本吃速度"的分工：SDXL LoRA 训练放 P100，交互式调试放笔记本。

4. **显存读数不可跨机器直接比较** —— 差异同时来自**分配器类型**（cudaMallocAsync 预留池 vs native 按需）和**模型放置策略**（P100 把 CLIP 放 CPU）。容量规划时 `vram_used` 单个数字没有意义。

5. **批量调度开销仅 0.4%，瓶颈 100% 在 GPU** —— 能讲清瓶颈定位方法，以及"ComfyUI 单实例串行执行，提吞吐只能加卡或加实例"的架构结论。

6. **容器化暴露了裸机的"意外依赖"（阶段三新增）** —— 长期使用的开发环境累积了大量没被任何 requirements 声明、但代码确实在用的间接依赖（本项目是 `requests`）。**裸机上"能跑"不等于"依赖完整"，换台机器部署照样会炸。** 能讲清"这不是容器的问题，是容器帮你提前发现了问题"，说明理解容器化的真正价值不是"打包"而是"环境显式化"。
   > 配套讲：本地 build 能过、CI 里 build 失败，十有八九就是开发机有代理/缓存配置而 CI runner 是干净的。

---

### D.11 阶段四进度（2026-09-15 启动）

**路线决策（重要）：放弃 kohya_ss GUI，改走 sd-scripts 命令行**

阶段四原计划用 `kohya_ss`（bmaltais）的 Gradio GUI，但探测发现其 `requirements_linux.txt` 硬卡 **`torch==2.7.0+cu128`**——这与 P100 双重不兼容：

| 层面 | 问题 |
| --- | --- |
| CUDA 版本 | cu128 = CUDA 12.8，需驱动 ≥ 570；P100 锁死 535（CUDA 12.2 上限） |
| 驱动 | 535 是 P100 唯一可用驱动，升不了 |

**kohya_ss GUI = Gradio 前端 + 底层 sd-scripts**。放弃 GUI 改走 sd-scripts 命令行的四个理由：

| 维度 | kohya_ss GUI | sd-scripts CLI |
| --- | --- | --- |
| 依赖体量 | 巨大（Gradio + 前端库），版本脆弱 | 小，核心就是训练引擎 |
| 可复现性 | 参数点界面，难记录 | **参数全在脚本里，git 可追踪** |
| 简历价值 | "会用 GUI" | "能写参数化、可复现的训练脚本" |
| 与运维背景契合 | 弱 | **强**（脚本化、自动化） |

两者用同一个训练引擎，出一样的模型。这个转向让阶段四产出物（参数化训练脚本 + 训练手册）**天然就是交付物本身**。

**环境部署（sd-scripts，P100）—— 一次通过：**

```
/opt/AIGC/kohya_sd/          # sd-scripts 仓库
└─ venv/                     # 独立 venv（与 ComfyUI 隔离）

requirements.txt 探测：无 torch 行（单独装），transformers==4.54.1（pin 在 4.x，
                      避开了 transformers 5.x 要求 torch≥2.5 的坑），
                      diffusers==0.32.1，accelerate==1.6.0，bitsandbytes 无版本号
```

**四连验证全过：**

| 验证 | 结果 |
| --- | --- |
| torch 未被 requirements 覆盖 | ✅ 2.6.0+cu124 |
| CUDA + sm_60 | ✅ |
| **bitsandbytes AdamW8bit 在 sm_60** | ✅ **可用！（超预期，省 ~30% 优化器显存）** |
| sd-scripts `library.train_util` 导入 | ✅ |

> **bitsandbytes 可用是意外收获**：之前预判它在 sm_60 上不稳（文档里写了备用方案 AdamW/Lion），实测 OK。这意味着可以用 `AdamW8bit` 而非 `AdamW`，给 SDXL 训练留出更多显存余量。

**数据集规划（第一个 LoRA：特定一只狗，~30 张）：**

- 主体决策：**训"特定的一只狗"而非泛化的"狗"概念**——主体一致，模型好学，第一次必过
- 图片要求：多角度/多姿态/多光照/背景干净，**多样性 > 数量**（30 张高质量优于 50 张凑数）
- 目录规范：`/opt/AIGC/training_dataset/dog_xxx/{images,tags}`
- 打标：WD14 Tagger 自动打标 + 人工加触发词
- 配置：`dataset_config.toml`（TOML 格式，比文件夹命名法清晰）
- 步数公式：`总步数 = ceil(图片数 × repeat × epoch / batch_size)`

**待办（下一步）：**

1. 用户准备狗的照片（~30 张）→ 待确认身边有没有可拍的狗
2. WD14 Tagger 打标（注意 onnxruntime 在 P100 上用 CPU 版）
3. 第一个 SD1.5 LoRA 训练命令（AdamW8bit + SDPA + fp16 + grad_ckpt + cache_latents）
4. 训练监控（复用阶段三 Grafana + nvtop + TensorBoard）

**P100 训练五条铁律（阶段四最容易翻车点，开工前必记）：**

| # | 铁律 | 违反后果 |
| --- | --- | --- |
| 1 | 只能 fp16（mixed + save） | bf16 直接报错 |
| 2 | 没有 FP8 | sm_60 不支持 |
| 3 | 注意力用 SDPA，关 xformers/Flash | CUDA error |
| 4 | cuDNN 禁用（同 ComfyUI） | 卷积初始化失败 |
| 5 | bitsandbytes 需验证 | 已验证可用 → 用 AdamW8bit |

外加：训练是连续满载几十分钟，**温度墙会让时长 +15%**（阶段三实测），散热改造 ROI 高。

---

### D.12 第一个 LoRA 完整闭环（2026-09-16）

**端到端跑通了第一次 LoRA 训练，并完成了对照评测与根因定位。**

#### 数据集（Stanford Dogs / Shiba Inu）

- 来源：Stanford Dogs Dataset（官方 `images.tar` 750MB），选 `n02115641` = **Shiba Inu（柴犬）**
- 取 ~50 张，**删除含人物料后重新打标**
- 打标：**WD14 Tagger（ONNX 路径）**，标签质量从"全是人"修正为"主体是狗"（`no_humans / animal_focus / shiba_inu`）
- 触发词：**自造词 `myshiba` + 保留品种标签 `shiba_inu`**（自造词便于验证、品种标签锚定到基线模型已认识的概念）

#### 训练（sd-scripts CLI，一次跑通）

```
train_network.py  |  1260 步  |  62 分钟  |  P100
optimizer: AdamW8bit（sm_60 可用）
network: lora, dim=16, alpha=8
precision: fp16 (mixed + save)
attention: SDPA（--sdpa）
其他: gradient_checkpointing, cache_latents, batch=4, cosine, warmup=75
loss: 0.205 → 0.183（稳步下降，健康）
产出: /opt/AIGC/training_output/dog_lora_v1/myshiba_v1.safetensors
```

#### 评测（关键的三步对照实验）

| 测试 | 提示词 | LoRA | 结果 | 结论 |
| --- | --- | --- | --- | --- |
| 训练中途样图 | sample_prompts | — | 双狗、脸歪、粗糙 | **不能作数**（ddim/20步/无负面词） |
| 假基线 | 含 `myshiba` | strength 0 | 两个狗头 | **被自造词污染**，不算数 |
| **真基线** | 删 `myshiba` | 关 | ✅ **正常单只柴犬** | 模型+pipeline 健康 |
| **LoRA 测试** | 含 `myshiba` | 0.5 | ❌ **两只柴犬** | **LoRA 是问题源** |

#### 根因定位

**基线正常单只 + LoRA 出双狗 = LoRA 本身有问题。**

根因是**数据集结构**：训练用了 Stanford Dogs 的 **~50 只"不同的柴犬"**，触发词 `myshiba` 学的是 50 只长相各异的狗平均下来的**模糊概念**。这种"平均主体"天然容易产生**形态模糊、双影、五官错位**——正是观察到的"两只狗、耳朵重叠"。

**这是数据集问题，不是参数问题。** 参数（dim/lr/precision/optimizer）都是对的，证据是 loss 健康下降、pipeline 全程无报错。

#### 正确的修法（下一轮重训）

| 方案 | 做法 |
| --- | --- |
| **A（推荐）** | 从 50 只里**挑形态最接近的 15~20 只**（同角度/同姿态占多数），让主体聚焦 |
| **B** | 只留**单一个体**的所有图（若数量够 20+）——回到最初的"单一主体"原则 |
| C | 维持现状但把 LoRA strength 降到 0.3~0.4 用（治标） |

> **核心教训**：阶段四开始时定的原则是"训特定的一只狗，主体越一致越好"，但实际用了 breed 子集（多个体），违背了自己的原则，结果正中预判。**主体一致性是 subject LoRA 的第一原则，比图片数量、参数调优都重要。**

#### 本次踩坑记录（序 41~45，已计入 D.2 总表）

| 序 | 坑 | 解决 |
| --- | --- | --- |
| 41 | WD14 打标 TF 依赖地狱（缺TF→Keras3不兼容→SavedModel读取失败） | 用 `--onnx` 路径，甩掉整个 TF |
| 42 | onnx 与 TF 2.15 的 ml_dtypes/numpy 冲突，transformers 可选 TF 导入崩 | 卸掉 TF（transformers 在 TF 缺席时自动切 torch 后端） |
| 43 | 训练中途样图粗糙误判为训练失败 | 评测必须用 ComfyUI + 正经参数（负面词/euler/25步），不用训练样图 |
| 44 | 自造词 myshiba 在 strength 0 时污染基线（不认识的 token 产生噪声嵌入） | 真基线必须把自造词从提示词删掉，不能只把 strength 设 0 |
| 45 | LoRA 出双狗/多耳 | ~~根因是 50 个不同个体的"平均主体"~~ **此结论已被 D.13 证伪** |

---

### D.13 真正的根因：生成分辨率与训练构图不匹配（2026-09-17，推翻 D.12）

> ⚠️ **本节推翻 D.12 的"数据集结构"结论。** D.12 把双狗畸形归因为"50 个不同个体的平均主体"，并建议换聚焦数据集重训。**这个结论是错的。**

#### 决定性证据

用户按 D.12 的建议选了 20 张高度一致的图（统一黄毛、面部相似、特写/全身各半）重训，**问题依旧**。随后逐一排除了所有嫌疑：

| 嫌疑 | 排查方式 | 结果 |
| --- | --- | --- |
| 数据集 | 多样（v1）vs 一致（v2/v4） | 都失败 → 排除 |
| 优化器 | AdamW8bit（v1/v2）vs AdamW（v4） | 都失败 → 排除 |
| 强度 | 0.1 ~ 0.8 全测 | 都失败 → 排除 |
| 文本编码器 | 同 lr vs 半速 lr（5e-5） | 都失败 → 排除 |
| 推理环境 | P100 vs RTX 5060 | 都失败 → 排除 |
| 触发词 | myshiba vs 纯 shiba inu | 都失败 → 排除 |
| **生成分辨率** | **512×512 → 768×512** | **✅ 一张完整的狗** |

**最后一行才是真正的根因。**

#### 根因：构图比例不匹配

```
训练数据：Stanford 柴犬，绝大多数横向构图（宽 > 高）
        ↓ LoRA 学到"狗在横向画框里"
生成 512×512（正方形）→ 横向构图塞不进 → 平铺出第二个头 → 双头/多头/无狗
生成 768/832/640×512（横向）→ 构图匹配 → ✅ 正常单只狗
```

**基线模型在 512×512 下正常，是因为没被横向训练数据带偏，对各种构图都适应；LoRA 因聚焦横向构图，在正方形下才会平铺。** 这恰恰证明 LoRA 学到了东西——**它从来就没坏过，只是我们一直用错了画幅去测它。**

#### 连带修正两个此前的误判

| 此前的结论 | 修正 |
| --- | --- |
| "AdamW8bit 在 sm_60 上算错"（D.11/D.12 的怀疑） | **存疑**。v1/v2 从未在正确画幅下被评测过，它们的"失败"可能也是分辨率造成的，AdamW8bit 没机会证明自己 |
| "数据集结构是根因，要换聚焦数据"（D.12） | **错误**。一致数据集也失败，证明多样性不是主因 |

> **meta 教训（本项目第五次推翻自己的结论）**：我们绕了数据集、优化器、文本编码器、强度、fp16 一整圈，根因却是**最朴素、最容易被忽略的"生成分辨率"**。排查时**别只盯着复杂的嫌疑，先核对最基础的变量**——画幅、路径、端口、大小写这类"不起眼"的东西，已经在我们这个项目里贡献了多个坑（`/opt/AIGC` vs `/opt/aigc`、`Workflows` vs `workflows`、以及这次的分辨率）。

#### 正确使用方式（最终）

| 项 | 值 |
| --- | --- |
| 触发词 | `shiba inu`（品种名即可，**别用 myshiba 自造词**——它在 strength 0 时会额外诱发多头，见坑 44） |
| 分辨率 | **768×512 / 832×512 / 640×512**（横向，匹配训练构图） |
| strength | 0.5 起 |
| 负面词 | 含 `multiple dogs / two dogs / multiple heads / two heads / cloned face / extra ears` |

#### 本次踩坑记录（序 46，已计入 D.2 总表）

| 序 | 坑 | 解决 |
| --- | --- | --- |
| 46 | **LoRA 在 512×512 下平铺出多头，换横屏分辨率后正常** | **生成分辨率必须匹配训练数据的构图比例**。训练图横向→生成横向，训练图正方形→生成 512×512。画框和数据构图一致，模型才不会平铺出多余主体 |

---

### D.14 阶段五进度（2026-09-18，LLM 服务化 + 统一网关）

#### LLM 实测基线（llama.cpp / P100，benchmarks §2.5）

| 发现 | 数据 |
| --- | --- |
| decode 硬顶（带宽瓶颈） | ornith-9B-Q8_0：**28.7 tok/s**，与 ctx 长度无关 |
| **ubatch A/B（本项目第 4 个 P100 调优参数）** | prefill 162 → **513 tok/s（3.2×）**；机理三向验证：小 prompt 零变化、decode 零变化、长 prompt 3.1~3.2× |
| **Q4_K_M 反直觉慢于 Q8_0（-22%）** | Pascal 无 Tensor Core，k-quants 反量化开销 > 权重变小的带宽收益；**P100 显存够用就 Q8_0** |
| 14B 的 decode 随 ctx 变长降速 | 22.5 → 19.6 tok/s（-13% @1459 tok），9B 无此现象 |
| KV 前缀缓存 | 1400 tok 重复前缀 TTFT 2.74s → 0.6s |
| `--parallel 1` 并发 | 纯排队（max/min ≈ 2）→ 并发控制必须在网关层 |

#### 第 6 次推翻自己的结论：Proxmox 透传 per-process 归属

坑 #36 原结论"透传拿不到 per-process 显存归属"被用户质疑后复测**证伪**：`query-compute-apps` 正常列出 PID/进程/显存（它只列 C 型计算进程，阶段三测试时的空表≠不支持）。7 处文档同步修正。meta 教训：**下"平台不支持"的结论前，先确认测试时刻被测对象真实存在。**

#### 统一网关 aigc-gateway v0.1（Windows 开发验证通过）

- 架构：FastAPI :8300，出图（异步任务队列 + 单 worker 串行）+ LLM（流式透传）+ API Key 鉴权 + `/metrics`（aigc_* 指标）+ `/files` 结果下载
- 设计决策全部挂钩实测（串行 worker ← ComfyUI 串行执行；队列深度为核心指标 ← 两后端均为串行+排队）
- 后端地址环境变量化（8188 本机 / 8288 Ubuntu 裸机 / 8289 容器），**网关位置与算力解耦**
- 资源共存规则修正（24G 内存后实测）：**轻负载（LLM + SD1.5 出图）可共存，重负载（训练/SDXL）需切换**——推翻 0.7 节的旧结论

#### 本次踩坑记录（序 47~50，已计入 D.2 总表）

| 序 | 坑 | 解决 |
| --- | --- | --- |
| 47 | requests `decode_unicode` 按 Latin-1 解码，中文 UTF-8 的 0x85（NEL）被当换行切碎 SSE JSON | 按字节切行再手动 decode |
| 48 | 并发判定用 wall vs sum(totals) 双重计数误判"有并行" | max/min 耗时比：≈2 串行、≈1 并行 |
| 49 | PowerShell `set VAR=value` 不设进程环境变量 → uvicorn 拿不到 API key，鉴权静默失效 | `$env:VAR="value"`；文档须注明 shell 类型 |
| 50 | `taskkill /IM python.exe /F` 连坐误杀 ComfyUI | 按端口找 PID 精杀 |

#### 09-20 下半场增补（文档与开发者体验）

- `docs/07` 补 **§5 对外服务与集成**（13 章）：服务目录（OpenAI 兼容是集成关键决策）/ 两级暴露路径（含 nginx 参考配置）/ 异步消费模式 / 集成示例 / SLA 承诺纪律
- `docs/08-平台使用指南.md` 新增：业务系统/开发者/运维三角色操作手册（含每日巡检、故障速查、扩缩容信号）
- 网关开发者体验三轮迭代：根路径服务名片（裸 404 → 自描述）、/docs 开放 + Authorize 按钮、chat 端点 body 示例（坑 #53）
- 简历完成（`简历-AIGC工程方向.md`，占位符待本人填写）

#### 阶段五剩余待办

1. ~~网关 `/metrics` + llama-server `--metrics` 接入 Prometheus~~ ✅ **09-20 四 target 全 up**（dcgm / llama-server / aigc-gateway / prometheus）。llama-server 走 `--metrics`；网关绑 172.17.0.1 docker0 网桥（坑 #52）
2. ~~网关部署到 Ubuntu~~ ✅ **09-20 systemd 托管**（`aigc-gateway.service`）。含 **GPU 准入控制**（坑 #51：LLM 驻留时 768×512 出图 OOM → 空闲显存 < 2.6G 等待/拒绝）+ ComfyUI `expandable_segments` 碎片治理
3. ~~`docs/07-企业私有化AIGC平台方案.md`~~ ✅ **09-20 完成**：13 章方案（选型矩阵/对外服务与集成/TCO 模型/许可合规/安全分区/实施路线图），全部数字用实测基线支撑
4. ~~GitHub 作品集打包~~ ✅ **09-20 完成**（`AIGC-Engineer-Portfolio/`：README（架构+实测+六次修正叙事）+ docs 7 份 + scripts/gateway/docker/systemd/workflows/benchmarks + .gitignore），待 push；~~简历优化~~ ✅ **09-20 完成**（`简历-AIGC工程方向.md`，全部数字可追溯，占位符待本人填写）
5. （可选）Grafana 应用层面板：LLM tok/s（`rate(llamacpp:tokens_predicted_total[1m])`）、网关队列深度（`aigc_image_queue_depth`）

---

### D.15 SDXL LoRA（2026-09-21 启动）

**动机**：SDXL 比 SD1.5 更被岗位看重；用 dog_lora_v2（v4 同款 20 张柴犬）同主体训练，与 SD1.5 v4 形成**同数据集跨代对照**。

**当日前置战果**：

- **Grafana 应用层面板**（`docker/grafana/aigc-app-dashboard.json`，6 面板：LLM 吞吐/并发/TTFT、出图队列深度/耗时/产能，描述里内嵌基线值与判读法）
- **散热复测双阴性**（benchmarks §3.3）：风扇在位温度墙依旧（79°C 钉死、尾部 898MHz）；功率墙 170W 两头不讨好（中位 21.12 vs 17.73s）→ 有效路径=导风罩+高静压扇（另排期）

**训练配置（P100 16G）**：`sdxl_train_network.py` + SDXL base 1.0 | dim 32/alpha 16 | AdamW8bit | fp16 + SDPA + grad_ckpt + cache_latents_to_disk | batch 1 | resolution 1024 + **bucket_no_upscale**（训练图 ≤700px 不拉伸）| 2000 步，实测 2.66 s/it ≈ 1.5h（含温度墙税）

**坑 #54（当日产出，详见 D.2）**：SDXL VAE fp16 nan——loss 从 step 1 全 nan，`cache_latents_to_disk` 验尸 **20/20 latent 全 100% nan**；换 fp16-fix VAE（官方文件名是 `sdxl_vae.safetensors`，`sdxl_vae_fp16fix` 是 ComfyUI 圈改名）+ `--vae=` 后 **nan 0/20、loss 0.18 健康**。P100 无 bf16，这个坑无法靠精度切换绕过，只能靠修复版 VAE。

**评测预案（明天）**：

- **分辨率 640×448，不是 1024²**——坑 #46 的前瞻应用：训练构图是小尺寸横屏（主力 ~500×335），1024 会重蹈平铺覆辙
- 推理侧 VAE **同样必须 fp16fix**（同 nan 问题，否则黑图）
- 三步对照：SDXL 基线 → LoRA（strength 扫描）→ SD1.5 v4 同 prompt 跨代对比（压轴素材）
- 工作流已就位：`Scripts/Workflows/sdxl_lora_workflow_api.json`（VAELoader + LoraLoader + 四标题约定）

**✅ 09-22 收官**：训练 1h28m（2000 步，loss 0.18→0.143）。评测**一次通过**：基线动画风 vs LoRA 写实风黄柴（风格+毛色双收敛），640×448 下无多头无残肢——坑 #46/#54 教训前置应用的直接回报。推理稳态 ~25s/张（benchmarks §2.6，05 手册 §14）。

---

## 文档维护说明

- 当前版本：**V1.5 合订本**（2026-09-17 更新，含阶段一 + 阶段二完整 + 阶段三 100% + 阶段四首个 LoRA 闭环及根因修正）
- 历史版本（V1 / V2 / V1.1 / V1.2 / V1.3 / V1.4 / V1.5）保留在目录中作为历史记录
- 配套交付物：
  - `benchmarks-实测数据.md`（**18 轮 340 样本 + 7 轮 LLM** / 三机对照 / 修正记录 / 容器化开销量化）
  - `benchmarks/raw/`（25 轮原始数据，50 文件，可一键重算）
  - `ComfyUI-API开发手册.md`（8 章完整手册）
  - `docker/Dockerfile.p100` + `docker/Dockerfile.win`（双卡镜像，可复现）
  - `docs/03-ComfyUI容器化部署交付手册.md`
  - `docs/04-GPU监控与告警方案.md`
  - `docs/05-LoRA训练工程化实战手册.md`（阶段四交付物：全链路 + 评测方法论 + 四次误诊排错实录）
  - `docs/06-LLM服务化部署手册.md`（阶段五交付物：P100 调优四参数 + ubatch 3.2× + 网关）
  - `docs/07-企业私有化AIGC平台方案.md`（阶段五交付物：企业级方案，TCO/合规/路线图）
  - `docs/08-平台使用指南.md`（用户视角：业务系统/开发者/运维三角色操作手册）
  - `gateway/aigc-gateway/`（FastAPI 统一网关 v0.1：出图队列 + LLM 透传 + 鉴权 + metrics + GPU 准入）
  - `comfy_client/`（共享包：ComfyUI 客户端唯一实现）+ `Scripts/`（comfy_ws.py / comfy_batch_gen.py / llm_bench.py / inspect_workflow.py）
- **当前有效基线**：5060 中位 **1.94s**、P100 中位 **7.90s**（均为 WebSocket 精测）
- 进度：**阶段一 ✅ | 阶段二 ✅ | 阶段三 ✅ | 阶段四 ✅ | 阶段五 ✅ 全部完成（LLM 实测/网关生产化/三层监控/07 方案/作品集/简历）——16 周计划收官**
- 后续可选方向（按岗位价值排序）：**SDXL LoRA 🔵 训练中（D.15，明天评测）** → Grafana 应用层面板 ✅（09-21）→ 散热改造复测 ✅（09-21 双阴性，导风罩方案另排期）→ K8s 选修（附录 A）
- 阶段四路线：**sd-scripts 命令行**（放弃 kohya_ss GUI）
- 阶段四首个 LoRA：`myshiba_v4.safetensors`（SD1.5 / Shiba Inu）**可用**，触发词 `shiba inu`，**必须用横向分辨率（768/832/640×512）**
- **根因（D.13，推翻 D.12）**：LoRA 畸形不是数据集/优化器/精度问题，而是**生成分辨率与训练构图不匹配**——训练图为横向，生成却用了正方形 512×512。**v1/v2（AdamW8bit）在横屏下也好用，AdamW8bit 平反**
- **LoRA 批量验证（09-17，benchmarks §2.4）**：768×512/25步 批量 20 张 **100% 成功**，中位 17.73s，吞吐 3.63 张/分；**再次实锤温度墙且降速 ~30%**（单张工作量越大降速越狠）
- 下一步：可进阶段五（LLM 服务化 + 统一网关 + 作品集），或先做 SDXL LoRA（P100 16G 可训，岗位更看重）
- 剩余可选优化（不阻塞）：镜像再瘦身
- 任何时候踩到新坑，先补到 1.5 故障排查表，再决定是否出新版

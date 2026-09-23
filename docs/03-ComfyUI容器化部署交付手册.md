# ComfyUI 容器化部署交付手册

> **文档用途**：将 ComfyUI 推理服务以容器形式交付到生产/测试环境的完整操作手册。
> **适用对象**：交付工程师、运维工程师、需要复现本环境的协作者。
> **适用平台**：Ubuntu 22.04 + NVIDIA P100（Pascal sm_60）为主，含 RTX 5060（Blackwell sm_120）变体。
> **版本**：V1.0（2026-09-15，对应镜像 `aigc-comfyui:2.6.0-cu124`）

---

## 1. 概述

本手册描述一个**离线可复现**的 ComfyUI 容器化交付方案。核心设计原则：

> **构建只依赖本地确定性输入，不依赖构建时刻的外部网络状态。**

同一个 Dockerfile，今天、下周、下个月构建，产出**字节级一致**的镜像。

### 1.1 交付物清单

| 文件 | 用途 |
| --- | --- |
| `docker/Dockerfile.p100` | P100（sm_60）镜像定义 |
| `docker/Dockerfile.win` | RTX 5060（sm_120）镜像定义 |
| `docker/docker-compose.yml` | ComfyUI + dcgm + Prometheus + Grafana 全家桶 |
| `docker/monitoring/` | Prometheus 配置 + 告警规则 + Grafana provisioning |
| `wheels/` | torch 及依赖的离线 wheel（构建输入） |
| `comfyui-src.tar.gz` | ComfyUI 源码（构建输入，pin commit） |
| `comfyui_commit.txt` | 源码 commit 追溯 |
| `requirements.lock` | Python 依赖精确版本锁（101 个包） |

### 1.2 可复现性三要素

```
源码（本地 tar + COMMIT 文件追溯）
  + torch（本地 wheels/，版本由文件决定）
  + 依赖（requirements.lock，精确版本）
  = 可复现镜像
```

**缺任何一个，"pin 了 commit"就只是锁住了代码，依赖树还在漂。**

---

## 2. 环境前置条件

### 2.1 硬件与驱动

| 项 | P100 节点 | RTX 5060 节点 |
| --- | --- | --- |
| GPU | Tesla P100 16G（sm_60） | RTX 5060 8G（sm_120） |
| 驱动 | **535 分支锁死**（555+ 的 CUDA runtime 已废 sm_60） | ≥ 572.x |
| 系统内存 | ≥ 16 GB（实测 24 GB 可并行多实例） | 32 GB |
| 磁盘 | ≥ 30 GB 可用（镜像 ~5 GB + 构建缓存 + 数据卷） | ≥ 20 GB |
| 虚拟化 | Proxmox VE 透传可运行；dcgm 时间序列为整卡口径（per-process 可用 `nvidia-smi --query-compute-apps` 现场查，09-18 实测） | 物理机或 WSL2 |

### 2.2 软件

```bash
# Docker（实测 29.4.3）
docker version

# NVIDIA Container Toolkit（装 Docker 不会自动带上，必须单独装）
which nvidia-ctk

# GPU 直通验证 —— 这一条通过即可继续
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

### 2.3 国内网络适配清单（关键）

**容器是干净环境，不继承宿主机的任何代理/镜像配置。** 以下每一项都要在构建输入里显式声明，漏一个构建就会失败或卡死：

| 源 | 漏掉的症状 | 本方案解法 |
| --- | --- | --- |
| apt | 卡在 `apt-get update` 十几分钟 | `sed` 换清华 mirrors |
| pip | `Errno 101 Network is unreachable` | `ENV PIP_INDEX_URL=清华源` |
| HuggingFace | 运行时下模型失败 | `ENV HF_ENDPOINT=hf-mirror` |
| GitHub | `curl 16 HTTP2 error` / `early EOF` | **改用本地源码 tar** |
| torch wheel | 容器内带宽 ~250 B/s | **改用本地 wheels/** |

> **"能连上"和"能用"是两回事**：`curl -sI` 返回 200 只证明 TCP/TLS 通了，不代表带宽可用。测连通性要测实际吞吐：
> ```bash
> curl -o /dev/null -w '%{speed_download} B/s\n' --max-time 20 <url>
> ```

---

## 3. 构建

### 3.1 准备构建输入（一次性）

```bash
cd /opt/AIGC/docker_workspace

# ① 源码 tar（pin commit）
cd /opt/AIGC/ComfyUI-server
git rev-parse HEAD > /opt/AIGC/docker_workspace/comfyui_commit.txt
tar czf /opt/AIGC/docker_workspace/comfyui-src.tar.gz \
    -C /opt/AIGC/ComfyUI-server \
    --exclude=./venv --exclude=./models --exclude=./output \
    --exclude=./input --exclude=./user --exclude=./.git \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='*.log' --exclude='*.db' \
    .
```

> **⚠️ tar `--exclude` 必须用 `./` 锚定顶层目录。**
> `--exclude=models` 是全局模式匹配，会误删源码里的 `comfy/ldm/models/`。
> 打包后必须验证完整性：
> ```bash
> for d in comfy/ldm/models comfy/ldm/modules comfy_extras app utils; do
>   n=$(tar tzf comfyui-src.tar.gz | grep -c "^\./$d/")
>   echo "$d: $n $([ "$n" -gt 0 ] && echo OK || echo MISSING)"
> done
> ```

```bash
# ② torch wheel（宿主机下载，容器内带宽不可用）
/opt/AIGC/ComfyUI-server/venv/bin/pip download \
    torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0 \
    --index-url https://download.pytorch.org/whl/cu124 \
    -d /opt/AIGC/docker_workspace/wheels

# ③ 依赖版本锁（从已验证镜像导出）
docker run --rm --entrypoint ./venv/bin/pip aigc-comfyui:2.6.0-cu124 freeze \
  | grep -viE "^(torch|torchvision|torchaudio|nvidia-|triton)==" \
  > /opt/AIGC/docker_workspace/requirements.lock

# ④ requirements.lock 里其余包的 wheel（彻底离线）
/opt/AIGC/ComfyUI-server/venv/bin/pip download \
    -r requirements.lock -d wheels/ \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

### 3.2 执行构建

```bash
cd /opt/AIGC/docker_workspace

# ⚠️ 不要加 | tail —— 管道缓冲会让你全程看不到进度
docker build --progress=plain -f Dockerfile.p100 -t aigc-comfyui:2.6.0-cu124 .

# 要留档用 tee（不缓冲）
#   docker build --progress=plain -f Dockerfile.p100 -t ... . 2>&1 | tee build.log
```

**预期输出**：

```
源码 commit: 22e40d2ace0f53da025b3a41cbe4b664ef807097
cuDNN 补丁已存在（来自宿主机副本），跳过
构建自检通过: 2.6.0+cu124
构建自检: 源码完整性 OK
```

**镜像体积**：~5.07 GB（`base` 基础镜像 + `--mount=type=bind` 两个优化，比初版省 3.2 GB）。

### 3.3 构建期自检的四维覆盖

| 维度 | 检查项 | 挡住的问题 |
| --- | --- | --- |
| 版本 | torch 是 2.6.0+cu124 | requirements.txt 覆盖 torch |
| 补丁 | cuDNN 补丁在 main.py | 补丁丢失 |
| 依赖 | 关键第三方包可 import | 隐形依赖缺失（如 requests） |
| 源码 | 关键目录存在 | tar 误排除（构建期静默，运行时才炸） |

> **自检不要用 `import server`**：ComfyUI 依赖 `main.py` 的导入顺序预热 `sys.modules`，直接 `import server` 会制造真实启动时不存在的假错误。**诊断与自检都要用真实入口。**

---

## 4. 部署

### 4.1 端口规划

```bash
# 启动前必做端口冲突预检
ss -tlnp | grep -E ':(3000|8189|9090|9400)\b' && echo "冲突" || echo "可用"
```

| 服务 | 端口 | 说明 |
| --- | --- | --- |
| ComfyUI 裸机 | 8188 | 保留作对照 |
| llama-server | 8000 | 已占用 |
| **ComfyUI 容器** | **8189** | 新增 |
| dcgm-exporter | 9400 | |
| Prometheus | 9090 | |
| Grafana | 3000 | |

所有服务**只绑 127.0.0.1**，外部访问走 SSH 隧道，不直接暴露到局域网。

### 4.2 启动

```bash
cd /opt/AIGC/docker_workspace
mkdir -p /opt/AIGC/{output_container,input,custom_nodes}

# .env 里的 Grafana 密码（不进版本控制）
echo "GRAFANA_PASSWORD=你的密码" > .env && chmod 600 .env

docker compose config >/dev/null && echo "compose 语法 OK"
docker compose up -d
sleep 30
docker compose ps
```

### 4.3 验证

```bash
# 四服务健康
docker compose ps

# ComfyUI 容器
curl -s localhost:8189/system_stats | python3 -m json.tool | head -20

# 模型可见性（挂载路径对齐后）
curl -s localhost:8189/object_info/CheckpointLoaderSimple \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['CheckpointLoaderSimple']['input']['required']['ckpt_name'][0])"

# 监控链路
curl -s localhost:9400/metrics | grep -c "^DCGM_FI"     # P100 上应为 16
curl -s localhost:9090/api/v1/targets | python3 -c "
import sys,json
for t in json.load(sys.stdin)['data']['activeTargets']:
    print(t['labels']['job'], t['health'])"
curl -s localhost:3000/api/health
```

---

## 5. 日常运维

### 5.1 启动参数对照（两张卡不同，别抄错）

| 参数 | P100 | RTX 5060 |
| --- | --- | --- |
| `--disable-xformers` | **必须** | 不需要 |
| `--disable-cuda-malloc` | 可选（零影响，为环境一致保留） | **必须**（+2.6s 劣化 + 批量卡死） |
| cuDNN 补丁（main.py） | **必须** | 不需要 |

### 5.2 升级流程

```bash
# 1. 更新源码 commit
cd /opt/AIGC/ComfyUI-server && git checkout <new_commit>
git rev-parse HEAD > /opt/AIGC/docker_workspace/comfyui_commit.txt
# 重新打 tar（3.1 ①）

# 2. 更新版本锁（若依赖变化）
# 重新导出 requirements.lock（3.1 ③）

# 3. 重建 + 灰度替换
docker build --progress=plain -f Dockerfile.p100 -t aigc-comfyui:<new_tag> .
docker compose up -d --force-recreate comfyui
sleep 90 && docker compose ps   # 确认 healthy

# 4. 回滚（镜像 tag 不变即可秒级回退）
docker tag aigc-comfyui:<old_tag> aigc-comfyui:2.6.0-cu124
docker compose up -d --force-recreate comfyui
```

### 5.3 日志与清理

```bash
docker compose logs -f comfyui                    # 实时日志
docker compose logs comfyui --tail 100            # 最近 100 行
docker system df                                  # 磁盘占用总览
docker builder prune -f --filter until=24h        # 清理构建缓存（构建成功后）
```

日志轮转已在 `daemon.json` 和 compose 中配置（`max-size: 50m, max-file: 5`），不会撑爆磁盘。

### 5.4 备份范围

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| 模型库 | `/opt/AIGC/model_storage/` | 数据，必须备份 |
| 产出 | `/opt/AIGC/output*/` | 按需 |
| 配置 | `docker_workspace/`（compose、monitoring、.env） | 全部纳入版本控制 |
| 构建输入 | `wheels/`、`comfyui-src.tar.gz`、`requirements.lock` | 可重建，但备份可省去重新下载 |

---

## 6. 故障排查速查表

| 症状 | 排查方向 |
| --- | --- |
| 构建卡在 `apt-get update` | apt 源没换 → 3.1 换清华源 |
| 构建报 `Errno 101 Network is unreachable` | pip 源没配 → `ENV PIP_INDEX_URL` |
| `git clone` HTTP2 error / early EOF | 改用本地源码 tar（3.1 ①） |
| 容器内 torch 下载极慢 | 改用本地 wheels/（3.1 ②） |
| 容器反复重启 + `ModuleNotFoundError: requests` | requirements.txt 漏声明 → 显式 `pip install requests` |
| `No module named 'comfy.ldm.models'` | tar `--exclude=models` 误删源码 → 用 `./` 锚定（3.1 ①） |
| 镜像体积比预期大 3 GB | 用了 `COPY wheels/` → 改 `--mount=type=bind` |
| 启动后模型列表为空 `[]` | 挂载路径与 `extra_model_paths.yaml` 结构不一致 → 对齐 |
| `import server` 自检报 `'utils' is not a package` | 用错了入口，真实启动无此问题 → 用 `python main.py` 验证 |
| 容器看不到 GPU | Toolkit 未装 / 未 `nvidia-ctk runtime configure` / 未重启 docker |
| Grafana 部分面板为空 | P100 拿不到 DCGM 的 DCP 指标（需 Volta+），见监控方案 §3 |

---

## 7. 安全基线

- 所有服务只绑 `127.0.0.1`，外部访问走 SSH 隧道，**不直接暴露局域网**
- ComfyUI 本身**无鉴权**，绝不映射到 `0.0.0.0`
- Grafana 密码存 `.env`（`chmod 600`），不进版本控制；compose 用 `${VAR:?}` 语法强制要求
- 模型只下 `.safetensors`，不下 `.ckpt`（pickle 反序列化风险）
- ComfyUI-Manager 只装在本地实例，不装进对外交付的镜像

---

## 8. 附录

### 8.1 版本基线

| 组件 | P100 节点 | RTX 5060 节点 |
| --- | --- | --- |
| Docker | 29.4.3 | Docker Desktop（WSL2） |
| NVIDIA Container Toolkit | 已装并 configure | WSL2 GPU 支持 |
| ComfyUI commit | 22e40d2a | 22e40d2a |
| PyTorch | 2.6.0+cu124 | 2.11.0+cu128 |
| 依赖锁 | requirements.lock（101 包） | 各自维护 |

### 8.2 已知限制

| 限制 | 说明 |
| --- | --- |
| per-process 显存时间序列 | dcgm 为整卡口径（P100 无 DCP 指标）；现场归属排查可用 `nvidia-smi --query-compute-apps` / `fuser -v /dev/nvidia*`（09-18 实测可用） |
| DCGM DCP 指标 | P100（Pascal）拿不到，需 Volta+ |
| MIG 切分 | P100 无 MIG，GPU 只能整卡分配 |
| Windows 容器 GPU 性能 | 经 WSL2 转发有损，仅用于构建验证 |

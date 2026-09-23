# LoRA 训练工程化实战手册

> **文档用途**：在 P100（Pascal sm_60 / 16G）上完成 LoRA 训练全链路（数据集 → 打标 → 训练 → 评测 → 批量验证）的工程化操作手册与排错实录。
> **适用对象**：AIGC 工程 / AI Infra / MLOps 工程师，以及需要复用本流程的交付方。
> **版本**：V1.0（2026-09-18，对应首个 LoRA `myshiba_v4` 全链路闭环）

---

## 1. 概述

本手册记录一次**完整但不顺利**的 LoRA 训练：训练本身一次跑通（loss 健康下降、全程无报错），但评测阶段先后做出 **4 次错误诊断**（数据集多样性、优化器、文本编码器、精度），最终定位的真根因是最朴素的变量——**生成分辨率与训练数据构图不匹配**。

因此本手册的核心不是"怎么训练"（这部分半天就能跑通），而是两条来自实战的原则：

| 原则 | 来源 |
| --- | --- |
| **① 训练成功 ≠ 模型可用，评测方法论才是 LoRA 工程的主体** | 训练 62 分钟一次跑通；评测与排错花了 3 天、推翻了 4 个结论 |
| **② 排查时先核对最基础的变量，再怀疑复杂的嫌疑** | 根因是画幅（512×512 vs 768×512），不是数据集/优化器/TE/精度中的任何一个 |

**核心认知**：LoRA 工程的价值不在"会点训练按钮"，而在**能设计对照实验区分"模型坏了"和"用错了"**。本案例里 LoRA 从头到尾没坏过——是我们一直用错了画幅去测它。

---

## 2. 硬件约束：P100 训练五条铁律

P100（Pascal / sm_60 / 2016 年数据中心卡）训练的所有参数选择都被架构卡死。**开工前必记，违反任意一条直接报错或静默算错：**

| # | 铁律 | 违反后果 |
| --- | --- | --- |
| 1 | 只能 **fp16**（`mixed_precision=fp16` + `save_precision=fp16`） | bf16 直接报错（sm_60 无 bf16 硬件） |
| 2 | **没有 FP8** | sm_60 不支持，想都别想 |
| 3 | 注意力用 **SDPA**（`--sdpa`），关 xformers / Flash | CUDA error（与推理侧一致，见交付手册 03 §5.1） |
| 4 | **cuDNN 禁用**（同 ComfyUI 补丁） | 卷积初始化失败 `CUDNN_STATUS_NOT_INITIALIZED` |
| 5 | **bitsandbytes 需先验证再用** | 本项目实测 AdamW8bit 在 sm_60 **可用**（超预期，省 ~30% 优化器显存）——但用前必须跑最小验证 |

**外加一条热管理约束**：训练是连续满载几十分钟，必然撞 79°C 温度墙（见监控方案 04 §7），**训练时长会比满频预期多 ~15%**。批量生成场景下降速更狠（~30%，见 §9）。

### 2.1 预检脚本（训练前必跑）

```bash
# 四连验证：torch 版本 / CUDA+sm_60 / bitsandbytes / sd-scripts 可导入
/opt/AIGC/kohya_sd/venv/bin/python - <<'EOF'
import torch
assert torch.__version__.startswith("2.6.0+cu124"), torch.__version__
assert torch.cuda.is_available() and torch.cuda.get_device_capability() == (6, 0)
print("torch + sm_60 OK")

import bitsandbytes as bnb
x = torch.randn(8, 8, device="cuda")
opt = bnb.optim.AdamW8bit([torch.nn.Parameter(x)], lr=1e-4)
opt.step(); print("bitsandbytes AdamW8bit OK on sm_60")

import library.train_util; print("sd-scripts import OK")
EOF
```

> **为什么必须验证 bitsandbytes**：社区对它的 sm_60 兼容性说法不一，文档里原本准备了 AdamW/Lion 备用方案。**"听说能用/不能用"都不算数，最小验证脚本跑过才算数。**

---

## 3. 路线决策：sd-scripts CLI，放弃 kohya_ss GUI

阶段四原计划用 `kohya_ss`（bmaltais）的 Gradio GUI，但探测其 `requirements_linux.txt` 发现硬卡 **`torch==2.7.0+cu128`**——与 P100 双重不兼容：

| 层面 | 问题 |
| --- | --- |
| CUDA 版本 | cu128 = CUDA 12.8，需驱动 ≥ 570；P100 锁死 535（CUDA 12.2 上限） |
| 驱动 | 535 是 P100 的终局驱动（555+ 的 CUDA runtime 已移除 sm_60），升不了 |

kohya_ss GUI 本质是 **Gradio 前端 + 底层 sd-scripts**。改走 sd-scripts 命令行的四个理由：

| 维度 | kohya_ss GUI | sd-scripts CLI |
| --- | --- | --- |
| 依赖体量 | 巨大（Gradio + 前端库），版本脆弱 | 小，核心就是训练引擎 |
| 可复现性 | 参数点界面，难记录 | **参数全在脚本里，git 可追踪** |
| 简历价值 | "会用 GUI" | "能写参数化、可复现的训练脚本" |
| 与运维背景契合 | 弱 | **强**（脚本化、自动化、可审计） |

两者用同一个训练引擎，产出相同的模型格式。**这个转向让阶段四的产出物（参数化训练脚本 + 本手册）天然就是交付物本身。**

---

## 4. 环境部署（一次通过）

```
/opt/AIGC/kohya_sd/          # sd-scripts 仓库
└─ venv/                     # 独立 venv（与 ComfyUI venv 隔离，互不污染）
```

关键版本基线（P100 训练环境）：

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| PyTorch | **2.6.0+cu124** | 与 ComfyUI 推理侧一致；**注意训练 venv 独立，装依赖时最容易被 requirements 覆盖 torch** |
| transformers | **4.54.1** | sd-scripts requirements 主动 pin 在 4.x，避开了 transformers 5.x 要求 torch≥2.5 的坑 |
| diffusers / accelerate | 0.32.1 / 1.6.0 | |
| bitsandbytes | 无版本号 | 装完必须跑 §2.1 的最小验证 |
| TensorFlow | **不装** | 见 §5.2——WD14 走 ONNX 路径后 TF 全链路不需要 |

> **教训（与推理侧同源）**：任何 `pip install -r requirements.txt` 之后，第一件事是 `pip list | grep torch` 确认 torch 没被覆盖。这个项目里 torch 被覆盖炸过 **4 次**。

---

## 5. 数据集与打标

### 5.1 数据集原则（第一原则：主体一致性）

阶段四开工时定的原则是"**训特定的一只狗，主体越一致越好**"，依据：

- subject LoRA 学的是触发词到视觉概念的映射，**主体一致，模型好学，第一次必过**
- 图片要求：多角度 / 多姿态 / 多光照 / 背景干净，**多样性 > 数量**（30 张高质量优于 50 张凑数）

**实际执行**：身边没有可拍的狗，改用 **Stanford Dogs Dataset**（官方 `images.tar` 750MB），选 `n02115641` = Shiba Inu（柴犬），取 ~50 张后**删除含人物料**，最终 ~20 张。

> **自我检讨**：用 breed 子集（多只不同个体）违背了"单一主体"原则。事后证明这不是畸形根因（见 §8），但**原则就是原则——第一个 LoRA 不该给自己加难度**。

### 5.2 打标：WD14 Tagger 的 ONNX 路径（绕开 TF 依赖地狱）

WD14 Tagger 的官方模型是 TF SavedModel 格式，在干净 venv 里会撞上**四层连环坑**：

```
缺 tensorflow → 装 TF 2.15+
  → Keras 3 不认旧 SavedModel 格式（报错）
    → 降 TF 2.15.1 → DataLossError（variables 目录缺失）
      → 换 --onnx 路径 → onnx 拉进 numpy 2.x → 与 TF 的 ml_dtypes 冲突
        → transformers 可选导入 TF 时整个崩掉
```

**最终解法（两步，稳定可复现）**：

```bash
# ① 彻底卸载 TF —— transformers 检测到 TF 缺席会自动切 torch 后端
pip uninstall -y tensorflow tensorflow-intel keras ml_dtypes
# ② 打标走 ONNX 路径（P100 上 onnxruntime 用 CPU 版即可，打标不是性能瓶颈）
python tag_images_by_wd14_tagger.py --onnx --batch_size 8 /opt/AIGC/training_dataset/dog_shiba/images
```

> **经验**：当一个依赖链连续三层都是版本兼容问题，**正确答案通常不是继续修，而是换一条路**。`--onnx` 一个参数甩掉了整个 TF 生态。

### 5.3 标签质量检查（必须人工抽查）

自动打标后**逐张抽查标签文件**，本项目当场抓到问题：部分图被打成人物标签（`1boy / shirt / male_focus`）——因为训练集里混了"人抱狗"的图，WD14 把视觉重心判给了人。

**处理**：删除含人物料 → 重新打标 → 确认标签为 `no_humans / animal_focus / shiba_inu / dog` 这类主体正确的词。

### 5.4 触发词设计

| 方案 | 结论 |
| --- | --- |
| 自造词 `myshiba` | ❌ **弃用**。未训练 token 的嵌入是噪声，**即使 LoRA strength=0，提示词里带着它也会诱发多头**——它会把你的"基线对照"污染成假基线（见 §7.3） |
| 品种名 `shiba inu` | ✅ **采用**。基线模型已认识这个概念，LoRA 只需做风格/主体微调，且不会污染对照实验 |

`dataset_config.toml` 关键配置：`keep_tokens=1`（保住每行第一个 tag 即触发词不被 shuffle）、`num_repeats=10`、batch 4。步数公式：`总步数 = ceil(图片数 × repeat × epoch / batch_size)`。

---

## 6. 训练

### 6.1 配置基线（SD1.5 LoRA / P100）

| 参数 | 值 | 备注 |
| --- | --- | --- |
| 底模 | v1-5-pruned-emaonly.safetensors | 与推理侧同一文件 |
| network | lora, **dim=16, alpha=8** | |
| optimizer | AdamW8bit（v4 对照用 AdamW） | 见 §8——两个优化器都不是问题 |
| precision | fp16（mixed + save） | 铁律 1 |
| attention | `--sdpa` | 铁律 3 |
| 其他 | gradient_checkpointing, cache_latents, batch=4, cosine, warmup=75 | |
| 文本编码器 LR | UNet LR 的一半 | 若用自造 token 必须训 TE；若 freeze TE（`network_train_unet_only`），自造词永远学不会 |

### 6.2 实测结果（v1，一次跑通）

```
train_network.py  |  1260 步  |  62 分钟  |  P100
loss: 0.205 → 0.183（稳步下降，健康）
产出: /opt/AIGC/training_output/dog_lora_v1/myshiba_v1.safetensors
```

v4（一致数据集重训版）loss 同样健康：epoch 1→10，0.215 → 0.142。

> **loss 健康下降 + pipeline 无报错，说明参数（dim/lr/precision/optimizer）全部正确。** 这是后面排错时能果断排除"参数嫌疑"的依据。

### 6.3 训练中的两个监控注意点

| 现象 | 结论 |
| --- | --- |
| **训练中途自动出的样图粗糙/畸形** | **不作数**。sample 用 ddim/20步/无负面词，质量天然差。评测必须走 ComfyUI + 正经参数（§7） |
| **训练时长比预期多 ~15%** | 温度墙（满载几十分钟必撞 79°C 拐点）。属预期内，不是故障 |

### 6.4 权重健康检查（check_lora.py）

训练产出后用脚本检查 safetensors 张量统计（范数分布、是否有 NaN/Inf）。**注意误报**：本项目检查脚本把 `alpha=8` 的常量张量（max=8.000）误标为异常——**常量不是权重，检查脚本要排除标量/常量张量**。

---

## 7. 评测方法论（本手册最核心的章节）

### 7.1 为什么评测比训练难

训练有 loss 曲线这个客观指标；**评测没有自动指标，全靠对照实验设计**。设计错了，结论就错——本项目前 4 次诊断全错，都错在实验设计。

### 7.2 三步对照实验（标准流程）

拿到 LoRA 后，按顺序做三个测试，**每一步只改一个变量**：

| 步骤 | 提示词 | LoRA | 回答的问题 |
| --- | --- | --- | --- |
| ① 真基线 | **不含触发词** | 关闭 | 底模 + pipeline 健康吗？ |
| ② LoRA 测试 | 含触发词 | 开启（strength 0.5 起） | LoRA 生效了吗？引入问题了吗？ |
| ③ 强度扫描 | 含触发词 | 0.1 ~ 0.8 梯度 | 最佳强度区间在哪？ |

**评测参数必须正经**：负面词齐全、euler、25 步——**绝不用训练中途样图下结论**。

### 7.3 假基线陷阱（本项目实测）

| 测试 | 提示词 | LoRA | 结果 | 判定 |
| --- | --- | --- | --- | --- |
| 假基线 | 含 `myshiba` | strength **0** | 两个狗头 | ❌ 被自造词污染，**不算数** |
| 真基线 | 删掉 `myshiba` | 关闭 | ✅ 正常单只柴犬 | 模型+pipeline 健康 |

**未训练 token 的嵌入向量是噪声，它待在提示词里就会扰动生成——和 LoRA 强度无关。** 所以对照实验里，"基线"和"测试"的提示词必须只差触发词本身，不能用"strength=0 但提示词带触发词"当基线。

### 7.4 嫌疑清单驱动的排除法

当②出问题而①正常，问题是 LoRA 引入的。此时**列出全部嫌疑变量，逐一设计对照实验排除**，而不是凭直觉猜：

| 嫌疑 | 对照设计 | 本项目结果 |
| --- | --- | --- |
| 数据集 | 多样（v1）vs 一致（v2/v4） | 都失败 → 排除 |
| 优化器 | AdamW8bit vs AdamW | 都失败 → 排除 |
| 强度 | 0.1~0.8 全扫 | 都失败 → 排除 |
| 文本编码器 | 同 LR vs 半速 LR | 都失败 → 排除 |
| 推理环境 | P100 vs RTX 5060 | 都失败 → 排除 |
| 触发词 | myshiba vs shiba inu | 都失败 → 排除 |
| **生成分辨率** | **512×512 → 768×512** | **✅ 一张完整的狗** |

**六连败之后，最后一行才是根因。** 这张表就是 §8 故事的骨架。

---

## 8. 排错实录：四次误诊到真根因

> 本节是本项目**第五次推翻自己结论**的完整记录（前四次见 benchmarks §0）。按时间顺序呈现，不删改历史。

### 8.1 误诊时间线

| 轮次 | 诊断 | 依据 | 结果 |
| --- | --- | --- | --- |
| 1 | **数据集多样性**：50 只不同柴犬的"平均主体"导致形态模糊 | 多只个体 → 触发词学到模糊概念 | ❌ 换 20 张高度一致的数据重训，**问题依旧** |
| 2 | **AdamW8bit 在 sm_60 上算错** | 老架构 + 8bit 优化器，可疑 | ❌ v4 换 AdamW，**问题依旧**（AdamW8bit 事后平反） |
| 3 | **文本编码器被训坏** | TE LR 过高可能冲坏语义 | ❌ 半速 LR 重训，**问题依旧** |
| 4 | **fp16 精度不足** | sm_60 无 bf16，只能 fp16 | ❌ 换 5060（fp16/bf16 都行）推理，**问题依旧** |
| **真** | **生成分辨率与训练构图不匹配** | 512×512 → 768×512 一换就好 | ✅ **确诊** |

### 8.2 根因：构图比例不匹配

```
训练数据：Stanford 柴犬，绝大多数横向构图（宽 > 高）
        ↓ LoRA 学到"狗在横向画框里"
生成 512×512（正方形）→ 横向构图塞不进 → 平铺出第二个头 → 双头/多头/无狗
生成 768/832/640×512（横向）→ 构图匹配 → ✅ 正常单只狗
```

**为什么基线模型在 512×512 下正常？** 底模没被横向数据带偏，对各种构图都适应；LoRA 聚焦了横向构图，在正方形画框里才会平铺。**这恰恰证明 LoRA 学到了东西——它从来就没坏过，只是我们一直用错了画幅去测它。**

### 8.3 meta 教训

**排查时别只盯着复杂的嫌疑，先核对最基础的变量。** 画幅、路径、端口、大小写这类"不起眼"的东西，在本项目里贡献了多个坑（`/opt/AIGC` vs `/opt/aigc`、`Workflows` vs `workflows`、以及这次的分辨率）。**嫌疑清单要按"验证成本"排序，不是按"可疑程度"排序**——改分辨率是零成本实验，却排到了最后。

### 8.4 最终正确使用方式

| 项 | 值 |
| --- | --- |
| 触发词 | `shiba inu`（品种名即可，**别用自造词**） |
| 分辨率 | **768×512 / 832×512 / 640×512**（横向，匹配训练构图） |
| strength | 0.5 起 |
| 负面词 | 含 `multiple dogs / two dogs / multiple heads / two heads / cloned face / extra ears` |
| 提示词骨架 | `shiba inu, 1dog, solo, one animal, <姿态>, best quality, detailed fur, sharp focus` |

---

## 9. 批量生成验证（链路最后一环）

用 `comfy_batch_gen.py` + LoRA 工作流（base + LoraLoader）在 P100 裸机跑 20 张批量（5 prompts × 4 seeds），数据已入 benchmarks §2.4：

| 指标 | 值 |
| --- | --- |
| 成功率 | **100%（20/20）** |
| 中位耗时 | 17.73 s |
| 冷态（满频）/ 降频后 | ~14.4 s / ~18.7 s（**降速 ~30%**） |
| 吞吐 | 3.63 张/分 |
| 显存峰值 | 2.77 GB |

**两个必须说透的点**：

1. **首张 0.35s 是 execution cache 命中**（此前手动跑过相同 seed），不是真实生成。`min_s`/`avg_s` 被它拉偏，**真实耗看位数**。基准测试换没跑过的 seed。
2. **温度墙降速幅度随单张工作量放大**：512×512/20步 降 ~14% → 768×512/25步 降 ~30%。**单张工作量越大，越早越狠地撞 79°C 拐点。** 大分辨率批量场景下，散热改造的性价比比直觉更高——这是容量规划的直接输入。

---

## 10. 故障排查速查表

| 症状 | 根因 | 解法 |
| --- | --- | --- |
| WD14 打标报 Keras/SavedModel/DataLossError | TF 版本连环不兼容 | 卸 TF，走 `--onnx`（坑 41/42） |
| 打标后标签全是人物词 | 训练图含人，WD14 判错主体 | 删人物料重打标，逐张抽查（§5.3） |
| 训练中途样图畸形 | sample 用 ddim/20步/无负面词 | 不作数，评测走 ComfyUI 正经参数（坑 43） |
| strength=0 的"基线"也畸形 | 自造 token 噪声嵌入污染 | 基线提示词必须删掉自造词（坑 44） |
| 自造触发词完全学不会 | `network_train_unet_only` 冻结了 TE | 自造 token 必须训 TE（TE LR = UNet 一半） |
| 检查脚本报 max=8.000 异常 | alpha 常量被误标 | 排除标量/常量张量（§6.4） |
| **LoRA 出双头/多头/无狗，基线正常** | **生成分辨率与训练构图不匹配** | **横屏数据 → 768/832/640×512 生成（坑 46）** |
| 训练比预期慢 ~15% | 温度墙 | 预期内；散热改造 ROI 见 §9 |

---

## 11. 工程产出清单（可复用）

| 产出 | 位置 | 复用价值 |
| --- | --- | --- |
| 参数化训练环境 | `/opt/AIGC/kohya_sd/venv` | 下一个 LoRA 直接用，预检脚本 §2.1 |
| 数据集配置 | `/opt/AIGC/training_dataset/dataset_config.toml` | keep_tokens / repeats / batch 模板 |
| 打标流水线 | WD14 `--onnx` + 删 TF | 稳定可复现，绕开 TF 地狱 |
| 标签清洗/触发词脚本 | `clean_tags.py` / `add_trigger.py` | 数据集处理模板 |
| 权重检查脚本 | `check_lora.py`（注意常量误报） | 产出物健康检查 |
| LoRA 批量验证 | `Scripts/comfy_batch_gen.py` + LoRA 工作流 | 评测→批量的标准收口动作 |
| 实测数据 | benchmarks §2.4 + `bench_20260917_171304.json` | 容量规划输入 |

## 12. 面试叙事要点

1. **"我的 LoRA 训练一次跑通，但评测推翻了 4 个结论"** —— 重点不是犯错，而是**每次误诊都有对照实验支撑、每次推翻都留了记录**。能讲清"假基线陷阱"（strength=0 还被自造词污染）和"嫌疑清单按验证成本排序"这两条方法论。
2. **"根因是最朴素的变量"** —— 数据集/优化器/TE/精度全排除后，根因是画幅。展示的是**系统性排除法**，不是运气。
3. **"训练环境被硬件卡死，五条铁律全部来自架构约束"** —— fp16/SDPA/cuDNN/bitsandbytes 验证，每条都能讲出违反的后果。
4. **"放弃 GUI 走 CLI 是工程决策"** —— 依赖体量、可复现性、简历价值三个维度，且产出物即交付物。
5. **"批量验证顺带量化了温度墙的 scaling law"** —— 降速幅度随单张工作量放大（14%→30%），把排错产出变成了容量规划输入。

---

## 13. 附录

### 13.1 版本基线（P100 训练节点）

| 组件 | 版本 |
| --- | --- |
| sd-scripts | kohya-ss/sd-scripts（CLI） |
| PyTorch | 2.6.0+cu124 |
| transformers / diffusers / accelerate | 4.54.1 / 0.32.1 / 1.6.0 |
| 底模 | SD 1.5（v1-5-pruned-emaonly） |
| 首个 LoRA | `myshiba_v4.safetensors`（Shiba Inu，dim16/alpha8） |

### 13.2 已知限制与下一步

| 项 | 说明 |
| --- | --- |
| ~~SDXL LoRA~~ | ✅ **09-22 已完成**（见 §14 附录）：P100 16G 可训，岗位更看重 |
| fp16 唯一精度 | sm_60 无 bf16/FP8，SDXL 训练显存会更紧 |
| 训练时长温度墙税 | ~15%；大分辨率批量生成场景 ~30% |

---

## 14. 附录：SDXL LoRA（2026-09-21/22 实测）

> SD1.5 v4 的**同数据集跨代对照**（dog_lora_v2，20 张柴犬）。一次通过——因为 SD1.5 的教训全部前置应用了。

### 14.1 与 SD1.5 的配置差异

| 项 | SD1.5 | SDXL | 原因 |
| --- | --- | --- | --- |
| 脚本 | `train_network.py` | `sdxl_train_network.py` | SDXL 双文本编码器 |
| dim / alpha | 16 / 8 | **32 / 16** | SDXL 惯例大一档 |
| batch | 4 | **1** | 16G 显存约束 |
| 分辨率 | 512 | 1024 + **bucket_no_upscale** | SDXL 原生 1024；小图不拉伸 |
| **VAE** | 底模自带 | **必须外挂 fp16-fix VAE** | ⚠️ 见下 |
| 步速 / 时长 | ~3s/it，62min/1260步 | **2.63 s/it，1h28m/2000步** | 模型大 3 倍 |

### 14.2 SDXL 独有的两个坑（均已实锤记档）

**① VAE fp16 nan（坑 #54，SDXL 最著名天坑）**

SDXL 的 VAE 在 fp16 下产生 NaN。症状：loss 从 step 1 全 `avr_loss=nan`。**验尸方法**：`cache_latents_to_disk` 后直接查 npz——本案例 20/20 个 latent 100% nan。

解法：`madebyollin/sdxl-vae-fp16-fix` + 训练加 `--vae=`。**注意官方文件名是 `sdxl_vae.safetensors`**（`sdxl_vae_fp16fix` 是 ComfyUI 圈改名，按后者找会 404）。P100 无 bf16，无法用"换精度"绕过，修复版 VAE 是唯一解。**推理侧同样必须挂 fp16fix VAE**（否则黑图/nan 图）。

**② 评测分辨率不是 1024（坑 #46 的 SDXL 版）**

SDXL 教程都说 1024² 出图，但本数据集训练构图是 ≤700px 小图横屏（`bucket_no_upscale` 保留原尺寸）。**评测分辨率必须匹配训练构图：640×448 一次通过；若用 1024²，会重蹈 512² 平铺出多头的覆辙。**

### 14.3 评测结果（三步对照，同 seed 1001）

| 测试 | 结果 |
| --- | --- |
| 基线（strength 0） | 动画风柴犬 |
| LoRA 0.5~1.0 | 写实风、单只完整黄柴、无多头无残肢 |

**风格收敛（动画→写实）+ 毛色收敛（黄）= LoRA 生效的直接证据。** 推理稳态 ~25s/张（640×448/25步，P100），详细数据见 benchmarks §2.6。

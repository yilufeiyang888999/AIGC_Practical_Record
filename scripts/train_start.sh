#!/usr/bin/env bash
# LoRA 训练启动脚本（sd-scripts CLI / P100 Pascal sm_60）
#
# 为什么是脚本而不是 GUI：参数全部落在文件里，git 可追踪、可复现、可审计。
# kohya_ss GUI 因硬卡 torch==2.7.0+cu128 与 P100 不兼容而放弃（见 docs/05 §3）。
#
# 用法：
#   ./train_start.sh sd15  --name myshiba_v5 --data /opt/AIGC/dataset/dog_lora_v2
#   ./train_start.sh sdxl  --name myshiba_sdxl_v2 --data /opt/AIGC/dataset/dog_lora_v2
#   ./train_start.sh sd15 --help
#
# 训练前必跑预检（四连验证 torch/sm_60/bitsandbytes/sd-scripts），见 docs/05 §2.1。

set -euo pipefail

# ── 路径基线（按实际部署调整）────────────────────────────────────
VENV="${VENV:-/opt/AIGC/kohya_sd/venv/bin/python}"
SD_SCRIPTS="${SD_SCRIPTS:-/opt/AIGC/kohya_sd/sd-scripts}"
MODEL_DIR="${MODEL_DIR:-/opt/AIGC/model_storage}"
OUT_ROOT="${OUT_ROOT:-/opt/AIGC/training_output}"
LOG_DIR="${LOG_DIR:-/opt/AIGC/logs}"

# 总步数公式：图片数 × repeat × epoch / batch
# 例：20 张 × repeat 14 × 10 epoch / batch 4 = 700 步（按需调 repeat/epoch，别硬写 max_train_steps）
REPEAT="${REPEAT:-14}"
EPOCH="${EPOCH:-10}"

# ── 参数解析 ─────────────────────────────────────────────────────
usage() {
  cat <<'EOF'
用法: train_start.sh <sd15|sdxl> [选项]
  --name NAME       输出目录名（默认 lora_YYYYmmdd_HHMMSS）
  --data DIR        数据集目录（必填，内含 <repeat>_<class>/ 结构或直接图片+txt）
  --repeat N        每张图重复次数（默认 14）
  --epoch N         训练轮数（默认 10）
  --batch N         批大小（sd15 默认 4 / sdxl 默认 1）
  --dim N           网络维度（sd15 默认 16 / sdxl 默认 32）
  --lr FLOAT        学习率（sd15 默认 1e-4 / sdxl 默认 1e-4）
  --dry-run         只打印命令不执行
EOF
}

MODEL="${1:-}"
if [[ -z "$MODEL" || "$MODEL" == "--help" || "$MODEL" == "-h" ]]; then usage; exit 0; fi
shift || true

NAME="lora_$(date +%Y%m%d_%H%M%S)"
DATA=""
BATCH="" DIM="" LR=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)   NAME="$2";   shift 2 ;;
    --data)   DATA="$2";   shift 2 ;;
    --repeat) REPEAT="$2"; shift 2 ;;
    --epoch)  EPOCH="$2";  shift 2 ;;
    --batch)  BATCH="$2";  shift 2 ;;
    --dim)    DIM="$2";    shift 2 ;;
    --lr)     LR="$2";     shift 2 ;;
    --dry-run) DRY_RUN=1;  shift ;;
    *) echo "未知参数: $1" >&2; usage; exit 1 ;;
  esac
done

[[ -n "$DATA" ]] || { echo "❌ 必须指定 --data" >&2; exit 1; }
[[ -d "$DATA" ]] || { echo "❌ 数据集目录不存在: $DATA" >&2; exit 1; }

# ── 训练前硬校验：别让 1.5 小时的训练跑在第 3 步才发现环境不对 ────
echo "▶ 预检：torch / sm_60 / sd-scripts"
"$VENV" - <<'PYEOF'
import sys, torch
assert torch.__version__.startswith("2.6.0+cu124"), f"torch 版本不符: {torch.__version__}"
assert torch.cuda.is_available(), "CUDA 不可用"
cap = torch.cuda.get_device_capability(0)
assert cap == (6, 0), f"计算能力应为 (6,0)=P100，实际 {cap}"
print(f"  torch {torch.__version__} | sm_{cap[0]}{cap[1]} OK")
PYEOF

echo "▶ 预检：显存是否够（训练与 LLM / SDXL 推理互斥，见 docs/06 共存规则）"
"$VENV" - <<'PYEOF'
import subprocess, shutil
if not shutil.which("nvidia-smi"):
    print("  ⚠️ 无 nvidia-smi，跳过显存检查")
else:
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
        text=True).strip().split(",")
    used, total = float(out[0]), float(out[1])
    free = total - used
    print(f"  显存 {used:.0f}/{total:.0f} MiB，空闲 {free:.0f} MiB")
    if free < 12000:
        print("  ❌ 空闲显存 < 12G。llama-server 是否还在跑？"
              " 训练前必须停 LLM：sudo systemctl stop llama-server")
        sys.exit(1)
PYEOF

mkdir -p "$OUT_ROOT" "$LOG_DIR"
OUT_DIR="$OUT_ROOT/$NAME"
LOG_FILE="$LOG_DIR/train_${NAME}_$(date +%H%M%S).log"

# ── 按模型组装命令 ───────────────────────────────────────────────
case "$MODEL" in
  sd15)
    SCRIPT="train_network.py"
    BASE="$MODEL_DIR/checkpoints/v1-5-pruned-emaonly.safetensors"
    BATCH="${BATCH:-4}"; DIM="${DIM:-16}"; ALPHA=$((DIM / 2)); LR="${LR:-1e-4}"
    RES="512,512"
    EXTRA=()
    ;;
  sdxl)
    SCRIPT="sdxl_train_network.py"
    BASE="$MODEL_DIR/checkpoints/sd_xl_base_1.0.safetensors"
    BATCH="${BATCH:-1}"; DIM="${DIM:-32}"; ALPHA=$((DIM / 2)); LR="${LR:-1e-4}"
    RES="1024,1024"
    # ⚠️ SDXL fp16 VAE 会产生 100% nan（坑 #54）。P100 无 bf16 可绕，
    # 必须外挂 fp16-fix VAE。文件名是 sdxl_vae.safetensors（官方名），
    # 按 sdxl_vae_fp16fix 找会 404。
    EXTRA=(--vae "$MODEL_DIR/vae/sdxl_vae.safetensors" --bucket_no_upscale)
    ;;
  *)
    echo "❌ 未知模型: $MODEL（可选 sd15 / sdxl）" >&2; usage; exit 1 ;;
esac

[[ -f "$BASE" ]] || { echo "❌ 底模不存在: $BASE" >&2; exit 1; }

CMD=(
  "$VENV" "$SD_SCRIPTS/$SCRIPT"
  --pretrained_model_name_or_path "$BASE"
  --train_data_dir "$DATA"
  --output_dir "$OUT_DIR"
  --output_name "$NAME"
  --resolution "$RES"
  --network_module networks.lora
  --network_dim "$DIM" --network_alpha "$ALPHA"
  --learning_rate "$LR"
  --unet_lr "$LR"
  --text_encoder_lr "$(python -c "print(f'{float(\"${LR}\")/2:g}')" 2>/dev/null || echo 5e-5)"
  --optimizer_type AdamW8bit          # P100 sm_60 已实测可用（预检脚本验证过）
  --mixed_precision fp16              # 铁律 1：sm_60 无 bf16，必须显式 fp16
  --save_precision fp16
  --sdpa                              # 铁律 3：xformers / flash-attn 在 sm_60 无内核
  --gradient_checkpointing
  --cache_latents_to_disk             # VM 只有 16G 内存，latents 不能常驻内存
  --train_batch_size "$BATCH"
  --max_train_epochs "$EPOCH"
  --lr_scheduler cosine
  --lr_warmup_steps 75
  --save_every_n_epochs 1
  --logging_dir "$OUT_DIR/logs"
  --sample_every_n_epochs 1
  --sample_prompts "$OUT_DIR/../sample_prompts.txt"
)
CMD+=("${EXTRA[@]}")

echo "▶ 输出目录: $OUT_DIR"
echo "▶ 日志: $LOG_FILE"
echo "▶ 总步数预估: 图片数 × repeat($REPEAT) × epoch($EPOCH) / batch($BATCH)"
echo
printf '%q ' "${CMD[@]}"; echo; echo

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "--dry-run：未执行"
  exit 0
fi

# tee 而不是重定向：训练 1.5 小时，必须能实时看到 loss 曲线，
# 而不是等跑完才发现 step 1 就是 nan。
"${CMD[@]}" 2>&1 | tee "$LOG_FILE"

echo
echo "✅ 训练结束。权重健康检查："
echo "   python scripts/check_lora.py $OUT_DIR/${NAME}.safetensors"
echo "⚠️  注意：检查脚本会把 alpha=$ALPHA 的常量张量误报为异常——常量不是权重。"

#!/bin/bash
# llm-switch.sh 定稿版（2026-09-18）——仓库存档，实际运行于 Ubuntu /opt/llama.cpp/
# 相对初版的 5 项改进：
#   1. 删除盲 gpu-reset → query-compute-apps 检测占卡进程 + 显存水位兜底，人工确认
#   2. 新增 --metrics（Prometheus 指标，接入监控栈）
#   3. --ubatch-size 32→256（prefill 3.2×，benchmarks §2.5.4）
#   4. 菜单注释与实际参数对齐（Q8 ctx=65536）
#   5. 启动信息 IP 改为动态获取（原硬编码 172.16.104.100 与实际不符）
# ================ 配置区域（llama.cpp 最新版 master） ================
MODEL_DIR="/opt/llama.cpp/models"
SERVER_BIN="/opt/llama.cpp/build/bin/llama-server"
PORT=8000
CUDA_DEVICE=0
# ================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 如果传了参数 --keep-running，则不杀已有进程
if [ "$1" != "--keep-running" ]; then
    echo -e "${YELLOW} 正在检查 GPU 占用...${NC}"
    pkill -9 -f "llama-server" 2>/dev/null
    sleep 2

    # 不再盲 gpu-reset（会无差别炸掉同卡所有进程；透传下支持性不明）
    # 主信号：query-compute-apps（09-18 实测透传下可用，但只列 C 型计算进程）
    # 兜底：显存水位（空闲基线 ~260 MiB，阈值留一倍余量，兜住 G 型进程与异常状态）
    VRAM_USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -i $CUDA_DEVICE 2>/dev/null)
    GPU_APPS=$(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null)

    if [ -n "$GPU_APPS" ] || [ "${VRAM_USED:-0}" -gt 500 ]; then
        echo -e "${RED}⚠️  检测到 GPU 仍被占用（显存 ${VRAM_USED:-?} MiB）：${NC}"
        [ -n "$GPU_APPS" ] && echo "$GPU_APPS"
        echo -e "${YELLOW}继续启动可能争抢显存导致 OOM。${NC}"
        read -p "仍要继续？[y/N] " GO
        [ "$GO" = "y" ] || { echo "已取消。"; exit 1; }
    fi
fi

# 【P100专属优化】仅追加环境变量，无破坏性修改
export CUDA_MODULE_LOADING=LAZY
export GGML_CUDA_NO_VMM=1
export GGML_CUDA_FORCE_MMQ=1
export GGML_CUDA_SM=60
export OMP_NUM_THREADS=8

# 检查文件
if [ ! -f "$SERVER_BIN" ]; then
    echo -e "${RED}❌  错误：找不到 llama-server${NC}"
    exit 1
fi

# 菜单（ctx 注释与实际参数已对齐）
clear
echo -e "${GREEN}=========================================${NC}"
echo -e "   LLaMA.cpp 模型切换工具（最新版）"
echo -e "${GREEN}=========================================${NC}"
echo "  [1] ornith-1.0-9b-Q6_K.gguf (65536 ctx) ✅ 均衡首选"
echo "  [2] ornith-1.0-9b-Q8_0.gguf (65536 ctx)  高质量首选（P100 主力，见 06 手册 §5.3）"
echo "  [3] Qwen3-14B-Q4_K_M (32768 ctx)         能力备选"
echo "  [4] Qwen3-Embedding-0.6B-f16 (embedding 模式)"
echo -e "\n  [0] 退出"
echo -e "${GREEN}=========================================${NC}"
read -p "请选择模型编号：" CHOICE

# 模型配置
case $CHOICE in
    0) exit 0 ;;
    1) MODEL="ornith-1.0-9b-Q6_K.gguf" && CTX_SIZE=65536 && BATCH_SIZE=256 ;;
    2) MODEL="ornith-1.0-9b-Q8_0.gguf" && CTX_SIZE=65536 && BATCH_SIZE=256 ;;
    3) MODEL="Qwen3-14B-Q4_K_M.gguf" && CTX_SIZE=32768 && BATCH_SIZE=256 ;;
    4) MODEL="Qwen3-Embedding-0.6B-f16.gguf" && CTX_SIZE=32768 && BATCH_SIZE=512 ;;
    *) echo -e "${RED}❌  无效选择${NC}" && exit 1 ;;
esac

# 如果是第二个终端启动 embedding，强制改端口为 8001
if [ "$1" = "--keep-running" ] && [ "$CHOICE" = "4" ]; then
    PORT=8001
fi

# 检查模型
if [ ! -f "$MODEL_DIR/$MODEL" ]; then
    echo -e "${RED}❌  模型不存在：$MODEL_DIR/$MODEL${NC}"
    exit 1
fi

# 启动信息（IP 动态获取，不再硬编码）
LAN_IP=$(hostname -I | awk '{print $1}')
echo -e "\n========================================="
echo " 模型：$MODEL | 上下文：$CTX_SIZE"
echo " 地址：http://${LAN_IP}:$PORT"
if [ "$1" = "--keep-running" ]; then
    echo " 模式：保留已有进程，启动新进程"
fi
echo -e "=========================================\n"

# ====================== 核心启动命令 ======================
# --ubatch-size 256：prefill 3.2×（benchmarks §2.5.4 A/B 实测）
# --metrics：Prometheus 指标端点（接入监控栈）
CUDA_VISIBLE_DEVICES=$CUDA_DEVICE "$SERVER_BIN" \
--model "$MODEL_DIR/$MODEL" \
--ctx-size $CTX_SIZE \
--batch-size $BATCH_SIZE \
--ubatch-size 256 \
--port $PORT \
--host 0.0.0.0 \
--n-gpu-layers 99 \
--cache-type-k q8_0 \
--cache-type-v q8_0 \
--no-mmap \
--parallel 1 \
--metrics \
$( [ "$CHOICE" = "4" ] && echo "--embedding" || echo "--chat-template chatml --n-predict 3072" )

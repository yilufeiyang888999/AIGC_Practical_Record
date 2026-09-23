#!/usr/bin/env bash
# 算力端编排一键部署 / 回滚（ComfyUI 容器 + GPU 监控栈）
#
# 对应 docs/03-ComfyUI容器化部署交付手册.md。
# 每一步都做了前置校验 —— 部署脚本的价值不在"能跑起来"，
# 而在"跑不起来时能在 10 秒内告诉你为什么"。
#
# 用法：
#   ./docker_deploy.sh check      # 只做前置检查，不部署
#   ./docker_deploy.sh up         # 部署/更新
#   ./docker_deploy.sh status     # 四服务健康 + 监控链路
#   ./docker_deploy.sh down       # 停止（保留数据卷）
#   ./docker_deploy.sh rollback   # 回滚 ComfyUI 到上一个镜像 tag

set -euo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-$(cd "$(dirname "$0")/../docker" && pwd)}"
IMAGE_TAG="${IMAGE_TAG:-aigc-comfyui:2.6.0-cu124}"
PREV_TAG="${PREV_TAG:-aigc-comfyui:2.6.0-cu124-prev}"

cd "$COMPOSE_DIR"

die() { echo "❌ $*" >&2; exit 1; }
ok()  { echo "✅ $*"; }

# ── 前置检查 ─────────────────────────────────────────────────────
check() {
  echo "▶ 前置检查"

  command -v docker >/dev/null || die "docker 未安装"
  docker compose version >/dev/null 2>&1 || die "docker compose 插件不可用"

  # GPU 直通是容器化的前提 —— 装 Docker 不会自动带 NVIDIA Container Toolkit
  docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 \
    nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1 \
    && ok "GPU 直通可用" \
    || die "容器内看不到 GPU。请先执行：
    sudo nvidia-ctk runtime configure --set-as-default && sudo systemctl restart docker"

  # 端口冲突预检：四个端口都要先确认没人占
  local busy
  busy=$(ss -tlnp 2>/dev/null | grep -E ':(3000|8189|9090|9400)\b' || true)
  [[ -z "$busy" ]] && ok "端口 3000/8189/9090/9400 空闲" \
                   || die "端口冲突：
$busy"

  # .env 缺失时 compose 会直接拒绝启动（用了 ${VAR:?} 语法），这里提前给明确提示
  [[ -f .env ]] || die "缺少 .env。执行：cp .env.example .env && chmod 600 .env"
  local mode; mode=$(stat -c '%a' .env)
  [[ "$mode" == "600" ]] || echo "⚠️  .env 权限是 $mode，建议 chmod 600"

  docker compose config >/dev/null && ok "compose 语法 OK"
  docker image inspect "$IMAGE_TAG" >/dev/null 2>&1 \
    && ok "镜像 $IMAGE_TAG 已存在" \
    || echo "⚠️  镜像 $IMAGE_TAG 不存在，请先构建（docs/03 §3.2）"
}

# ── 部署 ─────────────────────────────────────────────────────────
up() {
  check
  echo "▶ 创建挂载目录"
  sudo mkdir -p /opt/AIGC/{output_container,input,custom_nodes,model_storage}

  echo "▶ 启动"
  docker compose up -d

  echo "▶ 等待健康检查（模型加载慢，给 120s 宽限）"
  sleep 30
  docker compose ps
  echo
  status
}

# ── 状态 ─────────────────────────────────────────────────────────
status() {
  echo "▶ 四服务状态"
  docker compose ps --format 'table {{.Name}}\t{{.Status}}'

  echo
  echo "▶ ComfyUI 容器"
  curl -fsS localhost:8189/system_stats \
    | python3 -c "import sys,json; d=json.load(sys.stdin)['devices'][0]; \
print(f\"  {d['name']} | 显存 {d['vram_free']/1024**3:.2f}G 空闲 / {d['vram_total']/1024**3:.2f}G\")" \
    || echo "  ❌ ComfyUI 未就绪"

  echo
  echo "▶ 模型可见性（挂载是否对齐）"
  curl -fsS localhost:8189/object_info/CheckpointLoaderSimple \
    | python3 -c "import sys,json; \
print('  ', json.load(sys.stdin)['CheckpointLoaderSimple']['input']['required']['ckpt_name'][0])" \
    || echo "  ❌ 查不到模型列表"

  echo
  echo "▶ 监控链路"
  local n; n=$(curl -fsS localhost:9400/metrics 2>/dev/null | grep -c '^DCGM_FI' || echo 0)
  # P100 上预期 16 条 DCGM_FI 指标；为 0 说明 dcgm 没抓到卡
  [[ "$n" -gt 0 ]] && ok "dcgm-exporter 输出 $n 条 DCGM_FI 指标" \
                   || echo "  ❌ dcgm-exporter 无指标输出"

  curl -fsS localhost:9090/api/v1/targets \
    | python3 -c "
import sys,json
for t in json.load(sys.stdin)['data']['activeTargets']:
    print(f\"  {t['labels'].get('job','?'):12s} {t['health']}\")" 2>/dev/null \
    || echo "  ❌ Prometheus 未就绪"

  curl -fsS localhost:3000/api/health >/dev/null 2>&1 \
    && ok "Grafana 健康" || echo "  ❌ Grafana 未就绪"
}

down() {
  echo "▶ 停止（数据卷保留）"
  docker compose down
  ok "已停止。数据卷：docker volume ls | grep aigc"
}

rollback() {
  echo "▶ 回滚 ComfyUI 到 $PREV_TAG"
  docker image inspect "$PREV_TAG" >/dev/null 2>&1 || die "备份 tag $PREV_TAG 不存在"
  docker tag "$PREV_TAG" "$IMAGE_TAG"
  docker compose up -d --force-recreate comfyui
  sleep 90
  docker compose ps comfyui
  ok "已回滚。确认无误后执行 docker tag $PREV_TAG $IMAGE_TAG 固化"
}

case "${1:-}" in
  check)    check ;;
  up)       up ;;
  status)   status ;;
  down)     down ;;
  rollback) rollback ;;
  *) echo "用法: $0 {check|up|status|down|rollback}" >&2; exit 1 ;;
esac

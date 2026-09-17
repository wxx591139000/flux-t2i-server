#!/bin/bash
# ============================================================
#  FLUX 常驻生成服务 一键启动脚本（服务器端 · 通用 · 幂等）
#  用法:
#    bash start_resident.sh            # 启动（已在跑则跳过）
#    bash start_resident.sh --force    # 强制重启
#    bash start_resident.sh --check    # 只读探活：就绪 exit 0，否则 exit 1（看门狗轮询用）
#    bash start_resident.sh --stop     # 停止常驻服务
#
#  与 start_gen.sh 的区别：
#    start_gen.sh      每次冷启动 gen_flux.py → 每张图重新加载 ~31GB 模型（旧链路）
#    start_resident.sh 模型加载一次常驻内存 → 之后每张图只做推理（新链路）
#
#  依赖: server/flux_resident_server.py（由管理器 scp 到 WORKDIR）
#  环境变量: FLUX_OFFLOAD=none|model|sequential  FLUX_RESIDENT_TOKEN=xxx
#            FLUX_RESIDENT_PORT=9630  FLUX_RESIDENT_SCREEN=fluxd
# ============================================================
set -u
WORKDIR=${FLUX_WORKDIR:-/root/autodl-tmp/flux-t2i}
MODEL=${FLUX_MODEL:-/root/autodl-tmp/models/FLUX.1-dev}
PY=${FLUX_RESIDENT_PY:-/root/miniconda3/envs/flux/bin/python}
PORT=${FLUX_RESIDENT_PORT:-9630}
SCREEN=${FLUX_RESIDENT_SCREEN:-fluxd}
LOG="$WORKDIR/fluxd.log"
SERVER_PY="$WORKDIR/flux_resident_server.py"
OUT="$WORKDIR/resident_out"
OFFLOAD=${FLUX_OFFLOAD:-none}
TOKEN=${FLUX_RESIDENT_TOKEN:-}

MODE=${1:-}

# ── 就绪判定（只读）：进程活着 + /health 有响应 ──
f_health() {
  curl -sS --max-time 4 "http://127.0.0.1:$PORT/health" 2>/dev/null
}
f_ready() {
  screen -wipe >/dev/null 2>&1          # 先清僵尸，否则 Dead 会话会被 grep 误判为在跑
  screen -ls 2>/dev/null | grep -q "[.]$SCREEN" || return 1
  f_health | grep -q '"status"' || return 1
  return 0
}

case "$MODE" in
  --check)
    if f_ready; then
      echo "[fluxd] 已就绪"
      f_health
      exit 0
    fi
    echo "[fluxd] 未就绪"
    exit 1
    ;;
  --stop)
    screen -S "$SCREEN" -X quit 2>/dev/null
    pkill -f 'flux_resident_serve[r].py' 2>/dev/null
    screen -wipe >/dev/null 2>&1
    echo "[fluxd] 已停止"
    exit 0
    ;;
esac

# 已在跑且非 --force → 直接退出（幂等）
if [ "$MODE" != "--force" ] && f_ready; then
  echo "✅ 常驻服务已在运行，跳过启动（如需重启加 --force）"
  f_health
  exit 0
fi

echo "===== FLUX 常驻生成服务启动 ====="

# ── Step 1: 带卡模式 ──
if ! nvidia-smi >/dev/null 2>&1; then
  echo "⚠️  当前是无卡模式，请先在 AutoDL 控制台切到【带卡模式】再启动"
  exit 1
fi
# 坑（见 docs/PITFALLS.md）：无卡模式下 nvidia-smi 可能空输出但 exit 0，须判空串
GPU=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1)
if [ -z "$GPU" ]; then
  echo "⚠️  nvidia-smi 有输出但 GPU 名为空，确认带卡模式"
  exit 1
fi
echo "[1/4] GPU 就绪: $GPU"

# ── Step 2: 模型 ──
if [ ! -d "$MODEL/transformer" ] || [ ! -f "$MODEL/DOWNLOAD_DONE" ]; then
  echo "⚠️  模型未就绪（缺 $MODEL/transformer 或 DOWNLOAD_DONE）"
  echo "    下载脚本: $WORKDIR/dl_curl.sh"
  exit 1
fi
echo "[2/4] 模型就绪: $MODEL"

# ── Step 3: 服务脚本 ──
if [ ! -f "$SERVER_PY" ]; then
  echo "⚠️  缺少 $SERVER_PY（由管理器 scp 上传，或手动放置 server/flux_resident_server.py）"
  exit 1
fi
echo "[3/4] 服务脚本就绪: $SERVER_PY"

# ── Step 4: 起服务（screen 后台，可断 SSH）──
# 只清 fluxd 会话，绝不动 fluxgen / gen_flux.py —— 那是旧链路，清了会误杀别人的任务
screen -S "$SCREEN" -X quit 2>/dev/null
screen -wipe >/dev/null 2>&1
sleep 1
mkdir -p "$OUT"

ENVPREFIX="FLUX_OFFLOAD=$OFFLOAD FLUX_RESIDENT_PORT=$PORT"
[ -n "$TOKEN" ] && ENVPREFIX="$ENVPREFIX FLUX_RESIDENT_TOKEN=$TOKEN"

screen -dmS "$SCREEN" bash -c "cd $WORKDIR && $ENVPREFIX \
  $PY flux_resident_server.py --port $PORT --model $MODEL --out $OUT > $LOG 2>&1"

# 等端口就绪（模型加载是后台异步的，这里只等 HTTP 起来；最长 30s）
for i in $(seq 1 15); do
  sleep 2
  if f_health | grep -q '"status"'; then
    echo "✅ 常驻服务已启动 (screen: $SCREEN, port: $PORT)"
    echo "   日志: $LOG"
    echo "   输出: $OUT/"
    echo "   健康: curl http://127.0.0.1:$PORT/health"
    echo "   注意: 模型加载在后台进行，/health 的 model_loaded=true 后才可接单"
    f_health
    exit 0
  fi
done

echo "❌ 常驻服务 30s 内未响应，查看日志: $LOG"
tail -20 "$LOG" 2>/dev/null
exit 1

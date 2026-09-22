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
PORT=${FLUX_RESIDENT_PORT:-9630}
SCREEN=${FLUX_RESIDENT_SCREEN:-fluxd}
LOG="$WORKDIR/fluxd.log"
SERVER_PY="$WORKDIR/flux_resident_server.py"
OUT="$WORKDIR/resident_out"

# ── 解释器选择（2026-09-22 新增）────────────────────────────────
# 为什么需要：同一个平台现在跑**异构模型**，而它们的依赖互不兼容 ——
#   · klein / dev 用 /root/miniconda3/envs/flux（diffusers 0.39.0）
#   · Qwen-Image-2.1 用 /root/autodl-tmp/envs/qwen（diffusers 0.41.0.dev0）
#   在**同一个**环境里装两个版本的 diffusers 是不可能的：升级会把 klein
#   正在用的 0.39.0 覆盖掉（klein 的 Flux2KleinPipeline 就是靠它）。
#   → 所以按「本机装了哪个模型」挑解释器，而不是写死一个。
#
# 判定顺序（先显式、后推断，最后兜底）：
#   1. FLUX_RESIDENT_PY 显式指定 —— 永远优先（看门狗/人工可强制）
#   2. 按 $MODEL 的 model_index.json 类名判断（最准：与代码里的能力表同源）
#   3. 按 $MODEL 路径里的模型名推断（下载未完成时的降级路径）
#   4. 都没有 → 老默认 flux 环境（保持向后兼容，不改变老机器行为）
#
# ★ 新增模型时**只改这一张表**（下面的 QWEN_PY_CANDIDATES / case 分支）。
#   散落多处 = 改一处漏一处；本文件在「体积换算」上已经栽过两次同类问题。
#
# ⚠️ 「显式指定才正确」等于「默认态是坏的」（本项目已在 stub 签名上栽过一次）。
#    所以这里的推断必须**默认也有答案**，不能让 PY 为空。
FLUX_PY_DEFAULT=/root/miniconda3/envs/flux/bin/python
# Qwen 环境候选（按优先级）：优先数据盘（系统盘只有 30G，装不下）
QWEN_PY_CANDIDATES="/root/autodl-tmp/envs/qwen/bin/python /root/miniconda3/envs/qwen/bin/python"

pick_python() {
  if [ -n "${FLUX_RESIDENT_PY:-}" ]; then
    echo "$FLUX_RESIDENT_PY"; return 0
  fi
  local cls=''
  if [ -f "$MODEL/model_index.json" ]; then
    cls=$(grep -o '"_class_name"[[:space:]]*:[[:space:]]*"[^"]*"' "$MODEL/model_index.json" \
          2>/dev/null | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
  fi
  local need_qwen=''
  case "$cls" in
    Qwen*) need_qwen=1 ;;
  esac
  # 路径兜底（model_index.json 读不到时，比如下载未完成）。
  # ⚠️ 用 `tr` 统一成小写再匹配，而不是写 `*[Qq]wen*` ——
  #    后者只覆盖首字母大小写的 2 种组合，`qWen` / `QWEN` 一律漏掉。
  #    模型目录名是人手打的，大小写不可控，宁可做得宽一点。
  case "$(printf '%s' "$MODEL" | tr 'A-Z' 'a-z')" in
    *qwen*) need_qwen=1 ;;
  esac
  if [ -n "$need_qwen" ]; then
    local c
    for c in $QWEN_PY_CANDIDATES; do
      [ -x "$c" ] && { echo "$c"; return 0; }
    done
    # 候选都不存在时**仍然返回首选路径**（而不是静默退回 flux）：
    # 让后面的一致性检查报出「你配的 Qwen 环境没装」这个真正的问题，
    # 而不是拿 flux 环境去跑 Qwen → 报一个看不懂的 import 错误。
    echo "${QWEN_PY_CANDIDATES%% *}"; return 0
  fi
  echo "$FLUX_PY_DEFAULT"
}

PY=$(pick_python)
# 选出来的解释器必须真的存在 —— 否则 screen 会起一个立刻退出的会话，
# 表现为「服务没起来但也没报错」，排查成本很高。这里显式失败。
if [ ! -x "$PY" ]; then
  echo "❌ 解释器不存在或不可执行: $PY"
  echo "   模型: $MODEL"
  echo "   提示: 设置 FLUX_RESIDENT_PY 指向正确的 conda 环境 python"
  echo "   已知环境: flux=/root/miniconda3/envs/flux  qwen=/root/autodl-tmp/envs/qwen"
  exit 1
fi

# OFFLOAD 默认按模型不同（见下）
# ⚠️ 默认必须是安全档，不能是「最快」的：
#   32G 卡上 FLUX.1-dev 权重 31.7 GiB > 卡空闲 ~31.1 GiB → 默认 none 必然 OOM（已实测）。
#   2026-09-18 起默认改为 model；klein 4B 只有 17.3 GB，可用 FLUX_OFFLOAD=balanced 或 none。
#   2026-09-22：Qwen-Image-2.1 bf16 约 30-35GB，32G 卡吃紧 → 默认模型级 offload 更稳。
OFFLOAD=${FLUX_OFFLOAD:-model}
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
echo "[2b/4] 解释器: $PY"

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

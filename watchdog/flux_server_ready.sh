#!/bin/bash
# flux_server_ready.sh v2 —— FLUX 服务器「预热就绪」脚本（服务器端自包含，幂等）
#
# ⚠️ 2026-09-20 重写：v1 预热的是**旧链路**（gen_flux.py / start_gen.sh / screen fluxgen /
# out/），而 A 链自 2026-09-16 起已改为**常驻服务**（flux_resident_server.py /
# screen fluxd / 9630 /health）。旧判据与新链路无关 —— 部署上去只会起一个没人用的
# 旧链路，网站照样转圈。本版改为：把常驻服务拉起来并确认**模型已加载**。
#
# 使命：让刚开机 / 刚克隆的机器变成「可接单」状态。真正生图仍由任务中心按需调度，
# 本脚本只保证「常驻在跑 + 模型已加载」，这样本机 manager 一提交就能立刻出图，
# 不必等冷启动（FLUX.1-dev + offload=model 首次加载实测 1~3 分钟）。
#
# 用法:
#   bash flux_server_ready.sh --check   # 只读：就绪 exit 0，否则 exit 1（不启动、不清理）
#   bash flux_server_ready.sh           # 全量：清僵尸 → 拉起常驻 → 等模型加载 → 打 SERVER_READY
#
# 参数（全部可省略；由看门狗按 servers.json 里该机器的条目透传）：
#   FLUX_WORKDIR          工作目录        默认 /root/autodl-tmp/flux-t2i
#   FLUX_MODEL            模型目录        默认 /root/autodl-tmp/models/FLUX.1-dev
#   FLUX_OFFLOAD          none|model|sequential，默认 model（32G 卡跑 dev 必须 model，none 必 OOM）
#   FLUX_RESIDENT_PORT    常驻端口        默认 9630
#   FLUX_RESIDENT_PY      远端 python     默认 /root/miniconda3/envs/flux/bin/python
#   FLUX_RESIDENT_SCREEN  screen 会话名   默认 fluxd

WORKDIR=${FLUX_WORKDIR:-/root/autodl-tmp/flux-t2i}
MODEL=${FLUX_MODEL:-/root/autodl-tmp/models/FLUX.1-dev}
OFFLOAD=${FLUX_OFFLOAD:-model}
PORT=${FLUX_RESIDENT_PORT:-9630}
PY=${FLUX_RESIDENT_PY:-/root/miniconda3/envs/flux/bin/python}
SCREEN=${FLUX_RESIDENT_SCREEN:-fluxd}
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

# ── 就绪判定（只读）──
f_loaded() {
  # /health 里 model_loaded 为 true 才算就绪 —— "进程在" ≠ "能出图"
  curl -s -m 3 "http://127.0.0.1:$PORT/health" 2>/dev/null \
    | tr -d ' \n' | grep -q '"model_loaded":true'
}

f_check() {
  nvidia-smi >/dev/null 2>&1                  || return 1   # 带卡模式
  [ -f "$MODEL/DOWNLOAD_DONE" ]               || return 1   # 模型完整
  [ -f "$WORKDIR/flux_resident_server.py" ]   || return 1   # 常驻脚本已上传
  [ -f "$WORKDIR/start_resident.sh" ]         || return 1   # 启动脚本
  f_loaded                                    || return 1   # 常驻在跑 + 模型已加载
  return 0
}

if f_check; then
  if [ $CHECK -eq 1 ]; then
    echo "[flux-ready] 已就绪（带卡 + 模型 + 常驻已加载模型）"; exit 0
  fi
else
  if [ $CHECK -eq 1 ]; then
    echo "[flux-ready] 未就绪"; exit 1
  fi
fi

# ── 全量预热 ──
echo "===== FLUX 常驻服务预热 ====="
screen -wipe 2>/dev/null          # 清僵尸会话：Dead 会话会让"是否在跑"误判（既有坑）

# 只动常驻自己的会话与进程，**绝不碰旧链路 fluxgen / gen_flux.py**（两条链路互不干扰）
if pgrep -f 'flux_resident_serve[r].py' >/dev/null 2>&1 && ! f_loaded; then
  echo "[flux-ready] 常驻进程在但模型没加载完（或卡住），重启它"
  screen -S "$SCREEN" -X quit 2>/dev/null
  pkill -f 'flux_resident_serve[r].py' 2>/dev/null
  sleep 3
fi

cd "$WORKDIR" || { echo "[flux-ready] ❌ 工作目录不存在: $WORKDIR"; exit 1; }

FLUX_WORKDIR="$WORKDIR" FLUX_MODEL="$MODEL" FLUX_OFFLOAD="$OFFLOAD" \
FLUX_RESIDENT_PORT="$PORT" FLUX_RESIDENT_PY="$PY" FLUX_RESIDENT_SCREEN="$SCREEN" \
  bash start_resident.sh

# 等模型加载（dev + offload=model 冷启动实测 1~3 分钟，给 400s 上限）
for _ in $(seq 1 40); do
  sleep 10
  if f_loaded; then
    touch "$WORKDIR/SERVER_READY"
    echo "[flux-ready] ✅ 常驻服务已就绪（模型已加载），可接单"
    exit 0
  fi
done

echo "[flux-ready] ⚠️ 预热未完成（等模型加载超时 400s），下轮重试"
tail -20 "$WORKDIR/fluxd.log" 2>/dev/null
exit 1

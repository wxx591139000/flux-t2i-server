#!/bin/bash
# flux_server_ready.sh — FLUX 服务器「预热就绪」脚本（服务器端自包含，幂等）
# 使命：把刚开机 / 刚克隆的 FLUX 服务器变成「可接单」状态（带卡 + 模型就绪 + 脚本就绪 + 清残留污染）。
# 真正生图仍由任务中心按需调度，本脚本只保证服务器「拉起来能接活」。
#
# 用法:
#   bash flux_server_ready.sh          # 全量：校验 + 清理 + 打就绪标记 SERVER_READY
#   bash flux_server_ready.sh --check  # 只读状态检查: 就绪 exit 0，否则 exit 1（不清理，供看门狗轮询）
WORKDIR=/root/autodl-tmp/flux-t2i
MODEL=/root/autodl-tmp/models/FLUX.1-dev
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

# ── 就绪判定（只读）──
f_check() {
  nvidia-smi >/dev/null 2>&1 || return 1                      # 带卡模式
  [ -d "$MODEL/transformer" ]     || return 1                  # 模型已存在
  [ -f "$MODEL/DOWNLOAD_DONE" ]   || return 1                  # 模型下载完整标记
  [ -f "$WORKDIR/gen_flux.py" ]   || return 1                  # 生成脚本
  [ -f "$WORKDIR/start_gen.sh" ]  || return 1                  # 启动脚本
  return 0
}

if f_check; then
  if [ $CHECK -eq 1 ]; then
    echo "[flux-ready] 已就绪(带卡+模型+脚本)"; exit 0
  fi
else
  if [ $CHECK -eq 1 ]; then
    echo "[flux-ready] 未就绪"; exit 1
  fi
fi

echo "===== FLUX 服务器预热就绪 ====="
# 清理残留生成会话/输出（防污染 web_out）。仅在服务器非就绪才走到这，故无明显并发冲突；
# 即便已就绪全量跑也是幂等的，安全。screen -wipe 清僵尸，防 Dead 会话被 start_gen 误判运行中。
pkill -f 'gen_fl[u]x.py' 2>/dev/null
screen -S fluxgen -X quit 2>/dev/null
screen -wipe 2>/dev/null
rm -rf "$WORKDIR/out"/* 2>/dev/null
touch "$WORKDIR/SERVER_READY"
echo "[flux-ready] ✅ 服务器已就绪，可接单: $WORKDIR"
exit 0
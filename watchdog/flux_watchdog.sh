#!/bin/bash
# flux_watchdog.sh v2 —— FLUX 服务器看门狗（VPS 常驻，systemd 托管）
#
# 干什么：每 60s 逐台巡检 GPU 机。机器在线但「未就绪」→ 自动就地预热成可接单
#         （拉起常驻服务 + 等模型加载）。机器关机 → SSH 不通 → 静默跳过，不误操作。
#         这样**任何时候开机一台机器，几分钟内它就自动变成能出图的状态**，
#         不用等本机 manager 在线去拉起（本机 manager 并不总是开着）。
#
# ⚠️ 2026-09-20 改动：
#   1) 机器清单**不再硬编码**在脚本里 —— 改从 targets.conf 读，
#      该文件由 `gen_targets.py` 从 manager/servers.json 生成。
#      加机器只改 servers.json 一处，不再「这里加一行、那里加一条、还要改 ssh config」。
#   2) 就绪语义从旧链路（gen_flux.py/fluxgen）换成常驻服务（9630 /health model_loaded）。
#
# targets.conf 每行：host:port:user:workdir:model:offload
KEY=${WATCHDOG_KEY:-/opt/flux-watchdog/id_flux_watchdog}
DIR=/opt/flux-watchdog
KH=$DIR/known_hosts
CONF=$DIR/targets.conf
SCRIPT=$DIR/flux_server_ready.sh                 # 就地脚本（VPS 本地路径）
MIRROR=$DIR/mirror                               # 常驻服务文件镜像（VPS 本地，给裸机补件用）
SSH="/usr/bin/ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$KH"
SCP="/usr/bin/scp -i $KEY -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$KH"
INTERVAL=${WATCHDOG_INTERVAL:-60}

if [ ! -f "$KEY" ]; then
  logger -t flux-watchdog "⚠️ 缺少 $KEY（跑 watchdog/authorize_key.py 生成并把公钥装到各机）"
fi

if [ ! -f "$CONF" ]; then
  logger -t flux-watchdog "⚠️ 缺少 $CONF（用 gen_targets.py 生成），退出"
  exit 1
fi

while true; do
  while IFS=: read -r host port user workdir model offload; do
    [ -z "$host" ] && continue
    case "$host" in \#*) continue ;; esac
    workdir=${workdir:-/root/autodl-tmp/flux-t2i}
    model=${model:-/root/autodl-tmp/models/FLUX.1-dev}
    offload=${offload:-model}
    H="$SSH -p $port $user@$host"
    if $H 'true' 2>/dev/null; then                               # 机器在线（sshd 通）
      # ── 补件（幂等）：缺什么传什么，避免每轮无谓 scp ──
      # 新克隆/重装的机器上可能没有常驻服务脚本，不补的话预热必然失败，
      # 只能等本机 manager 开机上传 —— 那就失去「开机即自动可接单」的意义了。
      $H "mkdir -p $workdir" 2>/dev/null
      for f in flux_resident_server.py start_resident.sh; do
        if ! $H "test -f $workdir/$f" 2>/dev/null; then
          if [ -f "$MIRROR/$f" ]; then
            $SCP -P "$port" "$MIRROR/$f" "$user@$host:$workdir/$f" 2>/dev/null \
              && logger -t flux-watchdog "[$host] 补传 $f" \
              || logger -t flux-watchdog "[$host] ⚠️ 补传 $f 失败（mirror 有文件但传不过去）"
          else
            logger -t flux-watchdog "[$host] ⚠️ 缺 $workdir/$f 且 VPS 镜像 $MIRROR/$f 不存在"
          fi
        fi
      done
      if ! $H "test -x $workdir/flux_server_ready.sh" 2>/dev/null; then
        $SCP -P "$port" "$SCRIPT" "$user@$host:$workdir/flux_server_ready.sh" 2>/dev/null
      fi
      ENV="FLUX_WORKDIR=$workdir FLUX_MODEL=$model FLUX_OFFLOAD=$offload"
      if ! $H "$ENV bash $workdir/flux_server_ready.sh --check" 2>/dev/null; then
        logger -t flux-watchdog "[$host] 未就绪，预热中（model=$model offload=$offload）"
        $H "$ENV bash $workdir/flux_server_ready.sh" 2>/dev/null
        ok=0
        for _ in $(seq 1 6); do                                  # 轮询等就绪
          sleep 10
          $H "$ENV bash $workdir/flux_server_ready.sh --check" 2>/dev/null && { ok=1; break; }
        done
        [ $ok -eq 1 ] && logger -t flux-watchdog "[$host] ✅ 已就绪可接单" \
                       || logger -t flux-watchdog "[$host] ⚠️ 预热未完成，下轮重试"
      fi
    fi
  done < <(grep -v '^#' "$CONF" | grep -v '^$')
  sleep "$INTERVAL"
done

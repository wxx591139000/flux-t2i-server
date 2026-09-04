#!/bin/bash
# flux_watchdog.sh — FLUX 服务器看门狗（VPS 常驻，镜像 qwen_watchdog.sh 多机版）
# 监测各 FLUX 服务器：机器在线但「未就绪」（无卡/模型缺/脚本缺）→ 自动就地预热成可接单。
# 由 VPS systemd 服务长驻；各 FLUX 机共用同一把 key id_flux_watchdog。
# 机器关机/缺失 → SSH 探测失败 → 静默跳过，不误操作。
#
# 目标 FLUX 服务器列表：host:port:user   每次加新 flux 机只需加一行（并配好 key）。
KEY=/opt/flux-watchdog/id_flux_watchdog
KH=/opt/flux-watchdog/known_hosts
SCRIPT=/opt/flux-watchdog/flux_server_ready.sh          # 就地脚本（VPS 本地路径）
TARGETS=(
  "connect.westd.seetacloud.com:53806:root"    # flux1 (autodl-flux)
  "connect.weste.seetacloud.com:23192:root"    # flux2 (新)
)
WORKDIR='/root/autodl-tmp/flux-t2i'
SSH="/usr/bin/ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$KH"
SCP="/usr/bin/scp -i $KEY -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$KH"

while true; do
  for t in "${TARGETS[@]}"; do
    host=${t%%:*}; rest=${t#*:}
    port=${rest%%:*}; user=${rest#*:}
    H="$SSH -p $port $user@$host"
    if $H 'true' 2>/dev/null; then                                # 机器在线(sshd 通)
      # 确保就地脚本存在，再跑只读 --check
      $SCP -P $port "$SCRIPT" "$user@$host:$WORKDIR/flux_server_ready.sh" 2>/dev/null
      if ! $H "bash $WORKDIR/flux_server_ready.sh --check" 2>/dev/null; then
        logger -t flux-watchdog "[$host] 未就绪，预热中..."
        $H "bash $WORKDIR/flux_server_ready.sh" 2>/dev/null
        ok=0
        for _ in $(seq 1 6); do                                  # 轮询 ≤30s 等就绪
          sleep 5
          $H "bash $WORKDIR/flux_server_ready.sh --check" 2>/dev/null && { ok=1; break; }
        done
        [ $ok -eq 1 ] && logger -t flux-watchdog "[$host] ✅ 已就绪可接单" \
                       || logger -t flux-watchdog "[$host] ⚠️ 预热未完成，下轮重试"
      fi
    fi
  done
  sleep 60
done
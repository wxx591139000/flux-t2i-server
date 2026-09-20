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
# ⚠️ 两个都不能省：
#   -n  ssh 会把循环的标准输入**吃掉**（经典的 "ssh eats stdin in while read loop" 坑）。
#       不加 -n 的话，处理完第一台机器后 read 直接读到 EOF → 本轮循环提前结束，
#       后面的机器这一轮根本不会被巡检（实测：只有同步日志、没有判据日志，极难定位）。
#   BatchMode=yes  免密失败时不要卡在密码提示上
SSH="/usr/bin/ssh -n -i $KEY -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$KH"
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
  # 每轮重新读一次 targets.conf（改了机器清单不用重启服务）
  mapfile -t TARGETS < <(grep -v '^#' "$CONF" | grep -v '^$')
  for line in "${TARGETS[@]}"; do
    IFS=: read -r host port user workdir model offload <<< "$line"
    [ -z "$host" ] && continue
    case "$host" in \#*) continue ;; esac
    workdir=${workdir:-/root/autodl-tmp/flux-t2i}
    model=${model:-/root/autodl-tmp/models/FLUX.1-dev}
    offload=${offload:-model}
    H="$SSH -p $port $user@$host"
    if $H 'true' 2>/dev/null; then                               # 机器在线（sshd 通）
      # ── 补件 / 版本同步：本地与远端 md5 不一致就传 ──
      # ⚠️ 只判「文件在不在」是不够的：改了脚本后机器上的旧副本**永远不更新**，
      #    看门狗表面上在跑、日志也绿，实际执行的还是旧逻辑（2026-09-20 实测踩到：
      #    加了无卡模式判据，机器上仍是 17:29 的旧脚本，照样去跑 start_resident.sh）。
      #    新克隆的机器上什么都没有 → md5 不等 → 自动补件，一举两得。
      $H "mkdir -p $workdir" 2>/dev/null
      sync_file() {                       # sync_file <本地路径> <远端目录> <文件名>
        local lf=$1 rd=$2 fn=$3 want have
        want=$(md5sum < "$lf" 2>/dev/null | awk '{print $1}')
        [ -n "$want" ] || { logger -t flux-watchdog "[$host] ⚠️ 本地 $fn 读不到，跳过同步"; return 1; }
        have=$($H "md5sum < $rd/$fn 2>/dev/null" 2>/dev/null | awk '{print $1}')
        if [ "$want" != "$have" ]; then
          $SCP -P "$port" "$lf" "$user@$host:$rd/$fn" 2>/dev/null \
            && logger -t flux-watchdog "[$host] 同步 $fn（远端版本不一致）" \
            || logger -t flux-watchdog "[$host] ⚠️ 同步 $fn 失败"
        fi
      }
      sync_file "$SCRIPT" "$workdir" flux_server_ready.sh
      for f in flux_resident_server.py start_resident.sh; do
        if [ -f "$MIRROR/$f" ]; then
          sync_file "$MIRROR/$f" "$workdir" "$f"
        else
          logger -t flux-watchdog "[$host] ⚠️ VPS 镜像缺 $MIRROR/$f（跑 deploy_vps.py 补上）"
        fi
      done
      ENV="FLUX_WORKDIR=$workdir FLUX_MODEL=$model FLUX_OFFLOAD=$offload"
      # ⚠️ 必须看**退出码**：--check 成功和失败都会打印一行（"已就绪"/"未就绪"），
      #    判空会永远不成立，看门狗就再也不会预热了（静默失效，最阴的那种）。
      out=$($H "$ENV bash $workdir/flux_server_ready.sh --check" 2>/dev/null)
      rc=$?
      if [ $rc -eq 0 ]; then
        :                                                        # 已就绪，什么都不做
      elif echo "$out" | grep -q NOGPU; then
        # 无卡模式：预热也没用（必须人在控制台切带卡），硬试只会空转 60s + 每轮刷日志。
        # 节流：状态没变就别每分钟来一条（一天 1440 行会把真正的故障日志淹掉）。
        mkdir -p "$DIR/state"
        st="$DIR/state/${host}_${port}"
        if ! [ -f "$st" ] || [ -z "$(find "$st" -mmin -30 2>/dev/null)" ]; then
          logger -t flux-watchdog "[$host] ⏸ 无卡模式，跳过预热 —— 需到控制台切【带卡模式】（30 分钟内不再重复）"
        fi
        touch "$st"
      else
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
  done
  sleep "$INTERVAL"
done

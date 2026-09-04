# FLUX 服务器看门狗（VPS 端 · 预热就绪 + 保活）

镜像 qwen 生态的 `watchdog-vps/`（`/opt/qwen-watchdog/` + systemd）做法，让**常开 VPS** 做 FLUX 服务器集群的外部监护人：

- 各 FLUX 机（aws AutoDL）PID1 无 systemd/cron → 机器内装不了自启 → 由 VPS 轮询代管。
- **使命**：监测每台 FLUX 服务器，机器在线但「未就绪」（无卡 / 模型缺 / 脚本缺）→ 自动跑就地脚本把它预热成**可接单**状态。
- 真正生图仍由任务中心（本地 `flux_queue` 多服务器调度）按需 SSH 执行；看门狗只负责「拉起来能接活」+ 保活。
- 加新 flux 机：只需在 `flux_watchdog.sh` 的 `TARGETS` 加一行 `host:port:user` + 配好 key。

## 文件
| 文件 | 作用 |
|---|---|
| `flux_server_ready.sh` | 服务器端就地脚本：`--check` 只读状态 / 全量校验+清理+打 `SERVER_READY` |
| `flux_watchdog.sh` | VPS 端长驻循环：`TARGETS` 巡检，未就绪→预热→轮询确认 |
| `flux-watchdog.service` | VPS systemd unit（`Restart=always`） |

## 部署到 VPS（`vps-aliyun`）

```bash
# 1. 拷贝脚本到 VPS（用本机已配好的 vps-aliyun 别名）
scp watchdog/flux_server_ready.sh watchdog/flux_watchdog.sh watchdog/flux-watchdog.service vps-aliyun:/tmp/

# 2. VPS 上安装
ssh vps-aliyun '
  mkdir -p /opt/flux-watchdog &&
  cp /tmp/flux_server_ready.sh /tmp/flux_watchdog.sh /opt/flux-watchdog/ &&
  chmod +x /opt/flux-watchdog/*.sh &&
  cp /tmp/flux-watchdog.service /etc/systemd/system/flux-watchdog.service &&
  systemctl daemon-reload && systemctl enable --now flux-watchdog &&
  systemctl status flux-watchdog --no-pager
'

# 3. 生成/配独享 key 授权到各 FLUX 机（照 qwen id_watchdog）
ssh vps-aliyun "ssh-keygen -t ed25519 -N '' -f /opt/flux-watchdog/id_flux_watchdog"

# 4. 把公钥装到每台 FLUX 机的 authorized_keys
#    VPS 上执行，逐台：
#    ssh-copy-id -i /opt/flux-watchdog/id_flux_watchdog.pub -o UserKnownHostsFile=/opt/flux-watchdog/known_hosts -p <PORT> root@<HOST>
#    （flux1=P 53806 connect.westd；flux2=P 23192 connect.weste）
#    或以本机为中介 scp VPS公钥→本机→各 FLUX 机，见下一步

# 5. 验证（VPS 上）：手动只跑一轮确认各机标记就绪
ssh vps-aliyun 'bash -n /opt/flux-watchdog/flux_watchdog.sh && echo 语法OK'
```

## 运维
```bash
ssh vps-aliyun
journalctl -u flux-watchdog -f          # 日志
systemctl status flux-watchdog          # 状态
systemctl restart flux-watchdog         # 应用改动后重启
systemctl disable --now flux-watchdog   # 撤销（移除监控）
```

## 加 / 减 FLUX 服务器
- 加：`flux_watchdog.sh` 顶部 `TARGETS` 加 `"host:port:root"`（注释写 fluxN），把 key 公钥装到该机 → `systemctl restart flux-watchdog`。
- 减：删对应行 → `systemctl restart flux-watchdog`。
- 无需停任务：任务中心本身 `find_ready_server()` 每单探活，任一台就绪即可接力。

## 说明 / 边界
- **不干扰进行中生成**：看门狗只有 `--check`（只读校验 nvidia-smi+模型+脚本）通过才认为就绪并跳过全量清理；`--check` 失败才做清理。进行中的任务必然满足校验 → 不会被看门狗误杀。
- **关机即跳过**：SSH 探测失败（机器关机/欠费）→ 静默跳过，下轮再来，不误操作。
- 数据盘克隆已自带模型/脚本 → 正常情况开机即就绪，看门狗大多空转；主要价值在**缺专业部件时自动补** + 留档。
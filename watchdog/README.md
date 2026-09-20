# FLUX 服务器看门狗（VPS 端 · 自动预热 + 保活）

让**常开 VPS** 做 GPU 机群的外部监护人：

- GPU 机（AutoDL）PID1 无 systemd/cron → 机器里装不了自启 → 由 VPS 从外面轮询代管。
- **使命**：机器一开机就自动变成「可接单」状态（常驻服务在跑 + 模型已加载），
  不必等本机的 manager 在线去拉起 —— 本机 manager 并不总是开着。
- 真正生图仍由任务中心（本地 `flux_queue` 多机调度）按需 SSH 执行；看门狗只保证
  「拉起来能接活」+ 保活。

做法照抄已在跑的 `/opt/qwen-watchdog`（systemd + 独享 key + known_hosts），路径已验证。

## ⚠️ 2026-09-20 重写（v1 → v2）：两个必须知道的事实

1. **v1 从来没真正部署过**。`watchdog/` 在 commit `9c0cadd` 就有了，但 VPS 上
   `ls /opt/flux-watchdog` 是空的、服务 `inactive`。原因：部署散在 README 的手工命令里，
   改完代码没人会想起来跑。→ **现在压成一条命令 `deploy_vps.py`**。
2. **v1 预热的是旧链路**（`gen_flux.py` / `start_gen.sh` / `screen fluxgen` / `out/`），
   而 A 链自 2026-09-16 起已改为**常驻服务**（`flux_resident_server.py` / `screen fluxd` /
   9630 `/health`）。旧判据与新链路无关 —— 部署上去只会拉起一个没人用的旧进程，
   **网站照样转圈且没有任何报错**。→ v2 判据改为 `/health` 的 `model_loaded:true`。

## 文件

| 文件 | 作用 |
|---|---|
| `flux_server_ready.sh` | 服务器端就地脚本：`--check` 只读判就绪 / 全量：清僵尸 → 拉起常驻 → 等模型加载 → 打 `SERVER_READY` |
| `flux_watchdog.sh` | VPS 端长驻循环：读 `targets.conf` 逐台巡检，未就绪 → 预热 → 轮询确认；60s 一轮 |
| `targets.conf` | 机器清单，**不要手改** —— 由 `gen_targets.py` 从 `manager/servers.json` 生成 |
| `gen_targets.py` | `servers.json` → `targets.conf`（格式 `host:port:user:workdir:model:offload`） |
| `deploy_vps.py` | **一条命令部署**：生成清单 → 传文件 → 装 systemd → 启服务 → 跑自测 |
| `authorize_key.py` | 把 VPS 看门狗的公钥装到各 GPU 机（幂等）；`--check` 验证 VPS→各机 免密可达 |
| `selftest_ready.sh` | 判据自测：/tmp 沙箱 + 假 GPU + 假常驻服务，**不开机就能验证判据没写错** |
| `onboard_server.py` | **克隆/新增一台 GPU 机后的一条命令**：登记进 servers.json → 同步看门狗清单 → 装公钥 → 可选端到端验收 |

## 部署（改完代码随手跑一次）

```bash
python watchdog/deploy_vps.py                      # 部署 + 自测（默认别名 vps-aliyun）
python watchdog/deploy_vps.py --authorize          # 部署 + 顺手装公钥到当前开机的机器
python watchdog/deploy_vps.py --status             # 只看远端运行状态和日志
python watchdog/deploy_vps.py --dry-run            # 只打印将执行的命令
```

它会：生成 `targets.conf` → 传脚本 + `mirror/`（常驻服务两个文件）→ 落盘
`/opt/flux-watchdog` → 装 unit → `daemon-reload` + `enable` + **restart** → 回读
`systemctl status` → 跑 `selftest_ready.sh`。

> 为什么是 `restart` 不是 `enable --now`：服务已在跑时 `enable --now` 不会重新读脚本，
> 改完代码重新部署后跑着的还是旧逻辑（表现为「我明明改了怎么没生效」）。

## 加一台新 GPU 机（一条命令）

2026-09-20 现状：GPU 拿不到 → 需要克隆实例到新服务器。克隆完只需：

```powershell
# 克隆了一台 klein 机（能图生图），并把被取代的旧机停机留痕
python watchdog\onboard_server.py --name flux5 --host connect.westc.seetacloud.com --port 31234 ^
    --model klein --retire flux4

# 克隆的是 dev 机（只能文生图）
python watchdog\onboard_server.py --name flux6 --host ... --port ... --model dev

# 登记完顺手做端到端验收（真出图，几分钟）
python watchdog\onboard_server.py --name flux5 --host ... --port ... --model klein --accept

# 先看会写成什么样，不动文件
python watchdog\onboard_server.py --name flux5 --host ... --port ... --model klein --dry-run
```

`--model klein|dev` 会自动带上正确的模型路径 / offload / supports_edit
（klein：`offload=none` + 能图生图；dev：`offload=model` + **不能**图生图，32G 卡 `none` 必 OOM）。
它还会：同名条目**原地更新**不重复追加、写盘后**回读校验**、调 `deploy_vps.py --authorize`
同步清单并装公钥。

之后它就自治了：开机 → 看门狗 60s 内发现 → 缺件自动补传 → 拉起常驻 → 等模型加载 → 可接单。
**不再需要改 `~/.ssh/config`**（那是 2026-09-20 事故的根：仓库外的文件漏改 → 机器永远看不见）。

## 运维

```bash
ssh vps-aliyun
journalctl -u flux-watchdog -f               # 日志
systemctl status flux-watchdog               # 状态
systemctl restart flux-watchdog              # 应用改动
systemctl disable --now flux-watchdog        # 撤销（移除监控）
bash /opt/flux-watchdog/selftest_ready.sh    # 判据自测（4/4 才算对）
```

## 边界 / 不会误伤

- **关机即跳过**：SSH 不通（关机/欠费）→ 静默跳过，下轮再来，不误操作。
- **不干扰进行中生成**：`--check`（只读）通过就跳过；只有真未就绪才动。
- **不动旧链路**：只操作 `fluxd` 会话和 `flux_resident_server.py` 进程，绝不碰
  `fluxgen` / `gen_flux.py`。
- **判据语义**：`/health` 的 `model_loaded:true` 才算就绪 —— 「进程在」≠「能出图」
  （dev + offload=model 冷启动实测 1~3 分钟）。

## 已知待办

- GPU 机全部释放期间（2026-09-20 现状）看门狗空转，等重新克隆后跑 `authorize_key.py`。
- 远端 python 路径默认 `/root/miniconda3/envs/flux/bin/python`，若某台机器不同，
  需在 `flux_server_ready.sh` 的 `PY=` 处按机器覆盖（当前未做 per-target 字段）。

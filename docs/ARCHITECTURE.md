# 项目详细方案 — FLUX 文生图服务（通用）

> 版本：v2.4 · 2026-09-16

## 边界：本项目自带任务中心，下游站点是独立项目（v2.3 明确）

本项目**自带完整可用的生图网页任务中心**（`flux_web_service.py` 的页面 + `/api/submit` → `/api/status`
→ `/api/download` 三端点 + `flux_queue` 单 worker），不依赖任何其它项目。

面向客户的生图网站 `image-gen-site`（旧名 `ecom-image-studio`，2026-09-17 改名）是**独立项目**，
唯一关联是**通过 HTTP 消费上面三个端点**。
两边各自独立部署、独立健康；本项目侧的自检**不需要**下游站点存在，下游站点也只需保证
`submit` 与 `download` 用同一个 token（归属校验按 `job.user_id == token`）。
离线端到端自检见 README「两条链路及其边界（v2.3）」。

## 传输层与常驻档位（v2.8）

**一句话**：本机对 GPU 机的所有操作（探测 / 上传脚本 / 拉起常驻 / 拉图）都走
`flux_server_manager.run()` → `bash -lc "<ssh|scp 命令>"`。这条链路有两个必须守住的契约。

### 契约 1：bash 必须主动定位，不能假定在 PATH 里

`run()` 用 `bash -lc` 而不是 `cmd /c`，是为了避免 Windows cmd 对管道/引号/单引号的解析差异
（远端命令里大量 `| head -1`、`&&`、`2>/dev/null`）。代价是**依赖本机有 bash**，而
Git for Windows 默认只把 `<Git>\cmd` 写进 PATH —— 那个目录只有 `git.exe`，
`bash.exe` 在 `<Git>\bin`。于是同一份代码在不同启动方式下行为相反：

| 服务是怎么起来的 | PATH 里有 bash 吗 | 结果 |
|---|---|---|
| Git Bash 里跑 `python manager/flux_service.py` | 有（MSYS `/usr/bin`） | 正常 |
| 双击 `启动-*.bat`（explorer → cmd → powershell） | **没有** | 三台机全判「SSH 不通」 |

所以 `find_bash()` 按三级定位：PATH →（从 `which git` 反推 `<Git>` 根再拼 `bin/bash.exe`）
→ 常见安装路径；结果缓存。**找不到时返回 `NO_BASH: ...`，绝不退化成"服务器不可达"** ——
后者会把人引去查云控制台（实测浪费 40 分钟）。

### 契约 2：失败必须携带真因

- `run()` 失败时返回 **stderr**（ssh 的报错全在 stderr；旧版只取 stdout，等于把真因扔掉）。
- `probe_full()` 的错误分级：`NO_BASH` / `SSH 超时` / `SSH 不通（<ssh 原话>）` / 兜底文案。
- `flux_service.py` 启动时自检 bash —— 这类故障的表现是「所有服务器都不可达」，
  必须在启动那一刻就吼出来。

### 契约 3：常驻服务的 `FLUX_OFFLOAD` 默认值必须安全

`ensure_resident()` 把 `FLUX_OFFLOAD` 透传给远端 `start_resident.sh`。优先级：
**env `FLUX_OFFLOAD` > 该机器条目的 `offload` 字段 > 默认 `model`**。

`none`（全程显存）最快，但 32G 卡（RTX 4080 SUPER 32760 MiB）装 31.2 GiB fp16 权重
+ 推理激活值**必然 OOM**。所以每台默认机显式声明 `"offload": "model"`；
想追速度的大显存机器在自己条目上写 `none` 即可，逻辑不用动。

> 这三个契约由 `tests/test_transport_env.py`（8 项，离线）守住。改 `flux_server_manager.py`
> 或 `flux_resident_client.py` 后跑一遍；闸门有效性已用变异测试证明（改回旧写法会变红）。

## 健康探针（v2.4 新增）

**问题**：原 `GET /health` 返回硬编码 `{"status":"ok"}`，只能证明"HTTP 端口开着"。真实故障里最难受的一种是
**worker 线程异常退出**：web 照常 200、用户提交照常拿到 `queued`，但队列再也不会被消费，永远不出图 ——
从外部完全看不出来，只能等用户来问。

**分层**（两个 `/health` 是两个不同东西）：

| 探针 | 位置 | 回答 | 外呼 |
|---|---|---|---|
| 常驻服务 `/health`（默认 :9630） | GPU 机 | 模型加载了吗 / GPU / 队列深度 / 当前任务 | 不适用 |
| 对外 web `/health`（:9620） | 本机 | **本进程**健康吗 / 队列积压 / 后端疑似不可用 | **零外呼** |

**web 浅探针数据源**（全部进程内）：
```
FluxQueueScheduler.stats()  → gen_mode / queue_depth / inflight / waiting
                              / worker_alive / health_alive / stopped / uptime_sec
FluxDB.ping()               → SELECT 1（只证明连接可用，不查业务表）
web_uptime_sec              → 模块级 WEB_STARTED_AT
```

**判定规则**（`_Handler._health()`，`flux_web_service.py`）：
```
healthy = db_ok and not (has_sched and (sched_err or not worker_alive))
  未挂调度器（has_sched=False，web-only 模式）→ 算健康，worker_alive 报 null
  挂了但 worker 死 / stats() 读不出       → degraded(503)，异常原文进 errors[]
HTTP 码同步：200 / 503
```

**刻意不做**：不在探针里 SSH 或 HTTP 探 GPU 机。否则一台 GPU 机关机就会让本服务探针变慢/超时，
监控会把「后端不可用」误判成「本服务死了」。`waiting` 池大小是**零成本推导**出的后端线索
（waiting 池只装 `[SERVER_DOWN]` 任务）：`waiting>0 → backend_hint=server_down`，仅供参考。

**下游站点侧**（`image-gen-site`）：把上游 `/health` 包成自己的 `GET /api/health`，
一次响应里同时给出站点层与上游层，上游不可达返回 503 + `status=degraded` + `upstream.httpStatus=null`。
区分「连不上」（`httpStatus=null`）与「连上了但上游自报不健康」（`httpStatus=503`）—— 两者故障定位完全不同。


## 模型常驻生成路径（v2.1 新增）

**动机**：旧链路每张图都要走「pkill 旧进程 → screen 起 `gen_flux.py` → `from_pretrained` 重载
~31GB 权重 → 生成 1 张 → 进程退出」。N 张图 = N 次模型加载，吞吐被结构性地卡在 I/O 上。

**做法**：GPU 机上跑一个常驻进程，模型加载一次常驻显存，之后每个请求只做推理。

```
本地 (Windows)                                        服务器 (AutoDL)
┌─────────────────────────────────────────┐          ┌────────────────────────────────────┐
│ flux_queue._generate                    │          │ flux_resident_server.py            │
│   └─ [GEN_MODE=resident] ──────────────►│          │  ├─ JobStore  (内存任务表+落盘)     │
│       flux_resident_client              │          │  ├─ FluxWorker(单线程串行, 优先级队列)│
│         ├─ DirectTransport (urllib)     │◄────────►│  ├─ FluxPipeline  ← 常驻显存         │
│         │   本机可达时用（本地部署/隧道）│  HTTP    │  │   (FLUX_OFFLOAD=none|model|seq) │
│         └─ SshCurlTransport (ssh+curl)  │          │  └─ resident_out/<job_id>.png      │
│            远端 GPU 机时用（无需隧道）   │  ──ssh──►│                                    │
│         fetch_png: scp 拉回             │          │                                    │
└─────────────────────────────────────────┘          └────────────────────────────────────┘
```

- **协议**：`GET /health`（状态/GPU/队列深度）· `POST /generate` · `GET /status?job_id` ·
  `GET /image?job_id`（PNG）· `GET /jobs` · `POST /cancel`。只绑 `127.0.0.1`，不新增公网暴露面。
- **两种传输自动选择**：`FLUX_RESIDENT_BASE` 有值 → 直连；否则 → SSH 执行远端 curl。
  SSH 方式不建隧道、不用管隧道生命周期，与项目原有 `fsm.run` 走 ssh 的风格一致。
- **鉴权**：设 `FLUX_RESIDENT_TOKEN` 后所有请求需带 `X-Auth-Token`；不设则不校验（仅本机可绑定）。
- **错误分流**：`TransportError.kind` 分 `server_down`（进等待恢复池，等机器回来重试）与
  `failed`（业务失败，直接标记失败）。这个区分决定任务是否会被无限重试，必须精确。
- **能力增量**：`width/height/steps/seed/negative_prompt` 全部可透传到底层（**v2.7 起 web 层 `/api/submit`
  也接通了，不再只传 prompt**；legacy 的 `gen_flux.py` 仍是固定 768×1024 / steps 25）。
  服务端加硬边界：尺寸归一 16 的倍数并夹在 256~2048，steps 限 1~100，越界返回 400。
- **两条链路互斥**：常驻服务占住显存后，旧链路再上一份模型会 OOM。故 `flux_queue`（web 队列）
  与 `flux_server_manager.process_job`（小红书配图）**共用同一个 `FLUX_GEN_MODE` 一起切**。
- **回退**：`FLUX_GEN_MODE=legacy` 一键回到旧链路；旧的文件与函数**全部保留**，未删除。

## 多服务器 + VPS 看门狗（v2.0 新增）

- **多服务器注册表**：`manager/flux_server_manager.py` 的 `FLUX_SERVERS` 维护多台 FLUX 机（默认 flux1 `autodl-flux` + flux2 `autodl-flux2` + flux3 `autodl-flux3`；env `FLUX_SERVERS_JSON` 可覆写）。**v2.2 起支持自动发现**：`ssh_config_aliases()` 解析 `~/.ssh/config`，`discover_servers()` 把匹配 `FLUX_SERVER_ALIAS_GLOB`（默认 `autodl-flux*`）的别名自动登记为候选机 —— 克隆实例到新服务器后免配置接入。所有 SSH 操作接受 `server` 参数（None=默认机）。**v2.5 起默认机可配**：`_resolve_default()` 读 env `FLUX_DEFAULT_SERVER`，不设则候选机第一台（flux1，向后兼容）—— 用于第一台「开机但没卡」时把产线 / 不带 `--server` 的 CLI 的默认机临时指到别的机器；A 链不受影响（走 `find_ready_server()` 每单自选）。
- **探测（v2.2 重写）**：`fsm.probe_full()` 把「可达 / 带卡 / 模型文件 / 常驻服务」合成**一条远程命令**（`echo REACH; nvidia-smi …; test -f DOWNLOAD_DONE …; curl /health`），用标记行切分解析 —— 每台固定 **1 次 SSH 往返**（旧路径 3~4 次）。`fsm.probe_all()` **并行**探测（`ThreadPoolExecutor`），结果按 `FLUX_PROBE_TTL`（默认 20s）缓存。
- **选机分级（v2.2）**：`fr._pick_from()` 按 ①常驻在跑+模型已加载 ②可达+有卡+模型就绪 ③可达+有卡 ④可达（含无卡）逐级挑选，满足即停。第 ④ 级**故意保留**无卡机器，以便 `ensure_resident` 报出「需切带卡模式」的准确原因。CLI `servers` 与生产路径共用这一套判断（`_pick_from` 抽出来就是为了避免"命令说选 A、实际跑 B"）。
- **调度**：`flux_queue._generate` 按 `FLUX_GEN_MODE` 分发（v2.1）。**resident**（默认）走 `flux_resident_client.find_available_server()` → `ensure_resident()`（幂等拉起）→ `generate_via_resident()`；`ensure_resident` 复用探测结果里的 `gpu_ok`/`model_ok`，不再各补一次 SSH，并按**这台机器**的 `remote_base`/`remote_model` 透传 `FLUX_WORKDIR`/`FLUX_MODEL`（v2.3 修）。**legacy** 走 `fsm.find_ready_server()`（v2.2 起也走并行 `probe_all()`，语义不变）→ `start_generation()` → `wait_generation()`。两条路线都把 `server` 名回写 jobs 表。任一台上线即接单；`_health_loop` 用 `_any_ready()` 分流 —— resident 模式用 `fr.any_usable()`（可达+有卡+模型就绪，**不要求常驻已在跑**，否则换机后 waiting 任务会一直卡住）。保持单 worker 串行。
- **计费（v2.2）**：入队时 `usage_add(1)` 是**唯一**计费入口（`flux_quota.record_enqueued` 已删）；`precheck` 只用 `used` 判定（不再叠加 `inflight`，否则在途图算两次、额度只剩一半）；终态失败（业务失败 / 重试超限）经 `_refund_quota()` → `FluxDB.refund_job_once()` 幂等退还，`waiting` **不退**。
- **VPS 看门狗**（`watchdog/`，镜像 qwen `watchdog-vps`）：VPS(`vps-aliyun`) systemd 长驻 `flux_watchdog.sh`，`TARGETS` 逐台巡检。机器在线但「未就绪」→ 自动推送并跑就地 `flux_server_ready.sh` 预热成可接单（带卡+模型+脚本校验 + 清残留 + 打 `SERVER_READY`），轮询确认。语义=预热就绪+保活，真正生图仍由任务中心按需调度。
- **加机**：任务中心在 `FLUX_SERVERS` 加一条 + `~/.ssh/config` 加别名 + 免密；看门狗在 `TARGETS` 加一行 + 配 key → `systemctl restart flux-watchdog`。

## 系统架构图（文字描述）

```
┌─────────────── 本地 (Windows) ───────────────┐   ┌────────────── 服务器 (AutoDL) ──────────────┐
│                                               │   │                                               │
│  local/flux_gen_watchdog.py                   │   │  /root/autodl-tmp/flux-t2i/                   │
│  ├─ 定时检测可达性 (SSH)                       │   │  ├─ gen_flux.py   (diffusers 批量文生图)      │
│  ├─ 检测带卡模式 (nvidia-smi)                 │   │  ├─ start_gen.sh  (一键启动, screen后台)      │
│  ├─ 检测模型就绪 (DOWNLOAD_DONE)              │   │  ├─ dl_curl.sh    (curl流式下载)              │
│  ├─ scp 上传脚本 → 服务器                     │   │  └─ prompts.json  (业务提示词)                │
│  └─ bash start_gen.sh → 启动生成              │   │  模型 /root/autodl-tmp/models/FLUX.1-dev     │
│         │                                     │   │  │                                              │
│         └── scp 拉回结果 → output/            │   │  ├─ transformer/ (3分片 22GB)                │
└───────────────────────────────────────────────┘   │  ├─ text_encoder_2/ (T5, 2分片 9GB)          │
                                                     │  ├─ ae.safetensors  clip  vae (721MB)        │
                                                     │  └─ DOWNLOAD_DONE (下载完成标记)              │
                                                     └───────────────────────────────────────────────┘
```

## 模块划分与职责

| 模块 | 职责 |
|---|---|
| `server/flux_resident_server.py` | **(v2.1)** 常驻生成服务：HTTP 协议 + 单线程串行 worker + 模型常驻显存。自带 stub 模式（`--stub`，无需 GPU）便于离线自证 |
| `server/start_resident.sh` | **(v2.1)** 常驻服务幂等启动/探活：`--check`（只读，就绪 exit 0）/ `--force` / `--stop`。只操作 `fluxd` 会话，不碰旧链路的 `fluxgen` |
| `manager/flux_resident_client.py` | **(v2.1)** 常驻服务客户端与传输层：`probe / probe_all / find_available_server / _pick_from / any_ready / any_usable / ensure_resident / wait_model_loaded / generate_via_resident`，CLI 含 `servers`（候选机四态 + 会选哪台 + 未纳入别名）|
| `server/gen_flux.py` | (legacy) 加载 FluxPipeline（bf16 + CPU offload），读 prompts.json 逐张生成，同名输出跳过 |
| `server/start_gen.sh` | (legacy) 带卡检查→模型检查→脚本检查→screen 后台启动 gen_flux.py；幂等 |
| `server/dl_curl.sh` | curl 流式断点续传下载全部分片，停滞检测，完成打 DOWNLOAD_DONE |
| `local/flux_gen_watchdog.py` | 本地循环：可达性→带卡→模型→启动→确认→(可选)拉回（走旧链路） |

## 数据流 / 调用链路

**生成链路（v2.1 默认 · 常驻）**：
```
prompt → 常驻服务 /generate → FluxWorker(单线程) → FluxPipeline(常驻显存) → resident_out/<job_id>.png
       → scp 拉回 web_out/<job_id>/<job_id>.png（或 CLI 指定路径）
```

**生成链路（legacy）**：
```
prompts.json → gen_flux.py → FluxPipeline(FLUX.1-dev) → out/<NN>_<组名>/<key>.png
```

**常驻服务启动链路**（幂等）：
```
ensure_resident()                                   ← 管理器侧（manager/flux_resident_client.py）
  → upload_scripts()  scp flux_resident_server.py + start_resident.sh → {remote_base}/
  → ssh '<env> bash start_resident.sh'              ← 环境变量必须带 FLUX_WORKDIR / FLUX_MODEL（v2.3 修）
start_resident.sh → [无卡?]abort → [模型未就绪?]abort → [已在跑且非--force?]skip
                 → screen -dmS fluxd python flux_resident_server.py
                 → 轮询 /health（≤30s）确认 HTTP 起来（模型加载在后台继续）
```

⚠️ **路径必须按机器透传**：`start_resident.sh` 的 `WORKDIR` / `MODEL` 是默认值（`${FLUX_*:-默认}`），
而 `SERVER_PY="$WORKDIR/flux_resident_server.py"` 必须与 `upload_scripts` 的目标目录（`{remote_base}/`）一致。
克隆实例换了路径时若不带这两个变量，脚本会查错路径 → 误报「模型未就绪」（v2.3 已修，见 `docs/PITFALLS.md`）。

**一键启动链路**（幂等）：
```
start_gen.sh → [无卡?]abort → [模型未就绪?]abort → [已在跑?]skip → screen 启动 gen_flux.py
```

**看门狗链路**：
```
watchdog --download → SSH可达? → 带卡? → 模型就绪? → 已在跑? → scp上传 → start_gen.sh → 确认fluxgen → scp拉回 output/
```

## 对外服务架构（v1.1）

```
公网用户(浏览器) ── cloudflared 隧道 flux.zhuanlu.xyz → localhost:9620 ──► flux_web_service.py
                                                                            │ 网页提交提示词 / API / 激活码 / admin
                                                                            ▼
                                            flux_queue.py FluxQueueScheduler（优先队列 + 单worker串行）
                                              │ 去重 → 配额precheck → 队列上限 → 入队
                                              ▼
                                         worker: SSH 调 FLUX 服务器(复用 flux_server_manager) → 生成 → 拉图 web_out/ → done
                                              ▼
                                    flux_db.py(SQLite) + flux_quota.py + plans.yaml
```

### 模块划分与职责（v1.1，全部在 `manager/`）

| 模块 | 职责 |
|---|---|
| `flux_service.py` | main 入口，wiring DB→quota→queue→web→feishu_bot + 健康监控 |
| `flux_web_service.py` | 对外 HTTP 服务（stdlib http.server），网页 + API + 认证。**(v2.4)** `GET /health` 为浅探针（`_health()`，只读进程内状态、零外呼）；注意 `main()` 单独跑时 `scheduler=None`（web-only 模式）探针须容忍 |
| `flux_queue.py` | 队列调度器（核心）：PriorityQueue + 单 worker，复用 manager SSH 函数。**(v2.4)** 加 `stats()` 供探针读进程内状态（best-effort 快照，不加锁） |
| `flux_db.py` | SQLite 存储：users/codes/jobs/usage + `user_ensure`。**(v2.4)** 加 `ping()`（`SELECT 1`）供探针探活 |
| `flux_quota.py` | 月度图片配额（owner 无限） |
| `plans.yaml` | 套餐（default/basic/pro，月度图片数） |
| `feishu_notify.py` | 飞书通知（私信 + 图片上传/回传） |
| `feishu_bot.py` | 飞书"图图"对话式出图机器人（v1.7）：WS 监听 + 轮询回传 |
| `flux_server_manager.py` | 服务器管理器（SSH生成+拉图+插稿+飞书），被 queue 复用 |

### 飞书"图图"对话式出图（v1.7）

```
飞书用户 ── P2P 私聊发提示词 ──► lark_oapi WS 长连接 (feishu_bot._ws_listen)
                                    │  p2_im_message_receive_v1 事件
                                    ▼
                            _handle_prompt(open_id, text)
                              │ user_ensure(open_id)   # open_id 即 user_id
                              │ scheduler.submit()      # 复用队列：中文翻译/配额/去重/入队
                              ▼
                        确认回执（任务号+用量）
                              │
                              ▼
                        _poll_loop 每5秒轮询 job status
                              │ done ──► upload_image() → send_image() → 图片回传
                              │ failed ─► 回错误
                              │ waiting ─► 通知服务器恢复中
```

### 提交门（v1.1，对标转录 orchestrator）
```
submit(user,prompt,width,height,seed,steps,neg) → 去重(user:prompt:seed:WxH) → 配额precheck → 队列上限 → 入队(priority, seq)
worker: pop → SSH生成 → 拉图 web_out/<jobid>/ → done；服务器down → [SERVER_DOWN]重排队(3次)
```

## 关键设计决策

1. **diffusers 而非 ComfyUI**：服务器 github 被墙，ComfyUI git clone 失败；diffusers 纯 pip 可装
2. **curl 流式下载而非 huggingface_hub**：无卡模式下 hf_hub 下载器反复失败（非内存问题），curl 流式写盘内存极小、断点续传
3. **CPU offload**：32G 显存 + 大内存，用 `enable_sequential_cpu_offload` 最稳，兼容低显存
4. **分片权重而非单文件**：FLUX.1-dev 官方是 sharded，transformer 3 片 + T5 2 片；大小从 HF API 取真实 LFS 字节数
5. **screen 后台**：可断 SSH，任务不中断
6. **密钥环境变量化**：HF_TOKEN 从环境变量读，不硬编码（防 GitHub 泄露）
7. **stdlib http.server + 单 worker 串行**（v1.1）：无框架依赖、GPU 单卡严格串行，队列 worker 单线程
8. **Windows subprocess 用 bash -lc + UTF-8**（v1.1）：cmd.exe 错解析管道、GBK 崩中文、反斜杠路径当转义，见 PITFALLS

## 部署架构

- **服务器**：AutoDL VGPU 32G，conda env `flux`
- **模型路径**：`/root/autodl-tmp/models/FLUX.1-dev`
- **工作目录**：`/root/autodl-tmp/flux-t2i/`
- **SSH 别名**：`autodl-flux`（`~/.ssh/config`）
- **下载镜像**：`HF_ENDPOINT=https://hf-mirror.com`；pip：`mirrors.aliyun.com`
- **本地**：Windows，看门狗定时跑，结果拉回 `output/`
- **对外服务**（v1.1）：本地 Windows 跑 `flux_service.py`（端口9620），公网挂本地 `xhs-tunnel`；遵守双隧道约定不抢道

---

## 增量（2026-09-23）：对外入口与飞书机器人

### 对外域名（cloudflared 隧道 `xhs-tunnel`，配置 `~/.cloudflared/config.yml`）

| 域名 | 指向 | 用途 | 保护 |
|---|---|---|---|
| `flux.zhuanlu.xyz` | `localhost:3000` | image-gen-site 生产站点（客户用） | 无（公开） |
| `flux-admin.zhuanlu.xyz` | `localhost:9620` | **A 链商户后台（owner 用）** | `WEB_ADMIN_TOKEN`（20 位强口令） |
| `xhs.zhuanlu.xyz` | `localhost:8800` | 小红书笔记发布工具 | — |

- ⚠️ `config.yml` 的 **ingress 顺序敏感**，`http_status:404` 兜底必须**最后**
- ⚠️ 改完 `config.yml` **必须重启 cloudflared** 才生效（否则跑的是内存里的旧 ingress）
- 本机入口 `http://127.0.0.1:9620/admin` 仍然可用（不经过隧道）

### 飞书机器人「图图」

- 位置：`manager/feishu_bot.py`（类 `FeishuBot`），**随 9620 在 A 链进程内启动**
  （`manager/flux_service.py:58-59`）
- 接收：WebSocket 长连接（`im.message.receive_v1`）；发送原语在 `manager/feishu_notify.py`
- 现状能力：单聊 + 纯文本 + 一句话直出 + 回图；**群聊/图片/文件被硬过滤**
- 目标形态与分阶段计划：见 `docs/图图-飞书完整互动-设计方案.md`
- 架构约束：**随本机启停**（决策 1 选 A）；渠道层/编排层按"只依赖 HTTP"设计，便于将来迁 VPS

## 增量（2026-09-24）：图图命令面与取消链路（v2.10.0）

### 输入分流：命令 vs 提示词（共用同一条通道）

图图只有一条输入通道（飞书文本消息），命令与提示词共用它，所以**分流必须在最前面**：

```
飞书文本
  └─► _handle_prompt(open_id, text)
        ├─ ① _match_command(text) → ('help'|'list'|'cancel', arg)   ← ★ 必须最先
        │     ├─ help   → _cmd_help
        │     ├─ list   → _cmd_list       （读 db.jobs_inflight_by_user）
        │     └─ cancel → _cmd_cancel     （打墓碑 + drop_job + 跟踪终态）
        ├─ ② 未知斜杠写法 → 给指引（不静默当提示词）
        └─ ③ 否则 → 当提示词提交（_submit_locked）
```

判据严格性（`_match_command`）：只有「取消 + 纯数字序号（≤3 位）」或
「取消 + 6~16 位十六进制」才算命令 —— 后者对应 job_id 前缀
（`job_id = uuid.uuid4().hex[:16]`，见 `flux_queue.py`）。其余**回落为提示词**。

### 取消的写入链路（三道闸 + 一个独立闸）

```
_cmd_cancel（飞书） / POST /api/delete（网页）
  └─► db.job_mark_deleted(job_id, user_id)     ← 闸1：SQL 钉死 user_id（越权在数据层不可能）
  └─► scheduler.drop_job(job_id, job)          ← 闸2：剔调度器内存三处
        ├─ _waiting.discard     ★ waiting 也走这里，否则机器恢复会复活它
        ├─ _inflight.discard    （去重键，不清则同参数再也提交不了）
        └─ _pq 重建回填          （PriorityQueue 不支持按值删，取出后必须放回没删的）
  └─► db.feishu_track_set(phase='cancelled')   ← 终止飞书侧轮询
```

- **闸3 在 worker**：`_process()` 开头与提交结果前各查一次 `deleted_at` ——
  拿到已删任务**不调 GPU**；生成期间被删则**丢弃产物 + 物理清墓碑行**
- **独立闸（2026-09-24 补）**：`_recover_waiting_tasks()` 从内存等待池 pop 后
  **自己再判一次墓碑**。理由：调用方"应当"做的事不能当唯一防线 ——
  实测过漏掉它的后果（已删任务被复活，并在 4 小时超时分支**拿到退款**，
  等于开了「删了重传刷额度」的口子）

### ★ 两个判据不能合并

`/api/delete` 里：

```python
inflight = status in ('queued', 'generating')            # 有产物语义（决定"要不要立即清图"）
pooled   = status in ('queued', 'generating', 'waiting') # 调度器内存里可能有它（决定"要不要 drop"）
```

原本两者共用一个 `inflight`，而 `waiting` 不在其中 → 删 waiting 任务时不调 `drop_job`
→ 内存池留幽灵。**范围不同的两个判据绝不能共用一个变量。**

### 跨渠道取消

取消有两个入口（飞书命令 / 网页按钮），彼此不知道对方。
所以 `_poll_once()` 遇到 `deleted_at` 非空即终止跟踪（`phase='cancelled'`），
且 `feishu_tracks_pending()` 把 `'cancelled'` 计入终态 ——
否则网页端删了任务，飞书轮询会**每 5 秒空转、永不收敛**（P0-1 同款病）。

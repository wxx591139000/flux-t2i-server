# FLUX 文生图服务（通用）

> 版本：v2.7 · 2026-09-17
> 自部署 FLUX.1 文生图服务，**与具体业务解耦，可服务所有文生图需求**（小红书配图、公众号配图、海报底图……）。
> 基于 AutoDL VGPU 32G 服务器 + diffusers。
> 伞项目：本仓与下游站点仓 `image-gen-site` 并列存放于 `image-platform/`（两仓各自独立，唯一关联是 HTTP）。

## 核心能力

- 🖼️ **批量文生图**：读 `prompts.json` 逐张生成，支持分组输出目录
- 🔔 **开机看门狗**（本地）：检测服务器开机+带卡 → 自动上传脚本 → 一键启动生成（对标转录 bot 机制）
- ⏯️ **一键启动**（服务器）：`start_gen.sh` 幂等启动，screen 后台可断 SSH
- ⏬ **curl 流式下载**：适配无卡 2GB 内存，断点续传（huggingface_hub 在无卡模式会失败，curl 绕开）
- 🌐 **对外文生图服务**（v1.1 新增）：Web 提交提示词 + 排队调度 + 配额计费 + 公网 `flux.zhuanlu.xyz`。详见 `docs/WEB_SERVICE.md`
- 🌏 **中文提示词智能转换**（v1.3 新增）：客户输中文自动借鉴短剧 FLUX 方法论转成英文 30-80 词提示词，真正实现想要的图。详见 `docs/USER_GUIDE.md`
- 🔄 **队列服务器恢复自动重试**（v1.4 新增）：服务器 down 时任务进等待恢复池（不失败、不反复重试），健康监控检测到恢复后自动重新入队继续生成，全程无需手动介入。重试上限防毒瘤
- 👤 **商户管理中心 `/admin`**（v1.5 新增）：借鉴转录项目 admin.html，管理用户套餐/token/激活码（生成码、统计、改备注、下钻）。管理员登录 `X-Admin-Token` = `WEB_ADMIN_TOKEN`
- 🔗 **账户化**（v1.5 新增）：激活码=账户，客户多设备「绑定激活码」共享一份套餐，用量按账户聚合；商家按「客户/账户」管理（客户名/用量/设备数/详情）
- 💬 **飞书"图图"对话式出图**（v1.7 新增）：在飞书私聊发提示词 → 接入 FLUX 队列生成 → 完成后图片直接回传飞书对话。借鉴转录bot"小白"的 WebSocket 长连接范式，支持中文提示词自动翻译
- 🚀 **一键启动脚本**（v1.8 新增）：`start_service.ps1 -Target all/flux/xhs/tunnel` 分别拉起 FLUX 服务、小红书发布服务、公网隧道（幂等）；修好 cloudflared 启动 bug（撤 `--config` 改 cd 目录启动），规避旧 `transcribe-bot` 隧道抢道隐患
- 🖥️ **多服务器**（v2.0 新增，v2.2 支持自动发现）：任务中心 `FLUX_SERVERS` 注册表支持多台 FLUX 机（默认 flux1+flux2），每单自动挑任一台「可达+带卡+模型就绪」的服务器执行，任一台开机即可接单。**v2.2 起会扫 `~/.ssh/config` 自动纳入匹配的别名（克隆实例免配置接入），并自动跳过没 GPU 的机器**。详见 `docs/ARCHITECTURE.md` 与下方「多机与自动选在线机」
- 🐕 **VPS 看门狗**（v2.0 新增）：`watchdog/` 在常开 VPS(`vps-aliyun`) 用 systemd 长驻巡检各 FLUX 机，机器在线但未就绪 → 自动就地预热成可接单（预热就绪+保活）。部署见 `watchdog/README.md`
- ⚡ **模型常驻生成**（v2.1 新增）：`server/flux_resident_server.py` 把模型**加载一次常驻显存**，之后每张图只做推理。旧链路每张图都要 `from_pretrained` 重载 ~31GB 权重，是吞吐的结构性瓶颈。开关 `FLUX_GEN_MODE=resident`（默认）/ `legacy`，详见下文「生成路径」
- 🩺 **健康探针**（v2.4 新增）：对外 `GET /health` 由静态 `{"status":"ok"}` 改为**浅探针** —— 只读进程内状态、**不外呼 GPU 侧**，因此恒定快（实测 2~3ms），并能区分「服务活着」与「HTTP 活着但 worker 线程已死的僵尸」（后者会一直 200 但永不出图）。下游站点据此可加自己的 `/api/health`，把「站点活着」与「上游可用」分开报

## 目录结构

```
flux-t2i-server/
├── server/                  # 服务器端脚本
│   ├── flux_resident_server.py # (v2.1) 常驻生成服务：模型加载一次，之后只做推理
│   ├── start_resident.sh   # (v2.1) 常驻服务幂等启动/探活（--check / --force / --stop）
│   ├── gen_flux.py         # (legacy) diffusers 批量文生图（读 prompts.json）
│   ├── start_gen.sh        # (legacy) 一键启动（查GPU→查模型→screen后台跑）
│   ├── dl_curl.sh          # curl 流式分片下载（断点续传）
│   └── prompts.json        # (gitignored) 你的业务提示词
├── local/
│   └── flux_gen_watchdog.py # 本地开机看门狗（走旧链路）
├── manager/                 # 对外服务 + 服务器管理（v1.1）
│   ├── flux_service.py     # main 入口（DB→quota→queue→web→飞书bot）
│   ├── flux_web_service.py # 对外 HTTP 服务（网页+API）
│   ├── flux_queue.py       # 队列调度器（单worker串行，生成按 GEN_MODE 分发）
│   ├── flux_resident_client.py # (v2.1) 常驻服务客户端/传输层（直连 或 SSH curl）
│   ├── flux_db.py          # SQLite 存储
│   ├── flux_quota.py       # 月度配额
│   ├── plans.yaml          # 套餐
│   ├── .env.example        # (v2.1) 配置样例（含 FLUX_GEN_MODE / FLUX_RESIDENT_*）
│   ├── flux_server_manager.py # 服务器管理器（拉图+飞书；生成按 GEN_MODE 分发）
│   ├── feishu_notify.py    # 飞书通知（含图片上传/回传）
│   └── feishu_bot.py       # 飞书"图图"对话式出图机器人（v1.7）
├── example/
│   └── prompts.example.json # 通用提示词模板
├── output/                  # (gitignored) 生成结果拉回本地
├── web_out/                 # (gitignored) 对外服务生成图
└── docs/                    # 归档文档
```

## 使用文档

- 📘 `docs/flux服务-商家使用SOP.md` — 商户经营套餐/token/激活码的完整 SOP（登录、开账户、改套餐、设owner、下钻、API）
- 📗 `docs/flux服务-用户使用SOP.md` — 普通用户从零到生成图的完整 SOP（提示词、套餐、激活/绑定账户、换设备、FAQ）
- `docs/USER_GUIDE.md` — 用户使用指南（浏览器 + owner/API + 配额规则）
- `docs/WEB_SERVICE.md` — 对外服务架构文档
- 💬 **飞书出图**：飞书私聊"图图"机器人，直接发提示词（支持中文）即可出图，生成后图片回传对话

## 快速开始

### 1. 准备提示词

复制 `example/prompts.example.json` 为 `server/prompts.json`，改成你的需求：

```json
{
  "notes": [
    {"note": "组名", "images": [
      {"key": "cover", "prompt": "your prompt"},
      {"key": "P1", "prompt": "your prompt"}
    ]}
  ]
}
```

可选：`style_prefix`（全局追加风格）、`negative_prompt`。

### 2. 服务器部署

```bash
# 传脚本到服务器（服务器端）
scp server/*.sh server/*.py autodl-flux:/root/autodl-tmp/flux-t2i/
scp server/prompts.json autodl-flux:/root/autodl-tmp/flux-t2i/

# 下载模型（若未下完；HF_TOKEN 从环境变量读）
export HF_TOKEN=hf_xxx
ssh autodl-flux "screen -dmS fluxdl bash /root/autodl-tmp/flux-t2i/dl_curl.sh"
```

### 3. 启动（常驻服务 · 推荐）

```bash
# 传服务端脚本
scp server/flux_resident_server.py server/start_resident.sh autodl-flux:/root/autodl-tmp/flux-t2i/

# 启动常驻服务（幂等：已在跑则跳过）。模型加载在后台异步进行
ssh autodl-flux "cd /root/autodl-tmp/flux-t2i && bash start_resident.sh"

# 探活（就绪 exit 0），看门狗可轮询这个
ssh autodl-flux "bash /root/autodl-tmp/flux-t2i/start_resident.sh --check"

# 等模型常驻就绪后，本机直接出图（模型只加载这一次）
python manager/flux_resident_client.py gen "一双白色运动鞋，白底商品图" \
    --out out.png --width 1024 --height 1024 --seed 7
python manager/flux_resident_client.py bench "电商白底商品图" --count 5   # 量一次单张耗时
```

`FLUX_GEN_MODE=resident`（默认）下，对外 web 队列与小红书配图**都会自动拉起/复用这个常驻服务**，
不需要手工执行上面的命令 —— 只在首次冷启动时需要等一次模型加载。

**克隆实例到别的路径时**：`start_resident.sh` 里的 `WORKDIR` / `MODEL` 是默认值，但**管理器会按这台机器
注册的 `remote_base` / `remote_model` 透传** `FLUX_WORKDIR` / `FLUX_MODEL`（见 `docs/PITFALLS.md`）。
所以克隆机只要在 `FLUX_SERVERS_JSON` 里写对自己的路径即可；若手工 `ssh` 上去跑脚本，需自己带上这两个变量：

```bash
ssh autodl-flux "cd /root/autodl-tmp/flux-t2i && \
  FLUX_WORKDIR=/root/autodl-tmp/flux-t2i FLUX_MODEL=/root/autodl-tmp/models/FLUX.1-dev \
  bash start_resident.sh"
```

### 3'. 启动（旧链路 · 逃生口）

```bash
python local/flux_gen_watchdog.py --download            # 本地看门狗
ssh autodl-flux "bash /root/autodl-tmp/flux-t2i/start_gen.sh"
```

## 生成路径（v2.1）

同一套队列/配额/账户逻辑，生成环节有两种实现，用 `FLUX_GEN_MODE` 切换：

| | `resident`（默认） | `legacy` |
|---|---|---|
| 生成方式 | `flux_resident_server.py` 常驻进程，模型加载 1 次 | 每张图 `start_gen.sh` 冷启动 `gen_flux.py` |
| 每张图的模型加载 | **仅首次** | **每张一次**（~31GB 权重重新 `from_pretrained`） |
| 尺寸 / steps / 负向词 / seed | 支持透传（v2.7 起 web 层接通） | gen_flux.py 固定 768×1024 / steps 25（忽略透传参数） |
| 失败影响面 | 单张失败，其他张不受影响 | 整批失败 |
| 逃生口 | — | 出问题时 `FLUX_GEN_MODE=legacy` 一键回退 |

**两条链路不能同时跑同一台机器**：常驻服务占住显存后，旧链路的 `gen_flux.py` 再上一份模型会 OOM。
所以 `flux_queue`（web 队列）与 `flux_server_manager.process_job`（小红书配图）**一起**按同一开关切。

常驻服务的显存策略由 `FLUX_OFFLOAD` 决定：`none`（默认，全程显存，最快）→ `model`（模型级 offload，
比 sequential 快 2~3 倍）→ `sequential`（最省，与原 `gen_flux.py` 一致）。**先试 none；OOM 就逐级退。**

配置项与说明见 `manager/.env.example`。

## 多机与自动选在线机（v2.2）

**要解决的问题**：有时手上的服务器没 GPU，需要换到有卡的机器，甚至把整个实例克隆到新服务器。
希望系统自己找到当下能用的那台，不想每次改代码或配环境变量。

**候选机怎么来**

候选机 = 显式注册的 `flux1` / `flux2` / `flux3` + `~/.ssh/config` 里匹配 `FLUX_SERVER_ALIAS_GLOB`（默认 `autodl-flux*`）的别名。
克隆实例与原机是同布局（同 `remote_base` / `remote_model`），只差一个 SSH 别名 —— 所以克隆机只要在
`~/.ssh/config` 里有一条 Host，**就自动成为候选机，不用改代码**。

```bash
# 看当前有哪些候选机、哪台会被选中、哪些别名还没纳入
python manager/flux_resident_client.py servers
```

输出会列出每台的「可达 / 带卡 / 模型 / 常驻 / 已加载」五态，用 `▶` 标出会被选用的那台，
并在末尾列出**未纳入的 `~/.ssh/config` 别名** —— 克隆机如果起了别的名字（例如 `autodl-clone-gpu`），
照这行提示把模式加进去即可。

**怎么选**（分级，满足即停）

| 级别 | 条件 | 含义 |
|---|---|---|
| 1 | 常驻在跑 + 模型已加载 | 零冷启动，直接出图 |
| 2 | 可达 + 有卡 + 模型文件就绪 | 会被自动拉起常驻服务 |
| 3 | 可达 + 有卡 | 模型没就绪，仍返回并给出准确原因 |
| 4 | 可达（即使无卡） | 保留旧行为：准确报出「需切带卡模式」 |

第 4 级故意不剔除无卡机器 —— 旧实现是「第一台可达就选」，无卡时会在 `ensure_resident` 得到
「可达但无卡（需到 AutoDL 控制台切【带卡模式】）」这条很有用的报错，剔掉就丢诊断了。

**默认机 vs 自动选机（两个不同概念，别混）**

| | 谁在用 | 怎么定 |
|---|---|---|
| **自动选机** | A 链任务中心（`flux_queue` → `find_ready_server()`） | 每单现探，按上表分级挑当下可用的那台 |
| **默认机** | 小红书产线（`process_job`）与不带 `--server` 的 CLI | `FLUX_DEFAULT_SERVER=<name>`；不设则候选机第一台（`flux1`） |

默认机存在的意义是向后兼容（产线历史上绑 `flux1`）。当 `flux1`「开机但没卡」时，
设 `FLUX_DEFAULT_SERVER=flux3` 即可把默认机指到有卡的机器，**不用改代码**；
填了候选机里没有的名字会打一条 warning 并回退第一台。它只改默认值，不影响 A 链。

**性能**：`probe_full()` 每台固定 **1 次 SSH 往返**拿齐四态（旧路径 3~4 次：`echo` + `nvidia-smi` +
`test -f` + `curl`），且**并行探测**（N 台全关机时总耗时 ≈ 1 个 ConnectTimeout，而非 N 个），
结果按 `FLUX_PROBE_TTL`（默认 20s）缓存。

**waiting 任务恢复门**：判据是「可达 + 有卡 + 模型就绪」，**刻意不要求常驻服务已在跑**。
换机/克隆后机器刚开机时常驻还没起来，但 `ensure_resident` 会自动拉起；若拿「常驻已跑」当门槛，任务会一直卡在 waiting。

## 两条链路及其边界（v2.3）

本项目自带一个完整的「生图网页任务中心」，它**不依赖任何其它项目**：

```
浏览器 ──> flux_web_service.py (/ , /center)           ← 网页与任务中心页面
              └─ POST /api/submit   提交（去重→配额→入队）
              └─ GET  /api/status   轮询状态
              └─ GET  /api/download/<job_id>?token=   取 PNG（校验归属）
                        │
                 flux_queue.py 单 worker 串行
                        │  FLUX_GEN_MODE
              ┌─────────┴──────────┐
        resident（默认）        legacy（逃生口）
        常驻服务推理           每张图冷启动 gen_flux.py
              └─────────┬──────────┘
                   web_out/<job_id>/*.png
```

**面向客户的生图网站是独立项目**（`image-gen-site`，旧名 `ecom-image-studio`，2026-09-17 改名），
与本项目**唯一的关联是它通过 HTTP 消费本服务**：
`POST /api/submit` → 轮询 `GET /api/status` → `GET /api/download/<job_id>?token=`。
两边各自独立部署、各自独立健康，接口就是 `flux_web_service.py` 的这三个端点（契约见
`docs/WEB_SERVICE.md`）。改本项目时**不要假设**存在某个下游站点；改下游站点时也只需要这三个端点。

> ⚠️ 下游站点必须让 `submit` 与 `download` 用**同一个 token**（服务端按 `job.user_id == token` 校验归属，
> 无 token 时会现场生成新的随机 token，导致下载 403）。

`POST /api/submit` body 基础字段为 `{ prompt, priority }`；自 **v2.7** 起可附带可选生图参数
`width / height / seed / steps / negative_prompt`（resident 模式生效，缺省 = 服务端默认 768×1024 / steps 25）。
`GET /api/status` 响应回带 `seed / width / height`，前端可读回实际种子做复现。
注意：`negative_prompt` 虽已透传，但 FLUX.1-dev 是 guidance-distilled，diffusers 会静默忽略它。

### 端到端自检（离线，无需 GPU / 无需 SSH）

`FLUX_*` 那套断言都是离线可跑的：GPU 侧用 `server/flux_resident_server.py --stub`（合成 PNG），
传输走直连模式（`FLUX_RESIDENT_BASE`），因此**改完代码能立刻验完两条链路**，不必等开机。

```bash
# 一条命令起 A 链（任务中心）+ B 链（客户网站）→ 打完整链路 → 收摊
cd <workspace>/chain-verify && bash run_both_chains.sh
```

其中 `/health` 浅探针有专项脚本 `verify_health_probe.py`（26 项）：覆盖 web-only 模式、
worker 死→degraded、DB 坏→degraded、探针自身不抛，并用 **AST 源码级断言**证明探针体内零外呼
（不是靠"感觉很快"）。

## 健康探针（v2.4）

**两个 `/health` 是两个不同东西，别混：**

| 端点 | 归属 | 回答的问题 | 是否外呼 |
|---|---|---|---|
| `GET :${FLUX_RESIDENT_PORT}/health`（默认 9630） | GPU 侧常驻服务 | 模型加载了吗、GPU 是什么、队列多深 | 不适用（它自己就是被问的一方） |
| `GET :9620/health` | 本机对外 web 服务 | **本进程**健康吗、队列积压多少、后端是否疑似不可用 | **零外呼** |

web 侧浅探针返回：

```json
{"status":"ok","service":"FLUX 文生图","db_ok":true,"worker_alive":true,"health_alive":true,
 "gen_mode":"resident","queue_depth":0,"inflight":0,"waiting":0,"backend_hint":"idle",
 "web_uptime_sec":12.3,"uptime_sec":10.1,"errors":[]}
```

- `status` 只有 `ok` / `degraded`，且 **degraded 只表示本进程有问题**（DB 读不了 / worker 线程死了 /
  读不出调度器状态）。HTTP 码同步为 `200` / `503`。
- **后端可用性不在里面** —— 那要外呼 GPU 机，会让探针随 GPU 机开关机而变慢，监控就会把
  「后端不可用」误判成「本服务死了」。`waiting > 0` 与 `backend_hint=server_down` 是**零成本推导**出的
  提示（waiting 池只装 `[SERVER_DOWN]` 任务），仅供参考；要确证请直接问常驻服务的 `/health`。
- `worker_alive: null` 表示**没挂调度器**（web-only 模式，见 `flux_web_service.py` 的 `main()`），
  与 `false`（挂了但线程死了）是**两种不同情况**，判读时别合并。

## 配额与计费（v2.2）

- **计费入口唯一**：`flux_queue.submit()` 入队时 `usage_add(1)`（`flux_quota.record_enqueued` 已删除，避免两处计费）。
- **判定口径**：`precheck` 只用 `used`（不再叠加 `inflight` —— 入队已 +1，叠加会把在途图算两次，突发提交时额度只有一半）。
- **退还语义**：任务进入**终态失败**（业务失败 / 重试超限 `[RECOVER_SKIP]`）→ 自动退还 1 张，日志打 `↩️`；
  `waiting`（服务器 down 等恢复）**不退**（非终态，恢复后会继续跑）。退还幂等，靠 `jobs.refunded_at` 防重复退。

## 依赖

- 服务器：conda env `flux`（python3.12 / torch 2.13+cu130 / diffusers 0.39）
- 模型：`black-forest-labs/FLUX.1-dev`（gated，需 HF token，分片权重 ~31GB）
- 网络：服务器需 `HF_ENDPOINT=https://hf-mirror.com`（huggingface.co 被墙）；pip 走 aliyun 镜像

## 踩坑速览

详见 `docs/PITFALLS.md`。核心一条：**无卡模式 `huggingface_hub` 下载会失败，改用 curl 流式下载**（不是内存问题，是 downloader 问题）。

## 维护

- 阈值/参数集中在 `server/gen_flux.py` 顶部 argparse 与 `server/start_gen.sh` 变量区
- 分片下载大小清单在 `server/dl_curl.sh`（必须用 HF API 真实 LFS 字节数，勿猜）
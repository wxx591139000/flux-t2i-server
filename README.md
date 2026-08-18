# FLUX 文生图服务（通用）

> 版本：v1.8 · 2026-08-18（归档：`archive-20260818`）
> 自部署 FLUX.1 文生图服务，**与具体业务解耦，可服务所有文生图需求**（小红书配图、公众号配图、海报底图……）。
> 基于 AutoDL VGPU 32G 服务器 + diffusers。

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

## 目录结构

```
flux-t2i-server/
├── server/                  # 服务器端脚本
│   ├── gen_flux.py         # diffusers 批量文生图（读 prompts.json）
│   ├── start_gen.sh        # 一键启动（查GPU→查模型→screen后台跑）
│   ├── dl_curl.sh          # curl 流式分片下载（断点续传）
│   └── prompts.json        # (gitignored) 你的业务提示词
├── local/
│   └── flux_gen_watchdog.py # 本地开机看门狗
├── manager/                 # 对外服务 + 服务器管理（v1.1）
│   ├── flux_service.py     # main 入口（DB→quota→queue→web→飞书bot）
│   ├── flux_web_service.py # 对外 HTTP 服务（网页+API）
│   ├── flux_queue.py       # 队列调度器（单worker串行）
│   ├── flux_db.py          # SQLite 存储
│   ├── flux_quota.py       # 月度配额
│   ├── plans.yaml          # 套餐
│   ├── flux_server_manager.py # 服务器管理器（SSH生成+拉图+飞书）
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

### 3. 一键启动生成

```bash
# 本地看门狗（推荐：自动检测服务器开机+带卡 → 启动 → 拉回结果）
python local/flux_gen_watchdog.py --download

# 或手动在服务器上
ssh autodl-flux "bash /root/autodl-tmp/flux-t2i/start_gen.sh"
```

## 依赖

- 服务器：conda env `flux`（python3.12 / torch 2.13+cu130 / diffusers 0.39）
- 模型：`black-forest-labs/FLUX.1-dev`（gated，需 HF token，分片权重 ~31GB）
- 网络：服务器需 `HF_ENDPOINT=https://hf-mirror.com`（huggingface.co 被墙）；pip 走 aliyun 镜像

## 踩坑速览

详见 `docs/PITFALLS.md`。核心一条：**无卡模式 `huggingface_hub` 下载会失败，改用 curl 流式下载**（不是内存问题，是 downloader 问题）。

## 维护

- 阈值/参数集中在 `server/gen_flux.py` 顶部 argparse 与 `server/start_gen.sh` 变量区
- 分片下载大小清单在 `server/dl_curl.sh`（必须用 HF API 真实 LFS 字节数，勿猜）
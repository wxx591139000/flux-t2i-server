# FLUX 文生图服务（通用）

> 自部署 FLUX.1 文生图服务，**与具体业务解耦，可服务所有文生图需求**（小红书配图、公众号配图、海报底图……）。
> 基于 AutoDL VGPU 32G 服务器 + diffusers。

## 核心能力

- 🖼️ **批量文生图**：读 `prompts.json` 逐张生成，支持分组输出目录
- 🔔 **开机看门狗**（本地）：检测服务器开机+带卡 → 自动上传脚本 → 一键启动生成（对标转录 bot 机制）
- ⏯️ **一键启动**（服务器）：`start_gen.sh` 幂等启动，screen 后台可断 SSH
- ⏬ **curl 流式下载**：适配无卡 2GB 内存，断点续传（huggingface_hub 在无卡模式会失败，curl 绕开）

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
├── example/
│   └── prompts.example.json # 通用提示词模板
├── output/                  # (gitignored) 生成结果拉回本地
└── docs/                    # 归档文档
```

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
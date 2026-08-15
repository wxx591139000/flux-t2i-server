# 项目目标 SPEC — FLUX 文生图服务（通用）

> 版本：v1.1 · 2026-08-15

## 项目定位与目标

搭建一套**自部署 FLUX.1 文生图服务**，作为通用能力平台，**与具体业务解耦**，可服务所有文生图需求（小红书配图、公众号配图、海报底图、素材生成等）。

**核心目标**：
1. 服务器端能批量文生图（读提示词文件逐张生成）
2. 本地端能自动检测服务器开机+带卡 → 一键启动生成（对标转录 bot 的开机自启机制）
3. 无卡模式下也能完成模型下载（curl 流式绕开 huggingface_hub 失败问题）
4. 一键操作、幂等安全、断 SSH 可后台跑
5. **对外文生图服务（v1.1）**：Web 提交提示词 + 排队调度 + 配额计费 + 公网隧道（对标转录bot）

## 核心功能列表

| 功能 | 入口 | 说明 |
|---|---|---|
| 批量文生图 | `server/gen_flux.py` | 读 prompts.json 逐张生成，分组输出目录 |
| 一键启动 | `server/start_gen.sh` | 查GPU→查模型→screen后台跑；幂等（已在跑则跳过） |
| 模型下载 | `server/dl_curl.sh` | curl 流式+断点续传，适配无卡 2GB 内存 |
| 开机看门狗 | `local/flux_gen_watchdog.py` | 本地定时检测，自动上传+启动+拉回结果 |
| **对外Web服务** | `manager/flux_service.py` | 网页提交提示词 + API + 配额 + 公网 (v1.1) |
| **队列调度** | `manager/flux_queue.py` | 优先队列 + 单worker串行 + 服务器down重排队 (v1.1) |
| **配额计费** | `manager/flux_quota.py` + `plans.yaml` | 激活码 + 套餐 + 月度图片数 (v1.1) |

## 技术栈与架构概览

- **推理**：diffusers `FluxPipeline`（不用 ComfyUI，服务器 github 被墙）
- **模型**：`black-forest-labs/FLUX.1-dev`（gated，分片权重 ~31GB）
- **环境**：conda env `flux`（python3.12 / torch 2.13+cu130 / diffusers 0.39）
- **服务器**：AutoDL VGPU 32G（无卡/带卡两种模式）
- **下载**：curl + hf-mirror 镜像 + token 鉴权
- **后台**：screen（可断 SSH）
- **对外服务**：stdlib `http.server`（无框架）+ SQLite + PriorityQueue + cloudflared 隧道 (v1.1)

## 关键接口/组件定义

### prompts.json（输入格式）
```json
{
  "style_prefix": "可选，全局风格前置",
  "negative_prompt": "可选，负面提示",
  "notes": [
    {"note": "组名", "images": [
      {"key": "cover", "prompt": "..."}
    ]}
  ]
}
```

### gen_flux.py 参数
`--prompts`（提示词文件）/ `--out`（输出）/ `--model`（模型路径）/ `--start`/`--end`（范围）/ `--steps`（步数）/ `--seed`/`--width`/`--height`

### start_gen.sh 参数
`[--force]` 强制重启；无参数则幂等（已在跑跳过）

### 看门狗参数（本地）
`--once`（跑一次即退）/ `--interval`（检测间隔秒）/ `--download`（完成后拉回结果）

### 对外服务 API（v1.1）
| 端点 | 说明 |
|---|---|
| `GET /` | 提交页（提示词输入 + 任务列表 + 看图/下载 + 兑换激活码） |
| `GET /center` | 我的任务中心 |
| `GET /health` | 健康检查 |
| `POST /api/submit?token=` | `{prompt, priority?}` 提交生成任务 |
| `GET /api/status?job_id=` | 轮询任务状态 |
| `GET /api/download/<job_id>?token=` | 下载图片（校验归属） |
| `POST /api/activate` | `{code, token}` 兑换激活码 |
| `POST /api/admin/gen_codes` | `{admin_token, plan, n}` owner 生成激活码 |

### manager/.env 配置（v1.1）
`WEB_PORT`（默认9620）/ `WEB_ADMIN_TOKEN`（owner生成激活码）/ `FEISHU_APP_ID`/`FEISHU_APP_SECRET`/`FEISHU_OWNER_OPEN_ID`

## 已知约束与边界

- **需要带卡模式才能生成**（无卡无 GPU）；无卡仅能下载模型
- **模型是 gated 仓库**，需 HF token（从环境变量 `HF_TOKEN` 读，不硬编码）
- **服务器被墙**：huggingface.co / github 不可达，需 hf-mirror + aliyun pip 镜像
- **分片大小必须用 HF API 真实 LFS 字节数**，不能猜（猜小会损坏文件）
- 生成 768×1024（3:4）约需数秒~数十秒/张，CPU offload 下速度偏慢
- **GPU 单卡严格串行**：队列 worker 单线程，多用户需排队（~85s/张）(v1.1)
- **配额入队即扣**：每月图片数按 token 计，owner 无限 (v1.1)
- **密钥/凭证绝不入库**：`.env`、token、日志、运行时 data 均在 `.gitignore` (v1.1)
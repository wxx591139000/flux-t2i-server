# 项目详细方案 — FLUX 文生图服务（通用）

> 版本：v1.0 · 2026-08-15

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
| `server/gen_flux.py` | 加载 FluxPipeline（bf16 + CPU offload），读 prompts.json 逐张生成，同名输出跳过 |
| `server/start_gen.sh` | 带卡检查→模型检查→脚本检查→screen 后台启动 gen_flux.py；幂等 |
| `server/dl_curl.sh` | curl 流式断点续传下载全部分片，停滞检测，完成打 DOWNLOAD_DONE |
| `local/flux_gen_watchdog.py` | 本地循环：可达性→带卡→模型→启动→确认→(可选)拉回 |

## 数据流 / 调用链路

**生成链路**：
```
prompts.json → gen_flux.py → FluxPipeline(FLUX.1-dev) → out/<NN>_<组名>/<key>.png
```

**一键启动链路**（幂等）：
```
start_gen.sh → [无卡?]abort → [模型未就绪?]abort → [已在跑?]skip → screen 启动 gen_flux.py
```

**看门狗链路**：
```
watchdog --download → SSH可达? → 带卡? → 模型就绪? → 已在跑? → scp上传 → start_gen.sh → 确认fluxgen → scp拉回 output/
```

## 关键设计决策

1. **diffusers 而非 ComfyUI**：服务器 github 被墙，ComfyUI git clone 失败；diffusers 纯 pip 可装
2. **curl 流式下载而非 huggingface_hub**：无卡模式下 hf_hub 下载器反复失败（非内存问题），curl 流式写盘内存极小、断点续传
3. **CPU offload**：32G 显存 + 大内存，用 `enable_sequential_cpu_offload` 最稳，兼容低显存
4. **分片权重而非单文件**：FLUX.1-dev 官方是 sharded，transformer 3 片 + T5 2 片；大小从 HF API 取真实 LFS 字节数
5. **screen 后台**：可断 SSH，任务不中断
6. **密钥环境变量化**：HF_TOKEN 从环境变量读，不硬编码（防 GitHub 泄露）

## 部署架构

- **服务器**：AutoDL VGPU 32G，conda env `flux`
- **模型路径**：`/root/autodl-tmp/models/FLUX.1-dev`
- **工作目录**：`/root/autodl-tmp/flux-t2i/`
- **SSH 别名**：`autodl-flux`（`~/.ssh/config`）
- **下载镜像**：`HF_ENDPOINT=https://hf-mirror.com`；pip：`mirrors.aliyun.com`
- **本地**：Windows，看门狗定时跑，结果拉回 `output/`
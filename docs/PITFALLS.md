# 项目踩坑记录 — FLUX 文生图服务（通用）

> 持续更新。格式：`[日期] 问题 → 原因 → 解决`

## FLUX 部署下载（2026-08-15）

- **[08-15] huggingface_hub 无卡模式下载反复失败/被杀 → 初判以为是无卡 2GB 内存 OOM（错误）→ 实际是 downloader 本身在受限环境出错，与内存无关（纯下载不耗内存）→ 改用 curl 流式下载 + 断点续传（`-C -` + hf-mirror + token header），内存极小，实测 34MB/s 正常跑满**
- **[08-15] 分片大小硬编码猜错会损坏文件 → 分片是 sharded 权重，transformer 3 片 + T5 2 片，大小各自不同 → 必须用 HF API `tree/main?recursive=true` 查真实 LFS 字节数，不能猜；猜小会提前判定完成导致文件损坏**
- **[08-15] 失败下载产生大量 `.incomplete` 垃圾撑爆磁盘（25.8GB）→ 下载器反复失败残留 → 定期清理 `*.incomplete` 和 `.cache/huggingface/download`**
- **[08-15] FLUX.1-dev 是 gated 仓库（403）→ 需只读 token → token 从环境变量 `HF_TOKEN` 读，不硬编码**

## 服务器环境（2026-08-15）

- **[08-15] huggingface.co / github 被墙 → 用 `HF_ENDPOINT=https://hf-mirror.com`；pip 走 aliyun 镜像；ComfyUI git clone 失败 → 改用 diffusers（纯 pip 可装）**
- **[08-15] `pkill -f 脚本名` 自杀（命令行含匹配串）→ 用 `[u]` 转义：`pkill -f 'dl_fl[u]x.py'`**
- **[08-15] 无卡模式 `nvidia-smi` 空但 exit 0 → GPU 检查用 `if [ -z "$GPU" ]` 判断，不能只看 exit code**
- **[08-15] AutoDL 计费限时自动关机 → 常驻任务用 screen 防断 SSH，下载/生成要快**

## 参考链接

- 小红书侧原始记录：`ObsidW/审查/山西旅游-FLUX部署生成方案.md` 第 8 节
- 转录 bot 机制参考：`/root/autodl-active`（服务器会话恢复）
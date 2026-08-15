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

## 对外 Web 服务 / Windows（2026-08-15，v1.1）

- **[08-15] Windows `subprocess.run(shell=True)` 用 cmd.exe 错误解析 Linux 管道/重定向 → gpu_ready 报"系统找不到指定的路径"(255) → `run()` 改 `['bash','-lc',cmd]`**
- **[08-15] `subprocess text=True` 默认 GBK 解码 UTF-8 中文输出 → `UnicodeDecodeError` 崩掉 start_generation → 加 `encoding='utf-8', errors='replace'`**
- **[08-15] scp 本地 Windows 反斜杠路径被 bash 当转义符损坏 → 转正斜杠 `str(path).replace('\\','/')`**
- **[08-15] 队列生成前未清空服务器 `out/` → scp 拉回带上小红书产线历史图(cover/P1-P6) 污染 web_out → 生成前 `rm -rf {REMOTE_OUT}/*`**
- **[08-15] api token 放 body 不生效 → handler 只读 query/cookie → 用 `?token=` 传 submit/download**
- **[08-15] `WEB_ADMIN_TOKEN` 在 .env 加载前读到空值(403) → 模块 import 时 `_load_env()` 重新加载**
- **[08-15] `Set-Cookie` 在 `send_response` 前调用导致头损坏 → 存 `_cookie` 到 `_send` 里统一发送**
- **[08-15] cloudflared 重启加 `--logfile` 参数启动失败(0进程) → 用 Start-Process `-RedirectStandardError` 到日志文件，弃用 `--logfile`**
- **[08-15] 公网下载 urllib/python 报 SSL: UNEXPECTED_EOF → TLS 怪癖，curl 正常，非服务问题**

## 中文提示词转换（2026-08-15，v1.2）

- **[08-15] FLUX 不理解中文 → 客户提交中文提示词生成完全跑偏（如"人狗打架"生出女人）→ FLUX.1-dev 是英文单语模型 → 新增 `prompt_translator.py` 借鉴短剧 FLUX 方法论，用 LLM 把中文转成 30-80 词英文静态提示词**
- **[08-15] 火山方舟 LLM 端点 404 → 短剧项目 `pipeline.py` 用 `{BASE}/v3/chat/completions`（OpenAI 兼容格式）+ model `deepseek-v4-flash-260425` → 对齐此调用方式；LLM 配置从 `~/.claude/settings.json` 的 env 读（非 shell 环境变量）**
- **[08-15] Windows curl 发中文 body 按 GBK 编码 → 服务端 `_read_body` UTF-8 解码 UnicodeDecodeError，且 `errors='replace'` 后乱码导致 `has_chinese` 检测不到 → 服务端 `_read_body` 加 UTF-8 兜底；测试用 `--data-binary` + python urllib 发 UTF-8，勿用 curl 中文 body**

## 参考链接

- 小红书侧原始记录：`ObsidW/审查/山西旅游-FLUX部署生成方案.md` 第 8 节
- 转录 bot 机制参考：`/root/autodl-active`（服务器会话恢复）
- 对外服务架构：`docs/WEB_SERVICE.md`；使用指南：`docs/USER_GUIDE.md`
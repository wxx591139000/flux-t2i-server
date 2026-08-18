# 项目踩坑记录 — FLUX 文生图服务（通用）

> 持续更新。格式：`[日期] 问题 → 原因 → 解决`

## 运维启动脚本 / admin 登录（2026-08-18，v1.8）

- **[08-18] cloudflared 起不来："tunnel run accepts only one argument" → `start_service.ps1` 传 `-ArgumentList 'tunnel','run',...,'--config',path` 参数拆分坏，`--config` 被当位置参数 → 撤 `--config`，`cd .cloudflared` 目录 + `tunnel run xhs-tunnel`（config.yml 在目录内自动加载，COORDINATION.md 文档方式）**
- **[08-18] 公网 flux.zhuanlu.xyz 报 1033 / 530 → 本地 Windows 重启后 flux_service 和 cloudflared 隧道全掉 → 一键启动脚本拉起来；因果判断：机器重启后永久进程被回收，非代码 bug**
- **[08-18] 含中文的 .ps1 一执行就 ParserError（字符串缺终止符）→ 无 UTF-8 BOM，PowerShell 按系统 ANSI(GBK) 读中文乱码截断引号 → python 补 `\xef\xbb\xbf` BOM**
- **[08-18] admin 输对 token 仍报"Token 错误" → `/api/admin/users` 返回 `{"accounts":[]}`，前端 `login()` 检查 `d.users`（undefined）→ 改 `d.accounts!==undefined`（两处：login + 自动刷新 IIFE）**

## 飞书对话式出图（2026-08-16，v1.7）

- **[08-16] 飞书发图必须两步走 → 飞书图片消息不支持直接发二进制 → 先 `POST /open-apis/im/v1/images`（multipart，`image_type=message`）拿 `image_key`，再发 `msg_type=image` + `content={"image_key":...}`（与发文件 `/open-apis/im/v1/files` + `file_key` 结构等价）**
- **[08-16] lark_oapi WS 事件数据与轮询 REST 字段不同 → WS 用 `message.message_type`（非 `msg_type`）、`content` 在顶层、`message.chat_type` 判私聊 → 按 WS 结构解析，勿照抄 REST 字段名**
- **[08-16] 飞书 WS 长连接需 `lark-oapi` 依赖 → 未装则机器人静默不监听 → `_ws_listen` 内 try/except 导入并打错误日志，服务不崩**

## 商户管理中心 / 账户化（2026-08-16，v1.5）

- **[08-16] 管理页点登录没反应 → admin.html 是嵌入 Python `.format()` 模板，JS 花括号要写两遍（`{}`→`{{}}`），`tb.appendChild(tr);}}))}}` 多写一个右括号 → 渲染成 `}))}`（多一个 `)`），整个 `<script>` 语法错误不执行 → 用 `node --check` 校验服务端渲染出的 JS，修正为 `}})` + `}}`（`}})}}`）**
- **[08-16] 激活的码状态显示成"可用"而非"已用" → 激活时 `status='active'`，但 JS 徽标只认 `'used'` → `active` 落入"可用"分支 → JS 判定改为 `used = status && status!=='unused'`（active 归已用）**
- **[08-16] 激活码绑死单个 token，客户换设备丢套餐 → 激活码一次性绑 token → 借鉴转录项目账户模型：激活码=账户（`accounts` 表），客户任意 token「绑定激活码」并入同账户，用量按 `account_usage` 聚合**
- **[08-16] 商家看到海量随机 token 对不上客户 → 用户表全是 16 位随机 hex → 激活码=账户后，admin 主表改「客户/账户」维度（客户名 remark / 设备数 / 用量聚合），激活码即账户天然对应客户**

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

## 队列服务器恢复（2026-08-15，v1.4）

- **[08-15] 服务器 down 时任务重试 3 次就标 failed，恢复后无自动恢复机制 → 服务器恢复后任务永远失败，需手动重提 → 对齐转录 `orchestrator._recover_failed_tasks`：服务器 down 时任务进 `waiting` 池（不失败、不立即重排），健康监控检测到恢复时 `_recover_waiting_tasks` 自动重入队，每任务最多恢复 3 次（防毒瘤）**
- **[08-15] 旧版 `_process` 服务器 down 用 `queue.put` 立即重排 → 几秒内打满 3 次 retry → 改为进 waiting 池由健康监控（30s）统一恢复，避免打满**
- **[08-15] 曾因服务器 down 而 failed 的历史任务（retry 超限）不会自动恢复 → 需手动重置或重新提交（防毒瘤机制所致，非 bug）**

## 参考链接

- 小红书侧原始记录：`ObsidW/审查/山西旅游-FLUX部署生成方案.md` 第 8 节
- 转录 bot 机制参考：`/root/autodl-active`（服务器会话恢复）
- 对外服务架构：`docs/WEB_SERVICE.md`；使用指南：`docs/USER_GUIDE.md`
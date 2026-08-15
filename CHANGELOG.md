# CHANGELOG

## [v1.2] - 2026-08-15

**对外文生图服务完善 + 中文提示词智能转换**：网页下载按钮 + 借鉴短剧 FLUX 方法论的中文→英文提示词翻译。

- 新增 `manager/flux_web_service.py`：每个已完成任务网页带「⬇ 下载」按钮；修图片 URL token 写死 `X` 的隐患，改为显式带当前用户 token
- **新增 `manager/prompt_translator.py`**：中文提示词 → FLUX 友好英文提示词翻译器（借鉴短剧 `FLUX_SYSTEM_PROMPT` 方法论：静态场景/镜头/光线/构图/30-80词/技术质量标记；LLM 失败返回原文兜底）
- **`flux_queue.py`**：submit() 入队前检测中文，自动调用转换
- **`flux_db.py`**：jobs 表加 `original_prompt` 列（存原始中文）+ 幂等迁移
- **`flux_web_service.py`**：任务表格展示原始中文 + FLUX 实际英文用词；`_read_body` 加 UTF-8 兜底（Windows curl GBK 中文 body）
- 新增 `docs/USER_GUIDE.md`：用户使用指南（浏览器操作 + owner/API + 配额规则）
- 新增 `docs/WEB_SERVICE.md`：对外服务架构文档
- 更新 `docs/`：SPEC / ARCHITECTURE / TEST_PLAN / PITFALLS / PROGRESS 补 v1.1 对外服务 + v1.2 中文转换
- 更新 `README.md`：v1.1 对外服务能力 + manager/ 目录结构

**commit**: `归档: flux-t2i-server 2026-08-15`（v1.2） · **tag**: `archive-20260815-v2`

## [v1.1] - 2026-08-15

**对外文生图服务上线**（对标转录bot）：Web 提交 + 排队调度 + 配额计费 + 公网隧道。

- 新增 `manager/`：`flux_service`(入口) / `flux_web_service`(网页+API) / `flux_queue`(队列调度) / `flux_db`(SQLite) / `flux_quota`(配额) / `plans.yaml`(套餐) / `feishu_notify`(飞书通知) / `flux_server_manager`(SSH生成)
- 公网 `flux.zhuanlu.xyz`（本地 xhs-tunnel 加 ingress → localhost:9620）
- 修 Windows subprocess 三坑：cmd.exe 管道 / GBK 解码 / 反斜杠路径
- E2E 全链路实测通过（提交→生成→拉图→公网下载）

## [v1.0] - 2026-08-15

**通用化落地**：将小红书场景的 FLUX 部署沉淀为通用文生图服务项目，与业务解耦。

- 新增 `server/gen_flux.py`：diffusers 批量文生图（bf16 + CPU offload，分组输出，断点跳过）
- 新增 `server/start_gen.sh`：一键启动（带卡检查→模型检查→幂等→screen 后台）
- 新增 `server/dl_curl.sh`：curl 流式分片下载（断点续传 + 停滞检测）
- 新增 `local/flux_gen_watchdog.py`：开机看门狗（对标转录 bot）
- 新增 `example/prompts.example.json`：通用提示词模板
- 新增 `docs/`：SPEC / ARCHITECTURE / TEST_PLAN / PITFALLS / PROGRESS
- 密钥环境变量化（HF_TOKEN 不硬编码）

**commit**: `归档: flux-t2i-server 2026-08-15` · **tag**: `archive-20260815`
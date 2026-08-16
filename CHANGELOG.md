# CHANGELOG

## [v1.7] - 2026-08-16

**飞书"图图"对话式出图机器人**：从单向通知器升级为可对话出图，飞书私聊发提示词 → 生成后图片回传。

- 新增 `manager/feishu_bot.py`：WebSocket 长连接监听 P2P 私聊（借鉴转录bot"小白" feishu_channel 范式），收到文本 → `scheduler.submit(open_id, prompt)` → 确认消息 + 用量 → 轮询线程检测完成 → 上传图片回传
- `manager/feishu_notify.py`：新增 `upload_image()` / `send_image()` / `send_image_direct()`（飞书图片上传 `/open-apis/im/v1/images` + 图片消息发送）
- `manager/flux_db.py`：新增 `user_ensure()`（飞书 open_id 即 user_id，首次自动建用户）
- `manager/flux_service.py`：主入口集成 bot 启动（凭证从 .env 读，未配置则不启动）
- 设计：同进程内嵌直接调 scheduler（不走 HTTP）；owner（自己）通过 bot 无限量；每用户最多 1 个在途任务；首版仅 P2P 私聊、纯文生图（斜杠命令给提示语）

**验证**：图片回传链路实测通过（飞书收到测试图）；对话逻辑模拟通过（确认/在途拦截/配额/完成后回图）；服务重启 bot 激活，web 不受影响（本地+公网 200）

## [v1.6] - 2026-08-16

**新增两份使用 SOP 文档**（纯文档变更，无代码改动）。

- 新增 `docs/flux服务-商家使用SOP.md`：商户经营客户套餐的完整 SOP（登录商户中心、激活码=账户核心概念、生成激活码开新客户、客户/账户管理改套餐/设owner/下钻、激活码清单、最近任务、异常排查、API 批量操作、安全约定）
- 新增 `docs/flux服务-用户使用SOP.md`：普通用户从零到生成图的完整 SOP（身份Token认知、5步生成图、套餐额度、激活码激活/绑定账户、换设备场景、FAQ、API 调用、隐私安全）
- 更新 `README.md` 至 v1.6，新增「使用文档」索引

## [v1.5] - 2026-08-16

**商户管理中心 `\admin` + 账户化改造**（成套借鉴转录项目 admin.html + account 模型）。

### 商户管理中心 `/admin`（借鉴转录项目 admin.html）
- **鉴权**：`X-Admin-Token` / `Authorization: Bearer` 头 = `WEB_ADMIN_TOKEN`（非 cookie，页面存 localStorage）
- **激活码**：8 位去混淆字符集（`ABCDEFGHJKMNPQRSTUVWXYZ23456789`）、`status`(unused/active)/`expires_at`/`remark`、生成 `count 1-100` 校验
- **管理面板**：生成激活码表单 + 全码统计表 + 改备注 + 下钻 + 用户管理（套餐/owner/用量）+ 最近任务
- `flux_db.py`：codes 表迁移加 `created_at/expires_at/remark/status` 列；`code_generate` 改 secrets 去混淆 8 位；新增 `list_users/list_codes/list_jobs/search_users/code_set_remark`
- `flux_web_service.py`：新增 `/admin` 面板 + 8 个 admin API（users/codes/jobs/gen_codes/set_remark/set_plan/set_owner/search）

### 账户化改造（借鉴转录项目 account 模型）
- **激活码=账户**：一客户一账户，客户任意 token「绑定激活码」并入同一账户，多设备**共享一份套餐**
- **用量按账户聚合**：`users` 加 `account_id` 列；`quota.effective()` 按账户聚合（绑账户则套餐/用量/owner 取账户，否则自账户向后兼容）
- **商家按客户管理**：admin 主表改为「客户/账户」维度（账户/客户名/套餐/用量/设备数/详情），账户详情列出关联 token
- `flux_db.py`：`accounts` 表 + 账户方法（create/bind/usage/inflight/list）+ `code_activate` 建账户、新增 `code_bind` + 已有 active 码回填
- `flux_web_service.py`：激活改账户 + 新增 `/api/bind`、`/api/my` + 客户页「激活/绑定账户」框 + admin 账户维度界面
- E2E 验证通过：激活建账户→多设备绑定→用量聚合→账户详情→set_plan/set_owner→未绑 token 独立

## [v1.4] - 2026-08-15

**队列服务器恢复自动重试**（对齐转录 orchestrator 机制）：服务器 down 时任务不再失败，恢复后自动重试。

- `flux_queue.py`：服务器 down 时任务进 `waiting` 池（不失败、不立即重排）；健康监控检测到恢复时 `_recover_waiting_tasks` 自动重入队；每任务最多恢复 3 次（防毒瘤）；启动时 `_recover_stale_waiting` 恢复遗留任务
- `flux_db.py`：新增 `jobs_waiting()` 查询
- `flux_web_service.py`：任务状态新增「等待服务恢复」
- 单元测试通过：down→waiting 不失败→up→自动入队→生成完成；重试上限生效

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
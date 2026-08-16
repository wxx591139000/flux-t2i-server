# 项目推进进度 — FLUX 文生图服务（通用）

> 版本：v1.6 · 2026-08-16

## 里程碑回顾

- **2026-08-14**：小红书场景下最初部署 FLUX（山西旅游配图），踩坑记录到方案文档
- **2026-08-15**：沉淀为**通用项目** `flux-t2i-server`，与小红书解耦；完成 curl 流式下载解法；全流程跑通（下载32GB→切带卡→生成36张→拉回→智能插入7篇笔记）
- **2026-08-15（同日 v1.1）**：新增**对外文生图服务**（对标转录bot）：Web 提交 + 排队调度 + 配额计费 + 公网隧道 `flux.zhuanlu.xyz`；E2E 全链路实测通过
- **2026-08-16（v1.5）**：新增**商户管理中心 `/admin`**（借鉴转录项目 admin.html，管理套餐/token/激活码）+ **账户化改造**（激活码=账户，多设备绑定共享套餐，用量按账户聚合，商家按客户管理）
- **2026-08-16（v1.6）**：新增**两份使用 SOP 文档**（纯文档变更）——`docs/flux服务-商家使用SOP.md`（商户经营套餐全流程）+ `docs/flux服务-用户使用SOP.md`（用户从零到生成图）

## 已完成功能清单

### 商户管理中心 + 账户化（v1.5）
- [x] `/admin` 商户管理中心（借鉴转录项目 admin.html）：`X-Admin-Token` 头鉴权 = `WEB_ADMIN_TOKEN`
- [x] 激活码：8 位去混淆字符集、`status/expires_at/remark`、生成 `count 1-100` 校验、全码统计表、改备注、下钻
- [x] 激活码=账户：`accounts` 表 + `users.account_id` 迁移 + 已有 active 码回填
- [x] `code_activate` 建账户 + 新增 `code_bind`（换设备并入账户）
- [x] 用量账户聚合：`quota.effective()`（绑账户则套餐/用量/owner 按账户，未绑自账户向后兼容）
- [x] `accounts`/`codes` 迁移 + 账户方法（create/bind/usage/inflight/tokens/list）
- [x] 新增 `/api/bind`、`/api/my`；客户页「激活/绑定账户」框
- [x] admin 按「客户/账户」管理：账户/客户名/套餐/用量聚合/设备数/详情（账户下所有 token）
- [x] E2E 验证：激活建账户→多设备绑定→用量聚合→账户详情→set_plan/set_owner→未绑 token 独立

### 核心文生图（v1.0）
- [x] `server/gen_flux.py`：diffusers 批量文生图（bf16 + CPU offload，分组输出，断点跳过）
- [x] `server/start_gen.sh`：一键启动（带卡检查→模型检查→幂等→screen 后台）
- [x] `server/dl_curl.sh`：curl 流式分片下载（断点续传 + 停滞检测 + DOWNLOAD_DONE 标记）
- [x] `local/flux_gen_watchdog.py`：开机看门狗（对标转录 bot：检测→上传→启动→拉回）
- [x] `example/prompts.example.json`：通用提示词模板
- [x] 山西旅游 7 篇 36 张配图全流程跑通并插入笔记

### 队列服务器恢复（v1.4）
- [x] 服务器 down 时任务进 `waiting` 池（不失败、不立即重排）
- [x] 健康监控检测到服务器恢复时 `_recover_waiting_tasks` 自动重入队
- [x] 每任务最多恢复 3 次（防毒瘤），超限标 failed + `[RECOVER_SKIP]`
- [x] 启动时 `_recover_stale_waiting` 恢复遗留 waiting 任务（防重启丢失）
- [x] 网页状态新增「等待服务恢复」
- [x] 单元测试：down→waiting 不失败→up→自动入队→生成完成；重试上限生效

### 中文提示词转换（v1.2）
- [x] `manager/prompt_translator.py`：中文→FLUX 英文提示词翻译器（借鉴短剧 FLUX 方法论，LLM 失败返回原文）
- [x] `flux_queue.py` submit() 入队前检测中文自动转换
- [x] `flux_db.py` jobs 表加 `original_prompt` 列（存原始中文）
- [x] `flux_web_service.py` 任务表格展示原始中文 + FLUX 实际用词
- [x] E2E 验证：中文"一只橘猫坐在窗台上"→ 转英文 → FLUX 生成成功（PNG 有效）
- [x] 修 `_read_body` UTF-8 兜底（Windows curl GBK 中文 body）

### 对外服务（v1.1）
- [x] `manager/flux_service.py`：main 入口（wiring DB→quota→queue→web）
- [x] `manager/flux_web_service.py`：网页提交页 + API + 认证（cookie/token）
- [x] `manager/flux_queue.py`：优先队列 + 单 worker 串行 + 去重/配额/上限门 + 服务器down重排队
- [x] `manager/flux_db.py`：SQLite（users/codes/jobs/usage）
- [x] `manager/flux_quota.py` + `plans.yaml`：月度配额 + 套餐（default10/basic50/pro200）
- [x] `manager/feishu_notify.py`：飞书通知（独立机器人"图图"，与转录"小白"分离）
- [x] `manager/flux_server_manager.py`：SSH 生成/拉图/插稿/飞书，run() 已修 Windows 坑
- [x] 公网隧道 `flux.zhuanlu.xyz`（本地 xhs-tunnel）
- [x] **E2E 全链路实测**：提交→排队→FLUX 生成→拉图 web_out→公网下载（橘猫+小孩在岸边散步均成功）
- [x] 每个 done 任务网页带「⬇ 下载」按钮
- [x] `docs/WEB_SERVICE.md` + `docs/USER_GUIDE.md` 使用指南

## 进行中工作

- [ ] `flux-t2i-server` 二次归档（v1.2，tag archive-20260815-v2）

## 待办事项 / Roadmap

- [ ] **git 提交 + 打 tag** 本次 v1.1→v1.2 归档
- [ ] **小红书产线回归**：`flux_server_manager` 文件队列（run() 修复后）未再实测，需在服务器可用时跑通
- [ ] **长期守护**：服务进程 + cloudflared 隧道目前是手动/后台进程，会话结束会被回收；建议做开机自启守护脚本
- [ ] **owner 无限配额**：当前 owner 用预留 token，未配置 is_owner 无限逻辑（quota 里 owner 跳过）
- [ ] 清理测试失败记录（早期服务器未起时的 failed 任务）
- [ ] （可选）接入监控/告警（任务进度、下载完成通知）
# 项目推进进度 — FLUX 文生图服务（通用）

> 版本：v2.0 · 2026-09-04

## 里程碑回顾

- **2026-08-14**：小红书场景下最初部署 FLUX（山西旅游配图），踩坑记录到方案文档
- **2026-08-15**：沉淀为**通用项目** `flux-t2i-server`，与小红书解耦；完成 curl 流式下载解法；全流程跑通（下载32GB→切带卡→生成36张→拉回→智能插入7篇笔记）
- **2026-08-15（同日 v1.1）**：新增**对外文生图服务**（对标转录bot）：Web 提交 + 排队调度 + 配额计费 + 公网隧道 `flux.zhuanlu.xyz`；E2E 全链路实测通过
- **2026-08-16（v1.5）**：新增**商户管理中心 `/admin`**（借鉴转录项目 admin.html，管理套餐/token/激活码）+ **账户化改造**（激活码=账户，多设备绑定共享套餐，用量按账户聚合，商家按客户管理）
- **2026-08-16（v1.6）**：新增**两份使用 SOP 文档**（纯文档变更）——`docs/flux服务-商家使用SOP.md`（商户经营套餐全流程）+ `docs/flux服务-用户使用SOP.md`（用户从零到生成图）
- **2026-08-16（v1.7）**：**飞书"图图"对话式出图机器人**——从单向通知器升级为可对话出图（私聊发提示词→生成→图片回传），借鉴转录bot"小白"的 WS 长连接范式
- **2026-08-18（v1.8）**：**一键启动脚本 + admin 登录 bug 修复**——`start_service.ps1 -Target flux/xhs/tunnel/all` 分别拉起 FLUX / 小红书 / 隧道（修 cloudflared 启动 bug + 补 BOM）；修 admin 登录判定字段错（`d.users`→`d.accounts`）
- **2026-09-04（v2.0）**：**多服务器化 + VPS 看门狗 + 重启恢复修复**——任务中心 `FLUX_SERVERS` 注册表支持多台（flux1+flux2），`find_ready_server()` 每单挑任一台可用；`watchdog/` 新建 VPS 看门狗（镜像 qwen）；`flux_queue` 加 `_recover_orphaned_jobs` 重启不丢单；根治**僵尸 screen 会话被误判"运行中"→跳过启动→任务超时**的缺陷（4 处 `screen -wipe`）；重启验证 `server` 列落库 + 死机自愈链路全通

## 已完成功能清单

### 多服务器 + VPS 看门狗（v2.0）
- [x] `manager/flux_server_manager.py`：`FLUX_SERVERS` 多服务器注册表（env `FLUX_SERVERS_JSON` 可覆写）+ SSH 操作全部服务器感知（`server` 参数，None=默认 flux1）+ `probe/find_ready_server/any_ready`
- [x] `manager/flux_queue.py`：`_generate` 每单 `find_ready_server()` 挑任一台 + `server` 名回写 DB；`_health_loop` 用 `any_ready()`——任一台起来即恢复 waiting 池
- [x] `manager/flux_queue.py` `_recover_orphaned_jobs()`：重启后把 DB 残留 queued/generating 任务重入队（原先内存队列重启即丢成孤儿）
- [x] `manager/flux_db.py`：jobs 表加可空 `server` 列（幂等迁移）
- [x] `watchdog/`：`flux_server_ready.sh`（服务器端就绪，`--check`只读/全量）+ `flux_watchdog.sh`（VPS 巡检）+ `flux-watchdog.service`（systemd）+ `README.md`（部署）；镜像 qwen `watchdog-vps`
- [x] 根治僵尸 screen：`server/start_gen.sh`、`gen_running()`、`flux_queue._generate`、`watchdog/flux_server_ready.sh` 均加 `screen -wipe`
- [x] 实测：重启加载 v2.0 + 孤儿恢复 + 死机自愈（22:40 recovery） + `server=flux1` 落库全通

### 飞书"图图"对话式出图（v1.7）
- [x] `manager/feishu_bot.py`：WebSocket 长连接监听 P2P 私聊（lark_oapi.ws.Client，对齐转录bot feishu_channel）
- [x] 收到文本 → `scheduler.submit(open_id, prompt)`：中文自动翻译、配额检查、入队（复用现有队列）
- [x] 确认消息回执（任务号 + 用量）；每用户最多 1 个在途任务
- [x] 轮询线程检测 job done → 上传图片到飞书（`/open-apis/im/v1/images`）→ 发送图片消息回传
- [x] 失败/服务器down 异常兜底（回错误信息、waiting 通知恢复）
- [x] `feishu_notify.py` 新增图片上传/发送；`flux_db.py` 新增 `user_ensure`；`flux_service.py` 集成启动
- [x] owner（自己）通过 bot 无限量；斜杠命令给提示语；仅 P2P 私聊
- [x] 实测：图片回传链路 SEND OK + 对话逻辑模拟 + 服务重启激活 + web 不受影响

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

- [x] v2.0 多服务器 + VPS 看门狗 + 重启恢复修复（2026-09-04，待 git 归档）

## 待办事项 / Roadmap

- [ ] **flux2 接入**（等 clone 完成、端口 23192 可达）：免密 + 校验数据盘克隆 + 出现在 `find_ready_server` 备选；后续加机只需在 `FLUX_SERVERS`/`watchdog/TARGETS` 各加一条
- [ ] **VPS 看门狗部署**：`watchdog/` 代码已就绪，部署到 vps-aliyun（`/opt/flux-watchdog` + `systemctl enable --now` + 配 key），见 `watchdog/README.md`
- [ ] **遗留 failed 任务重提**：早期僵尸 screen 导致的 `生成超时` 历史失败（如 1786886515483）不会自愈，需要时手动重提（重提走新的多服务器+server 落库链路）
- [ ] **小红书产线回归**：重启脚本已升 v2.0，本地 `flux_gen_watchdog.py` 仍绑定 flux1 单机（默认），确认多服务器后小红书产线是否也要吃到（计划保持 flux1 不动）
- [ ] **长期守护**：服务进程 + cloudflared 隧道目前手动/后台进程，会话结束会被回收；建议开机自启守护脚本

---

## 2026-09-23 · 对外可用性修复 + 商户后台开公网 + 图图设计方案

**一句话**：把"朋友打开公网看到旧页面"这个长期问题查到了根、修掉了，
并给商户后台开了独立公网域名且轮换了弱口令。全量测试 **18/18 全绿**。

### 已完成

| 项 | 结果 | 关键证据 |
|---|---|---|
| 对外一键启动真正可用 | 隧道/9620/3000 全部强制重启 + **身份验收**（公网 title 必须等于 3000） | Playwright 真浏览器：公网 title == 3000 title，`公网==3000 → True` |
| 修 `~/.ssh/config` 读不到导致 9620 起不来 | 函数体整体进 try，降级为 warning | commit `94ee0a9`；门 `test_server_registry.py` 第 [6] 组 45/45 |
| 商户后台开公网独立域名 | `flux-admin.zhuanlu.xyz` → `localhost:9620` | `cloudflared tunnel ingress validate` = OK；规则匹配 rule#2 |
| 管理员口令轮换 | `WEB_ADMIN_TOKEN` 换成 20 位强口令 | 新口令 `GET /api/admin/users` → 200；**旧口令 `flux-admin-2026` → 403** |
| 图图机器人现状盘点 + 完整互动设计方案 | 产出设计方案（差距分析 + P0-P6 + 验收标准） | `docs/图图-飞书完整互动-设计方案.md` |
| 编排层三处根因修复 | ①数组 splatting 绑不上 switch ②`Start-Process` 重定向遇重复环境键必抛 ③活体检查冒充身份检查 | 见 `PITFALLS.md` 同日 3 条 |

### 进行中 / 下一步

- **图图 P0**：修 3 个会烧钱/会丢结果的缺陷 —— ①`MAX_ACTIVE=50` 超出静默丢弃改成必须回执
  ②加事件去重（`evt:` + `filekey:` 双键）③在途任务落 DB + 重启恢复
- 图图三个决策已拍板（见设计文档 §8）：**宿主选 A（本机进程内）/ 先做激活码绑定 / 只服务 owner 与 owner 的群**

### 已知未解（诚实记录）

- **`deploy_site_prod.ps1` 的 `pause` 卡死编排器**：根因（数组 splatting 绑不上 `-NoPause`）已修，
  但**"用户真实控制台不再卡死"这一条我没能亲自验证** —— 工具环境的 stdin 是管道，`pause` 立即返回，
  复现不出。已加进度文件/结论文件，下一次真实运行即可取证。
- `9620 /health` 仍不透传 `profile` / `capabilities`（P1 遗留）
- Qwen 显存实测（bf16 约 30-35GB vs 32G 卡）仍是最大未知数，等带卡


### 2026-09-23 追加 · 图图 P0 已完成

设计文档 §6 的 P0（修"会烧钱/会丢用户结果"的缺陷）已落地，**并多修了一个更严重的**：

| 编号 | 缺陷 | 严重度 | 修法 |
|---|---|---|---|
| **P0-1** ★ | `_send_result` 用 `job.get('image_path')`，而 job 是 `sqlite3.Row`（无 `.get()`）→ `AttributeError` 被轮询层吞掉 → **图片从来没发出去过**，且该任务每 5 秒重试永不收敛 | **核心功能 100% 失败** | 行对象一律先 `dict()`；轮询异常补 `type().__name__` |
| P0-2 | 无事件去重 → 飞书重放 = 重复出图 + **重复扣额度** | 烧钱 | `feishu_dedup` 表 + **原子** `INSERT OR IGNORE` 判重 |
| P0-3 | 在途任务只在内存 → 重启 9620 后结果**永远回不来** | 丢用户结果 | 落 `feishu_tracks` + 启动恢复 + `notified` 持久化 |
| P0-4 | 超在途上限时**静默不跟踪**（额度照扣、用户收不到图也不知道） | 双重损失 | 提交**前**判上限，超限不提交 + 明确回执；上限 50 → 200 |

**验证**
- 新增回归门 `tests/test_feishu_bot_p0.py`（**21 项**，离线可跑，不需要 GPU / 真发飞书）
- **全量门禁 18 → 19 道，19/19 通过**（新门被 `run_all.py` 自动扫到）
- **变异测试**：还原 P0-1 那行 `dict()` → 门变红并打出「实际发图 0 条」；
  让 `feishu_seen` 恒返回 False → 门变红（2 项）；删掉超限的 `return` → 门变红
- 新表在**真实数据库副本**上验证：建表正常、73 条既有 jobs 不受影响、
  `feishu_seen` 在真实 schema 上原子性正确
- ⚠️ **生效需要重启 9620**（Python 进程 import 后不再读盘；新表也在那时自动创建）

**版本**：v2.9.0 → **v2.9.1**（文档改动必升版本号）

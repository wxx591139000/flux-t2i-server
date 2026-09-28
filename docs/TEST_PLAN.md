# 项目测试计划 — FLUX 文生图服务（通用）

> 版本：v2.0 · 2026-09-04

## 测试范围与策略

本项目主要是**运维/部署类脚本**（无单元测试框架），采用**手动冒烟测试 + 脚本自检**策略，按部署阶段分层验证。v1.1 新增对外 web 服务，测试覆盖 Web 链路 + 队列 + 配额 + 公网隧道。v2.0 新增**多服务器**与 **VPS 看门狗**，测试覆盖多机选取 + 死机自愈 + 重启恢复 + 服务器 `server` 落库 + 看门狗预热。

## v2.0 新增测试范围

| 用例（已实测 ✅） | 步骤 | 预期 |
|---|---|---|
| `find_ready_server` 挑单 | `python -c "...find_ready_server()"` | 任一台可达+带卡+模型就绪返回；全关返回 None |
| 死机自愈 recovery | 服务器恢复可达后健康循环 | waiting 池任务自动重入队（实测 22:40 5 个恢复） |
| 僵尸 screen 根治 | 服务器留 Dead fluxgen 会话后启动 | `screen -wipe` 清僵尸 → 活会话重启 → 不误判"运行中" |
| 重启恢复孤儿 | 有 queued/generating 任务时重启 daemon | `_recover_orphaned_jobs` 重入队，不丢单 |
| `server` 列落库 | 任务完成后查 DB | `jobs.server='flux1'`（实测 ✅） |
| VPS 看门狗预热 | flux 机开机未就绪 → 看门狗 | 自动跑 `flux_server_ready.sh` → `SERVER_READY` 就绪（待部署后实测） |

**已知测试缺口**：flux2 未接入（clone 未通）；VPS 看门狗代码就绪未部署 vps-aliyun 实测；并发只测单 worker（无多 worker 并行）。

## 分层测试

### L1 脚本语法自检（无 GPU，本地可跑）
- `bash -n server/*.sh`：shell 语法检查
- `python -m py_compile server/gen_flux.py local/flux_gen_watchdog.py`：Python 语法
- `python -c "import json; json.load(open('example/prompts.example.json'))"`：JSON 合法

### L2 下载链路（无卡可测）
| 用例 | 步骤 | 预期 |
|---|---|---|
| curl 断点续传 | 手动中断 curl 后重跑 | 从断点续传，不重复 |
| 停滞检测 | 停网观察 | 3 次无增长后重启 curl |
| 下载完成标记 | 全部分片下完 | 生成 `DOWNLOAD_DONE` |
| 分片大小校验 | 对比 HF API LFS 字节数 | 字节数一致，勿猜 |

### L3 一键启动自检（带卡）
| 用例 | 步骤 | 预期 |
|---|---|---|
| 无卡中止 | 无卡下跑 start_gen.sh | 提示切带卡，exit 1 |
| 模型未就绪中止 | 删 DOWNLOAD_DONE 跑 | 提示模型未就绪 |
| 幂等 | 已在跑时再跑 | 跳过，提示已运行 |
| 强制重启 | `--force` | kill 旧 fluxgen 重启 |
| screen 后台 | 断 SSH 后重连 | 任务继续，日志心跳 |

### L4 生成验证（带卡）
| 用例 | 步骤 | 预期 |
|---|---|---|
| 单张生成 | `--start 0 --end 1` | 输出 PNG，尺寸 768×1024 |
| 断点跳过 | 再跑同任务 | 已存在文件跳过 |
| 分组输出 | 多组 notes | `NN_组名/` 子目录 |
| 结果可读 | 打开 PNG | 图像正常非花屏 |

### L5 对外 Web 服务（v1.1，本地可测）
| 用例 | 步骤 | 预期 |
|---|---|---|
| 服务启动 | `python manager/flux_service.py` | 端口9620监听，`/health` 200 |
| 提交页渲染 | 访问 `/` | 提示词输入 + 任务表格 + Token 显示 |
| 提交任务 | `POST /api/submit?token=` | 返回 job_id，入队 |
| 状态轮询 | `GET /api/status?job_id=` | queued→generating→done |
| 生成结果 | 服务器生成 | 图片落 `web_out/<jobid>/`，status=done |
| 下载 | `GET /api/download/<id>?token=` | 200 PNG，校验归属(403) |
| 激活码 | admin 生成 → 用户兑换 | 套餐升级、配额增加 |
| 配额超限 | 用满后提交 | 拒绝，提示月限 |
| 去重 | 同 token 同提示词再提交 | 拒绝，提示重复 |
| 服务器down | 停服务器后提交 | `[SERVER_DOWN]` 重排队，飞书通知 |
| 队列串行 | 连发多任务 | FIFO 依次执行，无并发 |
| 公网隧道 | `flux.zhuanlu.xyz` | 公网提交页 + 下载 200 |

### L6 E2E 回归（v1.1，已实测通过）
- ✅ 真实提交「小孩在岸边散步」→ FLUX 服务器生成 → 拉回 → 完成任务
- ✅ 公网下载 PNG 有效（768×1024）
- ✅ 下载按钮渲染（每个 done 任务带「⬇ 下载」）

### L7 飞书"图图"对话式出图（v1.7，已实测通过）
| 用例 | 步骤 | 预期 |
|---|---|---|
| 图片上传+回传 | `send_image_direct(owner, 测试png)` | 飞书收到图片消息（SEND OK） |
| 正常提示词 | 私聊发"一只橘猫坐在窗台上" | 确认回执（任务号+用量） |
| 在途拦截 | 任务未完成再发 | "请稍候再发" |
| 斜杠命令 | 发 `/help` | 提示语（暂不支持命令） |
| 完成回传 | 模拟 job done | 上传+发送图片+"请查收" |
| 失败回传 | 模拟 job failed | 回错误信息 |
| 服务集成 | 重启 flux_service | 日志 `🤖 飞书图图机器人已启动`，web 不受影响 |

### L8 运维启动脚本（v1.8，已实测通过）
| 用例 | 步骤 | 预期 |
|---|---|---|
| 脚本幂等 | `-Target flux/xhs` 连点两次 | 不产生重复进程（端口各 1 个监听）|
| 分别拉起 | `-Target flux` / `-Target xhs` | FLUX(:9620) / 小红书(:8800) 各自独立启动 |
| 隧道启动 | `-Target tunnel` | cloudflared xhs-tunnel 连上，公网恢复 |
| admin 登录 | 输 `flux-admin-2026` | 进入面板（bug 修复后）|

## 已知测试缺口

- ❌ 无自动化单元测试（脚本为部署工具，未引入 pytest）
- ❌ 无 GPU 环境无法做生成回归
- ⚠️ CPU offload 下生成速度慢，批量 30 张需较长等待，未做性能基准
- ⚠️ 分片大小是硬编码，若 HF 仓库文件变更需手动更新（脚本已注释来源）
- ⚠️ 小红书产线文件队列（`flux_server_manager` 的 run()）修复后未再实测（v1.1 改动复用同一 SSH 函数）
- ⚠️ 公网下载用 urllib/python 有 TLS EOO 怪癖，curl 正常（非服务问题）

---

## 增量（2026-09-23）：本轮验证记录

### A 链全量门禁

- `py -3.11 tests/run_all.py` → **18/18 道门通过，总耗时 48.8s**（无长期红的门）

### 新增/加强的验证

| 验证对象 | 方式 | 结果 |
|---|---|---|
| `ssh_config_aliases()` 读不到配置不崩 | `test_server_registry.py` 第 [6] 组：mock `Path.exists` 抛 `PermissionError` | 45/45 全绿；变异测试拆掉防护 → 3 项变红 |
| 公网是否真的指向 3000 | **Playwright 真浏览器**抓三方 `<title>` 比对 | `公网 == 3000 → True`；截图字节数一致 |
| 商户后台口令是否轮换生效 | 直接打 `/api/admin/users`（带/不带 token）+ Playwright 注入 localStorage 登录 | 新口令 200（进入面板）/ 旧口令 403 / 无口令 403 |
| 隧道 ingress 是否正确 | `cloudflared tunnel ingress validate` + `ingress rule <url>` | validate=OK；三条规则全部匹配正确 |

### 测试缺口（诚实记录）

- **`pause`/`Read-Host` 类交互阻塞无法在管道环境下自动化验证** —— 它只在真实控制台发作。
  对策：不靠测试，改为①禁用数组 splatting（源码级不变量检查）②进度文件/结论文件留痕
- 飞书机器人侧**目前没有任何自动化测试**（现有 `feishu_bot.py` 无对应用例）；
  设计文档 P1 已要求状态机与去重先补回归测试再上功能

## 增量（2026-09-24）：第 20 道门 + T11（v2.10.0）

- `py -3.11 tests/run_all.py` → **20/20 道门通过，总耗时 77.5s**（无长期红的门）

### 新增 / 扩展的门

| 门 | 项数 | 覆盖 |
|---|---|---|
| `tests/test_feishu_bot_cancel.py`（**新增**） | 55 | 命令解析矩阵 / 命令不产生提交 / 列表与权限 / 取消副作用（墓碑 + 剔队列 + 跟踪终态 + 不退款）/ 按状态措辞 / 序号与短码边界 / 跨渠道取消 |
| `tests/test_delete_job.py`（**扩展**） | 34 → **37** | 新增 T11（第 9 条不变量）：已删 waiting 不复活 / 不退款 / 不改状态，含源码级顺序断言 |

**替身策略**：`test_feishu_bot_cancel.py` 用**真实 FluxDB + 真实 FluxQueueScheduler**
（临时库），**只有飞书通知器是替身** —— 避免"替身自证替身"，断言的才是上生产的代码。

**阳性对照**（防假绿的关键）：T11 里有一条"正常 waiting 任务**确实**被重新入队"。
没有它，一旦函数"压根没跑"，其余断言会**静默通过**。

### 变异测试（7/7 全被抓住，还原后全绿）

| 变异 | 门反应 |
|---|---|
| 命令分派被摘掉（`cmd = None`） | **25 项红**（命令全被当提示词提交） |
| 命令判据放宽（`if True: return cancel`） | 8 项红（提示词被吞） |
| 取消时不打墓碑 | 10 项红 |
| 取消时不从调度器剔除 | 4 项红 |
| `jobs_inflight_by_user` 不排除墓碑 | 4 项红 |
| 轮询去掉墓碑检测 | 4 项红 |
| **`_recover_waiting_tasks` 去掉墓碑过滤** | T11c/d/g/h/j 红 → `实际退款 [('sAlive','…'),('sDead','等待服务器超时(250分钟)')]` |

最后一条是最有价值的证据：它证明「已删任务拿到退款」这个漏洞**在修复前真实存在**。

### 夹具教训（已进 PITFALLS）

门的第一版用了 `gen00001` 这类**非十六进制**假 job_id（真实是 `uuid4().hex[:16]`），
被严格判据**正确地**判为"不是命令" → 误判成"实现有 bug"。
**实现是对的，测试数据是错的。** 写"按格式识别"的测试时，夹具必须满足格式约束。

## 增量（2026-09-28）：第 21、22 道门（v2.11.0）

全量 **22/22**（`tests/run_all.py` 扫 `tests/test_*.py` 全跑 —— 防漏跑）。

### 第 21 道 · `tests/test_figu_params.py`（133 项，全绿）

| 分组 | 钉住的东西 |
|---|---|
| `t_command_parsing` | **31 条命令全部命中 / 18 条"像命令但不是"的提示词 0 误吞** ★ |
| `t_params_effective` | `/size /n /model` 真的落到 `jobs` 表的 `width/height/seed/model` 列（不是只回执好看） ★ |
| `t_pending_state_machine` | 待选落库 → 回 1 条号码 → 消费 → 清除；过期不消费 |
| `t_cross_session_isolation` | **三元组隔离**：A 会话的待选，B 会话回复无效 ★ |
| `t_guards` | 张数越界截断 / 认不出的模型名不是命令 |
| `t_multi_image` | **n 张复用同一份英文提示词**（打桩翻译函数 → 断言只翻译 1 次）+ seed 各不相同 ★ |
| `t_model` | 别名 → 完整 model id 透传到 `jobs.model`（**选机靠它**，不同模型在不同机器上） |
| `t_bind_and_quota` | 无效码明确失败不建账户 / 同码再绑并入已有账户 / **两个 open_id 共享同一份用量** |
| `t_cancelled_not_inflight` | **取消后的任务不再计入在途 → 取消完还能再提交**（v2.10.0 真缺陷回归）★ |

### 第 22 道 · `tests/test_figu_group.py`（46 项，全绿，0 跳过）

| 分组 | 钉住的东西 |
|---|---|
| `t_whitelist_parsing` | 逗号/分号/空格混合解析；**默认空集合**（安全默认） |
| `t_whitelist_gate` | 非白名单群 + @ 也**不提交、连回执都不发** ★ |
| `t_mention_judgement` | @图图 → 响应；**只 @别人 → 不响应** ★；未配 `FEISHU_BOT_OPEN_ID` 时降级为"任意 @"并放行 |
| `t_group_end_to_end` | 两道门都过 → 提交；回执**回群**且 @ 发起人；`chat_id/is_group` **落库** ★ |
| `t_p2p_unaffected` | **私聊不需要 @、仍走私聊**（P2 不得改坏 P0/P1） |
| `t_reply_routing` | `_reply` 群/私聊分流；`is_group=1` 但 `chat_id` 空 → 退化私聊（不静默丢） |
| `t_result_routing` | `_send_result` 群 → `send_image_to` / 私聊 → `send_image` |
| `t_dedup_before_group_filter` | **被忽略的群消息也登记去重**（重放不二次处理） |
| `t_session_isolation_group_vs_p2p` | 同一个人在群/私聊设的参数**互不影响**（三元组 key）★ |

### ★ 本轮门的写错一次（值得记）

`t_group_end_to_end` 第一版断言「未 @ 的消息**不产生回执**」时用了
`len(notif.to_chat) == 1` —— **假红**。原因：图图提交路径**固有发 2 条**回执
（即时「正在解析」+ 排队确认，见 `_submit_locked` 的「★ 立即回执」设计）。

> **判据：断言"某动作不产生副作用"时，写成「**不产生新增**」（前后差值 == 0），
> 不要写成「总数 == 某个数」。** 总数型断言会把已有的、无关的副作用一起算进来 →
> 一改实现就假红（而实现是对的）。
>
> 这与上面那条 `gen00001` 夹具教训是**同一类病**：**测试数据/断言与实现契约不符 → 假红**。
> 两条都已进 `docs/PITFALLS.md`。

---

## v2.12.0 新增：第 23 道门 `tests/test_figu_confirm.py`（**95/0**）

覆盖**提交前确认**状态机，12 组：

| # | 组 | 钉住的点 |
|---|---|---|
| 1 | `t_confirm_words` | **全等**判据：`好` 是确认、`好可爱的一只猫` 不是（35 条用例） |
| 2 | `t_default_on` | 线上默认**开**（断言 `fb.CONFIRM_SUBMIT is True`） |
| 3 | `t_flow_happy` | ★★ 发提示词**不提交** → 回「是」才提交；提交后待确认被清 |
| 4 | `t_decline` | 回「否」→ 不建任务，且**额度口径未变** |
| 5 | `t_new_prompt_discards` | 连发两句 → 只留最新；回「是」提交的是**最新**那句 |
| 6 | `t_snapshot_params` | ★★ 库里参数被改，仍按**快照**提交（3 张 / 1280×720） |
| 7 | `t_order_commands_first` | ★★ 顺序：命令不被确认拦；待选不被确认吃；有待确认时命令照常执行 |
| 8 | `t_isolation` | ★★ 别人 / 别的会话回「是」都无效（三元组 key） |
| 9 | `t_expired` | 过期后回「是」→ 不提交（且仍有回执，不静默丢弃） |
| 10 | `t_inflight_guard` | 在途超限 → **不发确认**（不让用户白等一场） |
| 11 | `t_switch_off` | `confirm_enabled=False` → 旧行为（回退保护） |
| 12 | `t_group_route` | 确认请求发到**群**并 @ 发起人 |

### ★ 这道门连带的"既有门修正"（值得记的经验）

默认行为一改，**既有 4 道门立刻红了**（cancel 1 项、p0 7 项）——
失败原因全部是「提示词不再直接进队列」，**不是**被测逻辑坏了。

处理：这 4 道门各自显式 `bot.confirm_enabled = False`（它们分别覆盖命令面 / P0 边界 /
参数 / 群聊），并各自注明「确认流程见 `test_figu_confirm.py`」。
**没有**改动那 4 道门的任何断言 —— 断言是对的，变的只是**前提**。

> 判据：改默认行为时先问「**有多少道门依赖这个默认值**」，再决定是"改门"还是"改断言"。
> 一律改断言会把真缺陷一起改掉（第 12 号陷阱的变体）。

### 变异测试（建议下次补）

本版**没做变异测试**（v2.10.0 做了 7/7）。高价值变异点：
① `FEISHU_TERMINAL_PHASES` 去掉 `'cancelled'` → 21 道门应红；
② `_mentioned_bot` 改成"文本里找 `@`" → 22 道门应红；
③ `session_key` 去掉 `chat_id`（退回二元组）→ 21/22 道门都应红；
④ `parse_confirm` 改成**子串匹配** → 23 道门应红（吞提示词）；
⑤ `_consume_confirm` 未知回复改 `return True` → 23 道门应红（新提示词被吃）；
⑥ `_start_confirm` 去掉 `feishu_pending_clear` → 23 道门的顺序组应红。

---

## v2.13.0 — 许可闸放行分支（2026-09-28）

**门文件数不变（仍 23）**，而是**扩了两道既有门** —— 契约改了，门必须跟着改。

| 门 | 变化 |
|---|---|
| `tests/test_commercial_gate.py` | 13 → **17 项** |
| `tests/test_model_capabilities.py` | `t_license` 增 1 条 |

**新增的 4 项**

1. `test_source_has_expose_branch` —— 放行分支必须真在源码里（断言 `get('expose_to_customers')isTrue`）；
2. `test_exposed_machine_is_selectable` —— 放行必须对**自动选机**生效（理由见下）；
3. `test_exposed_machine_must_have_note` —— 放行必须配 `expose_note`（**允许放开，不允许悄悄放开**）；
4. `test_mutation_dropping_expose_turns_red` —— 删掉放行分支 → 第 2 项必须变红。

### ★ 为什么第 2 项必须钉住「自动选机」而不是「need_model 路径」

`/api/models`（B 链下拉框的数据源）走的是 `find_available_server()`，**不带 `need_model`**。
如果放行只在 `need_model` 路径生效，那么 flux7 单独在线时 `/api/models` 会返回**空清单** →
客户连"选"的机会都没有 → 需求落空。

> 判据：门要钉**真实的调用路径**，不是"看起来等价"的路径。
> 这两条路径在源码里只差一个参数，行为却完全不同。

### ★ 既有的两条断言为什么**仍然必须绿**

`commercial_ok is False`（声明侧）**不因放行而改** —— 它记录的是**许可事实**，
业主放行走的是新增的 `expose_to_customers`。

所以「Qwen 必须标 `commercial_ok=False`」这条断言**在放行之后依然要绿**；
若有人为了"让它能用"去把 `commercial_ok` 改成 `true`，这道门会红 —— 这正是要拦的
（见 `PITFALLS.md`「为了让功能生效去改记录事实的字段」）。

### 本版没做的变异

上表第 4 项（放行分支）做了；其余建议变异点见上一节，本版仍未补。

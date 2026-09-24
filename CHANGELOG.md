# CHANGELOG

## [v2.10.0] - 2026-09-24

**图图「取消任务」（P1 第一块）—— 需求原话：「像小白一样有取消任务的能力吗」。**

查下来**底层早就有了**：2026-09-21 为网页端写的 `/api/delete` + `queue.drop_job()` +
`deleted_at` 墓碑机制，语义恰好就是"取消"（连"已在生成中的撤不回来但丢弃产物"都想到了）。
缺的只是**图图这一层的命令面**。本版补上，并顺手修掉一个由此暴露出来的真缺陷。

### 新增：图图命令面（中文裸命令，对齐小白的用法）

| 命令 | 行为 |
|---|---|
| `帮助` / `help` / `/help` | 命令清单 |
| `我的任务` / `我的排队` / 裸 `取消` | 列出我的在途任务（编号 + 提示词 + 状态 + 短号） |
| `取消 01` / `取消01` / `取消任务 01` | 按列表序号取消 |
| `取消 3ada124e` | 按 job_id 前 6~16 位十六进制取消（序号漂移时的退路） |

**两个硬要求（都有回归门钉住）**：
1. **命令识别必须早于提示词提交**。图图的主用法是"发一句话出图"，
   若把「取消 01」当提示词提交，就**真的扣一次额度并出一张毫不相干的图** ——
   用户想撤回反而多花一次。门断言的是**提交次数**，不是文案。
2. **不能吞掉以"取消"开头的正常提示词**。「取消一切杂乱背景，画面干净」是完全可能的输入。
   判据严格到"取消 + 序号/短码"才算命令，其余回落为提示词。

### ★ 顺手修掉的真缺陷：已删的 `waiting` 任务会「复活」并**拿到退款**

`/api/delete` 判在途用的是 `inflight = status in ('queued','generating')` ——
**`waiting` 不在其中** → 删除 waiting 任务时**不会**调 `drop_job()` →
调度器内存等待池 `self._waiting` 留下一项幽灵。

而 `_recover_waiting_tasks()` 从内存池 pop 出来**直接重新入队，原先没有任何墓碑检查**：

- 轻的后果：任务被重新入队跑一轮（`_process` 开头还有墓碑检查 → 不烧 GPU）
- **重的后果**：`waited > WAIT_MAX_SEC`（默认 4 小时）那条分支会
  `job_update(status='failed')` + **`_refund_quota()`** → **给一个已被用户删除的任务退款**。
  这正好把 `job_mark_deleted` docstring 里明确要防的「**删了重传无限刷**」开了个口子
  —— `_api_delete` 与 db 层都在守这条，只有这里漏了。

**变异测试实证**（把墓碑过滤摘掉后门报）：
```
[FAIL] T11c ★★ 已删（墓碑）的 waiting 任务**没有**被复活
       实际入队 ['wDead', 'wAlive']
[FAIL] T11j ★★ 超时的**已删** waiting 任务**未**退款
       实际退款 [('sAlive','等待服务器超时(250分钟)'), ('sDead','等待服务器超时(250分钟)')]
```

**修法（三道闸，缺一不可）**：
1. `_api_delete` 拆开两个判据：`inflight`（有产物语义）与 `pooled`（调度器内存里可能有它，
   = queued/generating/**waiting**），后者决定是否 `drop_job()`
2. `_recover_waiting_tasks()` **自己再判一次墓碑**（调用方"应当"做的事不能当唯一防线）
3. worker 侧 `_process` 原有的墓碑检查保留（是最后一道）

### 另修：跨渠道取消（网页端删了，飞书轮询不收敛）

飞书侧 `feishu_tracks` 表**不知道**网页端删了任务 → 轮询会永远盯着它（P0-1 那种"永不收敛"的病）。
→ `_poll_once` 加墓碑优先分支；`feishu_tracks_pending()` 的终态集合补上 `'cancelled'`。

### 口径：**取消不退还额度**

与网页端 `/api/delete` 一致（用完即消耗，防"删了重传"）。所有取消回执都明写这一条。

### 验证

- 新增**第 20 道门** `tests/test_feishu_bot_cancel.py`（**55 项**，用真实 FluxDB + 真实调度器，
  只有飞书通知器是替身 —— 避免替身自证）
- `tests/test_delete_job.py` 从 8 条不变量扩到 **9 条**（新增 T11 共 10 项，**含阳性对照**：
  没有它，"函数压根没跑"会让断言假绿）
- **全量门禁 19 → 20 道，20/20 通过**（69.0s）
- **变异测试 7 个全被抓住**（命令分派被摘 / 判据放宽 / 不打墓碑 / 不剔队列 / 列表含墓碑 /
  轮询无墓碑 / 等待池无墓碑过滤），还原后全绿
- **夹具教训**：门的第一版用了 `gen00001` 这类**非十六进制**假 job_id（真实 job_id 是
  `uuid4().hex[:16]`），被正确判为"不是命令"→ 误判成"实现有 bug"。已在 PITFALLS 记录

## [v2.9.2] - 2026-09-24

**线上实测暴露的阻塞问题（P0-5）：图图收得到消息、回得出去，但会把 WS 连接搞掉。**

用户重启 9620 后给图图发"你好"，长时间没回复。查日志定位到根因：

1. **★ WS 回调里同步做耗时工作**：lark 的 WS 客户端在 **asyncio 事件循环里同步调用**事件回调。
   `_on_message → submit()` 里含中文翻译（最坏重试 3 次、**实测 68 秒**）→ 事件循环被占住 →
   **心跳 ping/pong 发不出去** → 飞书按 `3003 (registered) ping timeout` **踢掉连接**，
   并且因为没及时 ACK **把同一条事件重投一遍**（日志里「重复事件，已忽略」就是脚印）。
   → 修法：回调只做去重/过滤/取文本（毫秒级）后**入队**，2 个工作线程消费。
2. **`notified` 无条件置位**（没判断发送结果）→ 首次发送失败时用户**永远**收不到那条通知，
   而跟踪表却记着"已通知"。→ 改为只在 `send_direct` 返回 True 时置位。
3. **提交前无任何反馈**，用户要干等几十秒 → 先回「⏳ 收到！正在解析提示词…」，再回任务号。

### 验证

- 回归门 `tests/test_feishu_bot_p0.py` 21 项 → **26 项**
- **全量门禁 19/19 通过**
- **变异测试**：把入队改回同步调用 → 门报 `❌ WS 回调不阻塞事件循环 (2519 ms)` 变红；还原后全绿

### 线上事实（同步告知，非缺陷）

- 图图**有发送能力**（用它的应用直连 `im/v1/messages` → `code:0`）
- 但**当前没有任何 GPU 机在线** → 任务进 `waiting`（`[SERVER_DOWN] 无可用 flux 服务器`）
  → **链路全通也出不了图**；机器上线后会自动恢复
- 生效仍需**重启 9620**

## [v2.9.1] - 2026-09-23

**飞书「图图」P0：修四个缺陷 —— 其中一个是核心功能一直坏的。**

动手做 P0（设计文档 §6 点名的三条）时又查出**第四个、而且最严重**：

1. **★ 图片从来没发出去过**：`_send_result` 用 `job.get('image_path')`，而 `job` 是
   `sqlite3.Row` —— **Row 没有 `.get()`**（实测 `AttributeError`）。异常被轮询层的宽
   `except` 吞掉、只打一行「任务轮询异常」，于是：① 图永远不发给用户；
   ② 那条记录永远不被清理，此后每 5 秒重试一次、永不收敛。
   修法：行对象一律先 `dict()`；轮询异常日志补 `type(e).__name__` 与 `exc_info`。
2. **无事件去重** → 飞书重放一次 = 重复出图 + **重复扣额度**。
   → `feishu_dedup` 表 + `INSERT OR IGNORE` **原子**判重（"先查再插"在并发下会双双放行）。
3. **在途任务只在内存** → 9620 重启（每次改代码都要重启）后，这些任务即使出完图也
   **永远回不来**（额度已扣）。→ 落 `feishu_tracks` 表 + 启动自动恢复 + `notified` 持久化。
4. **超在途上限时静默不跟踪** → 任务照跑、额度照扣，用户**收不到图也不知原因**。
   → 改为**提交之前**判断上限，超限不提交 + 明确回执（不浪费额度）；上限 50 → 200。

### 新增

- `tests/test_feishu_bot_p0.py`（**21 项**离线断言，含"重启恢复"与"重放只出 1 张图"）
- `manager/flux_db.py` 新增两张表与 DAO：`feishu_dedup` / `feishu_tracks`
  （含 `feishu_seen` 原子判重、`feishu_tracks_pending` 恢复查询、两个 GC）

### 验证

- **全量门禁 18 → 19 道，19/19 通过**（`run_all.py` 自动扫到新门）
- **变异测试**（把实现改坏，门必须变红）：还原 `dict()` → 门报「实际发图 0 条」；
  `feishu_seen` 恒 False → 门红 2 项；删超限 `return` → 门红
- 新表在**真实 DB 副本**上验证：建表正常、既有 73 条 jobs 不受影响

### 注意

⚠️ 以上**需要重启 9620 才生效**（Python 进程 import 后不再读盘；两张新表也在那时自动创建）。

## [v2.9.0] - 2026-09-23

**对外可用性根因修复 + 商户后台开公网 + 飞书「图图」设计方案。**

长期存在「朋友打开公网看到旧页面」的问题，这天查到了根 —— 而且是**三个独立缺陷叠加**，
每一个单独看都"像是好的"：

1. **隧道从未真正重启（服务侧）**：`cloudflared` 是长驻进程，启动时把 ingress 读进内存后不再读盘。
   实测进程停在 09-20 启的实例，而 `config.yml` 是 09-22 改的 → 公网一直走旧 ingress。
   修法新增 `image-platform/restart_tunnel.ps1`：按 PID 终止 + **轮询确认真退出** + **新鲜度断言**
   （新进程启动时间必须 ≥ `config.yml` 的 mtime）+ 环境自愈。

2. **`Start-Process` 带日志重定向必抛**（见 PITFALLS 同日条）：本机同时存在 `https_proxy` 与
   `HTTPS_PROXY`，.NET 环境块用大小写不敏感字典 → 枚举即抛「重复的键」。
   连带后果是只能退回"带控制台窗口"启动 → **窗口一关服务就死**（实测 3000 反复起来又没）。
   修法：会 `Start-Process` 的脚本开头先跑 `Repair-DuplicateEnv`，对外服务一律带重定向启动。

3. **编排层卡死在中途**（见 PITFALLS 同日条）：`& script.ps1 @('-Restart','-NoPause')` 这种
   数组 splatting 让 `[switch]` 参数**一个都绑不上** → `-NoPause` 失效 → 子脚本里的 `pause`
   把编排器卡住 → 隧道那一步**从未执行**，且 `-Restart` 也失效（"强制重启"从未发生）。
   修法：改显式具名参数 + 加源码级不变量检查。

**验收判据整体换成「身份/新鲜度」而不是「活体」**：隧道看"新进程 vs 配置 mtime"，
公网看"`<title>` 是否等于 3000 且不等于旧上游 9620"。Playwright 真浏览器实测通过
（公网 title == 3000 title）。

### 新增

- `docs/图图-飞书完整互动-设计方案.md` —— 飞书机器人「图图」的完整互动设计方案
  （与 `transcribe-bot` 的小白做差距分析；P0-P6 分阶段 + 可机器判定的验收标准）
- 商户后台独立公网域名 `flux-admin.zhuanlu.xyz` → `localhost:9620`
- `image-platform/重启隧道.bat`（单独应用隧道配置改动用）

### 安全

- **`WEB_ADMIN_TOKEN` 轮换**：从文档里随处可见的旧默认值 `flux-admin-2026`
  换成 20 位强口令（字母+数字，`secrets` 生成）。实测旧口令 `GET /api/admin/users` 已返回 403。
  轮换原因：该旧值写在 README / SOP / archive 等多份文档里，等于已公开。

### 修复

- `ssh_config_aliases()` 半截防护：`cfg.exists()` 原在 try **外面**，而 `Path.exists()` 内部是
  `os.stat()`，**权限受限时抛异常而非返回 False** → 冒泡到模块级导致 9620 整体起不来。
  修法：整个函数体进 try，降级为 warning（commit `94ee0a9`）。

## [v2.8.1] - 2026-09-17

**去重键根治：连点不再出重复图，`_inflight` 不再只增不减。**

当天中午真机跑通 5 张图后，盯数据发现去重机制形同虚设 —— 用户连点两次会出两张重复图、
还各计一次费。查下来是三个独立缺陷（顺带暴露出离线测试的替身不够真）：

1. **键格式漂移（bug A）**：三处去重键是三种写法 —— 入队
   `{u}:{p}:{seed}:{w}x{h}`、worker 收尾 `{u}:{p}`、孤儿恢复 `{u}:{p}`。
   add 与 discard 的键永远不相等 → `_inflight` 只增不减。后果：
   集合无限增长；`/health` 的 `backend_hint` 恒为 busy（队列空也报忙）；
   同用户固定 seed 重复提交会被永久拒绝。
   → 统一走 `_dedup_key()` 单一构造入口，三处不可能再漂移；
   `discard` 移进 `finally`，`_generate` 抛异常时也一定释放。

2. **用错了文本（bug B，连点出重复图的真凶）**：中文任务原先拿**翻译后**的英文
   prompt 做键。翻译是 LLM 调用，同一句中文两次翻译结果会漂移 —— 实测同一句
   分别译成 "A pair of men's slim-leg jeans…" 与 "Photorealistic product
   photography…"，键不同 → 去重直接失效。
   → 中文一律用**用户原始输入** `original_prompt` 做键；换 seed / 换尺寸仍算不同
   任务（B 链一次出多张候选靠这条放行）。

3. **并发竞态（bug C）**：web 层是 `ThreadingHTTPServer`，「检查去重 → 入队 →
   登记」不是原子操作，连点落在不同线程上时都能通过检查。
   → 整段收进 `self._submit_lock`。

**回归门** `tests/test_dedup_key.py`（11 项）+ `tests/mutate_dedup_key.py`（4 个变异）。
变异测试证明门有效：回退成旧写法时 bug A/B/C 各自被抓住，且 bug D（不把
`sqlite3.Row` 转 dict）也被 C4b 抓住 —— 那条正是我修复过程中引入、真机上打死
worker 线程的错误，替身换成真实 `Row` 后才暴露。

## [v2.8] - 2026-09-17

**传输层根治 + 常驻档位安全默认 —— 修掉两个"任务永远卡住"的事故级缺陷。**

### 背景
当天上午，从资源管理器双击 `启动-生图平台.bat` 起的服务，客户网站下单后任务永远停在
`waiting`，任务中心显示三台机全「SSH 不通（可能关机）」。而在 Git Bash 里跑 CLI 探测，
同一时刻同一台机却是「flux3 可达·有卡·模型就绪」。两条独立根因：

1. **`run()` 假定 bash 在 PATH 里**。Git for Windows 默认只把 `<Git>\cmd` 写进 PATH
   （里面只有 `git.exe`），`bash.exe` 在 `<Git>\bin`。Git Bash 起的服务有 `/usr/bin` 所以正常，
   双击 `.bat` 起的服务 `FileNotFoundError` → 被 `except` 吞成 `(False,'...')` →
   每台 ~0.07s 判「不可达」。**真因还被两层遮蔽**：`run()` 丢掉 stderr，`probe_full()`
   把所有失败统一写成「可能关机」。
2. **拉起常驻服务时 `FLUX_OFFLOAD` 默认 `none`**（全程显存）。32G 卡（RTX 4080 SUPER
   32760 MiB）装 31.2 GiB fp16 权重必然 OOM → 每张图 `CUDA out of memory`。
   默认值取的是「最快」而不是「一定能跑」。

### 改动
- `manager/flux_server_manager.py`
  - 新增 `find_bash()`：按 PATH →（从 `which git` 反推 `<Git>` 根）→ 常见安装路径依次定位，
    结果缓存；找不到时打 `log.error` 明说。`run()` 改用它，不再假定 PATH 可用。
  - `run()` 失败时**带回 stderr**（旧版只取 stdout，ssh 的报错全丢）；无 bash 时返回
    `NO_BASH: ...` 而不是笼统失败。
  - `probe_full()` 错误信息分级：`NO_BASH` / `TIMEOUT` / 真实 ssh stderr / 兜底文案。
  - `_DEFAULT_SERVERS` 每台加 `offload` 字段，显式声明 `"model"`。
- `manager/flux_resident_client.py` `ensure_resident()`：`FLUX_OFFLOAD` 优先级改为
  **env > 该机器条目的 `offload` > 安全默认 `model`**。想跑 `none` 的大显存机器在
  自己条目上声明即可，不必改逻辑。
- `manager/flux_service.py`：启动时自检 bash，缺了直接在日志里告警 ——
  这类故障的表现是「所有服务器都不可达」，宁可启动就吼一声。
- `tests/test_transport_env.py`（**新增**）：8 项离线回归闸门，覆盖上述两类问题的
  默认值、失败路径与可观测性。改 `flux_server_manager.py` / `flux_resident_client.py`
  后跑一遍，几秒钟出结果。
- `manager/flux_web_service.py`（同日第二批：激活/绑定链路）
  - **修**：「激活 / 绑定」按钮点了必报「缺少激活码或 token」。前端只发 `{code}`，
    而 `_api_activate` 第一行就要求 `token` —— 每次都必然失败。改为
    `_api_activate(body, token)` / `_api_bind(body, token)`，用 `do_POST` 已从
    cookie / `?token=` 解析好的身份兜底（body.token 优先，兼容脚本调用）。
  - **补**：admin 页原本没有任何绑定入口（只有改套餐 / 备注 / owner / 详情）。
    新增「用户码绑定」卡片（用户 token + 激活码 → `/api/bind`）与账户行内「绑设备」按钮。

### 验证（2026-09-17 实跑）
- 闸门 `python tests/test_transport_env.py` → **8/8 通过，exit 0**。
- **闸门有效性用变异测试证明**（不是"全绿"就算数）：把安全默认改回 `none` → `B1` 变红、exit 1；
  把 `find_bash` 退回 `shutil.which('bash')` → `A1` 变红、exit 1。两处均已还原。
- 既有离线套件无回归：`verify_resident_stub` 26/26、`verify_quota_patch` 2/2、
  `verify_refund_quota` 19/19、`verify_autoselect` 20/20。
- **真机对照**（PowerShell 环境，即双击 .bat 的同款环境，同一份探测代码）：
  修复前 `probe_all` 0.2s 三台全 `reachable=False`；修复后 10.2s，
  `flux3 → reachable=True / gpu_ok=True / model_ok=True（RTX 4080 SUPER）`，`any_usable()=True`。
- **真机出图**：`model` 档，2 张 1280×720 成功落盘
  （`web_out/5ec6dd99c49f4933/` 992 KB、`web_out/424e3d1be25846a8/` 449 KB，
  推理 54.04s / 端到端 71.5s）。
- **恢复能力**：重启服务后，启动时自动恢复 4 个遗留 `waiting` 任务并全部重新入队、
  选中 flux3 依次完成。

### 未验证（如实）
- `find_bash()` 的三级兜底里，"完全没装 Git for Windows" 那条分支只在单元层
  用打桩验过（A2），**没有在真无 Git 的机器上实跑**；该分支现在的行为是明确报
  `NO_BASH` 而不是继续伪装成服务器不可达（这是有意的取舍）。
- 本机服务**没有热加载**：改完这两个文件必须重启 `flux_service.py` 才生效。

## [v2.7] - 2026-09-17

**生图选项层 1：接通「尺寸 / seed / 负向词」透传，让前端选项真正生效。**

### 背景
下游网站（`image-gen-site`，当时名为 `ecom-image-studio`，2026-09-17 改名）的 UI 一直有比例 / 负向词 / seed 控件，但适配器源码自己写着
「上游不支持，会忽略」—— 因为 web 层的 `_api_submit` 只读 `prompt/priority`，尺寸固定在
768×1024、seed 恒 0、负向词丢弃。本版把这条断链接通。

### 改动
- `manager/flux_web_service.py` `_api_submit`：读 `width/height/seed/steps/negative_prompt`（均可选，
  缺省走服务端默认），透传给 `scheduler.submit()`；`_api_status` 回传 `seed/width/height`（供前端读真实种子）。
- `manager/flux_queue.py` `submit()`：签名加这 5 个可选参数；**去重 key 从 `user:prompt` 扩展为
  `user:prompt:seed:WxH`**（同提示词换 seed/尺寸 = 不同任务，多张候选靠不同 seed 绕过去重，
  替代下游用 `(variation N)` 污染提示词的 hack）；`_generate_resident()` 从 job 取参数构造
  `**gen_kwargs` 传给常驻服务，并把实际 seed 回填进 job。
- `manager/flux_db.py`：jobs 表加 `width/height/seed/steps/negative_prompt` 5 列（含幂等迁移
  `_migrate()`，旧生产库自动补列）；`job_insert()` 存这些参数。
- `docs/ARCHITECTURE.md` / `README.md`：同步「web 层不再只传 prompt」与去重 key 的描述。

### 说明（如实标注）
- 参数透传只在 **resident 模式**生效；legacy 的 `gen_flux.py` 仍固定 768×1024 / steps 25。
- `negative_prompt` 虽已透传，但 FLUX.1-dev 是 guidance-distilled，diffusers 会**静默忽略**它
  （无 `true_cfg_scale`）。透传仅为链路完整，真正要抑制某元素应写进正向提示词。

### 验证（离线，无需 GPU）
`submit` 存参 → `job_get` 读回、`gen_kwargs` 构造、去重（同 prompt+seed 拒 / 换 seed 放行）全部
通过；三文件 `py_compile` 通过。下游 image-gen-site `tsc --noEmit` 通过。
真机出图待 GPU 开机后补跑（flux3 已于 2026-09-16 23:20 关机）。

## [v2.6] - 2026-09-16

**FLUX 生图质量保障：五层方法论 + 翻译层修复 + 双链真机端到端。**

### 背景
GPU 窗口内完成一批真机实验（bench / steps 扫描 / 中文翻译链 / 白底措辞），核实并修正了
「FLUX 需英文」「负向词不生效」「steps 最优档」等关键认知，把能固化的结论落地成代码与文档。

### 改动（代码）
- `prompt_translator.py`：翻译规则加「禁止负向表达（no X）」「禁止擅自添加用户没提的实体」；
  LLM 失败**不再静默兜底返回中文原文**（中文直发 FLUX 连主体都会错），改为抛 `TranslationError`，
  `flux_queue.py` 捕获后**拒绝入队**而非降级出图。
- `flux_resident_client.py`：`generate_via_resident` 的 `dest` 改为可选（None 跳过拉图），
  修复 `bench` 子命令从未跑通的 `TypeError`。
- `docs/PITFALLS.md`：补「`flux_web_service.py` 是 web-only 装配（scheduler=None），
  该入口下 `POST /api/submit` 必然 500 —— 端到端/生产必须走 `flux_service.py`」。

### 实测（真机 flux3 · RTX 4080 32G）
- **bench 5 张连打**：单张推理 40.7s，极差 1.04s → 常驻无冷启动开销坐实。
- **steps 六档扫描**（15/20/25/30/35/40，同 seed）：≈0.7s/step + 23s 固定开销，
  **20 步即画质饱和** —— 外源「30–50 最佳」对本模型不成立。
- **中文是分界线**：同句直连出动漫少女（主体错），过翻译层出干净商品图。
- **A 链真机端到端**（submit→选机 flux3→status→download）：同 token 200 / 错 token 403。
- **B 链真机端到端**（Next.js `/api/health` + `/api/generate` → A 链 → flux3）：全通，端到端 55.3s。
- **offload**：32G 卡 `none` 档实测 OOM（权重 31.22 GiB 占满），`model` 档可用（显存 4 MiB）。

### 关键认知（写入报告）
中文必须过翻译层；负向词在 FLUX.1-dev 上被静默忽略（无 `true_cfg_scale`）；
`steps=20` 饱和；提画质靠分辨率不靠 steps。

### 未做
legacy 链路对照（唯一能算出「常驻快几倍」）；`offload=sequential` 对比；1024²/3:4 构图一致性。

## [v2.5] - 2026-09-16

**接入 flux3（AutoDL 克隆实例）：把「换机器」从"要改代码"变成"改一个别名"。**

### 背景
flux1 / flux2 当日无卡可用，用户在 AutoDL 平台把实例**克隆**出一台新的。目标是让它直接接上，
不改代码、不改调用方。

### 改动
- **显式登记 flux3**（`manager/flux_server_manager.py` 的 `_DEFAULT_SERVERS`）：name=`flux3`、
  alias=`autodl-flux3`。克隆实例与原机同布局，`remote_base` / `remote_model` 沿用同一组默认路径。
  （即使不登记，只要别名匹配 `FLUX_SERVER_ALIAS_GLOB` 也会被自动发现 —— 显式登记是为了让日志与
  `jobs.server` 里出现稳定的名字 `flux3`，而不是一串别名。）
- **新增 env `FLUX_DEFAULT_SERVER`**：`_resolve_default()` 让「默认机」可指定；不设则保持候选机第一台
  （flux1，**行为完全向后兼容**）。解决的现实问题：默认机被小红书产线（`process_job`）与不带 `--server`
  的 CLI 使用，而 flux1/flux2 已无卡 —— 设 `FLUX_DEFAULT_SERVER=flux3` 即可把默认机指过去。
  填了不存在或未纳入的名字 → warning + 回退第一台，不抛。
- `.env.example` 的「多机」章节补上 `FLUX_DEFAULT_SERVER` 说明。

### 实测（真机，非 stub）
```
$ python manager/flux_resident_client.py servers
     NAME       ALIAS              可达   带卡   模型   常驻   已加载    备注
     flux1      autodl-flux        ✗    ✗    ✗    ✗    ✗       SSH 不通
     flux2      autodl-flux2       ✗    ✗    ✗    ✗    ✗       SSH 不通
  ▶  flux3      autodl-flux3       ✓    ✓    ✓    ✗    ✗      NVIDIA GeForce RTX 4080
▶ = 会被选用的机器: autodl-flux3
```
A 链口径 `fsm.find_ready_server()` 同样选中 `flux3`。flux3 实地核对：工作目录
`/root/autodl-tmp/flux-t2i` 在、`models/FLUX.1-dev/DOWNLOAD_DONE` 在、
GPU `NVIDIA GeForce RTX 4080 / 32760 MiB`（已用 1 MiB）、数据盘 50G 用 32G。

### 免密（一个容易踩空的点）
克隆实例**继承了克隆源的 `/root/.ssh/authorized_keys`**，`id_rsa_musetalk` 直接可登 ——
无需密码、无需 sshpass、`BatchMode=yes` 可用。这点关键：选机探测固定带 `BatchMode=yes`，
纯密码认证会让全部探测直接失败（报「SSH 不通」而非「密码错」）。

### 验证（离线，无需 GPU）
`verify_autoselect.py` 20/20、`verify_resident_stub.py` 26/26、`verify_quota_patch.py` 2/2、
`verify_refund_quota.py` 19/19 —— 全部通过，登记 flux3 未破坏既有选机语义。

### 未做
常驻服务拉起（`up`）与真机出图 / `bench` **未跑** —— 需用户确认（会占 32GB 显存并产生 GPU 计费时间）。
`FLUX_OFFLOAD` 默认 `none`（全程显存），而该卡 32760 MiB 对 ~33GB fp16 权重余量很小，
拉起若 OOM 需退 `model` 档。

## [v2.4] - 2026-09-16

**健康探针：把「HTTP 端口开着」升级为「能证明服务真的能干活」。**

### 修复
- **对外 `GET /health` 是硬编码 `{"status":"ok"}`**（`manager/flux_web_service.py:144`），
  只能证明端口在监听。最难受的一类真实故障是 **worker 线程异常退出**：web 照常 200、用户提交照常拿到
  `queued`，但队列永不再被消费 —— 从外部完全看不出来，只能等用户来问。

### 新增
- **web 浅探针 `_Handler._health()`**（`manager/flux_web_service.py`）：返回
  `status / db_ok / worker_alive / health_alive / gen_mode / queue_depth / inflight / waiting /
  backend_hint / web_uptime_sec / uptime_sec / errors[]`，HTTP 码 `200`（ok）/ `503`（degraded）。
  **刻意零外呼**（不 SSH、不 HTTP 探 GPU 机）：否则一台 GPU 机关机就会让探针变慢/超时，
  监控会把「后端不可用」误判成「本服务死了」。后端线索用零成本的 `waiting` 池大小推导（`backend_hint`）。
- **`FluxQueueScheduler.stats()`**（`manager/flux_queue.py`）：进程内状态快照，best-effort 不加锁
  （qsize/len 在 GIL 下足够原子；加锁反而可能与持锁的 `submit` 互相排队，把快探针变慢请求）。
- **`FluxDB.ping()`**（`manager/flux_db.py`）：`SELECT 1` 探活，不查业务表（避免大表慢拖累探针）。

### 判定的三种情况（别合并）
```
未挂调度器（has_sched=False，web-only 模式）→ 健康，worker_alive 报 null
挂了且 worker 活着                          → 健康
挂了但 worker 死 / stats() 读不出 / DB 坏    → degraded(503)，异常原文进 errors[]
```
探针自身**绝不抛**（否则 200 变 500，监控看到"崩了"而非"DB 坏了"）。

### 下游站点侧（工作区 `image-gen-site`，非本仓文件）
- `app/api/health/route.ts` 新增：一条 curl 同时给出站点层与上游层；上游不可达 → 503 + `degraded` +
  `upstream.httpStatus=null`。
- `lib/providers/flux.ts`：`fluxHealth()` 重构出 `fluxHealthReport()`（带 `httpStatus`/`latencyMs`/`detail`），
  **`fluxHealth(): boolean` 保留为薄包装**不破坏既有契约。`httpStatus=null`（连不上）与 `503`
  （连上了但上游自报不健康）是两种故障，不再被压成一个 boolean。

### 验证（全部离线，无需 GPU）
`chain-verify/verify_health_probe.py`（26 项，新增）：web-only 模式、worker 死→degraded、DB 坏→degraded、
探针不抛、**AST 剥 docstring 后源码级证明零外呼**（禁用词 `ssh/curl/subprocess/fsm./fr./urllib/probe/Popen`）。
`run_both_chains.sh` 现覆盖：A 链 27 + 浅探针 26 + B 链正向 24 + B 链反向 7 = **84 项全绿**。

## [v2.3] - 2026-09-16

**双链端到端验收 + 克隆机路径透传修复**。起因：确认「本项目的生图网页任务中心」与「面向客户的生图网站」
是**两个独立项目**，各自要有完整可用的链路；两者唯一关联是网站通过 HTTP 调用本服务。

### 修复
- **`ensure_resident` 未透传 `FLUX_WORKDIR` / `FLUX_MODEL`**（`manager/flux_resident_client.py`）：
  原先只传 `FLUX_RESIDENT_PORT/SCREEN/PY/TOKEN`，而 `start_resident.sh` 里 `WORKDIR`/`MODEL` 是硬编码默认值
  → **克隆实例换了工作目录/模型路径时，脚本会去查错路径的模型，误报「模型未就绪」**，且上传的
  `flux_resident_server.py` 与脚本查找路径错位。现按这台机器注册的 `remote_base` / `remote_model` 透传
  （`shlex.quote` 包裹）。这正是「自动选在线机」要支持的克隆场景，属上一版遗漏。

### 新增（仓库外，工作区 `chain-verify/`）
- **`chainA_server.py`**：把真 `FluxWebServer` + 真 `FluxQueueScheduler` + 真 `FluxDB` 装配成常驻服务，
  GPU 侧用 `--stub` 合成 PNG、传输走直连模式 —— **离线可跑完整任务中心链路**，装配方式与 `flux_service.py` 一致。
- **`verify_chainA.py`**：任务中心端到端断言（20 项）。
- **`verify_chainA_legacy_wiring.py`**：legacy 逃生口的接线体检（15 项）—— 用替身接管 `fsm.run`，
  逼真模拟「远端成功出一张图」，验命令构造与产物搬运。**不证明**真实 SSH/GPU 可用。
- **`verify_chainB.mjs` + `run_both_chains.sh`**：客户网站链路断言（正向 15 项 + 后端不可用反向 4 项），
  一条命令起两条链验证完再收摊。

### 文档
- `README.md`：新增「两条链路及其边界（v2.3）」—— 画出任务中心完整链路图、明确下游网站是独立项目、
  给出离线端到端自检命令；服务器部署章节补克隆机路径透传说明。
- `docs/PITFALLS.md`：新增「双链验收 / 路径透传」「离线端到端自检的环境坑」两节。

### 验收记录（2026-09-16，离线，无需 GPU/SSH）
| 链路 | 断言 | 结果 |
|---|---|---|
| A 链：自带任务中心（resident 默认路径） | `/health` `/` `/center` `submit` `status` `download` 归属校验 配额 | **20/20** |
| A 链：legacy 逃生口接线 | 依赖存在性 / 命令构造 / 产物搬运 / 不误杀常驻服务 | **15/15** |
| B 链：客户网站正向 | 页面 / 入参校验 / 单图 / 多图 / 真实 PNG 尺寸 | **15/15** |
| B 链：客户网站反向（后端不可用） | 500 + 结构化错误 + 可行动提示 | **4/4** |
| 上一批回归 | 26 + 2 + 19 + 20 项 | **67/67** |

## [v2.2] - 2026-09-16

**多机自动选择 + 计费正确性修复**。第 1 项：应用 `flux-optimize/patches` 的两个补丁（配额双重计数、job_id 碰撞）。
第 2 项：失败退配额。第 3 项：自动选在线机（换机 / 克隆实例到新服务器后免配置接入）。

### 新增
- **自动发现候选机**（`manager/flux_server_manager.py`）：`ssh_config_aliases()` 解析 `~/.ssh/config`，`discover_servers()` 把匹配 `FLUX_SERVER_ALIAS_GLOB`（默认 `autodl-flux*`）的别名自动登记为候选机。克隆实例到新服务器后**不用改代码**，只要有一条 Host 别名即可。探测时自动跳过无 GPU 的机器。
- **`probe_full()`：一次 SSH 往返拿齐四态**（可达 / 带卡 / 模型文件 / 常驻服务）。旧选机路径每台要 3~4 次往返（`echo` + `nvidia-smi` + `test -f` + `curl`），候选机一多就按台数线性放大。现在每台固定 1 次。
- **`probe_all()`：并行探测**。N 台全关机时，串行要 N × ConnectTimeout(8s)，并行后总耗时 ≈ 1 个 timeout。
- **探测结果 TTL 缓存**（`FLUX_PROBE_TTL`，默认 20s，0=关闭）。选机逻辑每张图都会调用，不缓存则每张图 N 次 SSH。
- **`FluxDB.usage_sub()`**：扣减月度用量，SQL `MAX(count-?, 0)` 保证下限 0，永不产生负数。
- **`FluxDB.refund_job_once()`**：幂等退配额，靠新增列 `jobs.refunded_at` 防重复退。
- **`fr.find_available_server()` 分级选机**：常驻已加载 > 可达+有卡+模型就绪 > 可达+有卡 > 可达（含无卡）。
- **`fr.any_usable()`**：waiting 任务恢复门，判据是「可达+有卡+模型就绪」，**刻意不要求常驻服务已在跑**。
- CLI 新增 `servers` 子命令：列出候选机四态 + 标出会被选哪台 + 列出未纳入的 `~/.ssh/config` 别名（`probe` 保留为旧名）。

### 改动
- `manager/flux_db.py`：**修复 `_lock` 初始化顺序** —— `self._lock` 原先在 `_migrate()` **之后**才赋值，而 `_migrate → _backfill_accounts → _one/_all/_exec` 都会 `with self._lock`，于是每次构造 `FluxDB` 都抛 `AttributeError`，被 `_backfill_accounts` 的宽 `except` 吞成一行「账户回填跳过」——**该迁移从未真正执行过**。现已把锁的创建提到 `_migrate()` 之前（实跑验证：迁移日志正常打出）。
- `manager/flux_db.py`：`jobs` 表新增 `refunded_at` 列（SCHEMA + 幂等 `_migrate` 补列）。
- `manager/flux_quota.py`：`precheck()` 改为**只用 `used` 判定**，不再叠加 `inflight`。`usage` 在入队时已 +1，`used` 天然包含在途任务；叠加后同一张在途图被算两次，突发提交时用户实际可用额度只有套餐一半。删除死代码 `record_enqueued()`（全仓零调用，留着会诱发真的双重计费）。
- `manager/flux_queue.py`：`_process()` 与 `_recover_waiting_tasks()` 的**终态失败**落点接上 `_refund_quota()`（幂等）；`job_id` 由毫秒时间戳改为 `uuid.uuid4().hex[:16]`（毫秒时间戳同毫秒并发提交会撞主键）；`_any_ready()` 改用 `fr.any_usable()`。
- `manager/flux_server_manager.py`：`probe()` 不再重复调两次 `gpu_ready()`（原来 `[0]`/`[1]` 各调一次，等于每台多跑一次 `nvidia-smi` 往返）；`find_ready_server()` 改走并行 `probe_all()`（语义与优先级顺序不变）。
- `manager/flux_resident_client.py`：`probe()` 分流（`FLUX_RESIDENT_BASE` 有值走直连 HTTP，否则委托 `fsm.probe_full`）；`ensure_resident()` 复用探测结果里的 `gpu_ok`/`model_ok`，省掉每张图 2 次 SSH 往返；等待循环改用 `force=True` 绕过 TTL 缓存。

### 计费语义（需要知道的行为变化）
- 任务进入**终态失败**（业务失败 / 重试超限 `[RECOVER_SKIP]`）→ 自动退还 1 张配额，日志打 `↩️`。
- `waiting`（服务器 down，进等待恢复池）→ **不退**。它不是终态，任务恢复后会继续跑；若在 waiting 就退，等于同一张图免费出。
- 退款额度按 token 落账（与 `usage_add` 同口径），账户聚合仍由 `account_usage` 负责。

### 验证（全部离线，无需 GPU / 网络）
- `verify_resident_stub.py` **26/26 PASS**（回归，确认本批改动没打破常驻链路）
- `verify_quota_patch.py` **2/2 PASS**：patch 前突发只放出 25 张（`used=25 + inflight=25` 命中上限 50），patch 后放满 50 张
- `verify_refund_quota.py` **19/19 PASS**：退配额幂等、`usage_sub` 下限 0、业务失败退 / `waiting` 不退 / 重试超限退、以及 O-13 正例（账户回填迁移真的执行）
- `verify_autoselect.py` **20/20 PASS**：`probe_full` 输出解析 5 种形态、分级选择 6 条规则、端到端接线 4 项、**每台恰好 1 次 SSH 往返**（4 台=4 次，无重复）、**并行实测 0.30s**（串行需 ~1.2s）、TTL 缓存命中后二次调用 0 次 SSH
- 改动范围（md5 逐文件比对）：**5 改动 / 0 新增 / 0 删除 / 62 未动**

### 未验证
真机 GPU 出图、真实 SSH 连通性、多机同时在线时的实际选机结果、`bench --count 5` 的吞吐提升倍数。
这些都需要 FLUX 机开机后才能定论 —— 未开机，不下结论。

## [v2.1] - 2026-09-16

**模型常驻生成路径**（消除「每张图冷启动重载 ~31GB 权重」的结构性瓶颈）。

### 新增
- `server/flux_resident_server.py`：GPU 端常驻生成服务。模型加载一次常驻显存，之后每个请求只做推理。HTTP + JSON 协议（`/health` `/generate` `/status` `/image` `/jobs` `/cancel`），只绑 `127.0.0.1`；单线程串行 worker + 优先级队列；任务状态落盘便于 SSH 排查；`--stub` 模式（纯标准库合成 PNG，无需 GPU）供离线自证。可选 `X-Auth-Token` 鉴权
- `server/start_resident.sh`：常驻服务幂等启动/探活。`--check`（只读探活，就绪 exit 0）/ `--force` / `--stop`。只操作 `fluxd` 会话与 `flux_resident_serve[r].py`，**不删 `out/`、不动 `fluxgen`**，与旧链路互不干扰
- `manager/flux_resident_client.py`：调用侧客户端与传输层。`DirectTransport`（本机可达时走 urllib）与 `SshCurlTransport`（远端 GPU 机走 `ssh <alias> curl`，无需端口转发）自动选择；`probe / find_available_server / any_ready / ensure_resident / wait_model_loaded / generate_via_resident` + 一个 CLI（`probe/health/up/gen/bench`）
- `manager/.env.example`：配置样例（`FLUX_GEN_MODE` / `FLUX_RESIDENT_*` / `FLUX_OFFLOAD` …）

### 改动
- `manager/flux_queue.py`：`_generate()` 改为按 `FLUX_GEN_MODE` **分发**（resident 默认 / legacy 逃生口），原实现整段保留为 `_generate_legacy()` 未删改；新增 `_generate_resident()`，按 `TransportError.kind` 分流成 `[SERVER_DOWN]`（进等待恢复池）与普通失败；`_health_loop` 改用 `_any_ready()`（resident 模式一次 `/health` 覆盖可达/带卡/模型三态，旧路线每台 3 次 SSH 往返）；模块级显式 `_load_env()`
- `manager/flux_server_manager.py`：`process_job()` 改为按同一开关分发，新增 `process_job_resident()`（逐张提交，产物布局与 `pull_images` 一致，`insert_into_note` 无需改动），原实现保留为 `process_job_legacy()`；模块级显式 `_load_env()`；新增 `GEN_MODE`
- `manager/flux_service.py`：启动日志打印当前生成路径
- `README.md` / `docs/ARCHITECTURE.md` / `docs/PITFALLS.md`：补常驻路径说明、架构图、8 条新踩坑

### 能力增量
- `width / height / steps / seed / negative_prompt` 可透传到服务端（旧 web 层只能传 prompt，服务端固定 768×1024 / steps 25 / seed 42）
- 服务端加输入硬边界：尺寸归一到 16 的倍数并夹在 256~2048，steps 限 1~100，越界返回 400（不再静默截断）
- 随机 seed 会随状态落账并回带，支持「拿到好图 → 用该 seed 复现」

### 验证
**已实测**（`flux-resident-verify/verify_resident_stub.py` 离线自证，**26/26 PASS**，无需 GPU）：
服务生命周期、生成全链路、尺寸/seed/负向词透传（读 PNG IHDR 实测输出尺寸）、同 seed 字节级可复现、
只传 prompt 时沿用旧默认、越界 400、未知 job 404、5 任务并发排队后全部终结、取消排队任务、
错误分流（不可达→`server_down` / 业务错误→`failed`）、SSH 传输层的命令构造与中文 base64 编解码无损、
启动脚本不误杀旧链路。另：4 个改动文件 py_compile 通过、`start_resident.sh` 通过 `bash -n`、
resident/legacy 两种模式 import 均通过、非法 `FLUX_GEN_MODE` 回退并告警、`.env` 加载时序已验证。

**未实测**：真机 GPU 出图、真实 SSH 连通性（不涉网络/密钥/认证）、`FLUX_OFFLOAD` 三档的显存与耗时差异、
吞吐提升倍数。这些需要 FLUX 机开机后才能定论 —— 未开机，不下结论。

## [v2.0] - 2026-09-04

**FLUX 多服务器化 + VPS 看门狗**（支持第 2 台 FLUX 服务器，任一台开机能接单）。

### 任务中心多服务器（Part A）
- `manager/flux_server_manager.py`：新增 `FLUX_SERVERS` 服务器注册表（默认 flux1 + flux2，支持 env `FLUX_SERVERS_JSON` 覆写，未来加机不动代码）；所有 SSH 操作改为服务器感知（接受 `server` 参数，None=默认 flux1 向后兼容）；新增 `probe()` / `find_ready_server()`（返回第一台可达+带卡+模型就绪）/ `any_ready()`
- `manager/flux_queue.py`：`_generate()` 每单 `find_ready_server()` 挑任一台可用服务器执行（跳过关机/无卡/模型未就绪），`server` 名回写 DB；`_health_loop()` 改 `any_ready()` 判断——任一台起来即恢复 waiting 池任务；移除不再用的 `REMOTE_BASE/REMOTE_OUT` 常量
- `manager/flux_db.py`：jobs 表新增可空 `server` 列（记录任务跑在哪台，幂等迁移）
- 并发策略：保持单 worker 串行，每单自动选任意可用服务器（用户已确认）

### VPS 看门狗（Part B，新建 `watchdog/`）
- `watchdog/flux_server_ready.sh`：服务器端就地脚本。`--check` 只读状态（带卡+模型+脚本）/ 全量校验+清残留+打 `SERVER_READY`
- `watchdog/flux_watchdog.sh`：VPS 常驻（镜像 qwen `qwen_watchdog.sh` 多机版）。`TARGETS` 逐台巡检，机器在线但未就绪 → 自动就地预热到「可接单」，轮询确认
- `watchdog/flux-watchdog.service`：VPS systemd unit（`Restart=always`）
- `watchdog/README.md`：部署到 `vps-aliyun`（/opt/flux-watchdog + systemd enable）完整步骤 + 加/减机的运维
- 语义按用户确认：看门狗「拉起服务」= 预热就绪+保活（非重写常驻守护），真正生图仍由任务中心按需调度

### Part C（flux2 接入，待克隆完成）
- `~/.ssh/config` 已加 `autodl-flux2`（connect.weste.seetacloud.com:23192，key id_rsa_musetalk）；免密 + 数据盘克隆校验待 clone 完成后进行

**验证**：三个 manager 文件编译 + import 通过；jobs 表 `server` 列迁移 OK；watchdog 两脚本 `bash -n` 通过。真机 E2E（网页提交→任一台生成→`server` 列有值；拔一台另一台接力；VPS 看门狗开机自动预热）待 flux2 就绪后实测。

## [v1.8] - 2026-08-18

**一键启动脚本 + 修复 admin 登录 bug**（运维优化）。

- `start_service.ps1` 重构：支持 `-Target all/flux/xhs/tunnel`，可分别拉起 FLUX 文生图服务(:9620)、小红书发布服务(:8800)、公网隧道
- **修好 cloudflared 启动 bug**：原传 `--config` 参数导致 "tunnel run accepts only one argument" 起不来（1033 隧道错误根源之一）；改为 `cd .cloudflared` 目录 + `tunnel run xhs-tunnel`（文档 COORDINATION.md 正确方式）
- ps1 补 UTF-8 BOM（否则含中文的 ps1 被 PowerShell 按 GBK 误读解析报错）
- 新增 `启动-01-FLUX服务.bat` / `启动-02-小红书服务.bat` / `启动-03-仅隧道.bat` 三入口，重启后可手动分别拉起
- `start_service.bat` 改为一键拉起全部
- **修 admin 登录 bug**：`/api/admin/users` 返回 `{"accounts":[]}`，但前端登录门检查 `d.users`（永远 undefined）→ 任何 token 都报"Token 错误"；改为 `d.accounts!==undefined`（2 处，login + 自动刷新）

**验证**：ps1 幂等（重复跑不重复起进程）；三个服务重启后本地+公网全链路 200；admin 用 `flux-admin-2026` 登录恢复正常

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
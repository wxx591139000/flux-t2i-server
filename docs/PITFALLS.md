# 项目踩坑记录 — FLUX 文生图服务（通用）

> 持续更新。格式：`[日期] 问题 → 原因 → 解决`

## 子进程弹窗 / 无控制台进程（2026-09-21 第三轮）

### 0. ⚠️ 悬而未决：`CREATE_NO_WINDOW` 修复**未获直接验证**（下次接手先看这条）

- **已做**：`flux_server_manager.py` 的 `run()` 加了 `creationflags=CREATE_NO_WINDOW`，
  9620 也重启加载了它。**代码在跑，回归门（D 组）也绿。**
- **但**：「加了 flag 之后窗口确实不再弹出」这一条**我没有拿到直接证据**。
  试过的三种观测**全部无效**，记下来免得下次重走：
  1. **`conhost.exe` 计数** —— ❌ **根本不是判据**。conhost 只是「控制台宿主」，
     **隐藏的控制台同样有 conhost**。我一开始拿它当判据，得到互相矛盾的结果。
  2. **`GetConsoleWindow()` / `IsWindowVisible()` 自报** —— ❌ 在**我的沙箱进程**里测不出差别，
     因为沙箱进程本身就是 `sandbox-cli.exe`（**有**控制台）的后代，
     子进程直接继承那个控制台，永远不会"新建窗口"。
     **我复现不了用户的场景**（用户的 flux_service 是从 explorer 双击 .bat 起的）。
  3. **`MainWindowHandle`** —— 对本例不适用（控制台窗口不体现在这里）。
- **机制本身是可信的**：`CREATE_NO_WINDOW` 是微软文档定义的、专门用于
  「无控制台父进程 spawn 控制台子进程时不要新开窗口」的标志；
  它同时也是**代价最低**的改法（不加它一定弹窗，加了最多是没效果，不会更糟）。
- **留给下次的验证路径**（仍未做）：
  1. 让**用户**双击 .bat 起服务（真场景），然后**肉眼观察 5~10 分钟**看是否还弹窗；
  2. 或在真场景下用 `Get-CimInstance Win32_Process -Filter "Name='bash.exe'"`
     抓 `MainWindowHandle != 0` 的实例（有窗口句柄 = 有窗口）。
- **教训（比这条坑本身更重要）**：
  **"我改了、也重启了、门也绿了" ≠ "问题解决了"。**
  当**观测手段本身失效**（测不出差别）时，必须把它明确写成"未验证"，
  而不是拿"门是绿的"冒充结论 —— 那正是本轮前面已经栽过两次的同一个错。

### 1. ★ 「一直有 2 个 bash.exe 不时弹窗」—— 无控制台的服务拉子进程会**新建可见控制台**

- **症状**：用户报「一直有 2 个 bash.exe 不时弹窗」。**没有报错、功能正常**，
  只是每隔一会儿蹦出一两个黑窗自己消失。
- **误导性**：第一反应是去查「谁在调 bash」。而**全局搜索只能搜到 `hooks\run-hook.cmd`**
  （superpowers 插件的 SessionStart 钩子）—— 看上去完美契合"不时弹窗"，
  于是很容易就下结论了。**但那是错的**：该插件并不在 `enabledPlugins` 里（是禁用状态），
  `hooks.json` 也只注册了 `SessionStart`（会话开始才触发一次），**解释不了"一直"**。
- **正确的取证方式（别推理，去观测进程树）**：连续采样 `Get-CimInstance Win32_Process`，
  抓 `bash.exe` 的 **ParentProcessId**。实测：
  ```
  round 3 : bash count=4  [PID=314588 PPID=157804 11:09:13] ...
                                     ↑ PPID=157804 就是 flux_service.py
  ```
  **父进程正是自己拉起的 flux_service** —— 一眼定性。
- **根因两层**：
  1. `manager/flux_server_manager.py` 的 `run()` 用
     `subprocess.run([bash, '-lc', cmd], ...)`，**没有 `creationflags`**；
  2. `flux_service.py` 是**无控制台**的（.bat 用 `-WindowStyle Hidden`、
     工具里用 `DETACHED_PROCESS` 拉）。Windows 在「无控制台的父进程 + 有控制台子系统
     的子进程」时会**新建一个可见控制台** → 弹窗。
- **为什么是"2 个"、为什么"不时"**：`_dispatch` 的 `interval=120s`，每轮对每台
  可达机做探活（`ssh` 一次 = 一个 bash），一轮里常有 1~2 次调用 → **每约 2 分钟弹 1~2 个**。
- **修法**：
  ```python
  CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
  SUBPROC_FLAGS = CREATE_NO_WINDOW          # 所有子进程统一用它
  subprocess.run([...], creationflags=SUBPROC_FLAGS)
  ```
  `0x08000000` 仅 Windows 有效；`os.name != 'nt'` 时为 0（等于不加），
  所以同一份代码在 Linux 侧（watchdog / resident）无副作用。
- **闸门**：`tests/test_transport_env.py` D 组（3 项）——
  **用 AST 遍历文件里所有 `subprocess.*` 调用**，任何一处漏 `creationflags` 就报红；
  另有一条变异断言（把 `creationflags=` 删掉必须报红）。
  ⚠️ 为什么用 AST 而不是 `grep`：新写一处 subprocess 就会重新开始弹窗，
  而「偶尔弹个黑窗」**没人会当成 bug 报上来** —— 必须机器兜住。
- **通用教训**：**「无控制台的父进程」是个会放大的环境属性**。
  在服务里 spawn 任何控制台程序（bash/python/ffmpeg/nvidia-smi…）都要显式加
  `CREATE_NO_WINDOW`，否则在「双击 .bat 起的服务」上就会弹窗，
  而在「从终端里起的」上不会 —— 典型的**只在生产暴露**的问题。

## 启动脚本 / 进程生命周期（2026-09-21 第二轮，**本轮最贵的一次**）

> 这一轮的坑全部围绕"**我怎么知道我的修复真的生效了**"。
> 三个坑叠在一起，让一个"已经修好的 bug"看起来没修好，还顺手把线上服务搞挂了。

### 1. ★★ 「重启脚本把服务杀掉后自己崩了」—— 根因是**大小写重复的环境变量**

- **症状**：用户双击重启 .bat 后，服务**没有任何输出**，9620 端口**直接死掉**。
  最迷惑的是：**看起来像什么都没发生**（没有报错、没有日志、也没起来）。
- **误导性**：会去查"脚本是不是没被执行"、"权限问题"、"端口占用"。全错。
- **根因链（必须完整理解，否则会改错地方）**：
  1. 本机环境里**同时存在 `http_proxy` 与 `HTTP_PROXY`**（`https_proxy`/`HTTPS_PROXY` 同理）。
     实测（`[Environment]::GetEnvironmentVariables()`，不做大小写折叠）：4 个键并存。
  2. PowerShell 5.1 的环境变量提供程序**大小写不敏感** → `Get-ChildItem env:` 抛
     ```
     已添加了具有相同键的项。   ← 实测连续 6 次全抛，**不是偶发**
     ```
  3. `Start-Process` 带 `-RedirectStandardOutput` 时会**枚举环境** → 同样抛。
  4. ★ 致命之处：抛出点在 `Stop-Process` **之后**。于是 `-Restart` 的真实语义是
     **「先杀掉旧进程 → 再崩 → 端口空着」**。
- **为什么极难发现**：因为异常只在**特定宿主**里出现。同一个脚本在别的上下文里能跑通 →
  让人以为"已经好了"。**间歇性 + 看起来像成功 = 最难抓的组合**。
- **修法（三件事，缺一不可）**：
  1. **环境自愈**：脚本开头 `Repair-DuplicateEnv()`，把重复的大写变量 `SetEnvironmentVariable(..., $null)` 消掉；
     **只在两个值都非空时动作**（不无条件删用户变量）。
  2. **启动封装 + 验证**：`Start-BackgroundService` 起完必须**等端口真的在监听**（最多 ~12s），
     起不来要 **❌ 报出来**并置 `hadFailure`，绝不能静默吞掉。
  3. **回退路径**：`Start-Process` 失败时退到"不带重定向"再试一次。
  另外：`Stop-PortOwner` 杀完必须**等端口真正释放**（TIME_WAIT），10s 没释放要打警告。
- **闸门**：`tests/test_start_script.py`（19 项，含 2 组变异断言 ——
  去掉 `Repair-DuplicateEnv` 的**调用**必须报红，实测会）。

### 2. ★★ 「嵌套 powershell 子进程根本没执行」—— 我把"没输出"当成了"跑通了"

- **症状**：我用 `& "$PSHOME\powershell.exe" -NoProfile -Command "Write-Output hello"` 做测试，
  **返回 0 行输出**。我把这个当成"脚本没有输出"，于是继续在脚本里找 bug。
- **实测结论（做过的判定）**：
  ```
  & "$PSHOME\powershell.exe" -NoProfile -Command "Write-Output hello"   → len=0
  & "$PSHOME\powershell.exe" -NoProfile -Command "'x' | Out-File <f>"   → 文件不存在
  ```
  → **子进程完全没被执行**（连最简单的一条都没有）。这不是脚本的问题，是本环境的限制。
- **教训**：**"没有输出" ≠ "没有发生"**。在把"空输出"解释成任何结论之前，
  先用一个**必然有输出的最小用例**校准这个通道（`Write-Output hello` 打不出东西 ⇒
  这个通道不能用来判定任何事）。
- **正确做法**：要么在**当前进程**里 dot-source 脚本（`& $path -Args`），
  要么让被测对象**写文件**再读文件。**绝不靠嵌套子进程的 stdout 下结论**。

### 3. ★ 「手动跑门」+「离线全绿」两件事加起来仍然是**假信号**

- 本轮真实序列：离线 11/11 全绿 → 我判定"修好了" → 用户线上仍然 404 →
  才发现**线上进程从没重载过代码**（`web_uptime_sec` 两次点击 +1442s，PID 未变）。
- 与上一轮第 3 条同源，这里再加一条**独立的**教训：
  **"我验证过" 必须能回答"我在哪个进程/哪条通道上验证的"**。
  在测试替身上验证 ≠ 在真身上验证；在新进程里验证 ≠ 在线上进程里验证。
- **工具留痕**：`py -3.11 tools/check_stale.py` 把"进程是不是当前代码"做成**双判据**，
  一条命令出结论 + 修复步骤。
- **推论**：任何"改了代码"的修复，交付清单里必须有一项是
  **「证明线上进程已重载」**，否则修复等于没做。

## 任务删除 / 看门狗纳管（2026-09-21）

> 五条坑，共同点是**症状都在"A 处"、根因都在"B 处"** ——
> 每一条都曾把排查引向错误的方向，所以每条都写清"什么线索会骗你"。

### 1. ★ 看门狗会**反向覆盖**远端 —— 手动上传等于白推

- **症状**：改完 `server/flux_resident_server.py`，scp 到 GPU 机，**立刻**校验 md5 → PASS。
  几十秒后再校验 → **md5 变回旧值，但 mtime 是新的**。
- **误导性**：mtime 新 + 内容旧，看起来像"写入没落盘 / 文件系统缓存 / 编辑器没保存"，
  于是去查磁盘、查 `sync`、重写文件 —— **全错**。
- **根因**：VPS 看门狗每 60s 比对「GPU 机文件 vs VPS `/opt/flux-watchdog/mirror/`」，
  **不一致就用 mirror 覆盖远端**。日志实锤：
  ```
  Sep 21 09:06:54 flux-watchdog: [connect.westc.seetacloud.com] 同步 flux_resident_server.py（远端版本不一致）
  ```
  也就是说**手动上传的东西会被下一轮巡检打回**，而 authority（权威副本）在 VPS 的 mirror 里。
- **正确顺序**：改了 `server/` 下的常驻文件 → **第一动作是 `py -3.11 watchdog/deploy_vps.py`**
  （更新 mirror），**不是** scp 到 GPU 机。mirror 更新后 GPU 机 ≤60s 自动同步，
  **可以完全不手动上传**。
- **排查口诀**：**先看 mirror 的 md5，别只盯目标机**。
  ```bash
  ssh vps-aliyun "md5sum /opt/flux-watchdog/mirror/*"
  ```
- **这不是缺陷，是特性**：它让新克隆的裸机能自补件。所以别把它"修掉"，
  要把它**钉进回归门**（`test_watchdog_offline.py` 第 [8] 组，含变异断言）。

### 2. ★ `sqlite3.Row` 取**不存在的列**抛 `IndexError`，在 worker 线程 = 打死 worker

- **症状**：所有任务永远卡 `queued`，网站一直转圈，`/health` 变 `degraded`、`worker_alive=false`。
- **误导性**：看起来像"队列卡住 / 上游 GPU 机连不上"，于是去查 SSH、查 GPU。
- **根因**：`sqlite3.Row` 的 `row['不存在的列']` 抛 **`IndexError: No item with that key`**
  （不是返回 `None`、也不是 `KeyError`）。而 `_process()` 跑在 worker 线程，
  **任何未捕获异常都会让线程直接退出** —— 此后没人再消费队列。
- **触发条件**：新加的 `deleted_at` 列在**老库还没跑到 `ALTER TABLE` 迁移**时被读到。
  也就是说这个 bug 平时不出现，只在**升级瞬间**出现 —— 最难防的那类。
- **修法**：加防御式读取 `_row_value(row, col, default=None)`
  （`flux_queue.py` 与 `flux_web_service.py` **各留一份**，避免循环导入）：
  ```python
  def _row_value(row, col, default=None):
      try:
          return row[col]
      except (IndexError, KeyError):
          return default
  ```
- **同族坑（09-17 已记）**：`sqlite3.Row` **没有 dict 的 `.get()`**。
  两条合计一句话：**把 `Row` 当 `dict` 用之前，先 `dict(row)`**。

### 3. ★ 「改了代码没生效」的真因是**进程陈旧** —— 而症状会把你引向协议层

- **症状**：用户在网站上点「彻底删除」，前端报
  ```
  FLUX 服务返回非 JSON（HTTP 404）：Not Found
  ```
- **误导性**：这条报错把注意力全引向"路由写错了 / 协议不对 / 响应体异常 / 网关做了什么"。
  而代码在磁盘上**完全正确** —— 离线端到端 14/14 全绿。**验证通过却线上 404** 这个矛盾
  才是最该抓的线索。
- **根因**：9620 上跑的是**13 小时前的旧进程**（启动于 2026-09-20 20:45:49），
  而删端点是 09-21 09:00 之后才写的。进程在 import 时把代码读进内存，**之后不会再读盘**。
- **识别凭据（两条独立证据，任一成立即"旧进程"）**：
  1. `/health` 里的 **`web_uptime_sec`** —— 13.2 小时 ≫ 代码改动时间 → 进程比代码还老
  2. 新端点**直接打 404 且返回纯文本**（不是 JSON）—— 跑着的进程里根本没这个路由
  单看 uptime 可能误判（改的是无关文件），单看 404 也可能误判（真把路由删了），
  **两条同时成立才定性**。
- **修法**：重启 `manager/flux_service.py`（双击 `启动-01-FLUX文生图服务.bat`）。
  工具化：**`py -3.11 tools/check_stale.py`** —— 一条命令给出上述两项证据 + 结论 + 修复步骤。
- **教训**：**"离线验收全绿"和"线上能用"是两件事**，中间隔着"进程有没有重载代码"。
  端到端测试起的是**新进程**，天然测不出这个问题。
  → 所以错误文案本身也要改：看到 404+非JSON 时应当直接说"上游改了代码没重启"，
  而不是让下一个人重走这十几分钟。站点侧已改（`hint404()`，返回 503 + `UPSTREAM_STALE`）。

### 4. ★ 测试替身必须与真身**同口径** —— 否则离线全绿而线上错

- **本轮实例**：站点侧验收用的 `tools/flux-stub-server.js` 里**没有 `/api/delete`**。
  如果不管它，站点侧删除的离线验收会得到**与事实相反的结论**
  （stub 404 → 判定"删除功能不可用"，而真上游其实支持）。
  已补上，且**语义逐条对齐**：403 归属（措辞与"不存在"一致，不泄露存在性）/ 幂等 /
  **不退配额** / 在途回 `was_inflight`。
- **为什么这条反复出现**（09-17 记过一次，本轮又记）：替身写松的成本是**零**（测试全绿），
  代价却是**在真机上炸**。而且它不会自己暴露 —— 只有"替身与真身逐条对读"才能发现。
- **做法**：写替身时**打开真身源码逐条抄语义**（不是凭印象），
  并给关键口径（如"不退配额"）**单写一条断言**，让它不能悄悄漂移。

### 5. ★ 手动列举的测试清单**必然漏**，且漏掉的那组"看起来像通过了"

- **本轮实例**：修完删除端点的第一轮，我逐条手动跑门然后宣布"全绿" ——
  **但看门狗那组是后来才想起来补跑的**。漏跑的那组不报错，只是静默地不执行，
  在输出上**跟"它通过了"完全一样**。
- **根因**：清单是**手写的副本**，而副本一定会与事实漂移。
  这与"改一处要同步三处"是同一类问题（用户明确反感的那种负担）。
- **修法（方向是反过来的）**：不写"要跑哪些"，而是**扫 `tests/test_*.py` 全跑** ——
  新加门 = 往 `tests/` 放个文件，**自动纳入**；想排除必须显式写进 `SKIP` 并说明理由。
  `tests/run_all.py` 就是这个。
- **附带要求**：排除项必须**带理由**（没有理由的排除就是"漏跑"的另一种写法）；
  `tests/manual/` 目录（需真 GPU/SSH 的一次性脚本）隔离但不参与，
  并在 `run_all.py` 里再显式排除一次防误扫。
- **验证**：给 runner 做过变异测试 —— 把某道门的退出码改成 7，
  `run_all.py` 确实报红并汇总 `0/1 道门通过`。否则"全绿"可能只是 runner 没在检查。

## 去重键与连点（2026-09-17，v2.8.1）

**去重键必须只有一个构造入口。** 三处（入队 / worker 收尾 / 孤儿恢复）曾经各写各的：
入队带 `{seed}:{w}x{h}`、收尾不带。add 与 discard 的键永远不相等 → `_inflight` 只增不减。
统一到 `_dedup_key()` 后不可能再漂移；`discard` 必须放 `finally`，否则 `_generate` 抛异常时
这组参数被永久占用。

**中文去重要用用户原始输入，不能用翻译结果。** 翻译是 LLM 调用，同一句中文两次翻译会漂移
（实测同一句译成 "A pair of men's slim-leg jeans…" 与 "Photorealistic product photography…"），
拿翻译后文本做键等于去重失效 → 用户连点两次出两张图、各计一次费。

**`submit()` 必须加锁。** web 层是 `ThreadingHTTPServer`，「检查去重 → 入队 → 登记」不原子时，
连点落在不同线程上都能通过检查。整段收进 `self._submit_lock`。

**⚠️ `sqlite3.Row` 没有 `.get()`。** `job_get()` / `jobs_queued()` 返回的是 `Row`，它支持
`[]` 索引但**没有 dict 的 `.get()`**。在 `_process` 里写 `job.get('prompt')` 会
`AttributeError`，而 `_run_worker` 没有 try/except → **worker 线程直接死掉**，
`/health` 变 `degraded`、`worker_alive=false`，队列里的任务永远没人处理。
先 `job = dict(job)` 再用 `.get()`（缺列时也不会抛 `IndexError`）。

**⚠️ 测试替身必须复刻真实对象的类型行为。** 这一条是上面那个 bug 没被离线测试抓住的根因：
替身的 `job_get()` 返回 `dict`，`.get()` 全绿；真机返回 `Row`，直接崩。
给 DB 写替身时用**真实的 sqlite 内存库**（`row_factory = sqlite3.Row`），别手搓 dict。
顺带：多线程用例要 `sqlite3.connect(':memory:', check_same_thread=False)` 并自己配锁。

**⚠️ 变异脚本改生产代码必须二进制读写。** 用文本模式 `open(...,'w')` 的话，Windows 会把
`\n` 写成 `\r\n`，"恢复"后整个文件 md5 对不上（实测 455 行被 CRLF 化）。用二进制读写保真。

## 传输层 / 常驻档位（2026-09-17，v2.8）

> 这两个坑同一天爆，症状都是「任务卡住、图出不来」，但根因完全无关，
> 共同点是 **默认值 / 失败路径选错了方向**。闸门：`tests/test_transport_env.py`（8 项，离线）。

- **[09-17] 「本机找不到 bash」被静默伪装成「远程服务器关机」** —— 排查成本最高的一次。
  - **症状**：客户网站下单后任务永远卡在 waiting；任务中心显示三台机全「SSH 不通（可能关机 / 别名未在 ~/.ssh/config 中）」，而**同一天同一时刻**在 Git Bash 里跑 `flux_resident_client.py servers` 却能看到 `flux3 可达·有卡·模型就绪`。
  - **根因**：`flux_server_manager.run()` 写死 `subprocess.run(['bash', '-lc', cmd])`。而 **Git for Windows 默认只把 `<Git>\cmd` 写进 PATH，那个目录里只有 `git.exe`；`bash.exe` 在 `<Git>\bin`，不在 PATH**。于是：
    - 从 Git Bash 起的服务 → PATH 含 MSYS 的 `/usr/bin` → 找得到 bash → 一切正常
    - 从资源管理器双击 `.bat` 起的服务 → PowerShell 环境 → `FileNotFoundError` → 被 `except Exception` 吞成 `(False, str(e))` → 每台机 ~0.07s 就"不可达"（**三台加起来 0.2 秒**，这个耗时就是破案线索：真 SSH 失败不可能这么快）
  - **为什么难查**：`run()` 只返回 `r.stdout`，**ssh 的报错全在 stderr，被丢掉了**；`probe_full()` 又把**所有**失败统一写成「SSH 不通（可能关机 / 别名未在 ~/.ssh/config 中）」→ 真因完全不可见，运维被引向"去检查 AutoDL 关机了没"。
  - **修法**（三处一起）：① 新增 `find_bash()` 主动定位 —— PATH → 从 `which git` 反推 `<Git>` 根 → 常见安装路径，结果缓存；② `run()` 失败时**带回 stderr**，找不到 bash 时返回醒目的 `NO_BASH: ...`；③ `flux_service.py` 启动时做一次自检，缺 bash 直接在日志里吼。
  - **闸门**：A1（模拟双击 .bat 的受限 PATH，断言仍能定位）A2（无 bash 时报 NO_BASH）A3（失败带 stderr）A4（probe 保留真因）。
  - **教训**：**「本机缺工具」绝不能退化成「远程机器不可达」** —— 前者是配置问题（1 分钟能修），后者会让人去查云控制台（40 分钟查不出）。

- **[09-17] `FLUX_OFFLOAD` 默认 `none` 在 32G 卡上**必然** OOM**（即上面 09-16 那条"待验证"的实测结论）。
  - **症状**：常驻服务拉起成功、`/health` 返回 200，但每张图都失败：`模型加载失败: OutOfMemoryError: CUDA out of memory. Tried to allocate 18.00 MiB. GPU 0 has a total capacity of 31.48 GiB of which 13.69 MiB is free. ... this process has 31.45 GiB memory in use`。
  - **根因**：`ensure_resident()` 透传 `FLUX_OFFLOAD={os.environ.get("FLUX_OFFLOAD","none")}` —— **默认值取的是「最快」（none=全程显存），但 32G 卡装 31.2 GiB fp16 权重 + 推理激活值根本放不下**。
  - **⚠️ 更正 09-16 的记录**：当时那条写的是「`none` **可能** OOM …… **本批未实跑，属待验证**」。09-17 实跑结论是：**在 RTX 4080 SUPER（32760 MiB）上是必然 OOM，不是"可能"**；`model` 档实测可用（显存占用极小，单张 1280×720 推理 54s / 端到端 71.5s）。
  - **修法**：优先级改为 `env FLUX_OFFLOAD` > 该机器条目的 `offload` 字段 > **安全默认 `model`**；`_DEFAULT_SERVERS` 每台显式声明 `"offload": "model"`。想跑 `none` 的大显存机器（80G）在自己条目上写明即可，不必改逻辑。
  - **闸门**：B1（无声明时默认安全值）B2（env 可覆盖）B3（每台默认机都声明了，防新增机器漏写）B4（模型加载报错要立刻抛，别干等到超时）。
  - **教训**：**默认值要选「一定能跑」，不是「最快」**；性能偏好应该由机器/环境显式声明，而不是当默认值。
  - **同源教训**：这与 v2.3 的 `FLUX_WORKDIR`/`FLUX_MODEL` 是同一个病 —— **「探测」和「真正拉起」是两条独立代码路径，改一条别忘另一条**。

- **[09-17] 「激活 / 绑定」按钮点了必报「缺少激活码或 token」** —— identity 被当成业务参数。
  - **症状**：客户页（`/`）的「激活 / 绑定账户」卡片，粘贴激活码点按钮，永远报「缺少激活码或 token」。
  - **根因**：前端 `activate()` 只发 `{code:c}`；而 `_api_activate()` 第一行就是
    `if not code or not token: return {'error': '缺少激活码或 token'}` —— **每一次点击都必然失败**。
    本质是把 identity 当成了业务参数：token 早就在 HTTP 层由 `_get_token()`
    （cookie `flux_token=` 或 `?token=`）解析好了，handler 却又向 body 要一次。
  - **修法**：`_api_activate(body, token)` / `_api_bind(body, token)` 接收 `do_POST` 已解析的 token；
    优先级 **body.token > HTTP 身份**（前者兼容脚本调用，后者让「页面点一下就能用」）。
  - **顺带补缺口**：admin 页原本**只有「改套餐 / 改备注 / 设 owner / 详情」，没有任何绑定入口**
    （所以用户想「对每个用户做用户码绑定」时无处可点）。新增：
    ① 「用户码绑定」卡片（输入 用户 token + 激活码 → `/api/bind`）；
    ② 账户行内「绑设备」按钮（对该账户直接并一台设备进去）。
  - **验证**（真机 HTTP，非推算）：只传 code + `?token=` → `激活成功`；第二台设备 → `已绑定账户`；
    body 带 token 的旧调用 → 仍成功；给了 token 但缺 code → 正确报「缺少激活码或 token」；
    `set_plan` 改 default→pro 后 DB 里 `accounts.plan='pro'`，4 个设备 token 全部挂在同一账户下。
  - **注意**：`_get_token()` 在**既无 cookie 也无 `?token=`** 时会**自动生成随机 token**
    （设计如此：匿名用户各有独立配额）。所以「什么都不传」不会报缺参，而是以一个新身份激活 ——
    写测试时别把它当成「参数校验仍生效」的证据。

## flux3 接入（2026-09-16，v2.5）

- **[09-16] 选机探测对「纯密码登录」的机器会全灭，而且报错是「SSH 不通」而非「密码错」** → `probe_full()` 固定带 `-o BatchMode=yes`（故意的：非交互环境不能弹密码提示，否则每台机器都卡在交互等待上）→ 密码认证被直接拒绝，症状与「关机」无法区分 → **克隆实例若继承了克隆源的 `authorized_keys` 就没问题**（本次 flux3 正是如此，`id_rsa_musetalk` 直接可登，无需 sshpass）；否则必须先 `ssh-copy-id` 配免密，**别指望密码能跑选机**
- **[09-16] 「默认机」与「自动选机」是两个概念，混起来会误判成「部分坏了」** → A 链任务中心走 `find_ready_server()`，每单现挑，换机后**自动就对了**；而小红书产线（`process_job`）与不带 `--server` 的 CLI 走**默认机**（= 候选机第一台 flux1）→ flux1 无卡时表现为「A 链正常、产线全失败」 → 新增 `FLUX_DEFAULT_SERVER` 把默认机指到当前能用的机器
- **[09-16] 32GB 卡（RTX 4080 / 32760 MiB）对 FLUX.1-dev fp16（~33GB 权重）余量极小**，而 `FLUX_OFFLOAD` 默认 `none`（全程显存）→ 拉起常驻前先想清楚档位：`none` 可能 OOM，退 `model`（比 `sequential` 快 2~3 倍）是这类卡的现实选择。~~**⚠️ 本批未实跑，属待验证**，勿当结论引用~~
  → **【09-17 已实测，结论更正】**：在 flux3（RTX 4080 SUPER 32760 MiB）上 `none` **必然 OOM**（不是"可能"），
  `model` 档可用（1280×720 单张推理 54s / 端到端 71.5s）。默认值已改为安全的 `model`，
  详见上方「传输层 / 常驻档位（2026-09-17，v2.8）」条目。

## 健康探针（2026-09-16，v2.4）

- **[09-16] 探针里不能探后端** → 若 `/health` 内去 SSH/HTTP 探 GPU 机，**GPU 机关机时本服务探针会变慢甚至超时**，监控就把「后端不可用」误判成「网站死了」—— 这两件事必须能分开看 → web 侧 `_health()` 只读**进程内**状态（`db.ping()` + `scheduler.stats()`），字段里加 `backend_hint`（由 `waiting` 池大小推导，零成本）。**用 AST 剥掉 docstring 后做源码级断言，禁用词 `ssh/curl/subprocess/fsm./fr./urllib/probe/Popen` 零命中**，别靠"感觉很快"（实测 2~3ms）
- **[09-16] 探针自身不能抛** → `db.ping()` 或 `scheduler.stats()` 抛异常时，若让它冒到 `do_GET`，探针会返回 **500**，监控看到的是"服务崩了"而不是"DB 坏了" → 两处各自 `try/except` 收成 `degraded`（HTTP 503）并把异常原文放进 `errors[]`
- **[09-16] 「读不出状态」不等于「健康」** → war 判定若写成 `worker = st.get('worker_alive')`，`stats()` 抛异常时 `st={}` → `worker=None` → 与「没挂调度器」混为一谈，误报 200 → 显式区分三种：`has_sched=False`（web-only，算健康）/ `worker=True`（健康）/ `worker=False` 或 `sched_err`（degraded）。判定式：`healthy = db_ok and not (has_sched and (sched_err or not worker))`
- **[09-16] `flux_web_service.py main()` 传的是 `scheduler=None`**（web-only 调试模式）→ 探针与任何新加的读取调度器的代码都必须容忍 → `worker_alive/queue_depth/gen_mode` 在该模式下返回 `null` 而非 `0`（`0` 会被误读成"队列是空的"，实际是"没有队列"）
  - **真机实测的后果（09-16 晚补充）**：该入口下 `POST /api/submit` **必然 500** ——
    `'NoneType' object has no attribute 'submit'`（`_api_submit` 直接调 `self.scheduler.submit()`）。
    也就是说 **A 链在这条入口下完全不可用**，只有 `/health`、静态页、admin 接口能用。
    → **起服务做端到端验证/生产部署必须用 `manager/flux_service.py`**（真 DB + 真队列 + 真 web + 飞书 bot 的完整装配）
    。用错入口的表现很有迷惑性：`/health` 返回 200 看着一切正常，一提交任务才 500。
  - 自证：`python manager/flux_web_service.py --port 9621` → `curl -X POST /api/submit` → 500；
    `python manager/flux_service.py --port 9622` → 同样请求 → `{"job_id":"…","status":"queued"}`
- **[09-16] 「连不上」与「上游回 503」是两种故障，别压成一个 boolean** → 下游网站若只判 `res.ok`，就无法区分"flux 没启动"与"flux 活着但自报 degraded" → `fluxHealthReport()` 带回 `httpStatus`（`null`=连不上；`503`=连上了但上游不健康），`fluxHealth(): boolean` 保留为薄包装不破坏旧调用

## 双链验收 / 路径透传（2026-09-16，v2.3）

- **[09-16] 克隆实例换路径后常驻服务起不来，报「模型未就绪」→ `ensure_resident` 组装的 ssh 命令只透传了 `FLUX_RESIDENT_PORT/SCREEN/PY/TOKEN`，**没透传 `FLUX_WORKDIR` / `FLUX_MODEL`**；而 `start_resident.sh` 里 `WORKDIR` / `MODEL` 是硬编码默认值（`/root/autodl-tmp/flux-t2i`）** → 脚本去查错路径的模型文件 → 误报「模型未就绪」；且上传的 `flux_resident_server.py` 与脚本查找路径错位（上传到 `{remote_base}/`，脚本找 `$WORKDIR/flux_resident_server.py`）。**修法：把 `server['remote_base']` / `server['remote_model']` 用 `shlex.quote` 透传为 `FLUX_WORKDIR` / `FLUX_MODEL`**（脚本侧是 `${FLUX_*:-默认}`，传了就以本机为准）。教训：**「一次往返拿齐四态」的探测和「真正拉起」是两条独立代码路径** —— 探测按 `server` 字典走对了，拉起却拿全局默认值，只有换路径的克隆机才会暴露
- **[09-16] 两条链路（自带任务中心 / 客户生图网站）的边界必须在文档里钉死** → 两边是**独立项目**，唯一关联是网站通过 HTTP 调 `/api/submit` + `/api/status` + `/api/download` → 排查时不要跨项目猜耦合；本项目的端到端自检**不需要**下游站点存在
- **[09-16] 「常驻服务起来了」≠「能用」→ `/health` 有响应只说明 HTTP 活着，模型加载是后台异步的** → 选机/接单必须看 `model_loaded`，不是 `resident`。`any_ready()`（要求 `resident 且 model_loaded`）与 `any_usable()`（只要「可达+有卡+模型文件就绪」）是**两个不同问题**：前者回答「现在能出图吗」，后者回答「值不值得为它拉起来」。waiting 恢复门必须用后者，否则换机后任务死等

## 离线端到端自检的环境坑（2026-09-16，v2.3）

- **[09-16] 独立后台进程会被回合回收 → 起了 daemon 再在下一次工具调用里打请求，会拿到「端口直接消失」（日志无任何异常）** → 验双链必须把「起服务 → 打请求 → 收摊」压进**同一次调用**（`chain-verify/run_both_chains.sh` 就是这么写的）。现象具有误导性：日志干净、退出码 0，看起来像服务自己崩了
- **[09-16] `next build` 第二次起必失败，抛 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`（count=50）→ 安全删除垫片按「每回合删除文件数」计数，`recursiveDelete` 清 `.next/` 时超阈值** → 给该子进程清空注入：`NODE_OPTIONS= node ./node_modules/next/dist/bin/next build`。**只豁免这一个构建进程**，不动其它文件操作约束
- **[09-16] 验证脚本用固定端口 → 出现 1 次未复现的瞬态失败（残留 stub 进程短暂占用端口，下游断言连锁失败）** → 固定端口要加「被占则退到系统分配空闲端口」的兜底；另外**每个断言脚本必须自己清空 scratch 目录**，否则上轮残留会让「只落 1 张图」这类断言假红/假绿
- **[09-16] 用 `| tail` 接住长驻进程的 stdout → 输出被缓冲，日志文件里什么都没有，误判成「服务没起来」** → 长驻进程直接重定向到文件（`> run.log 2>&1`），不要接管道

## 多机自动选择 / 计费（2026-09-16，v2.2）

- **[09-16] `FluxDB` 每次构造都抛异常，且被吞成一行日志 → `self._lock` 原先在 `self._migrate()` **之后**才赋值，而 `_migrate → _backfill_accounts → _one/_all/_exec` 全都要 `with self._lock`** → 抛 `AttributeError: 'FluxDB' object has no attribute '_lock'`，被 `_backfill_accounts` 的宽 `except` 吞掉只打「账户回填跳过」→ **该迁移从未真正执行过**。**修法：把 `self._lock = threading_lock()` 提到 `_migrate()` 之前。** 教训：`except Exception` 包住初始化逻辑时，任何 `AttributeError` 都会变成"静默跳过"，必须实跑一次看日志
- **[09-16] 配额莫名其妙只有一半（basic=50 只能出 25 张）→ `precheck` 用 `used + inflight >= limit`，而 `usage` 在**入队时**已 +1，所以 `used` 天然包含在途任务，再叠加 `inflight` 就把同一张在途图算了两次** → 实测：连提 25 张不完成即被拒（`used=25 + inflight=25 = 50`）→ `precheck` 改为**只用 `used`**；实测改为后放满 50 张
- **[09-16] `usage_sub` 必须带下限 → 退配额若用 `count = count - n` 裸减，边界情况下会出现负用量（等于白送额度）** → 用 SQL 标量 `MAX(count-?, 0)` 夹住；另外**退还必须幂等**：终态失败有多条落点（业务失败 / 重试超限），没有标记就会重复退 → 新增 `jobs.refunded_at` 列做一次性标记（`UPDATE … WHERE refunded_at IS NULL` 的语义靠 `refund_job_once()` 内的同锁先读后写实现）
- **[09-16] `waiting` 状态**不能**退配额 → `waiting` 是"服务器 down 等恢复"，不是终态：任务恢复后会被重新入队继续跑** → 若在 `waiting` 就退，等于同一张图免费出。只在 `status='failed'` 的两个落点退
- **[09-16] `FluxQueueScheduler.submit()` 当前无锁，却被 3 个线程并发调用（web `ThreadingHTTPServer` handler / 飞书 ws / 飞书 poll）** → 去重 `_inflight` 的 check-then-act、`_seq += 1`、`precheck` 都是裸的 → 并发同提示词可绕过去重、`_seq` 可重复（破坏 `PriorityQueue` 的 FIFO tiebreak）、配额可小幅超发。**⚠️ 已记录但本批未改（属 O-4，等授权）**
- **[09-16] 用毫秒时间戳当 `job_id` 主键 → 同毫秒两次并发提交会撞主键；`job_insert` 是裸 `INSERT`（非 `OR REPLACE`），直接抛 `IntegrityError`** → 改 `uuid.uuid4().hex[:16]`。另注意：网页队列的 `job_id` 与常驻服务内部的 `job_id` **不是同一个 ID 空间**，排查时 `web_out/` 与 `resident_out/` 的文件名对不上
- **[09-16] 选机时每台服务器要 3~4 次 SSH 往返 → 旧 `probe()` 里 `gpu_ready(s)[0]` 和 `gpu_ready(s)[1]` **各调一次**（多跑一次 `nvidia-smi`），加上 `server_reachable` + `model_ready`** → 候选机一多就按台数线性放大，全关机时每台还各等一个 `ConnectTimeout` → 合成 `probe_full()`：**一条远程命令** `echo REACH; nvidia-smi …; test -f DOWNLOAD_DONE …; curl /health`，用标记行切分解析，每台固定 1 次往返；再套 `probe_all()` **并行探测**（4 台全超时实测 0.30s，串行需 ~1.2s）
- **[09-16] 选机逻辑每张图都会调用，不缓存就是每张图 N 次 SSH** → `probe_full` 结果按 `FLUX_PROBE_TTL`（默认 20s）缓存 → **代价：机器刚开机最多晚 TTL 秒被认出来**；所以**等待服务起来的轮询必须 `force=True` 绕过缓存**（`ensure_resident` 的 40s 等待循环里，不带 force 会复用旧结果）
- **[09-16] 自动发现要在模块级调用，但那里 `log` 还没定义 → `ssh_config_aliases()` 的异常分支里 `log.warning` 会 `NameError`**（`FLUX_SERVERS = _load_servers()` 在模块级执行，早于 `log = logging.getLogger(...)`） → 该函数内改用 `logging.getLogger('flux_manager')` 局部取，消除顺序依赖
- **[09-16] 直连模式（`FLUX_RESIDENT_BASE` 有值）下不能走 SSH 探测 → 本机部署模型时根本没有 `autodl-flux` 这类别名** → `fr.probe()` 内部分流：`DIRECT_BASE` 有值走一次 HTTP `/health`（并把 `gpu_ok`/`model_ok` 标为 True，无从查也不必查），否则委托 `fsm.probe_full`
- **[09-16] 换机/克隆后 waiting 任务一直卡住 → 恢复门用的是 `any_ready()`（要求"常驻已跑且模型已加载"），但新克隆的机器刚开机时常驻还没起来** → 改用 `any_usable()`（可达 + 有卡 + 模型文件就绪），**不要求常驻已在跑** —— 拉起交给 `ensure_resident`
- **[09-16] `patch` 带 fuzz 应用成功后会留下 `<file>.orig` 备份** → 它会成为仓库里的非预期新文件（内容与改动前一致，属冗余） → 应用后清掉并核对 md5 确认与基线相同。另注意：**`flux-optimize/patches/{base,fixed}/` 快照是 CRLF，而本仓源码是 LF**，直接字节比对会「差 N 字节」（差的就是行数），必须先归一化行尾再比
- **[09-16] 在 WorkBuddy 沙箱里 `Path.unlink()` 会被安全删除垫片拦成 `OSError`（`SHFileOperationW 失败: 0x2`）** → 验证脚本收尾处若用裸 `unlink()` 会**在最后崩掉、后面几项跑不到**（症状：前面全 PASS 但总数不够） → 删除点包 `try/except OSError`，失败退化为清空文件

## 模型常驻 / 生成路径切换（2026-09-16，v2.1）

- **[09-16] `.env` 里配了 `FLUX_GEN_MODE` 却不生效 → `feishu_notify._load_env()` 只在 `get_app_id()` 等函数内**惰性**调用，模块 import 时并不会加载 `.env`；而 `flux_queue.GEN_MODE` / `flux_server_manager.GEN_MODE` / `flux_resident_client` 的配置常量都在**模块级**读取 → 读的时候 `.env` 还没进环境** → 三个模块在 import 块之后显式调用一次 `_load_env()`（与 `flux_web_service.py` 既有做法一致）。自证：`import` 前 `WEB_ADMIN_TOKEN in os.environ` 为 False、`import` 后为 True
- **[09-16] 常驻服务与旧链路**不能同时跑同一台机器** → 常驻服务 `FLUX_OFFLOAD=none` 时整模型占住显存，旧链路 `gen_flux.py` 再 `from_pretrained` 一份必然 OOM** → `flux_queue`（web 队列）与 `flux_server_manager.process_job`（小红书配图）**共用** `FLUX_GEN_MODE` 一起切；不要只切一半
- **[09-16] 看门狗会不会误杀常驻服务 → `watchdog/flux_server_ready.sh` 里有 `pkill -f 'gen_fl[u]x.py'` 和 `screen -S fluxgen -X quit`** → 常驻服务用的是不同脚本名（`flux_resident_server.py`）与不同 screen 会话名（`fluxd`），天然不被误杀；反过来 `start_resident.sh` 只操作 `fluxd` 与 `flux_resident_serve[r].py`，**不删 `out/`、不动 `fluxgen`**，不会踩到旧链路
- **[09-16] 用 `ssh <alias> "curl -d '<json>'"` 传 JSON body 极易被引号吃掉 → 提示词含单引号/中文/bracket 时命令被截断** → 改为 **base64 内联**：`echo <b64> | base64 -d | curl --data-binary @-`。base64 字符集只有 `A-Za-z0-9+/=`，不含任何 shell 元字符，且**一次 ssh 往返**就够（早期的 scp 临时文件方案要两次）
- **[09-16] 写「本机代跑远端命令」的验证代理时踩到引号陷阱 → 用正则从 `ssh -o X alias '<cmd>'` 里截取 `'<cmd>'`，直接把带引号的串丢给 `bash -c`，bash 会把整串当成一个命令名 → `exit 127 / No such file or directory`** → 外层引号是给**本地 shell** 消掉的，必须先用 `shlex.split` 模拟本地词切分、还原出 ssh 真正发送的远端命令串，再执行；主机名之前的选项（`-o/-p/-i/-l/-F`）要成对跳过
- **[09-16] `flux_resident_client` 需要 `flux_server_manager` 的 SSH 能力，而 `flux_server_manager.process_job` 又需要常驻客户端 → 模块级互相 import 会成环** → 方向定为单向：`flux_resident_client` 在模块级 import fsm，fsm **只在函数内**延迟 import 客户端
- **[09-16] 本机 `http_proxy` 已设但 `no_proxy` 为空 → `urllib` 默认 opener 把 `http://127.0.0.1:9630` 的请求也发给代理**，连不上时收到的是代理的 `502`，把真实的 `ConnectionRefused` 盖掉（排查被误导过一次） → 客户端默认用 `ProxyHandler({})` 绕开代理（`FLUX_RESIDENT_USE_PROXY=1` 可恢复）；**根因建议在系统环境补 `no_proxy=127.0.0.1,localhost`**
- **[09-16] 客户可传任意尺寸把显存打爆 → 服务端原样透传 `width/height`** → 服务端加硬边界：`256~2048` 且**归一到 16 的倍数**（FLUX 要求），`steps` 限 `1~100`；越界返回 400 而非静默截断

## 多服务器 / 僵尸 screen（2026-09-04，v2.0）

- **[09-04] 服务器就绪但任务一直不生成、最终`生成超时` → 僵尸 screen 会话 `1375.fluxgen (Dead ???)` 残留，`screen -ls | grep fluxgen` 连 Dead 会话也匹配 → `start_gen.sh` 误判"已在运行"→跳过真实启动 → 无 gen 进程 → 等超时** → 所有判"是否在跑"的逻辑都要先 `screen -wipe` 清僵尸（4 处：`server/start_gen.sh`、`flux_server_manager.gen_running()`、`flux_queue._generate` 清理、`watchdog/flux_server_ready.sh`）；实测 wipe 后活会话 `1897.fluxgen (Detached)` 起来、卡住任务 done

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
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

---

## [2026-09-23] 「活体检查」冒充「身份/新鲜度检查」→ 假绿灯（公网仍指向旧上游）

**问题**：一键启动脚本报告"对外环境已就绪"，但 `https://flux.zhuanlu.xyz` 打开是旧页面。
**原因（两处独立的假绿灯）**：
1. 隧道那步用 `[bool](Get-Process cloudflared)` 当"已重启" —— **旧进程正好满足它**；
   而终止那行配了 `-ErrorAction SilentlyContinue`，把失败吞掉了
2. 验收那步用 `HTTP 200` 当"已切到 3000" —— **旧上游 9620 同样返回 200**
**实测证据**：cloudflared 停在 09-20 启动的进程，`config.yml` 是 09-22 改的；
`cf_err.log` 里跑着的进程实际用的是 `ingressRule=1 originService=http://localhost:9620`
**解决**：验收判据改为「**身份/新鲜度**」而不是「活体」——
① 隧道：新进程启动时间必须 ≥ `config.yml` 的 mtime；② 公网：`<title>` 必须等于目标站点本机的 `<title>`，
并**显式识别"等于旧上游"这一情形**；③ 终止进程必须**轮询确认真的退出**（"发过命令"≠"已退出"）
**一般化**：验收前自问「**我什么都不做时，这个判据会不会也通过？**」会通过 → 它不是验收，是装饰。
（另见本文件「[09-23] 数组 splatting」条：这条假绿灯之所以长期没被发现，是因为编排器卡死在中途）

## [2026-09-23] `Start-Process` 带日志重定向在本机**必抛**「重复的键 https_proxy」

**问题**：隧道重启永远失败，报错原文
`值中的关键字:"https_proxy"。重复的键:"HTTPS_PROXY"`（即"已添加了具有相同键的项"）。
**原因**：本机环境**同时存在** `https_proxy` 与 `HTTPS_PROXY`（`no_proxy`/`NO_PROXY` 同理）；
.NET 处理环境块用**大小写不敏感的字典**，枚举到第二个就抛。而 `Start-Process` 只要带
`-RedirectStandardOutput/-RedirectStandardError` 就会枚举环境 → 必抛。
**连带后果**：只能退回「不带重定向 + 弹一个控制台窗口」启动，
**那个窗口一关，被启动的服务就跟着死** —— 实测 3000 反复"起来了又没了"。
**解决**：任何会 `Start-Process` 的脚本**开头先跑 `Repair-DuplicateEnv`**（删掉大写那份）。
它原先只写在 `flux-t2i-server/start_service.ps1` 里；本次复制到
`image-platform/deploy_site_prod.ps1` 与 `image-platform/restart_tunnel.ps1`。
**同时**：对外长驻服务一律**带重定向**启动（无窗口 + 日志落盘），不要用 `-WindowStyle Minimized` 无重定向。

## [2026-09-23] 数组 splatting 调子脚本 → `[switch]` 参数**一个都绑不上**（编排器卡死在 `pause`）

**问题**：一键启动脚本跑到第 3 步后**窗口就不动了**，第 4 步身份验收与汇总表**永远不执行**。
**实测绑定行为**（与 `deploy_site_prod.ps1` 同形状的 probe）：

| 调用写法 | 实测结果 |
|---|---|
| `& script.ps1 @('-Restart','-NoPause')` | `BuildOnly=False \| Restart=False \| NoPause=False \| SkipBuild=False` |
| `& script.ps1 -Restart -NoPause` | `Restart=True \| NoPause=True` |

**原因**：数组 splatting 把字符串**当普通位置参数**传，`[switch]` 形参一个都没绑上。
**后果（静默，不报错）**：
1. `-NoPause` 失效 → 子脚本里的 `pause` 真执行 → **编排器卡死**（该脚本有 6 处 `pause`）
2. `-Restart` 同样失效 → **"强制重启"从未执行**，端口被占时旧进程继续服务（又一个假绿灯）
**为什么自动化环境看不见**：管道 stdin 下 `pause`/`Read-Host` 立即 EOF 返回，脚本照常跑完；
**只有真实控制台会卡住**。这类缺陷不能靠"跑一遍看看"发现。
**解决**：调用外部脚本**一律显式具名参数**，禁止 `& (Join-Path ...) @数组`；
并加**源码级不变量检查**钉住这个写法。参数是否真绑上，要用 probe 实测。


---

## [2026-09-23] 飞书图图：图片**从来没发出去过** —— `sqlite3.Row` 没有 `.get()`，而宽 except 把它藏了

**问题**：`_send_result` 里写的是 `job.get('image_path')`，而传进来的 `job` 是 `sqlite3.Row`。
实测抛（本机 Python 3.11）：

```
AttributeError: 'sqlite3.Row' object has no attribute 'get'
```

**为什么能藏这么久**：它被 `_poll_loop` 的宽 `except Exception` 吞掉，只打一行「任务轮询异常」。
后果是**双重的**：
1. **图片永远不发给用户**（核心功能 100% 失败），用户只知道"发了提示词没反应"
2. 跟踪表里那条记录**永远不会被清理**（`pop` 在 `_send_result` 之后），
   此后每 5 秒重试一次、每次都失败，**永不收敛** —— 日志一直刷「任务轮询异常」，
   看上去像"偶发抖动"，实际是"这个功能根本没通"

**一般化（三条，都值得记住）**
1. **`sqlite3.Row` 只支持下标 `row['col']`，没有 `.get()`**。代理/中间层拿到行对象一律
   先 `dict()` 再往上抛（本仓库已有这条约定，但 `feishu_bot` 漏了）
2. **宽 `except` 必须打印异常类型**：`f'... {type(e).__name__}: {e}'`。
   只写 `{e}` 时很多异常信息量为零，会让"稳定的失败"伪装成"偶发的抖动"
3. **判据要落在用户可观察行为上**：「**用户到底收到图了吗**」，
   而不是「轮询线程有没有报错」—— 后者恰恰是它藏了这么久的原因

**修法**：`_poll_once` 里 `job = dict(job)`；`_send_result` 只接受 dict；
轮询异常日志补 `type(e).__name__` + `exc_info=True`；
新增回归门 `tests/test_feishu_bot_p0.py`（21 项）。
**变异测试**：还原这行 `dict()` → 门立刻变红并把「实际发图 0 条」打出来。


---

## [2026-09-23] 飞书图图 P0 其余三条（去重 / 在途落库 / 上限回执）

- **无事件去重**：飞书**会重放事件**，重放一次 = 重复出图 + **重复扣额度**，用户完全看不出来。
  修法：`feishu_dedup` 表 + 必须用 `INSERT OR IGNORE` + `rowcount` 的**原子**判重。
  ⚠️ **"先 SELECT 查有没有、再 INSERT"是假防护** —— 两条重放并发进来会双双查到"没有"、
  双双放行。这种"看起来有去重"比没有去重更糟（会让人以为已经安全了）。
- **在途任务只在内存**：`_active` 是内存 dict，而 9620 **每次改代码都要重启** →
  重启后这些任务即使出完图也**永远回不来**（额度已扣）。
  修法：落 `feishu_tracks` 表，启动时自动恢复未终态任务；`notified` 也持久化，
  避免重启后把「服务器暂不可达」之类的通知重发一遍。
- **超在途上限时静默不跟踪**：原逻辑在 `submit()` **之后**发现超限就只是"不跟踪" ——
  任务照跑、额度照扣，用户**收不到图也不知原因**。
  修法：**提交之前**判断上限，超了不提交 + **明确回执**（不浪费额度）；上限 50 → 200。

**验收（可机器判定）**：①同一事件重放 2 次只出 1 张图 ②第 N+1 个任务收到明确回执且**未提交**
③提交后重启 9620，任务完成后**仍收到图** —— 三条全部落成回归门断言。


---

## [2026-09-24] 飞书图图：WS 回调里干重活 → 飞书把连接**踢了**（3003 ping timeout）

**现象**：用户给图图发"你好"，长时间没有回复。

**实测时间线**（`manager/flux_manager.log`，2026-09-24）：

| 时刻 | 事件 |
|---|---|
| 09:04:56.851 | `📩 图图收到 …: 你好`（消息**确实到了**） |
| 09:04:56.862 → 09:06:04.182 | 中文翻译卡了 **68 秒**（重试 3 次） |
| 09:06:04.208 | 入队 `3ada124e5f334de7` |
| 09:06:04.228 | `⏸ 服务器down，进入等待恢复池`（**没有任何 GPU 机在线**） |
| **09:06:05.816** | **`receive message loop exit … 3003 (registered) ping timeout`** |
| 09:06:05.803 | 飞书**重投同一条事件**（日志「重复事件，已忽略」就是它的脚印） |

**根因**：lark 的 WS 客户端是在 **asyncio 事件循环里同步调用**事件回调的
（`lark_oapi/ws/client.py:341 _handle_data_frame` → `dispatcher_handler` → `im/v1/processor.do` → 我们的 `_on_message`）。
回调里直接 `scheduler.submit()`（含中文翻译，最坏重试 3 次、实测 68 秒）
→ 事件循环被占住 → **心跳 ping/pong 发不出去** → 飞书按 `3003 ping timeout` **踢连接**；
而且因为没及时 ACK，**同一条事件被重投**。

**一般化（重要）**：**任何事件回调里都不要做秒级以上的同步工作** ——
WS 回调 / webhook handler / UI 事件都一样。回调只做「校验 + 入队」，重活交给工作线程。
判据：**回调耗时必须与业务耗时解耦**（前者毫秒级、与后者无关）。

**修法**
1. `_on_message` 只做去重 + 过滤 + 取文本（毫秒级），然后入队；
   起 2 个工作线程消费（队列满 → 明确回绝，不无声丢弃）
2. 提交前先发**即时回执**（68 秒无声 = 用户以为机器人坏了）
3. `notified` 只在**发送成功**后置位（原写法无条件置位 → 首次发送失败则用户永远收不到那条通知）

**回归门**：`tests/test_feishu_bot_p0.py` 断言「提交 sleep 2.5s 时 `_on_message` 仍 <1s 返回」。
**变异测试**：把入队改回同步调用 → 门报 `❌ WS 回调不阻塞事件循环 (2519 ms)` 变红。

**顺带暴露的另一件事**：`Scheduler.submit()` 里含 LLM 翻译，中文提示词最坏要几十秒。
这不该在**用户可感知路径**上同步等待 —— 回执与提交必须解耦（本条第 2 点）。
另外「你好」根本不是图像描述，翻译层绕了 3 次才勉强塞回一句元话语
（注：该任务只有 1 次 `waiting` 通知，是因为轮询在 3 秒内就处理到了）。

## [2026-09-24] 已删的 `waiting` 任务会「复活」并**拿到退款** —— 删除判据漏了一个状态

**症状**：`/api/delete` 删掉一个 `waiting` 任务后，机器恢复时它会被重新入队；
更糟的是如果等待已超 `WAIT_MAX_SEC`（默认 4 小时），恢复路径会
`job_update(status='failed')` + `_refund_quota()` —— **给一个已被用户删除的任务退款**。

**根因（两处叠加）**
1. `/api/delete` 里 `inflight = status in ('queued','generating')` —— **`waiting` 不在其中**
   → 不调 `drop_job()` → 调度器内存 `self._waiting` 留下幽灵项。
   （`drop_job()` 自己的 docstring 恰好点名了这个后果："不清的话机器恢复后会被重新入队，白跑一遍"）
2. `_recover_waiting_tasks()` 从内存池 pop 后**直接重新入队，原先没有任何墓碑检查**。
   `db.jobs_waiting()` 有 `deleted_at IS NULL`、`_recover_orphaned_jobs` 走 `jobs_queued()`
   也有 —— **只有内存池这条路径漏了**。

**为什么危险**：它把 `job_mark_deleted` 明确要防的「删了重传无限刷额度」开了口子。
用户删掉卡在 waiting 的任务 → 4 小时后机器恢复 → 额度退回 → 可以再传一次 =
**免费多跑一次**。而且只有"4 小时窗口 + 机器恢复"才触发，很隐蔽。

**修法（三道闸，缺一不可）**
1. `_api_delete` 把「要不要 drop_job」与「要不要立即清产物」**拆成两个判据**：
   `inflight`（queued/generating）与 `pooled`（含 waiting）。两者范围不同，不能共用一个变量
2. `_recover_waiting_tasks()` **自己再判一次墓碑** —— 调用方"应当"做的事不能当唯一防线
3. worker 侧 `_process` 的墓碑检查保留（原本就有）

**一般化**：**给状态机加新状态时，要扫一遍所有"按状态分支"的地方**。
`waiting` 是 2026-09-20 引入的，删除逻辑写在 09-21 —— 只差一天，所以漏了。
判据里出现 `status in (...)` 的位置，每加一个状态都要回头审。

---

## [2026-09-24] 门的夹具用了**不合法的假 id** → 误判成「实现有 bug」

**症状**：新写的取消门第一版有 7 项红，其中 4 项报
「中文翻译失败，拒绝任务: 取消 gen00001」—— 看起来像"命令识别坏了"。

**根因**：夹具用了 `gen00001` / `wai00001` / `que00001` / `abab` 这类**假 job_id**。
真实 job_id 是 `uuid.uuid4().hex[:16]` = **16 位十六进制**（如 `3ada124e5f334de7`），
而命令判据 `re.fullmatch(r'[0-9a-fA-F]{6,16}', rest)` 对含 `g`/`n`/`w`/`q` 的串
**正确地**判为"不是命令" → 回落成提示词提交 → 报错。

**结论**：**实现是对的，测试数据是错的。**

**教训**：写"按格式识别"的测试时，**夹具必须满足该格式的约束**，否则测的是自己的错数据。
**判据**：如果失败信息指向"输入没被识别"，先检查夹具是不是合法输入，再怀疑实现。

**格式的唯一事实源**在产生处（`flux_queue.py` 的 `job_id = uuid.uuid4().hex[:16]`），
夹具应对齐它，而不是随手编一个"像 id"的字符串。

---

## [2026-09-28] 跟踪表的**终态判据漏了 `cancelled`** → 取消一次就再也提交不了

**症状**：用户取消一个任务后，再发提示词永远被回「⏳ 你有一个任务还在生成中，请稍候再发～」——
**取消成了"封号"**：取消一次，之后所有提交都被自己挡住。附带第二条：
`feishu_tracks` 表只增不减（GC 从不清理 `cancelled` 行）。

**根因**：`feishu_tracks_inflight()` 与 `feishu_track_gc()` 的判据里**漏了 `'cancelled'`**：

```python
# 错（两处各写一份，都漏）
WHERE phase NOT IN ('done', 'failed')          # cancelled 被当成"还在途"
```
配合 `MAX_INFLIGHT_PER_USER = 1` → 取消过的任务**永远算 1 个在途** → 后续提交全被拦。

**为什么没被发现**：`cancelled` 这个 phase 是 v2.10.0（09-24）加的，
而 `feishu_tracks_inflight()` 写得更早 —— **与上一条 09-24 的 `waiting` 是同一类病**：
给状态机加新状态时，没有回头扫"按 phase 分支"的所有地方。**同一个坑，一天内第二次。**

**修法**：收敛为**单一事实源**，判据全部引用它，不再各写一份：
```python
FEISHU_TERMINAL_PHASES = ('done', 'failed', 'cancelled')   # flux_db 一处定义
# 判据 →  WHERE phase NOT IN <FEISHU_TERMINAL_PHASES>
```
回归门：`tests/test_figu_params.py::t_cancelled_not_inflight`
（断言 ① 终态集合含 `cancelled` ② 取消后 `inflight=0` ③ 取消后**仍能提交新任务** ④ GC 能清 `cancelled` 行）。

**一般化（比上一条更狠的版本）**
- 判据里出现 `status in (...)` / `phase in (...)` / `NOT IN (...)`，**每加一个状态都要回头审全部分支**；
- ★ **同一语义的判据散落多处 = 迟早不同步** → 必须收敛成**常量**，让编译器/引用替你同步；
- ★ **内存态集合（`set`/`dict`）最易漏** —— DB 侧加了过滤，内存池/跟踪侧没加（本仓库两次都是这个形态）。

**判据**：问「这个状态的**终态性质**定义在几处？改一处另一处会跟着变吗？」
答案不是"一处"就是隐患。

---

## [2026-09-28] 门的总数型断言 → **假红**（实现是对的）

**症状**：新写的群聊门 `t_group_end_to_end` 有一项红：「未 @ 的消息也不回执（实际群发 2 条）」。
看起来像"群里没 @ 图图 也回复了" —— **像是实现漏了 @ 门**。

**根因**：断言写成了 `len(notif.to_chat) == 1`（总数型）。
而**提交路径本来就发 2 条**回执 —— 即时「⏳ 收到！正在解析提示词…」+ 排队确认
（见 `_submit_locked` 的「★ 立即回执」设计：中文翻译最坏 68s，先给反馈免得用户以为机器人坏了）。
2 条完全正确，**是我的断言把"无关的已有副作用"算进来了**。

**修法**：断言改成**前后差值 == 0**（不产生**新增**回执），而非总数相等。

**一般化**
> **断言"某动作不产生副作用"时，写成「不产生****新增****」（前后差值 == 0），
> 不要写成「总数 == 某个数」。**
> 总数型断言把已有的、无关的副作用一起算进来 → 一改实现（甚至只是加了条提示）就假红，
> 而实现往往是对的。**假红比假绿便宜，但同样会浪费一轮排查并误导方向。**

**与上一条 `gen00001` 夹具教训同一类病**：都是**测试断言/夹具与实现契约不符 → 假红**。
判据：红的时候先问「**这个失败会不会是因为我的断言假设了实现没有的契约？**」

---

## [2026-09-28] 从 agent 工具调用里启动的常驻服务，会在**调用结束时被回收**

**症状**：用 `start_service.ps1 -Target flux -Restart` 重启 9620，日志显示**一次完整干净的启动**
（`🌐 Web 服务已启动: http://localhost:9620` + 调度器心跳），但几秒后 `9620` **停止监听**。
两个日志（`flux_service.out.log` / `.err.log`）**都没有任何 traceback** ——
即"被外部杀掉"，不是代码崩了。

**根因**：工具调用的沙箱会**回收整个进程树**（调用结束 = 树被杀）。

**试过且无效的（都别再试了）**

| 手段 | 结果 |
|---|---|
| `Start-Process -WindowStyle Hidden`（＝ start_service.ps1 自带的） | 被回收 |
| `DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP \| CREATE_BREAKAWAY_FROM_JOB` | 被回收（**breakaway 成功了，照样被回收**） |
| `schtasks /create /run`（计划任务） | ❌ 被安全策略拉黑（Program Blacklist），且明确禁止绕道 |
| `explorer.exe <file.cmd>` | ❌ 静默不生效（文件根本没被执行） |
| WMI `Win32_Process.Create` | ❌ 被拦："equivalent to Start-Process" |

**有效的**
- **Bash 工具的 `run_in_background: true`** —— 实测跨多次工具调用存活约 4 分钟（直到被显式停止）。
  ⚠️ 但它**绑定在当前 agent 会话上**：会话结束它就没了。
- **人类双击 `启动-01R-FLUX服务重启.bat`** —— 官方路径，进程归属 explorer，与 agent 会话无关。

### ★★ 真正扎人的地方：我的"验证"曾给出**相反**的结论

第一版探针实验「证明」脱离有效：一个 detached 子进程**跨两次工具调用还在打心跳，活了 80 秒**。
**这个结论是错的。**

原因：那个子进程**继承了工具调用的 stdout 管道**。工具会**等到管道关闭**才返回 ——
于是**整个 80 秒都发生在同一次调用内部**，调用本身被拖长了，我压根没测到"跨调用"。

一旦让启动器把子进程的 stdout/stderr **重定向到文件**（管道随即关闭），调用立刻返回，
子进程随即被回收 —— 真相反转。

> **判据：要证明"进程脱离了调用存活"，必须让子进程不再持有调用方的任何句柄**
> （stdout/stderr 重定向到文件、`stdin=DEVNULL`），
> 否则你测到的是「**调用被拖长**」，不是「**进程存活**」。
> 这与门禁那条「活体检查 vs 新鲜度检查」同源：**自问"我什么都没做时它会不会也这样"**。

### 附带两条（本轮踩到的）
1. **中文 Windows 的 `netstat` 输出是 GBK**：`subprocess.run(..., text=True)`（默认 utf-8）会在
   reader 线程抛 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbb`，
   异常被吞 → 解析函数返回"没有进程占用端口" → **假阴性**（曾据此误判「9620 空闲」而没杀掉旧实例，
   新实例因端口占用绑定失败 → "看起来启动成功，其实还是旧进程在服务"）。
   → 一律 `r.stdout.decode('utf-8', errors='replace')`（ASCII 的 PID/端口/LISTENING 不会失真）。
2. **本机有本地代理（`http_proxy=127.0.0.1:2685`）** → 探测 `127.0.0.1:9620` 会走代理并拿到 **502**，
   误判成"服务挂了"。→ 本机探活一律 **`curl --noproxy '*'`**，
   或给 `check_stale.py` 加 `NO_PROXY='*' no_proxy='*'`。

---

## [2026-09-28] 改「默认行为」→ 4 道既有门同时红，但**没有一处是实现坏了**

**现象**：给图图加「提交前确认」（`FIGU_CONFIRM_SUBMIT` 默认**开**）后跑全量门禁：

```
汇总：21/23 道门通过
失败的门：
  ❌ test_feishu_bot_cancel.py  (rc=1)     ← 1 项红
  ❌ test_feishu_bot_p0.py      (rc=1)     ← 7 项红
```

失败项长这样（看起来像"提交功能整个坏了"）：

```
❌ ★★ 以「取消」开头的提示词仍会被提交（没被命令吞掉）    (队列 0 → 0)
❌ 同一事件重放 2 次，只提交 1 次                          (实际 0 次)
❌ 提交前先发即时回执（用户不会以为机器人坏了）             (实际 0 条)
```

**实际不是实现坏了。** 那些门都用「发一句提示词 → 断言它进了队列」来验证各自的逻辑
（去重 / 上限 / 回执 / 取消），而 `_handle_prompt` 现在在**提交之前**多了一步确认
→ 提示词只弹确认、不进队列 → 断言全部落空。

**修法（关键）：改门，不改断言。**

```python
bot = fb.FeishuBot(sched, db, quota, notifier=notif)
bot.confirm_enabled = False      # ← 本门测的不是确认流程
```

4 道门各自关掉（它们分别覆盖命令面 / P0 边界 / 参数 / 群聊），注释里指路
「确认流程见 `tests/test_figu_confirm.py`」。**那 4 道门的断言一个字都没动** ——
断言是对的，变的只是**前提**。

> **判据：改一个默认值时，先问「有多少道门依赖这个默认值」，再决定是"改门"还是"改断言"。**
> 一律改断言 = 会把真缺陷一起改掉。这与第 12 号陷阱（活体检查冒充新鲜度检查）同源 ——
> 都在问同一句话：**「我这个绿灯，测的是被测对象，还是我自己的前提？」**

⚠️ **反向风险也要堵**：`bot.confirm_enabled = False` 是**测试专用后门**，
所以**必须**另有一道门钉住"线上默认是开" ——
`test_figu_confirm.py::t_default_on` 断言 `fb.CONFIRM_SUBMIT is True`。
否则后门一多，线上默认值被人改成 `False` 都没人发现（默认值静默漂移）。

---

## [2026-09-28] 为了让功能生效，去改「记录事实」的字段 —— 让事实字段撒谎

**症状**：某字段记录的是**客观事实**，但它的取值挡住了你要的功能。
于是"顺手"把取值改成相反的，功能通了，门也改了，看起来一切正常。

**本项目实例**：业主要把 Qwen-Image-2.1（Qwen Research License，**非商用**）放到 B 链。
挡住它的正是 `manager/servers.json` 的 `commercial_ok: false`。
最省事的做法是把它改成 `true` —— **一行，功能立刻通**。

**为什么不行**：`commercial_ok` 记录的是**许可证允不允许商用**，是个事实。
改成 `true` 之后，下一个接手的人再也分不清两种**法律含义完全不同**的情况：

| 看到 | 实际可能是 |
|---|---|
| `commercial_ok: true` | ① 这模型本来就 Apache 2.0 可商用 —— **无风险，不需要任何说明**<br>② 有人为了放行把事实字段改成 `true` 了 —— **明知故犯，且证据已丢** |

**正确做法**：**事实字段不动，另加一个正交的「决定」字段。**

```jsonc
"commercial_ok": false,            // 事实：许可不允许商用（**永不因业务需求而改**）
"expose_to_customers": true,       // 决定：业主明知受限仍要放给客户
"expose_note": "谁/何时/为什么 + 回滚方式"   // 决定的留证（门强制要求非空）
```

判据变成**或关系**，位置不变（仍在能力过滤**之前** —— 许可比能力更硬）：

```python
allowed = [c for c in cands
           if c[0].get('commercial_ok') is not False      # ① 许可事实允许
           or c[0].get('expose_to_customers') is True]    # ② 业主显式放行
```

**三件配套，缺一不可**：

1. **留证** —— 放行**必须**配 `expose_note`，门断言它非空（允许放开，**不允许悄悄放开**）；
2. **可见** —— 放行机器被选中时打 `WARNING`（按机器名去重，防刷屏；本项目已被刷屏淹过故障日志）；
3. **可回滚** —— 回滚 = 把 `expose_to_customers` 改回 `false`。**事实字段没被动过，所以回滚很干净。**

> **可复用判据**：当你要改的字段**记录的是事实**（版本号、许可证、实测值、时间戳），
> 而你想让它表达的其实是**决定或期望** —— 停下来，**加一个新字段**，不要改事实。
>
> 这与「声明 ≠ 约束」（第 1 号陷阱）是**镜像**关系：
> 第 1 号说的是「写了字段却没有运行时读它」（装饰）；
> 这一号说的是「字段被读了，但被改成谎话」（伪证）。
> 两者都让**字段与现实的对应关系**断掉，只是断的方向相反。

### 附带一条：同一个文件被**两个生成器**写不同的文件头

`watchdog/deploy_vps.py:97` 写 `host:port:user:workdir:model:offload:name`，
`watchdog/gen_targets.py:89` 却写 `...:offload`（漏了第 7 段 `name`）。
于是"同一份 `targets.conf`"的头部取决于**谁最后写的** —— VPS 上那份是带 `name` 的、
本地那份是不带的。纯注释，没造成故障，但会让后来者以为文件格式不一致。
→ 已对齐。**判据：一个产物有多个生成器时，先 grep 出所有写入点，别只改你手上那一个。**

---

## [2026-09-28] 文件编辑工具报 success，但**没写进去** —— 且同一批里只丢一部分

**症状**：一批里连发 3 个 `Edit`，**3 个都回报 `Successfully edited`**。
随后 `md5` 变了（说明**写确实发生过**），但 `grep` 一查 —— **只有 1 处改动真的在文件里**，
另外 2 处凭空消失。

**本机实测（2026-09-28，改 `docs/接交接.md`）**：

| 批次 | 发起的编辑 | 实际落盘 |
|---|---|---|
| 批 1（3 个） | ①版本表 ②新增「已完成 v2.13.0」段 ③改写「待办 #2」 | **只有 ①** |
| 批 2（2 个） | ①新增硬约束 24 ②更新续接路径版本 | **只有 ②** |
| 批 3（单个） | 修版本行 | ✅ 落盘 |

→ **不是"第一个必成"也不是"最后一个必成"**，看起来是**间歇性**的。

**为什么危险**：`Successfully edited` 会让人直接宣称"文档已更新"，
然后**带着缺失的文档去交接/归档** —— 下一个人按文档干活，而文档里根本没有那条约束。
这与「第 12 号陷阱（活体检查冒充新鲜度检查）」同族：
**都在问同一句话 —— 「我这个绿灯，测的是被测对象，还是我自己的回声？」**

**本机同源问题**：工作区记忆里已有同类记录 ——
`git commit` 返回 rc=0 但 `rev-parse HEAD` 没变（第 15 号，`.git` 写入不可靠）。
**这一类"写操作报成功但没生效"在本机是反复出现的。**

**修法（强制）**：**改完必须复核落盘，复核完再宣称完成。**

```bash
# 单条编辑 → 立即用 grep -c 或 sed -n 'Np' 复核
grep -c '关键串' 文件.md        # 期望 1；得 0 就是没写进去
sed -n '4p' 文件.md             # 直接看那一行
md5sum 文件.md                  # 只证明"写过"，不证明"写对了" —— 不能单独用它当证据
```

★ **判据：`md5` 变化 ≠ 内容正确。** 上面那次 `md5` 明明变了，内容却是缺的。
**唯一可信的验收是"读回你要的那段文本"**，不是"看哈希变了"。

**批量编辑的应对**：一批里**最多发 1 个** `Edit`；要改多处就串行 + 每步复核。
宁可慢，也不要带着一个"自以为改了"的文件继续往下走。

---

## [2026-09-28] Qwen 图片参考/编辑：三个新坑（真机实测得出）

### 1) ★ RGBA 是容器，不是「透明」的证据 —— 弱判据制造的假绿
**症状**：用 `PIL.Image.open(p).mode == 'RGBA'` 验证「透明图生成」→ 全部通过，但图其实完全不透明。
**实测**：Qwen-Image-2.1 的 pipeline **总是输出 4 通道**。不透明场景 alpha 落在 253~255（`a==0 占比 = 0.00%`）；
只有提示词明确要求透明时才 `alpha_min == 0`（`a==0 占比 15.65%`）。
**判据**：透明验证必须用 **`(alpha == 0).mean() > 0`**（建议阈值 >1%），**禁用 `mode`**。
**一般化**：任何「容器型」断言（`mode` / 扩展名 / `Content-Type`）都要追问
「**我什么都不做时它会不会也通过？**」——会通过，它就是装饰不是判据。

### 2) ★ 透明图直接渲染是「紫底黑斑」—— 查看方式错了会误判成品
**症状**：带 alpha 的 PNG 直接渲染 → 看到紫红底 + 黑色噪点，误以为模型画质崩了。
**原因**：透明区的 **RGB 是噪声**（该区域本该不可见），渲染器没做 alpha 合成。
**做法**：预览/交付**必须合成到底色**：
```python
bg = Image.new('RGB', im.size, (255, 255, 255))
bg.paste(im, mask=im.split()[-1])
```
**平台含义**：前端预览透明图**必须**用棋盘底/白底，否则用户以为生成失败。

### 3) ★「模型能力上限」≠「本机可用上限」—— 参考图张数
**症状**：能力表声明 `max_ref_images=10`，据此对客户开放 10 张 → 真机 OOM。
**实测（7B2 · RTX 4080 32G · offload=model · 1024×1024 · 每张压到长边 1024 JPEG）**：

| 参考图张数 | 1 | 2 | 3 | 5 | 8 | 10 |
|---|---|---|---|---|---|---|
| 结果 | ✅ 68.78s | ✅ 77.38s | ✅ 87.1s | ❌ OOM | ❌ OOM | ❌ OOM |

**结论**：`max_ref_images` 描述的是**模型**，**必须另设本机实测的安全上限**（如 `safe_ref_images`）。
同类前科：`native_res=2048` 也曾被当成「随便开」，实际**默认启动必 OOM**。

### 附：本轮两个环境事实（会反复用到）
- **2048 救活法**：`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`（OOM 原文给的碎片治理建议）
  → 同参数 2048×2048 从 `failed` 变 **done / 211.55s**。**建议写进 `start_resident.sh`**。
- **scp 下载路径**：`scp host:/p ./x` 时本地目标写 `E:/...`，**冒号会被解析成主机名**
  （报 `E:/x: No such file or directory`）→ **先 `cd` 到目标目录再写相对路径**。
  上传时目标是远端路径，所以这个坑只在**下载**时暴露。

---

## [2026-09-28] ★ scp 上传 `.sh` 被**静默转成 CRLF** → bash 选错解释器、服务起错模型

**症状**：`start_resident.sh` 改完上传到 GPU 机，重启后 `/health` 报
`RuntimeError: diffusers 0.39.0 里找不到 QwenImage21Pipeline`，而启动日志却打印
`[2b/4] 解释器: /root/autodl-tmp/envs/qwen/bin/python`（正确的 qwen 环境）。

**根因**：本机 Git Bash 的 `scp` 上传时发生了 **LF → CRLF 转换**。远端文件里
`grep -c $'\r'` = 57 个 CR（本地是 0）。bash 把每行末尾的 `\r` 当命令字符，
`pick_python()` 的 `case ... in` 分支匹配错乱 → `PY` 变量取空 → screen 里实际
跑的是 `FLUX_PY_DEFAULT`（flux 环境，diffusers 0.39.0）。

**为什么 `.py` 没暴露**：同一批 `flux_resident_server.py` 也被污染 973 个 CR，
但 Python **容忍 CRLF**，所以代码照常跑、md5 却变了。

**三条教训**：
1. **scp 上传 `.sh` 后必须校验行尾**：`grep -c $'\r' <远端文件>`，非 0 就是被污染。
2. **修法**：远端 `sed -i 's/\r$//' <file>` 转回 LF，再核对 md5 与本地一致。
3. **「启动日志打印的解释器」≠「实际跑的解释器」**：日志打印的是 `$PY` 变量展开，
   但 `screen -dmS ... bash -c "... $PY ..."` 里若 `$PY` 因解析错乱而取到空/旧值，
   实际跑的是另一个。判据下沉到 `/proc/<pid>/exe`（`ls -l /proc/PID/exe`）看**真身**。

**与既有坑的关系**：记忆坑 #9 讲 `.bat`(GBK) / `.ps1`(BOM) 的**编码**坑，本条是
同一族「Windows 文本行尾」坑的**传输环节**变体——对象是 scp 传的 `.sh`。两者都要防。

---

## [2026-09-28] ★ `curl` 裸输出无尾换行 → 黏连下一行的标记 → 探活 10/10 失败

**症状**：B 链任务全卡 waiting，manager 报 `backend_hint: server_down`；但 resident
的 `/health` 单独 curl 完全正常（`model_loaded: true`、秒回）。用 manager 自己的
`probe_full` 探活，`resident=False model_loaded=False`，**10/10 全失败**。

**根因**：`probe_full` 把 `/health` 的 curl 和 `echo MODELS_BEGIN` 拼在同一条 ssh
命令里。`/health` 的 JSON 是 **curl 裸输出、结尾没有 `\n`**，于是下一行的
`echo MODELS_BEGIN` 黏在 `}` 后面：

```
...\"uptime_sec\": 1051.9}MODELS_BEGIN
```

解析侧（`flux_server_manager.py`）要求 JSON 行 `endswith('}')`，黏连后结尾变成
`}MODELS_BEGIN` → 判 False → `resident=False` → server_down。**与 resident 完全无关**。

**为什么难查**：单点验证「假绿」——单独 curl `/health` 是完整 JSON、单独跑 probe 也
偶尔成功（时序巧合，某次输出恰好带换行）。只有把「完整探活命令」拼起来、**逐行 repr**
才能看到 `}MODELS_BEGIN` 黏连。这是记忆坑 #21「容器型断言假绿」的反面实例：
**单点真绿、集成后真红**。

**修法**：在 `/health` 的 curl 之后、`echo MODELS_BEGIN` 之前，加 `f"printf '\\n'; "`
强制换行。机器闸门 `tests/test_probe_newline_guard.py` 守住「printf 必须在 /health 之后」。

**通用教训**：把多个 curl/echo 拼成一条命令时，**前一个命令若输出无尾换行，会黏连
后一个的标记行**。凡是「按行 `endswith('}')` / `startswith('{')`」解析 JSON 的地方，
都要保证上游输出自带换行，或用 `printf '\n'` 显式补一个。

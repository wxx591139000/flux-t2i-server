# 归档摘要 — 界面手动选 FLUX 模型（请求 D）+ VPS 看门狗 active 收敛（请求 E）

> 归档日期：2026-09-21 17:15 · 仓库：`wxx591139000/flux-t2i-server`（A链 / 生图服务器）
> 关联仓库：`wxx591139000/image-gen-site`（B链 / 客户生图网站）
> 本次两个提交：`flux-t2i-server@64c4945`、`image-gen-site@98684ba`（均已推送 main）

---

## 一、本次做了两件事

| 请求 | 一句话 | 涉及端 |
|---|---|---|
| **D** | 文生图时可以在界面手动选择用哪种 FLUX 模型 | GPU 常驻服务 + manager + 站点前端 |
| **E** | 多台 GPU 机只开一台，由 VPS 看门狗维护「当前使能哪台」，A/B 链只扫开机的这台 | VPS 看门狗 + manager |

### 请求 D：界面手动选模型

**用户怎么用**：站点 `/create` → 展开「高级选项（模型 / 质量 / 固定种子 / 负向提示词）」→
「出图模型」一行出现若干个按钮：

- **自动**（默认）= 不传 `model`，用 GPU 上当前已加载的模型，**零切换开销**
- 各模型按钮（名字来自 `/api/models`，**前端不写死清单**）
  - 标注 `已载` = 该模型当前已在显存里，选它和「自动」等效
  - 标注 `未就绪` = 模型文件缺失，选了大概率失败
  - **图生图模式下非 klein 系列自动置灰**并标「不支持图生图」
- 选了非当前模型时，下方提示「切换模型需要重新加载权重，本批任务会先排队等待（数十秒级）」

代码位置（实测行号）：`image-gen-site/app/create/page.tsx`
- 渲染：**944–1010**（`{models.length > 0 && (` 起，到 `)}` 止）
- 拉清单：`refreshModels` **349–363**

**真机实测**（flux6，13:54 关机前抢跑）：

| 指定模型 | 观察到的切换 | 出图 | 运行时长 |
|---|---|---|---|
| `FLUX.1-dev` | klein→dev，队列切换 | ✅ done seed=999 | 38.6s（25 步） |
| `FLUX.2-klein-4B` | dev→klein | ✅ done seed=999 | 13.6s（4 步） |

**同一 seed 下时长差 2.8 倍 = 采样参数画像按模型生效的证据。**

⚠️ 采样参数是**模型属性**不是全局常量：klein 4B 是 step-distilled，官方固定
4 步 + guidance 1.0；旧代码 `DEFAULT_STEPS=25` / `DEFAULT_GUIDANCE=3.5` 是
FLUX.1-dev 时代遗留，**从部署第一天起就在静默劣化质量**。现在由
`server/flux_resident_server.py:MODEL_PROFILES` 按 pipeline 类名分发。

### 请求 E：active 收敛

**为什么做**：现状 `enabled: true` 的语义是「永远探活这台」。用户同一时刻只开一台，
于是多数条目 `true` 却离线 → **每轮选机都要为每台关机机白等一次 SSH 超时**。

**现在的语义**：`enabled: false` = 「这台永久停用（已释放）」的人工开关（不变）；
`active` = 「VPS 探测到这台此刻活着」的自动事实（看门狗维护）。两者正交。

**架构**：看门狗跑在 VPS 上每 60s 巡检，**它是唯一知道谁在线的组件**，
把事实写 `/opt/flux-watchdog/active.json`；manager 每轮直读（TTL 20s 缓存），
`probe_all()` 第一动作就是 `filter_active()` 收敛候选集 —— 关机机器**零 SSH 开销**。

```
VPS 看门狗 (每60s巡检) ──写──> /opt/flux-watchdog/active.json
                                        │
                                   manager 直读 (TTL 20s)
                                        │
                              filter_active() 收敛候选
                                        │
                              probe_all() 只探开机的
```

---

## 二、★ CRLF 陷阱（真机部署后立即踩到，已定案）

**症状**：active.json 里出现 `"flux1\r"`（带控制字符）→ manager 用 `'flux1'` 匹配
**永远不中** → 候选集为空 → 「机器明明开着却不出图」+ **全程零报错**。

**根因**：`targets.conf` 由 Windows 侧 `gen_targets.py` 写出，默认行尾 CRLF；
远端 bash 的 `read` 把 `\r` 留在**最后一个字段**（= name）。

**三处加固**（任一处单独失守都能拦住）：

1. 生成端：`write_bytes()` 而非 `write_text()`，强制 LF
2. 看门狗：`line=${line%$'\r'}` 剥行尾；name 段再剥一次防御
3. manager：`active_names()` 里 `str(n).strip()` 归一

**验证**：真机 `cat -A /opt/flux-watchdog/targets.conf` 显示干净的行尾 `$`（无 `^M`）。

---

## 三、改动清单

### A链 `flux-t2i-server` @ 64c4945（+702 / -15，7 文件）

| 文件 | 改动 |
|---|---|
| `manager/flux_server_manager.py` | +154：`watchdog_cfg()` / `read_active()` / `active_names()` / `filter_active()`；`probe_all()` 收敛；`known_models()` |
| `manager/servers.json` | +17：顶层加 `watchdog` 块（ssh / active_path / ttl_sec）+ active 语义说明 |
| `watchdog/flux_watchdog.sh` | +96：`write_active()`（原子写 tmp+mv）、迟滞计数 CONFIRM=2、离线立即 `rm -f` 计数、CRLF 归一 |
| `watchdog/gen_targets.py` | +12：targets.conf 加第 7 段 `name`；强制 LF |
| `watchdog/deploy_vps.py` | +11：targets.conf 改写字节（LF） |
| `tests/test_active_server.py` | **新建**，45 项 |
| `tests/test_watchdog_offline.py` | +10：第 7 段断言（原来写死 6 段） |

### B链 `image-gen-site` @ 98684ba（请求 D）

`lib/providers/types.ts`（`ModelInfo` / `GenerateRequest.model`）、
`lib/providers/flux.ts`（`listModels()` + 透传）、
`app/api/models/route.ts`（**新建**，永不 503）、
`app/api/generate|edit/route.ts`（解析 model）、
`app/create/page.tsx`（模型选择器 UI）、
`tools/flux-stub-server.js`、`tools/offline-e2e-check.js`（+9 断言 → 63/63）

---

## 四、验收状态（截至归档时刻的真实观测）

| 项 | 结果 |
|---|---|
| A链回归门 | **13/13 全绿**（`py -3.11 tests/run_all.py`，含新建 `test_active_server.py` 45 项） |
| B链离线验收 | **63/63**（`node tools/offline-e2e-check.js`） |
| B链 `tsc --noEmit` / `next build` | 干净 / 通过 |
| VPS 看门狗 | `systemctl is-active` = **active** |
| VPS active.json | 真机输出 `{"active":[], ...}`（三台全关机，准确） |
| targets.conf 行尾 | 干净 LF（`cat -A` 验证，无 `^M`） |
| manager 直读 VPS | ✅ `read_active()` 拿到数据 |

### ⚠️ 但有一项**尚未生效**（必须处理）

**本机 manager（9620）进程比代码老**：

```
进程启动:  16:33:48  (web_uptime_sec = 2391.8)
代码 mtime: 16:57     (manager/flux_server_manager.py)
```

Python 进程 import 时把代码读进内存，之后**不再读盘**。所以跑着的 9620
**不认识** `filter_active` / `active_names` / `watchdog_cfg` ——
端到端测试起的是新进程，天然测不出这个问题（这是本工作区铁律 #1）。

**处理**：重启 manager（9620）。见下一节「待办」。

---

## 五、待办（按优先级）

### 1. 重启 manager 才能让 active 收敛真正生效 ⚠️ 最高优先级

工具里起的后台进程**回合结束就被回收**，必须**用户在自己终端**启动：

```bash
# 用项目根目录的启动脚本，或直接在已开的窗口里 Ctrl+C 后重跑
E:\myClaudCodeWorkspace\image-platform\启动-生图平台.bat
```

重启后自检（uptime 应该是个位数秒）：

```bash
curl -s --noproxy '*' http://127.0.0.1:9620/health | python -c "import sys,json;d=json.load(sys.stdin);print('web_uptime_sec =',d['web_uptime_sec'])"
```

### 2. 真机端到端实测（需要用户开一台机器）

当前三台全关机，`active=[]`。**开 flux5 或 flux6**，等约 2 分钟
（CONFIRM=2 轮 × INTERVAL=60s），然后：

```bash
py -3.11 -c "
import sys; sys.path.insert(0,'E:/myClaudCodeWorkspace/flux-t2i-server')
sys.path.insert(0,'E:/myClaudCodeWorkspace/flux-t2i-server/manager')
import manager.flux_server_manager as fsm
fsm.clear_active_cache()
print(fsm.read_active(force=True))
"
```

期望 `active` 里有那台机器名。然后在站点触发一次出图，确认选的是 active 那台。

### 3. 遗留（上次已提出，仍待定夺）

- **flux1 / flux5 是 `enabled: true` 但停机** —— 现在有了 active 机制，
  关机机已不参与探活，这条的紧迫性下降；但仍建议对**已释放**的机器显式标 `false`。
- **9620 的 `/health` 仍不透传 `profile`**（只有直查 GPU:9630 看得到）
- **站点 3000 当前未启动**（归档时刻 `curl` 返回 000）

### 4. 模型许可硬约束（别踩）

**klein 4B = Apache 2.0 可商用**；**klein 9B / 9B Base / FLUX.2 dev = FLUX NCL 非商用**。
做对外经营的服务，**本地模型只有 4B 系列能合法用**。
**不要在 9B/dev 上做质量对比，许可就排除了**。详见 `docs/模型选型评估.md`。

---

## 六、关键不变式（后来的重构别顺手改掉）

1. **三态语义不能合并**：`active_names()` 返回 `None` / 空集 / 有值 ——
   `None` = 读不到事实 → 降级全量扫描（可用性优先）；
   空集 = 拿到了事实且确实一台没开 → 返回 `[]`（准确答案，不许瞎猜成全量）。
2. **离线立即剔除，新增需 CONFIRM=2 轮** —— 用户明确要求前者；后者防 SSH 抖动
   导致「机器好好的，manager 恰好那一瞬来读就看到零候选」。
3. **`targets.conf` 必须 LF** —— CRLF 会让 name 带 `\r`，静默失效且零报错。
4. **改 `server/` 下常驻文件 → 第一动作是 `deploy_vps.py`**（更新 VPS mirror），
   不是 scp 到 GPU 机。看门狗每 60s 用 mirror 覆盖远端，先 scp 会被打回。
5. **英文提示词直传，一个字符都不改**；只有含中文才走翻译层
   （单点在 `flux_queue.submit()` 的 `has_chinese()` 判断）。

---

## 七、常用命令（一键可达）

```bash
# A链：全部回归门（13 道，~34s）
py -3.11 tests/run_all.py

# A链：只看 active 那一门（45 项）
py -3.11 tests/test_active_server.py

# A链：看候选机 + 当前 active 收敛结果
py -3.11 manager/flux_resident_client.py servers

# A链：单台 GPU 机完整验收（11 项）
py -3.11 manager/acceptance_gpu.py --server flux6

# 看门狗：部署（改完常驻文件第一动作就是它）
py -3.11 watchdog/deploy_vps.py

# 看门狗：新克隆实例开机后装公钥（它用自己的 key，不是本机 key）
py -3.11 watchdog/authorize_key.py

# 看门狗：零成本判据自测（不用开机）
ssh vps-aliyun 'bash /opt/flux-watchdog/selftest_ready.sh'

# B链：离线验收（63 项）
cd E:/myClaudCodeWorkspace/image-platform/image-gen-site && node tools/offline-e2e-check.js
```

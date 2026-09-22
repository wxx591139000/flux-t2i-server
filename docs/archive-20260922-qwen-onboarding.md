# 归档摘要 — FLUX 文生图服务 · Qwen-Image-2.1 接入（异构三模型）

> 归档日期：2026-09-22 · 仓库：`wxx591139000/flux-t2i-server` 分支 `main`
> 关联仓库：`wxx591139000/image-gen-site`（本轮未改动）
> 前置归档：`docs/archive-20260921-active-model.md`（文生图按任务指定模型）

## 一句话摘要

把平台从「FLUX 专属」升级为「**异构三模型**」：同一套 manager 可调度
FLUX.1-dev / FLUX.2-klein-4B / Qwen-Image-2.1，**能力由上游按类名声明、manager 按能力选机**，
不再在代码里散落 `if 模型名 == ...`。

---

## 一、核心架构：三层能力声明

这是本轮最重要的设计决定，也是后续所有改动的支点。

| 层 | 位置 | 声明什么 | 为什么放在这 |
|---|---|---|---|
| 第 1 层 | `manager/servers.json` | **哪台机装了哪些模型**（`extra_models[]` + `remote_model`） | 机器属性，换机只改这一个 JSON |
| 第 2 层 | GPU 机 `server/flux_resident_server.py` | **每个模型能干什么**（`MODEL_CAPABILITIES`） | 能力来自 `model_index.json` 的 `_class_name`，是实测事实 |
| 第 3 层 | `manager/flux_resident_client.py` | **按能力过滤选机**（`_filter_by_caps`） | manager 不持有模型知识，只做匹配 |

**指导原则**：**让能力成为上游声明的数据，而不是散落三处的 `if-else`**。
前一轮（`ff71df4`）已把「支持编辑」从硬编码搬成声明，本轮把这套办法推广到全部能力维度。

---

## 二、统一能力表 `MODEL_CAPABILITIES`

替换原来的单布尔位 `MODEL_EDIT_CAPABLE`（一个布尔位无法表达「原生 2K」「最多 10 张参考图」这类事实）。

字段（=`CAPABILITY_FIELDS`）：

```
text2img, edit, multi_ref, mask_param, transparent,
max_ref_images, native_res, cfg_param, ref_param
```

三个模型的取值（实测为依据，非照 README 推断）：

| 模型 | `_class_name` | text2img | edit | multi_ref | mask_param | max_ref_images | native_res | cfg_param |
|---|---|---|---|---|---|---|---|---|
| FLUX.1-dev | `FluxPipeline` | ✅ | ❌ | ❌ | ❌ | 0 | 1024 | `guidance_scale` |
| FLUX.2-klein-4B | `Flux2KleinPipeline` | ✅ | ✅ | ✅ | ✅ | 多张 | 1024 | `guidance_scale` |
| Qwen-Image-2.1 | `QwenImage21Pipeline` | ✅ | ✅ | ✅ | ❌ | 10 | **2048** | `true_cfg_scale` |

**返回给前端的白名单**（`/models`）：条目含 `capabilities`；
**刻意不外传 `path`** —— 那是 GPU 机绝对路径，对前端毫无用处且属内部拓扑信息。

---

## 三、★ 本轮最重要的一次修正：`mask` → `mask_param`

### 事故经过

能力表最初给 Qwen 写了 `'mask': True` —— 依据是 Qwen README 里
「specify local edits via circles, painted annotations, or separate masks」。

**实测证伪**：拉 `QwenImage21Pipeline.__call__` 的 `inspect.signature`，
**23 个参数里没有 `mask`**。Qwen 的局部编辑是**语义级**的 ——
把圈选/涂抹**合成进参考图**后走 `image=` 传入，README 那句话描述的是**输入图形态**，
不是 API 入参。

### 修法（四件事一起做）

1. **字段改名 `mask` → `mask_param`**，含义精确到 API 形态：「**有独立 mask 入参**」。
   原名混论了两个命题（「支持局部编辑」/「有独立掩码入参」），将来必然有人按错的那个读。
2. **Qwen 标 `mask_param: False`**，并在代码里附实测依据注释。
3. **worker 判据改为「能力表 + 签名」两者都满足**，且报错要**给出可行动的替代方案**：
   ```
   若要用 Qwen 做局部编辑，请把圈选/涂抹**合成进参考图**后再走 image= 传入
   （Qwen 走语义级局部编辑，没有 mask 入参）。
   ```
4. **`StubPipeline.PIPELINE_SIGNATURES['QwenImage21Pipeline']` 同步修正**：
   去掉错误的 `mask`，补上 Qwen 独有的 `output_resolution` / `use_kv_cache` /
   `prompt_embeds_mask` / `negative_prompt_embeds_mask`。

### 一般化教训

> **「支持某功能」≠「有对应入参」。**
> 摘要（README / 能力表）会漂移，签名 / 文件 / 版本号不会。
> 声明与一手事实冲突时**以一手事实为准，且要显式报错**（不静默丢弃）。

---

## 四、`cfg_param` 参数名适配

FLUX 用 `guidance_scale`，Qwen 用 `true_cfg_scale`。

**不能两个都塞进 `cand`** —— `inspect.signature` 过滤虽然能丢掉不适用的那个，
但会产生**误导日志**「已丢弃: ['true_cfg_scale']」，让下一个人以为模型缺能力。

做法：能力表显式声明 `cfg_param`，`_generate()` 里
`cfg_name = caps.get('cfg_param') or 'guidance_scale'`。

---

## 五、★ Qwen 负向提示词陷阱（必须记住）

Qwen 官方 docstring 原文：

> `negative_prompt` ... **Ignored when `true_cfg_scale` is not greater than 1**

而 `true_cfg_scale` 默认 **1.0**，且官方明确「Qwen-Image 2.1 is meant to be sampled
without guidance」。

**推论**：默认参数下传 `negative_prompt` 等于没传。用户若反馈「负向提示词没用」，
先查 `true_cfg_scale` 是否 > 1，而不是怀疑代码丢了参数。

已写入 `MODEL_PROFILES`：
```
Qwen-Image-2.1：官方默认 40 步 / CFG 1.0（参数名 true_cfg_scale）...
```

---

## 六、`start_resident.sh` 按模型选解释器

### 为什么必须做

klein 用的 flux 环境是 diffusers **0.39.0**，而 Qwen 需要 **0.41.0.dev0**。
**同一个环境不可能同时满足** → 必须独立环境 → 启动脚本必须会挑解释器。

### 判定优先级

**显式 `FLUX_RESIDENT_PY` > `model_index.json` 的 `_class_name` > 路径名（大小写归一）> 兜底 flux**

```bash
case "$cls" in Qwen*) need_qwen=1 ;; esac
case "$(printf '%s' "$MODEL" | tr 'A-Z' 'a-z')" in *qwen*) need_qwen=1 ;; esac
```

### 三个刻意的设计决定

1. **大小写归一用 `tr`**：原先写 `*[Qq]wen*` 只覆盖 2 种组合，`qWen` / `QWEN` 会漏。
2. **路径抽成变量**（`QWEN_PY_CANDIDATES` / `FLUX_PY_DEFAULT`）：
   原先判定逻辑里直接写路径 → 4 处重复 = 「改一处漏一处」。
   现在判定表是**唯一同步点**，`pick_python` 体内无任何硬编码环境路径。
3. **候选全不存在时返回首选路径而非静默退回 flux**：
   静默退回会把「环境没装」伪装成看不懂的 import 错误。

新增日志行 `[2b/4] 解释器: $PY` —— 启动时一眼看到用的是哪个环境。

---

## 七、改动清单

| 文件 | 改动 |
|---|---|
| `server/flux_resident_server.py` | `MODEL_CAPABILITIES`（三模型全维度）；`FALLBACK_CAPABILITIES`；`StubPipeline.PIPELINE_SIGNATURES` 补 Qwen 档；worker mask 分支改双判据；HTTP 入口 mask 闸改用 `mask_param`；`MODEL_PROFILES` 加 Qwen；`scan_models()` 与 `/models` 白名单加 `capabilities`；`_generate()` CFG 名适配；多图分支按 `ref_param` 选参数名；`_health()` 带 `capabilities` |
| `server/start_resident.sh` | 新增 `pick_python()` + 变量化的候选路径 + `[2b/4]` 日志 |
| `manager/servers.json` | 新增 **flux7** 条目（Qwen 专用机）；`remote_model` 指向**数据盘** `/root/autodl-tmp/qwen-models/Qwen-Image-2.1`；note 修正（`+ 透明(RGBA)`，并注明**无独立 mask 入参**） |
| `manager/flux_resident_client.py` | `_filter_by_caps` / `_caps_of` / `find_available_server(need_caps=)`；注释更新 `mask` → `mask_param` |
| `manager/flux_server_manager.py` | `known_models()` 支持 `extra_models[]`；`probe_full()` 追加 `/models` 解析 |
| `watchdog/gen_targets.py` | 新增 `watchdog: false` 过滤（与 `enabled: false` 语义正交） |
| `manager/_add_qwen_server.py` | **新建**：一次性幂等脚本，同步 flux7 的 `remote_model` 与 note |
| `tests/test_model_capabilities.py` | **新建** 72 项 |
| `tests/test_start_python_select.py` | **新建** 30 项 |
| `tests/test_verify_qwen_env.py` | **新建** 28 项 |
| `tools/verify_qwen_env.py` | **新建**：GPU 机环境自证脚本 |
| `tools/install_key_askpass.py` | **新建**：Windows 免 sshpass 装公钥 |
| `tests/test_edit_capability.py` | `need` 集合 `mask` → `mask_param`（契约本身变了） |
| `tests/test_edit_offline.py` / `tests/test_server_registry.py` | 前轮已改 AST 语义断言 |

---

## 八、新增的三道门

| 门 | 项数 | 钉住什么 |
|---|---|---|
| `test_model_capabilities.py` | 72 | 能力表结构 / CFG 适配 / `mask_param` 全 False + 源码级签名校验 + **无旧字段名** / **双向**签名自洽 / 变异 / 许可硬约束 |
| `test_start_python_select.py` | 30 | 解释器判定表 + 7 用例**真跑 bash** + 3 组变异 |
| `test_verify_qwen_env.py` | 28 | 自证脚本自身契约：**阶段 [0] 前置检查** / `mask` 断言**方向**（必须断言「不存在」）/ 硬判据 / 两种运行位置交叉核对 / 变异 |

**关键**：`mask_param` 的签名校验是**双向**的 ——
说有的必须真有，**说没有的必须真没有**。
只查单向只能防「多报」，防不住「低报」把用户无谓挡在门外。

---

## 九、★ 本轮新增的第一条铁律：判断「装完了没有」先看进程

### 我犯的错（假阴性）

跑自证脚本报 `FAIL QwenImage21Pipeline 存在` → 我一度宣布「环境有问题」。
**实际环境是好的** —— 当时 `pip`/`conda` **正在把 diffusers 从 0.40.0 覆盖到 0.41.0.dev0**
（进程 PID 1940 已跑 1h36m，日志尾部 `Building wheel for diffusers: started`），
我把**中间态当成了终态**。

### 一般化

> 任何「装完后验证」的脚本，**第一步必须是查安装进程是否还在**。
> 还在跑 → **拒绝给结论**（判 FAIL 并提示"等它结束再重跑"）。
> 否则产出**假阴性** —— 比假阳性更贵：会让你回去改本来没错的东西。

### 落地

`tools/verify_qwen_env.py` 的**阶段 [0]**：扫 `/proc/*/cmdline` 找
`setup_qwen_env` / `dl_qwen` / `pip install`，在跑就判 FAIL 并给出明确提示。

`tests/test_verify_qwen_env.py` 钉住这条**不能被删**。

**同源教训**：任何"读取当下状态"的检查，都要先问「**这个状态还在变化吗**」。

---

## 十、GPU 机 flux7（Qwen 专用）

| 项 | 值 |
|---|---|
| SSH | `ssh -p 13889 root@connect.weste.seetacloud.com` |
| hostname | `autodl-container-g1hp9n7vf8-a57848c7` |
| 代码 | `/root/autodl-tmp/flux-t2i/`（`flux_resident_server.py` md5 `a95fe782...`、`start_resident.sh` md5 `02ef7b5d...`，与本地/VPS mirror 三处一致） |
| 环境 | `/root/autodl-tmp/envs/qwen`（**前缀模式，在数据盘**）：torch 2.13.0+cu130 / diffusers **0.41.0.dev0** / transformers 5.17.0 |
| 权重 | `/root/autodl-tmp/qwen-models/Qwen-Image-2.1` = **33.12 GB 全部下完**，7 个 safetensors **结构校验 0 损坏** |
| 磁盘 | 数据盘 50G 用 39G 余 12G；系统盘 30G 用 22G 余 8.1G |
| 注册表 | `watchdog: false`（**刻意**，见下） |

### 为什么 `watchdog: false` 是刻意的

看门狗会每 60s 把 GPU 机文件与 VPS mirror 比对，不一致就**反向覆盖远端**。
flux7 尚**未验证过显存**（bf16 约 30-35GB vs 32G 卡，**最大未知数**），
贸然纳入自动拉起会让它开机后被推到「可接单」状态而可能 OOM。

**改回 `true` 的前置条件（两条都已满足）**：
1. 环境装完（✅ 已装且已自证）
2. 解释器自动选择（✅ `pick_python` 已实现并真机干跑 4 用例全对）

→ 还剩一条**未做**：**真机出图验证**（见遗留待办）。

---

## 十一、★ AutoDL 磁盘布局（本轮代价最高的一条）

**`/root` 是系统盘（overlay 30G），`/root/autodl-tmp` 才是数据盘（`/dev/md0` 50G）。**

同一根因伪装成**三种症状**，最难查：

1. 大文件下载到一半 → `OSError: [Errno 28] No space left on device`
2. `pip` 装到一半失败
3. `pip install git+https://...` → `fatal: write error: No space left on device`
   / `invalid index-pack output`
   —— **看着像网络问题 / 包太大**，真因是 `/tmp` 也在系统盘

**修法（三条一起做）**：
1. 环境/权重一律放**数据盘**：`conda create -p /root/autodl-tmp/envs/<name>`（前缀模式）
2. `TMPDIR` / `PIP_CACHE_DIR` 都指到数据盘
3. **下载前做磁盘预检**（别下到一半才炸 —— 半截文件清理更麻烦，还白烧带宽）

**排查命令**：`df -h /` 与 `df -h /root/autodl-tmp` **必须分开看**
（只看 `df -h` 会因为 `/dev/md0` 同时挂 `/init` 与数据盘而读串）。

---

## 十二、验收证据

| 项 | 结果 |
|---|---|
| `py -3.11 tests/run_all.py` | **17/17 道门通过**，53.5s |
| 变异测试 | 6 组全部确认门会变红（非走过场） |
| 三处 md5 | `flux_resident_server.py` / `start_resident.sh` 本地 = VPS mirror = flux7，完全一致 |
| `deploy_vps.py` | 已跑（改 `server/` 下文件的**第一动作**）；VPS 服务 active，自测 5/5 |
| flux7 未纳入 `targets.conf` | ✅ `watchdog: false` 生效 |
| 真机干跑解释器选择 | Qwen → `/root/autodl-tmp/envs/qwen/bin/python`；klein/dev → flux 环境；显式覆盖生效（4 用例全对） |
| 权重完整性 | 7 个 safetensors 逐个读 header 校验，**0 损坏** |
| manager 能力过滤（离线） | `{}`→3 台；`{edit}`→flux5+flux7；`{edit,multi_ref}`→flux7；`{edit,transparent}`→flux7；`{edit,mask_param}`→`[]`（正确报「无候选」） |
| Qwen 环境自证（真机） | **环境可用 ✅** |

---

## 十三、遗留待办（归档时未闭合）

### P0 · 等带卡后立刻做

1. **显存实测** —— **最大未知数**：Qwen bf16 约 30-35GB vs 32G 卡。
   做法：先 scp `tools/verify_qwen_env.py` 到 `/root/autodl-tmp/flux-t2i/`，
   带卡后复跑 → 再跑 `python manager/acceptance_gpu.py --server flux7`。
2. **文生图 / 图生图真机各跑一次** —— 至今**一次都没在真机上跑过**。

### P1

3. 通过后把 flux7 的 `watchdog` 改回 `true` → 跑 `deploy_vps.py`。
4. **B 链 image-gen-site 界面**（因「先只做自用/内部验证」暂缓）。
5. **模型选择下拉 / 图生图上传框的 UI 归属**：
   实测确认 **A 链自己的网页没有这两样**
   （`_HTML_INDEX` 里 `model` 出现 0 次、`type="file"` 0 次、`api/edit` 0 次），
   `/api/models` 端点存在但**只供 B 链消费**。
   → 若要让朋友用上「选模型 + 图生图」，UI 必须做在 B 链，或给 A 链页面补 UI。

### P2

6. `9620 /health` **仍不透传 `profile` / `capabilities`**（只有 GPU 侧 `/health` 带）。
7. `docs/HANDOFF-2026-09-21.md` 需补 Qwen 相关变更；
   新交接提示词已引用 `HANDOFF-2026-09-22.md`，**该文件尚未创建**。
8. `manager/servers.json` 里 flux2/flux3/flux4 条目仍留痕（`enabled: false`）。
   ⚠️ 别再误标 flux4 停用 —— 被释放的是 flux.2 klein 4B / 4B2。

---

## 十四、本次归档时的工作区状态

**归档前**：20 个文件未提交（9 改 + 7 新增 + 备份目录 `.backup-20260922-qwen/`）。

**提交后**：见 `git log --oneline -1`。

归档前复跑 `py -3.11 tests/run_all.py` = **17/17 全绿**（归档不改代码，仅记录）。

---

## 核心文档索引

| 文档 | 路径 |
|---|---|
| 项目目标 SPEC | `docs/SPEC.md` |
| 项目详细方案 | `docs/ARCHITECTURE.md` |
| 项目测试计划 | `docs/TEST_PLAN.md` |
| **项目踩坑记录** | `docs/PITFALLS.md` |
| 项目推进进度 | `docs/PROGRESS.md` |
| 新会话交接提示词 | `docs/交接提示词-新会话-v2.md` |
| 模型选型评估 | `docs/模型选型评估.md` |

## 交接

说「**按交接提示继续**」→ 读对应记忆 + `docs/接交接.md` 对齐锚点无缝续接。

日常一条命令验收全部：`py -3.11 tests/run_all.py`

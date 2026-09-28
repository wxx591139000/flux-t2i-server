# Qwen-Image「图片参考 / 编辑」平台配套方案 v1.0

- 日期：2026-09-28
- 需求来源：Dancing 指定「ob 库里的 Qwen-Image-2.1 笔记」——`E:\ObsidianHouse\ObsidW\03 Areas领域\图像与视频\2026-09-20 Qwen-Image-2.1开源：轻量高能，创作编辑一体化.md`
- 承接版本：flux-t2i-server v2.13.0（Qwen 放行到 B 链 + flux7 纳入看门狗）
- 验证机型：7B2（`connect.westd.seetacloud.com:36832`，从 7B1 克隆，RTX 4080 32G）

---

## 0. 结论先行

1. **笔记里的 6 项能力，GPU 侧（`server/flux_resident_server.py`）已全部具备**，而且多图通路是完整的（`images` list → `image_paths` → 逐张喂 pipeline）。**不需要动 GPU 侧代码**。
2. **真正的缺口在 manager 三层 + B 链前端** —— 整条 manager 链路被写死成「**单张**参考图」，笔记的最大卖点「最多 10 张参考图」到不了 GPU。这是本次要补的主线。
3. **两条硬约束必须先解**（否则「10 张」是纸面能力）：
   - `MAX_EDIT_BYTES = 12 MB`：10 张参考图 base64 后可达 ~107 MB → **物理装不下**；
   - **2048×2048 在 32G 卡上 OOM 已实测**（1024×1024 通过）。`native_res=2048` 是**模型能力**，不等于**本机可跑**。
4. 圈选/涂抹/独立掩码在 Qwen 上是**没有 `mask` 入参**的：GPU 侧对 `mask` 参数**显式 400 拒绝**，并指向唯一正解 —— **把掩码做成「第二张图」一起传**（与笔记原文「原图和独立掩码作为两张图片输入」一致）。所以前端需要的不是「mask 参数」，而是**多图上传 + 一个画布**。

---

## 1. 笔记能力清单（原文口径）

| # | 能力 | 笔记原文要点 |
|---|---|---|
| 1 | 文生图 + 编辑二合一 | 视觉生成 7B（32 层 Single-Stream DiT），与编辑同模型 |
| 2 | 原生透明 | 按提示词决定输出普通图或**带透明通道**的图；支持透明图层编辑与**抠图**（RGB→RGBA） |
| 3 | **多图参考（≤10 张）** | 「最多支持 10 张参考图输入」；示例：6 张人像合合照、5 件商品穿搭、10 张家居室内设计 |
| 4 | **灵活指定编辑区域** | 支持**圈选、涂抹和独立掩码**三种局部编辑方式 |
| 5 | 一致性与任务覆盖 | 人像/商品一致性增强；支持全景图、信息图、分镜图生成 |
| 6 | 质感 | 文字排版、人物光影 |

⚠️ 第 4 条有个隐藏前提：**掩码是「作为图片」传入的**，不是独立参数。笔记原文：「为保留完整的原始图像信息，Qwen-Image-2.1 还支持将**原图和独立掩码作为两张图片输入**」。

---

## 2. 现状 vs 笔记：三层差距表

（依据：本地代码逐文件核对 + 7B2 真机 `/health` 实测能力表）

| 笔记能力 | ① GPU resident :9630 | ② manager :9620 | ③ B 链前端 | 缺口 |
|---|---|---|---|---|
| 文生图 | ✅ | ✅ | ✅ | 无 |
| 单图参考编辑 | ✅ `image` | ✅ `ref_image` | ✅ 单个 `refImage` | 无 |
| **多图参考（≤10）** | ✅ **`images` list**（优先）/ `image`；`image_paths` 落 params；worker 逐张加载；超 `max_ref_images` 报错 | ❌ **三处只传单张** | ❌ `refImage` 是单值 | **需贯通** |
| 圈选 / 涂抹 | ⚠️ 靠「合成进参考图」 | ❌ 单图 → 传不了第二张 | ❌ 无画布 | **需多图 + 前端画布** |
| 独立掩码 | ⚠️ 同上；`mask` 参数被 **400 拒绝** | ❌ | ❌ | 同上 |
| 透明 RGBA | ✅ 出图固定存 **PNG**（`f'{job_id}.png'`） | ❓ 待核实下载链路是否保 alpha | ❓ 预览需棋盘底 | **需核实** |
| 人像/商品一致性 | 模型能力，无平台成本 | — | — | 无 |
| 编辑任务类型 | 模型能力，无平台成本 | — | — | 无 |

### 2.1 单图瓶颈的确切位置（三处）

```
B链前端  app/create/page.tsx:303   const [refImage, setRefImage] = useState<PreparedRef | null>(null)
         app/create/page.tsx:577   ...(useEdit ? { image: refImage!.dataUrl } : {})
   ↓
manager  flux_web_service.py:350   ref_image = (data.get('image') or '').strip()      ← 单个字符串
         flux_web_service.py:386   scheduler.submit(..., ref_image=ref_image, ...)
   ↓
manager  flux_queue.py:151         def submit(..., ref_image: str = None, ...)
         flux_queue.py:240         self._put_ref(job_id, ref_image)
         flux_queue.py:245         def _put_ref(self, job_id, ref_image)             ← 单张
   ↓
manager  flux_resident_client.py:804  if ref_image_b64: body['image'] = ref_image_b64  ← 关键：永远单张
         flux_resident_client.py:806  endpoint = '/edit' if ref_image_b64 else '/generate'
   ↓
GPU      flux_resident_server.py:1579  raw_list = body.get('images')   ← ✅ 早已支持 list
```

**结论**：GPU 侧等着接 `images`，manager 侧却只会发 `image`。缺口是**一段贯通工作**，不是能力缺失。

### 2.2 顺带坐实的既有 bug：B 链「自动选支持编辑的模型」从未生效

`manager/flux_web_service.py` 的 `_api_models` 里，注释写着「id / name / 就绪 / **能否图生图**都够了」，但 slim 白名单里**没有 `supports_edit`**：

```python
slim = [{'id': m.get('id'), 'name': m.get('name') or m.get('id'),
         'ready': bool(m.get('ready')),
         'class_name': m.get('class_name'),
         'size_gb': m.get('size_gb')}          # ← 少了 supports_edit
        for m in models if m.get('id')]
```

而 B 链前端在按它做判断：
- `app/create/page.tsx:398` `models.filter((m) => m.supports_edit === true)`
- `app/create/page.tsx:541` `models.find((m) => m.supports_edit === true && m.ready)`
- `app/create/page.tsx:1025` `const blockedForEdit = Boolean(refImage) && m.supports_edit === false;`

因为 `undefined === true` 恒为 false → **自动选模型这条路一直是死的**（用户只能手动选）。修一行即可：slim 里补 `'supports_edit': bool(m.get('supports_edit'))`。

### 2.3 两条硬约束（实测）

| 约束 | 值 | 后果 |
|---|---|---|
| `MAX_EDIT_BYTES`（GPU `/edit` 请求体） | **12 MB** | 10 张参考图 base64 后 ~107 MB（按每张 8MB 上限算）→ **装不下**；即便每张压到 1MB，10 张 base64 ≈ 13.3 MB 仍**超标** |
| `MAX_REF_BYTES`（单张解码后） | 8 MB | 单张上限本身合理 |
| `MAX_DIM` | 2048 | 见下 |
| 2048×2048 实跑 | **OOM** | `Tried to allocate 2.25 GiB`，`allocated 23.54 GiB` + `reserved-but-unallocated 6.82 GiB`（碎片）。1024×1024 通过（70.7s） |

推理：`MAX_EDIT_BYTES` 不提高，**「最多 10 张」在当前配置下无法达到** —— 这是纸面能力与真实能力的分界线。

---

## 3. 设计方案

### 3.1 GPU 侧：不改（已具备），仅两处调优

| 项 | 动作 | 理由 |
|---|---|---|
| 多图通路 | **不动** | `images` list / `image_paths` / worker 逐张加载 / `multi_ref` 上限校验 均已就绪并实测 |
| `MAX_EDIT_BYTES` | 12 MB → **64 MB** | 让 10 张 × ~4MB 真实可用；配合前端压缩做双保险 |
| 分辨率策略 | **不放开 2048**，先把 `MAX_DIM` 语义与「推荐上限」分开 | 见 3.4 |

⚠️ 另有一条**文档与实现不一致**需修：`flux_resident_server.py:1419` 的 docstring 说 `/edit` 支持 `image_url`，但实现（`_save_named_image`）**只接受 base64**，且注释明确写「URL 下载也不做（SSRF 面）」。docstring 应删掉 `image_url` 说法。

### 3.2 manager 侧：三处贯通（核心工作）

**改法原则**：**兼容优先** —— 单张调用方（老前端、老任务、`acceptance_gpu.py`）必须继续能跑；实现上「统一收敛成 list，只有 >1 张时才发 `images`」。

| # | 文件 / 位置 | 现状 | 改为 |
|---|---|---|---|
| 1 | `flux_web_service.py:_api_edit` | `ref_image = (data.get('image') or '').strip()` | 接受 `images`（list）或 `image`（单张）；`images` 优先；统一交给 queue |
| 2 | `flux_queue.py:submit / _put_ref` | `ref_image: str` | `ref_images: list`（内部）；`_put_ref` 存 list；DB 的 `edit_mode` 判据不变（非空即 1） |
| 3 | `flux_resident_client.py:generate` | `ref_image_b64: str` → `body['image']` | `ref_images_b64: list` → **1 张发 `image`，>1 张发 `images`**（保持老 worker 兼容） |

**为什么第 3 处要「1 张仍发 `image`」**：GPU 侧 `_save_ref_images` 里 `images` 优先、`image` 兜底，两条路都通；但**只传 1 张时发 `image` 能让老 worker（若存在）也能接**，成本是零。这属于「不给自己制造兼容债」。

### 3.3 B 链前端：多图 + 画布 + 透明预览

| 功能 | 做法 |
|---|---|
| 多图上传 | `refImage` → `refImages: PreparedRef[]`；拖拽多选；缩略图列表可删可排序（**顺序即语义**：笔记示例里「模特/衣服/鞋/包/帽」的顺序影响结果） |
| 每张压缩 | 沿用现有 `PreparedRef` 管线（含 width/height/bytes），对每张独立跑；**建议目标 ≤1.5MB/张**，10 张 ≈ 15MB 原始 |
| 掩码画布 | 新增「局部编辑」入口：在图上**圈选/涂抹** → 导出**掩码 PNG**（白=编辑区）→ 作为**追加的一张参考图**（`refImages = [原图, 掩码图, ...]`）。⚠️ 不要尝试传 `mask` 参数（Qwen 无此入参，会 400） |
| 透明预览 | 结果预览容器用**棋盘底**（CSS 棋盘 or 双层背景），否则透明区在浅色页面上看不出来 |
| 修 `supports_edit` | manager slim 补字段（见 2.2），前端逻辑**不用改**，自动选模型即刻生效 |

### 3.4 分辨率策略（2048 的正确姿势）

实测：**1024 通过，2048 OOM**（model offload，32G 卡）。三个候选解法，按性价比排序：

| 方案 | 动作 | 代价 | 状态 |
|---|---|---|---|
| **A. 碎片治理** | 启动带 `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` | 几乎无 | ✅ **已验证有效**：同参数 2048×2048，改前 OOM → 改后 **done / 211.55s** |
| B. 更省档位 | `FLUX_OFFLOAD=sequential` | 慢 2~3 倍 | 未测 |
| C. 加 VAE tiling | 代码加 `pipe.vae.enable_tiling()` | 小改动，针对解码段峰值 | 未做（代码里目前**完全没有** tiling/slicing） |

**建议**：先落 A（若成立）；无论 A 是否成立，**在能力表里把「推荐分辨率」与 `native_res` 分开声明**（例如新增 `recommended_res: 1024`），避免前端把 `native_res=2048` 当成「随便开」——这正是本次踩到的坑：`native_res` 描述的是**模型**，不是**本机**。

### 3.5 能力暴露与许可

- Qwen-Image-2.1 是 **Qwen Research License（非商用）**，已由 v2.13.0 的 `expose_to_customers: true` + `expose_note` 由业主显式放行到 B 链（决定与事实分离，回滚=改回 false）。
- 本次新增的「多图参考 / 局部编辑」**不引入新的许可面** —— 仍跑同一个 Qwen 权重，只是把已有能力接通。**但要提醒**：能力一旦到 B 链，客户就能用「最多 10 张参考图」这类高价值功能，**计费与限流需要同步考虑**（当前 `flux_queue` 按任务计数，多图不改变计费口径，但单任务耗时会显著上升，见 3.6）。

### 3.6 成本影响（必须点名）

多图 ≠ 免费：
- 参考图张数直接影响**文本+图像 token 数**（Qwen 把图像编码进上下文），10 张参考图的**首步预填充**明显变长；
- 实测基线：1024×1024 / 20 步 / 单图 → **70.7s**（其中首步 18.99s 是冷启动搬运）。多图下首步会更长。
- → 建议在方案落地时**实测 2/5/10 张的耗时曲线**，再决定是否给 B 链开放到 10 张（或先放到 3~5 张）。

### 3.7 回滚

| 改动 | 回滚 |
|---|---|
| manager 三处 | 单张逻辑保留为兼容路径；`git revert` 对应 commit 即可，数据层面无迁移（`_put_ref` 存 list 时对单张向下兼容） |
| `MAX_EDIT_BYTES` | 改回 12MB（`MAX_EDIT_BYTES` 常量） |
| 前端多图 | `refImages[0]` 即等价于旧行为 |
| `expandable_segments` | 去掉环境变量恢复原状 |
| `expose_to_customers` | 改回 `false` 或删掉 → 恢复「仅自用」 |

---

## 4. 风险与未验证项

**已验证（本次实测）**
- ✅ 7B2 上 Qwen 模型加载成功（`QwenImage21Pipeline`，offload=model，~39s）
- ✅ 文生图 1024×1024 出图成功，70.7s，无 OOM
- ✅ 能力表与笔记逐项吻合（`multi_ref`/`max_ref_images=10`/`transparent`/`mask_param=false`/`cfg_param=true_cfg_scale`）
- ✅ 2048×2048：**默认启动必 OOM**；加 `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` 后 **done / 211.55s**（对照实验，唯一差异就是这个环境变量）
- ✅ `start_resident.sh` 的 `pick_python()` 在真机上正确选中 `/root/autodl-tmp/envs/qwen/bin/python`（Qwen 专用环境）
- ✅ `/edit` 通路在真机生效（日志：`edit 模式：1 张参考图 → 参数名 'image'（单张）`）
- ✅ 负向用例按设计拒绝：尺寸 2064 → 400「尺寸需在 256~2048 之间」；11 张参考 → 400「参考图最多 10 张」；带 `mask` → 400「目标模型不支持 mask 入参…请把圈选/涂抹合成进参考图后用 image 传入」

**未验证（本方案依赖但尚无一手证据）**
- ⚠️ `offload=sequential` 与 VAE tiling 未测（A 方案已够用，暂不需要；留作后备）
- ⚠️ **多图参考（2/5/10 张）在真机上是否真跑通** —— GPU 代码具备 ≠ 真跑过。**这是本方案最大的未验证项，必须实测**
- ⚠️ 透明 RGBA 输出是否真带 alpha 通道（代码存 PNG ✅，但模型是否真产出 alpha 未验证）
- ⚠️ manager → 下载链路是否保 PNG alpha（未读 `_api_download` 的传输细节）
- ⚠️ 多图下的耗时曲线与显存峰值

**已知风险**
- 32G 卡 + bf16 33GB 权重：**只有 model offload 这一档能跑**，`none` 必 OOM（已实测）。任何改动都不能让 offload 回落。
- B 链是对外站点：多图能力开放后，**单任务成本上升**，需评估是否调整计费。

---

## 5. 配套交付物

- 测试用例集：`docs/Qwen图片参考与编辑-测试用例.md`
- 实测报告：`docs/Qwen图片参考与编辑-测试报告-20260928.md`

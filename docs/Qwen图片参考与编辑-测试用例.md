# Qwen-Image「图片参考 / 编辑」测试用例集

- 日期：2026-09-28
- 被测对象：`flux-t2i-server` 的 GPU 侧常驻服务（`server/flux_resident_server.py`，端口 9630）
- 被测机型：7B2（`connect.westd.seetacloud.com:36832`，RTX 4080 32G，`offload=model`）
- 配套方案：`docs/Qwen图片参考与编辑-方案-v1.0.md`
- 执行报告：`docs/Qwen图片参考与编辑-测试报告-20260928.md`

---

## 0. 前置条件与约定

### 0.1 环境前置
```bash
# 密钥直连（无需密码）
SSH="ssh -p 36832 -i ~/.ssh/id_rsa_musetalk root@connect.westd.seetacloud.com"
$SSH 'curl -s http://127.0.0.1:9630/health'      # 必须 model_loaded=true
```
启动：
```bash
$SSH 'cd /root/autodl-tmp/flux-t2i && FLUX_MODEL=/root/autodl-tmp/qwen-models/Qwen-Image-2.1 \
      FLUX_OFFLOAD=model bash start_resident.sh --force'
```

### 0.2 调用方式（本地发，避免手拼 JSON）
```bash
# 单张：{"prompt":..., "image":"<base64>"}
# 多张：{"prompt":..., "images":["<base64>", ...]}       ← images 优先
# 统一走 POST /edit
B64=$(base64 < local.png | tr -d '\n')
```
⚠️ 不要传 `mask` 参数 —— Qwen 无此入参，GPU 侧会 **400 拒绝**（这是**预期行为**，见 C4）。

### 0.3 判据分两类（必须区分）
| 类型 | 含义 | 举例 |
|---|---|---|
| **机器断言** | 可脚本判定，非 0/1 明确 | 任务 `status=done`；文件存在且 >0B；`PIL` 读出的 `size` == 请求值；HTTP 400 + 指定文案；日志含「N 张参考图」；⚠️ **透明判据禁用 `mode=='RGBA'`**，必须用 `alpha.min()==0`（见 §0.5） |
| **人眼复核** | 需看图判断语义是否达成 | 「牛仔出现在马背上」「BLOOM 改成 Qwen-Image」「透明背景是否干净」 |

★ **凡能机器断言的，一律不靠人眼。** 人眼只用于「语义正确性」。

### 0.4 素材（来自 ob 库官方示例图，路径前缀）
```
E:\ObsidianHouse\ObsidW\03 Areas领域\图像与视频\assets\wiki-img\qwen-image-2-1\
```
| 素材 | 用途 |
|---|---|
| `img-020.png` | 掩码用例**原图**（马 + 木桩） |
| `img-021.png`（6.9KB） | 掩码用例**掩码图**（纯掩码，极小） |
| `img-022.png` | 掩码用例**官方结果**（对照，非输入） |
| `img-016/017.png` | 圈选（输入/官方结果） |
| `img-018/019.png` | 涂抹（输入/官方结果） |
| `img-011/012.png` | 抠图 RGB→RGBA（输入/结果） |
| `img-009/010.png` | 文字编辑 BLOOM→Qwen-Image |
| `img-013/014/015.png` | 多图官方**展示拼图**（⚠️ 不是独立输入图，10 张用例需另行构造，见 B4） |

---

## A 组 · 基础通路与尺寸约束

| ID | 目的 | 输入 | 期望 | 判据 | 成本 |
|---|---|---|---|---|---|
| **A1** | 文生图基线 | `{"prompt":"a red apple on a wooden table, studio photo","width":1024,"height":1024,"steps":20,"true_cfg_scale":4.0,"seed":42}` | 出图 | 机器：`status=done`；PNG 存在；`size==(1024,1024)` | 70.7s ✅已跑 |
| **A2** | 尺寸下界 | `width=height=256` | 接受 | 机器：`status=done`；`size==(256,256)` | ~10s |
| **A3** | 16 倍数归一 | `width=1000`（非 16 倍数） | 归一 | 机器：`size[0] % 16 == 0` 且 ≈1000 | ~30s |
| **A4** | 尺寸超限拒绝 | `width=2064`（>2048） | 400 | 机器：HTTP 400 + 文案含 `尺寸需在 256~2048` | 秒级 |
| **A5** | **2048 原生分辨率** | `width=height=2048, steps=20` | 出图 | 机器：`status=done` | **已实测 OOM ❌**（见报告） |

---

## B 组 · 多图参考（笔记核心卖点）

| ID | 目的 | 输入 | 期望 | 判据 | 成本 |
|---|---|---|---|---|---|
| **B1** | 单图参考编辑（回归基线） | `image`=img-020；prompt「给马戴一顶草帽」 | 出图 | 机器：`done` + 日志含 `1 张参考图` | ~70s |
| **B2** | 双图参考 | `images`=[img-020, img-021]；prompt「生成一张坐在马背上的牛仔」 | 出图 | 机器：`done` + 日志含 `2 张参考图`；人眼：牛仔在马背 | ~90s |
| **B3** | 5 张参考 | `images`=5 张不同图；prompt「把这几件单品组合到同一套穿搭」 | 出图 | 机器：`done` + 日志含 `5 张参考图`；人眼：元素都出现 | ~120s |
| **B4** | **10 张参考（上限边界）** | `images`=10 张（通路验证可用同一张重复 10 次；效果验证需 10 张不同图） | 出图 | 机器：`done` + 日志含 `10 张参考图` | ~180s |
| **B5** | **11 张超限拒绝** | `images`=11 张 | 400 | 机器：HTTP 400 + 文案含 `参考图最多 10 张` | 秒级 |
| **B6** | `images` 优先于 `image` | 同时给 `images`（2张）和 `image`（1张） | 按 2 张走 | 机器：日志含 `2 张参考图` | ~90s |

★ B4 是**笔记「最多 10 张」的唯一硬证据**，必须跑。B5 证明上限真的在生效（不是写了没用）。

---

## C 组 · 局部编辑三方式（圈选 / 涂抹 / 独立掩码）

⚠️ **前提认知**：Qwen **没有 `mask` 入参**。三种方式在平台上的**唯一落地形态**都是「掩码作为一张图一起传」。所以 C 组的价值在于**验证这条路走得通 + 验证 mask 参数确实被拒**。

| ID | 目的 | 输入 | 期望 | 判据 | 成本 |
|---|---|---|---|---|---|
| **C1** | 圈选编辑 | `images`=[原图, 圈选图]；prompt「去掉红色圈内的手表」 | 出图 | 机器：`done` + `2 张参考图`；人眼：圈内对象被改 | ~90s |
| **C2** | 涂抹编辑 | `images`=[img-018, 涂抹图]；prompt「在白色涂抹区域添加一名潜水员」 | 出图 | 机器：`done`；人眼：涂抹区出现潜水员 | ~90s |
| **C3** | **独立掩码作第二图**（笔记原文用法） | `images`=[img-020, img-021]；prompt「生成一张坐在马背上的牛仔人物」 | 出图 | 机器：`done` + `2 张参考图`；人眼：对照 img-022 语义 | ~90s |
| **C4** | **`mask` 参数必须被拒** | `image`=img-020 + `mask`=任意 base64 | 400 | 机器：HTTP 400 + 文案含 `不支持 mask 入参` 且含 `合成进参考图` | 秒级 |

★ C4 是**负向用例**：它证明「平台不是忘了实现 mask，而是明确引导到正确路径」。**这条变红反而说明 Qwen 换了实现**。

---

## D 组 · 透明与抠图

| ID | 目的 | 输入 | 期望 | 判据 | 成本 |
|---|---|---|---|---|---|
| **D1** | 文生透明图 | `prompt="a red apple, transparent background, PNG with alpha channel"` | RGBA | 机器：**`img.mode` 含 `A`** 且 alpha 有非 255 像素；人眼：背景干净 | ~70s |
| **D2** | 抠图（RGB→RGBA） | `images`=[img-011]；prompt「提取主体，输出透明背景图层」 | RGBA | 机器：`mode` 含 `A`；人眼：主体完整、背景透明 | ~90s |
| **D3** | 透明图再编辑 | `images`=[D1 产物]；prompt「把苹果换成绿色」 | RGBA 保留 | 机器：`mode` 仍含 `A` | ~90s |

★ D 组的机器断言是**硬判据**：`PIL.Image.open(p).mode in ('RGBA','LA')` 或 `'A' in getbands()`。**不能只看「图出来了」**——PNG 可以存成无 alpha 的 RGB，肉眼看不出。

---

## E 组 · 一致性与文字

| ID | 目的 | 输入 | 期望 | 判据 | 成本 |
|---|---|---|---|---|---|
| **E1** | 文字编辑 | `images`=[img-009]；prompt「把图中的文字 BLOOM 改成 Qwen-Image」 | 改字 | 机器：`done`；人眼：文字正确（**必须人眼**，OCR 可选增强） | ~90s |
| **E2** | 人像一致性 | `images`=[人像图]；prompt「把背景换成雪山，人物不变」 | 保脸 | 机器：`done`；人眼：面部特征一致 | ~90s |
| **E3** | 商品一致性 | `images`=[商品图]；prompt「把商品放到大理石台面上」 | 保纹理/文字 | 机器：`done`；人眼：包装文字与纹理保留 | ~90s |

---

## F 组 · 编辑任务类型

| ID | 目的 | 输入 | 期望 | 判据 | 成本 |
|---|---|---|---|---|---|
| **F1** | 全景图 | `images`=[img-027]；prompt「生成 360° 全景图」 | 出图 | 机器：`done`；人眼：全景构图 | ~90s |
| **F2** | 信息图 | `images`=[img-029]；prompt「扩展成信息图」 | 出图 | 机器：`done`；人眼：含信息图要素 | ~120s |
| **F3** | 分镜图 | `images`=[img-031]；prompt「根据三视图生成分镜」 | 出图 | 机器：`done`；人眼：多格分镜 | ~120s |

---

## G 组 · 参数口径与平台约束

| ID | 目的 | 输入 | 期望 | 判据 | 成本 |
|---|---|---|---|---|---|
| **G1** | `true_cfg_scale` 生效 | `true_cfg_scale=1.0` vs `4.0` 同 seed 两次 | 结果不同 | 机器：两图字节不同（md5 不等） | 2×70s |
| **G2** | 负向提示词需 CFG>1 | `negative_prompt="blurry"` + CFG=1.0 vs CFG=4.0 | CFG>1 才生效 | 机器：`done`（对比由人眼/G1 联合判断） | 2×70s |
| **G3** | **10×8MB 超出请求体上限** | 构造 10 张各 8MB 的 `images` | 400 | 机器：HTTP 400（`MAX_EDIT_BYTES=12MB`）→ **证明「10 张」纸面能力与真实上限的差距** | 秒级 |
| **G4** | 单张 8MB 上限 | 1 张 9MB 的 `image` | 400 | 机器：400 + 文案含 `图片过大` | 秒级 |

★ G3 是本方案「必须调 `MAX_EDIT_BYTES`」的**直接证据**。

---

## 执行骨架（可复跑）

```python
# tools/run_qwen_ref_tests.py  ← 建议落地成脚本（本次以手工+脚本混合执行）
# 要点：
#  1. 每张参考图：PIL 打开 → resize 到 ≤1024 → 存 JPEG(quality=85) → base64
#     （既压体积，又避免超 MAX_REF_BYTES）
#  2. POST /edit，拿 job_id
#  3. 轮询 /jobs 到 status in (done, failed)
#  4. done → scp 产物回本地 → PIL 断言 size/mode
#  5. 每条独立写一行结果（id, status, runtime, 断言, 备注）
```

**回本判定**：全部用例 `done` 且机器断言通过 = 通路成立；任一条 `failed` 记录原始 error 原文与显存快照，**不粉饰**。

---

## 结果记录（填写于报告）

| ID | 状态 | 耗时 | 机器断言 | 人眼结论 | 备注 |
|---|---|---|---|---|---|
| A1 | done | 70.7s | size=1024² ✅ | 通过 | 首次跑通 |
| A5 | **failed** | — | — | — | `OutOfMemoryError`，见报告 |
| … | | | | | |

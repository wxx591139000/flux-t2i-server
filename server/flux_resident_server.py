#!/usr/bin/env python3
"""
FLUX.1 常驻生成服务（GPU 端）—— 消除「每张图冷启动」的结构性瓶颈

【为什么要有它】
原链路（legacy）每张图都走一遍：
    flux_queue.py      pkill 旧生成进程 / 清 out/
    start_gen.sh       screen 起 python gen_flux.py      ← 重新 import diffusers
    gen_flux.py:62     from_pretrained(...)              ← 重新加载 ~31GB 权重
    gen_flux.py:80     pipe(...)                         ← 只生成 1 张，然后进程退出
10 张图 = 10 次模型加载。吞吐被结构性卡住。

本服务把模型加载一次常驻内存，之后每个请求只做「生成」，不做「加载」。

【协议】HTTP + JSON，默认只绑 127.0.0.1（不新增公网暴露面）
    GET  /health                 → {status, model_loaded, model_id, profile, offload, queue_depth, current_job, gpu, ...}
    GET  /models                 → {models:[{id,name,class_name,ready,size_gb,profile}], current, dirs}
    POST /generate               → {job_id, status:"queued", queue_depth}
    GET  /status?job_id=<id>     → {job_id, status, image_path, error, elapsed, runtime, seed}
    GET  /image?job_id=<id>      → PNG 二进制（加 &b64=1 → JSON {b64}）
    GET  /jobs?limit=20          → 最近任务列表（运维排查用）
    POST /cancel {job_id}        → 取消「排队中」的任务（生成中的不打断）

POST /generate 请求体（除 prompt 外全部可选）：
    {"prompt": "...", "negative_prompt": "", "width": 768, "height": 1024,
     "steps": 25, "seed": null, "guidance_scale": 3.5, "priority": 0,
     "model": "FLUX.2-klein-4B"}
  · 缺省即沿用 gen_flux.py 原有的固定值（768×1024 / steps 25 / seed 42），
    所以只传 prompt 的老调用方行为不变。
  · seed 传 null 或省略 = 每张随机且返回实际 seed（可复现：拿返回的 seed 再传一次）。
  · model（2026-09-21 新增）传**模型 id**（见 GET /models），不传 = 用当前已加载的模型。
    传了就由单 worker 在该任务开跑前**排队换模型**（前面的任务跑完才换，不打断任何人）。
    steps/guidance 的默认值与上限会按**目标模型**重算（klein 4步/1.0、dev 25步/3.5），
    所以「换模型」不会带到错误的采样参数。

【鉴权】设 FLUX_RESIDENT_TOKEN 后，所有请求需带 X-Auth-Token 头；不设则不校验
（服务只绑 127.0.0.1，仅本机/SSH 可达）。

【启动】推荐用 server/start_resident.sh（幂等、带自检）：
    bash start_resident.sh            # 启动
    bash start_resident.sh --check    # 只读探活，就绪 exit 0

【环境变量】
    FLUX_OFFLOAD          none | model | sequential   （默认 none = 全程显存，最快）
    FLUX_RESIDENT_TOKEN   共享令牌（可选）
    FLUX_STUB             1 = 不加载真实模型，用合成占位图（无需 GPU；供本地联调与自证）
    FLUX_OUT_DIR          图片落盘目录（默认 <script_dir>/resident_out）

【显存策略】先试 none；OOM 就退 model；再 OOM 才用 sequential。改环境变量重启生效。
    none        pipe.to("cuda")                    最快，需显存装得下整模型
    model       enable_model_cpu_offload()         比 sequential 快 2~3 倍
    sequential  enable_sequential_cpu_offload()    原 gen_flux.py:64 的策略，最慢但最省
"""
import argparse
import base64
import json
import os
import queue
import struct
import sys
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# ── 默认值：与 gen_flux.py 对齐，保证「只传 prompt 也行为不变」 ──
DEFAULT_WIDTH = 768
DEFAULT_HEIGHT = 1024
DEFAULT_STEPS = 25
DEFAULT_SEED = 42
DEFAULT_GUIDANCE = 3.5

# 尺寸/步数硬边界：防止客户传 8192x8192 把显存打爆（FLUX 要求 16 的倍数）
MIN_DIM, MAX_DIM, DIM_MULTIPLE = 256, 2048, 16
MAX_STEPS = 100
MAX_REQ_BYTES = 64 * 1024
# 【2026-09-18 新增】/edit 要带参考图：base64 后体积膨胀约 1.37 倍。
# 1024×1024 PNG 约 1.5–2 MB → base64 后约 2.7 MB。给 12 MiB 上限留足余量，
# 同时仍拦住「塞个大视频进来」这类滥用。**只对 /edit 放宽，t2i 维持 64 KB**。
MAX_EDIT_BYTES = 12 * 1024 * 1024
MAX_REF_BYTES = 8 * 1024 * 1024                     # 参考图解码后上限（8 MB）

STATUS_QUEUED = 'queued'
STATUS_GENERATING = 'generating'
STATUS_DONE = 'done'
STATUS_FAILED = 'failed'
STATUS_CANCELED = 'canceled'
TERMINAL = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELED)


class BadRequest(ValueError):
    """入参不合法（→ HTTP 400），与内部故障区分开。"""


# ══════════════════ 纯标准库 PNG 合成（stub 模式用，免 PIL 依赖）══════════════════
def synth_png_bytes(w: int, h: int, rgb=(190, 190, 190)) -> bytes:
    """生成一张纯色 PNG。纯 stdlib（zlib+struct），不依赖 Pillow。"""
    row = b'\x00' + bytes(rgb) * w
    raw = row * h

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)      # 8bit / truecolor RGB
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(raw, 6))
            + chunk(b'IEND', b''))


class _SynthImage:
    def __init__(self, w, h, rgb):
        self._w, self._h, self._rgb = w, h, rgb

    def save(self, path):
        Path(path).write_bytes(synth_png_bytes(self._w, self._h, self._rgb))


class _SeedGen:
    """torch.Generator 的最小替身（stub 模式用，避免无 torch 环境下 import 失败）。"""

    def __init__(self, seed):
        self._seed = int(seed)

    def initial_seed(self):
        return self._seed


class StubPipeline:
    """不加载真实模型的替身，供无 GPU 环境自证链路。

    ⚠️ **签名必须与真实 Flux2KleinPipeline.__call__ 逐参数一致**（2026-09-19 实测）：
        真实签名（inspect 实测，**没有 kwargs 兜底**）：
          image, prompt, height, width, num_inference_steps, sigmas, guidance_scale,
          num_images_per_prompt, generator, latents, prompt_embeds,
          negative_prompt_embeds, output_type, return_dict, attention_kwargs,
          callback_on_step_end, callback_on_step_end_tensor_inputs,
          max_sequence_length, text_encoder_out_layers
        ⚠️ 注意 `image` 在**第一位**、`prompt` 在第二位（dev 的 FluxPipeline 相反）。

    为什么必须一致（血泪教训，2026-09-19 带卡实测）：
      替身原先**多**声明了 `negative_prompt` 并带 `**kw` —— 比真实 klein 宽松。
      于是 resident 无条件传 negative_prompt 时，离线测试全绿，而真机上 klein
      **5/5 任务全崩**（TypeError: unexpected keyword argument 'negative_prompt'）。
      **替身一旦比真实模型宽松，「离线全绿」就不再是任何保证。**

    另：替身**不做** `**kw` 兜底，正是为了让多传的参数在离线就炸掉，而不是留到带卡。
    """

    def __call__(self, image=None, prompt=None, height=1024, width=1024,
                 num_inference_steps=4, sigmas=None, guidance_scale=1.0,
                 num_images_per_prompt=1, generator=None, latents=None,
                 prompt_embeds=None, negative_prompt_embeds=None,
                 output_type='pil', return_dict=True, attention_kwargs=None,
                 callback_on_step_end=None, callback_on_step_end_tensor_inputs=None,
                 max_sequence_length=None, text_encoder_out_layers=None):
        if not prompt:
            # 位置传参错位时（把提示词塞给 image）会在更早处暴露，这里兜住真正的空提示词
            raise ValueError('prompt 不能为空')
        seed = 0
        if generator is not None:
            try:
                seed = int(generator.initial_seed())
            except Exception:
                seed = 0
        # 传了参考图 → 用它的颜色影响输出，让「edit 确实读到了图」断言可辨
        ref_rgb = None
        if image is not None:
            try:
                small = image.convert('RGB').resize((8, 8))
                px = list(small.getdata())
                n = len(px)
                ref_rgb = (sum(p[0] for p in px) // n,
                           sum(p[1] for p in px) // n,
                           sum(p[2] for p in px) // n)
            except Exception:
                ref_rgb = (10, 10, 10)
        time.sleep(0.15)                     # 模拟一点耗时，便于观察状态流转
        base = abs(hash(prompt)) % 120
        rgb = ref_rgb if ref_rgb else (150 + base % 60, 170 + (seed % 50), 200 - base % 40)
        img = _SynthImage(int(width), int(height), rgb)

        class _Out:
            images = [img]

        return _Out()


# ══════════════════════════ 任务仓库 ══════════════════════════
class JobStore:
    """内存任务表 + 状态落盘（落盘便于 SSH 直接 cat 排查，也便于服务重启后追溯）。"""

    def __init__(self, out_dir: Path, status_dir: Path, keep: int = 500):
        self.out_dir = out_dir
        self.status_dir = status_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.status_dir.mkdir(parents=True, exist_ok=True)
        self.keep = keep
        self._jobs = {}
        self._order = []
        self._lock = threading.Lock()

    def _persist(self, rec: dict):
        try:
            (self.status_dir / f"{rec['job_id']}.json").write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass                              # 落盘失败不影响主流程

    def create(self, params: dict) -> dict:
        job_id = uuid.uuid4().hex[:16]
        rec = {
            'job_id': job_id,
            'status': STATUS_QUEUED,
            'params': params,
            'image_path': '',
            'error': '',
            'submitted_at': time.time(),
            'started_at': None,
            'finished_at': None,
            'elapsed': None,
            'runtime': None,
            'seed': None,
        }
        with self._lock:
            self._jobs[job_id] = rec
            self._order.append(job_id)
            while len(self._order) > self.keep:      # 内存里只留最近 keep 条
                self._jobs.pop(self._order.pop(0), None)
        self._persist(rec)
        return rec

    def get(self, job_id: str):
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **kw):
        with self._lock:
            rec = self._jobs.get(job_id)
            if not rec:
                return None
            rec.update(kw)
            snap = dict(rec)
        self._persist(snap)
        return snap

    def recent(self, limit: int = 20) -> list:
        with self._lock:
            ids = self._order[-limit:][::-1]
            return [dict(self._jobs[i]) for i in ids if i in self._jobs]

    def counts(self) -> dict:
        with self._lock:
            c = {}
            for r in self._jobs.values():
                c[r['status']] = c.get(r['status'], 0) + 1
            return c


# ══════════════ 生成 worker（单卡严格串行，与旧链路一致）══════════════
class FluxWorker(threading.Thread):
    def __init__(self, store: JobStore, pipeline_holder: dict):
        super().__init__(daemon=True, name='flux-gen-worker')
        self.store = store
        self.holder = pipeline_holder       # {'pipe','ready','error','offload'}
        self.q = queue.PriorityQueue()
        self._seq = 0
        self._stop = threading.Event()
        self.current_job = None
        self._lock = threading.Lock()

    def submit(self, job_id: str, priority: int = 0):
        with self._lock:
            self._seq += 1
            self.q.put((int(priority), self._seq, job_id))

    def cancel_queued(self, job_id: str) -> bool:
        """把还在队列里的任务标记取消。生成中的不打断（GPU 中途打断风险高）。"""
        rec = self.store.get(job_id)
        if not rec or rec['status'] != STATUS_QUEUED:
            return False
        self.store.update(job_id, status=STATUS_CANCELED,
                          error='已取消（排队中）', finished_at=time.time())
        return True

    @property
    def depth(self) -> int:
        return self.q.qsize()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                _, _, job_id = self.q.get(timeout=1)
            except queue.Empty:
                continue
            rec = self.store.get(job_id)
            if not rec or rec['status'] != STATUS_QUEUED:
                continue                                  # 排队期间被取消
            # ── 换模型（2026-09-21 新增）──────────────────────────────────────
            # 位置很关键：**在单 worker 循环里、真正开跑之前**。
            # 因为 worker 是串行队列，这里天然就是「排队等」——
            # 前面的任务跑完才轮到我，此时才换模型，不打断任何人。
            # 若放到 HTTP 层去做，就会出现「用户 A 刚提交、用户 B 一选模型就把
            # A 正在生成的那张图的模型换掉」→ 用错模型出图，且极难排查。
            self._ensure_model(rec)
            if self.store.get(job_id)['status'] != STATUS_QUEUED:
                continue                                  # 换模型期间被取消
            while not self.holder['ready'] and not self.holder['error'] and not self._stop.is_set():
                time.sleep(0.5)                           # 模型还在加载：等着，不丢任务
            if self.holder['error']:
                self.store.update(job_id, status=STATUS_FAILED,
                                  error=f"模型未就绪: {self.holder['error']}",
                                  finished_at=time.time())
                continue
            self.current_job = job_id
            try:
                self._generate(rec)
            finally:
                self.current_job = None

    def _ensure_model(self, rec: dict):
        """任务要求了别的模型 → 就地把常驻模型换掉（阻塞到加载完成）。

        不换的两种情况都要**明确放过**，否则会把能跑的任务卡死：
          1. 任务没指定 model（`None`）→ 用当前模型，兼容所有老调用方
          2. 指定的就是当前模型 → 零开销

        ⚠️ **stub 模式也走这条路径**（2026-09-21 修正）。曾经在这里对 stub 直接
        return —— 理由是「stub 没有真实模型可言」。但那让「换模型」这条唯一的
        风险路径**在离线环境完全无法验证**：只有 GPU 才能试，而它又正是最容易错的地方
        （profile 重算、队列换模型时序、失败处置）。stub 现在同样认 model_id 并重算画像，
        于是 `tools/_e2e_models_tmp.py` 能零成本覆盖整条链路。
        """
        want = (rec.get('params') or {}).get('model_id')
        if not want:
            return
        cur = self.holder.get('model_id') or ''
        if want == cur:
            return

        models = scan_models()
        path, err = resolve_model_dir(want, models)
        if err:
            # ⚠️ 不把任务标失败：模型清单可能只是暂时读不到（磁盘抖动/权限）。
            #    记在 holder 上让 /health 可见，任务留给用户重试 —— 直接判死并退款
            #    会把「选错模型」和「机器故障」混成一件事。
            self.holder['error'] = f'切换模型失败：{err}'
            print(f'[fluxd] ❌ 切换模型失败（任务 {rec["job_id"]}）：{err}', flush=True)
            return
        stub = self.holder.get('offload') == 'stub'
        offload = 'stub' if stub else (self.holder.get('offload') or 'model')
        print(f'[fluxd] 🔁 任务 {rec["job_id"]} 要求 {want}，'
              f'当前 {cur or "(未加载)"} → 开始排队切换（offload={offload}）', flush=True)
        self.holder['ready'] = False
        # 复用本机原有的 offload / stub 策略：换模型不该顺带改显存策略
        load_model_async(self.holder, str(path), offload, stub)
        # 阻塞等它加载完（worker 串行队列本身就是「排队等」的载体）
        while not self.holder['ready'] and not self._stop.is_set():
            time.sleep(0.5)
        if self.holder['ready']:
            print(f'[fluxd] ✅ 已切到 {want}', flush=True)

    def _generate(self, rec: dict):
        job_id = rec['job_id']
        p = rec['params']
        self.store.update(job_id, status=STATUS_GENERATING, started_at=time.time())
        t0 = time.time()
        try:
            seed = p.get('seed')
            if seed is None:
                seed = int(time.time() * 1000) % (2 ** 31)   # 随机但落账，可复现
            seed = int(seed)
            if self.holder.get('offload') == 'stub':
                generator = _SeedGen(seed)
            else:
                import torch                             # 真实路径才需要 torch
                generator = torch.Generator('cpu').manual_seed(seed)
            # ── 调用参数：**一律按 __call__ 签名过滤**（2026-09-19 修）────────────
            # 教训：原先 `negative_prompt` 是无条件硬传的。FLUX.1-dev 的 FluxPipeline
            # 接受它，但蒸馏版 klein 的 Flux2KleinPipeline **根本不接受**，而两个 pipeline
            # 都**没有 kwargs 兜底**（已用 inspect 实测）→ 传错就是硬 TypeError。
            # 2026-09-19 带卡实测：klein 上 t2i 与 edit 共 5/5 任务全 FAIL，
            # 报 `TypeError: Flux2KleinPipeline.__call__() got an unexpected keyword
            # argument 'negative_prompt'`。
            #
            # ⚠️ 离线为什么全绿：StubPipeline 当时比真实 klein **更宽松**
            # （既声明了 negative_prompt、又带 **kw），把真实约束掩盖了。
            # 替身一旦比真实模型宽松，「离线全绿」就不再是任何保证 —— 本文件已把
            # StubPipeline 收紧到与 klein 同签名。
            #
            # 现在统一按签名过滤：签名里没有的参数**不传**，并打印被丢弃的键，
            # 让「模型不支持某参数」在日志里看得见，而不是靠人去猜。
            import inspect
            sig = set(inspect.signature(self.holder['pipe'].__call__).parameters)
            # 采样参数按**模型画像**取默认值，不再用全局常量（2026-09-21 修复）——
            # 蒸馏版 klein 必须 steps≈4 / guidance=1.0，用 dev 时代的 25/3.5 会静默劣化质量。
            prof = self.holder.get('profile') or {}
            p_steps = int(prof.get('default_steps') or DEFAULT_STEPS)
            p_guid = float(prof.get('guidance') or DEFAULT_GUIDANCE)
            cand = {
                'negative_prompt': p.get('negative_prompt') or '',
                'num_inference_steps': int(p.get('steps') or p_steps),
                'guidance_scale': float(p.get('guidance_scale') or p_guid),
                'width': int(p.get('width') or DEFAULT_WIDTH),
                'height': int(p.get('height') or DEFAULT_HEIGHT),
                'generator': generator,
            }
            kw = {k: v for k, v in cand.items() if k in sig}
            dropped = sorted(set(cand) - set(kw))
            if dropped:
                print(f'[fluxd] 当前模型不支持这些参数，已丢弃: {dropped}', flush=True)

            # ── 图生图分支（2026-09-18 新增）────────────────────────────
            # 传了参考图才带图像参数。**参数名同样按签名动态判定**，不硬编码：
            #   klein → `image`（已实测定死）；FLUX.1-dev 没有该参数 → 明确报错，
            #   不静默降级成文生图（那会照常出图、照常扣费，却完全不是用户要的）。
            ref_path = p.get('image_path')
            if ref_path:
                img_param = next((n for n in ('image', 'images', 'image_latents') if n in sig), None)
                if img_param is None:
                    raise RuntimeError(
                        f'当前模型不支持图生图：__call__ 没有 image/images/image_latents 参数'
                        f'（可用参数：{sorted(sig)[:15]}...）'
                    )
                from PIL import Image as _Img
                ref_img = _Img.open(ref_path)
                # 保持宽高比 + 色彩管理（与 klein-setup/ab_klein_dev.py 的 load_ref_image 一致）
                ref_img = _prepare_ref_image(ref_img, int(kw.get('width') or DEFAULT_WIDTH))
                kw['image'] = ref_img
                print(f'[fluxd] edit 模式：参考图 {ref_path} → 参数名 {img_param!r}', flush=True)
            # ⚠️ 必须用**关键字**传 prompt（2026-09-19 修，第二个地雷）：
            #    Flux2KleinPipeline.__call__ 的第一个位置参数是 **image**、prompt 在第二位
            #    （inspect 实测 ['self','image','prompt','height',...]）；
            #    FluxPipeline(dev) 才是 ['self','prompt','prompt_2',...]。
            #    原先写 `pipe(p['prompt'], **kw)` → 对 klein 等于 image=提示词文本，
            #    图生图时更会撞成 "got multiple values for argument 'image'"。
            #    统一走关键字，两种模型都对。
            if 'prompt' not in sig:
                raise RuntimeError(
                    f'当前模型的 __call__ 没有 prompt 参数（可用参数：{sorted(sig)[:15]}...）')
            out = self.holder['pipe'](prompt=p['prompt'], **kw)
            img_path = self.store.out_dir / f'{job_id}.png'
            out.images[0].save(str(img_path))
            elapsed = round(time.time() - t0, 2)
            self.store.update(job_id, status=STATUS_DONE, image_path=str(img_path),
                              seed=int(seed), runtime=elapsed, finished_at=time.time())
            print(f"[fluxd] OK   {job_id} {elapsed}s seed={seed} {img_path.name}", flush=True)
        except Exception as e:
            self.store.update(job_id, status=STATUS_FAILED,
                              error=f'{type(e).__name__}: {e}', finished_at=time.time())
            print(f"[fluxd] FAIL {job_id}: {type(e).__name__}: {e}", flush=True)
        finally:
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass


def _prepare_ref_image(im, size: int):
    """把参考图规整成能喂给模型的图。

    【2026-09-18 新增，修掉两个实测问题】
      1. 「宽高比被破坏 → 人腿变短」：原做法强制 resize 成正方形，1200×1800 的竖构图
         高度被压掉 43%，主体明显变形 → 改为**保持宽高比 + 居中 pad**。
         （不用 crop —— 那会把商品裁掉。）
      2. 「输出偏暗发灰」：`.convert("RGB")` 会丢弃 ICC 色彩配置（Unsplash 等多是
         Display-P3），按裸 RGB 解读损失饱和度；且 resize 未指定重采样
         → 改为带 ICC 时用 ImageCms 转 sRGB + 显式 LANCZOS。
    实测证据（修正前 4 张 edit 的对比度 4/4 全部下降 = 发灰）。

    ⚠️ 【2026-09-19 修 · 缩放基准 min→max】
      上面第 1 条当时**只改对了一半**：`scale = size / min(w, h)` 让「长边」溢出画布，
      而 `paste` 的负偏移会被 PIL **静默裁掉**（不报错、不警告）——
      等于把「压扁 43%」换成了「裁掉 33%」，用户看到的还是「人腿变短」。
      实测（size=1024，用 klein-setup/refs/ 的原始素材跑本函数）：
        ref-jeans.jpg 1200×1800 → min 得 1024×1536 → 画布 1024×1024 → **高度只留 66.7%**
        ref-shoes.jpg 1200×960  → min 得 1280×1024 → 画布 1024×1024 → **宽度只留 80.0%**
      必须用 `max` 才能保证长边 ≤ 画布，从而真正「完整保留 + 居中 pad」。
      （同源错误也在 klein-setup/ab_klein_dev.py:178，已同步修 —— 否则 A/B 对照与
        本函数用的预处理不一致，对照结论就不可比。）
      回归门：tests/test_prepare_ref_image.py（断言不裁切 + 宽高比保持）。
    """
    from PIL import Image as _Img

    icc = getattr(im, 'info', {}).get('icc_profile')
    if icc:
        try:
            from io import BytesIO
            from PIL import ImageCms
            src = ImageCms.ImageCmsProfile(BytesIO(icc))
            dst = ImageCms.createProfile('sRGB')
            im = ImageCms.profileToProfile(im, src, dst, outputMode='RGB')
        except Exception:  # noqa: BLE001
            pass                                     # 色彩转换失败不阻断出图
    if im.mode != 'RGB':
        im = im.convert('RGB')

    w, h = im.size
    if w == h:
        return im.resize((size, size), _Img.LANCZOS)
    # ⚠️ 必须用 max：用 min 会让长边溢出画布，paste 的负偏移被 PIL 静默裁掉
    #    （2026-09-18 的版本就是这个错，2026-09-19 修，详见上面 docstring）
    #    不变量：max(nw, nh) == size，故 paste 偏移恒 >= 0，内容零裁切。
    scale = size / max(w, h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    im = im.resize((nw, nh), _Img.LANCZOS)
    canvas = _Img.new('RGB', (size, size), (127, 127, 127))   # 中灰填充，不引入色彩倾向
    canvas.paste(im, ((size - nw) // 2, (size - nh) // 2))
    return canvas


def _resolve_pipe_class(model_path: str):
    """按模型目录的 model_index.json 推断 diffusers 主类。

    【2026-09-18 新增】原先硬编码 `FluxPipeline` —— 换 klein（Flux2KleinPipeline）必失败。
    以模型自述的 `_class_name` 为准，不硬编码类名：
      FLUX.1-dev        → FluxPipeline
      FLUX.2-klein-4B   → Flux2KleinPipeline（diffusers 0.39.0 自带，不需 git 版）
    """
    import json
    from pathlib import Path as _P
    mi_path = _P(model_path) / 'model_index.json'
    if not mi_path.is_file():
        raise RuntimeError(f'{model_path} 下没有 model_index.json，不是 diffusers 格式的模型目录')
    mi = json.loads(mi_path.read_text(encoding='utf-8'))
    cls_name = (mi.get('_class_name') or '').strip()
    if not cls_name:
        raise RuntimeError(f'{mi_path} 里没有 _class_name 字段')
    import diffusers
    cls = getattr(diffusers, cls_name, None)
    if cls is None:
        raise RuntimeError(f'diffusers {getattr(diffusers, "__version__", "?")} 里找不到 {cls_name}')
    return cls, cls_name


# ── 模型采样参数画像（2026-09-21 新增，此前是个**真实缺陷**）─────────────
#
# 事故：DEFAULT_STEPS=25 / DEFAULT_GUIDANCE=3.5 是 FLUX.1-dev 时代遗留的默认值，
# 换到 klein 后**从未按模型校正**。而 klein 4B 是 **step-distilled + guidance-distilled**
# 模型 —— 官方固定 4 步、guidance 锁 1.0（见 BFL 官方文档与 HF 模型卡）。
#
# 后果（两条，都是静默的）：
#   1. **guidance 错 3.5 倍**：站点从不传 guidance_scale，于是恒用 3.5。
#      蒸馏版把 CFG 烘进权重，推理时 guidance 必须为 1.0；给 3.5 属于超范围外推，
#      有害无益。这是**从部署第一天起就一直在发生的**质量损失。
#   2. **steps 空转**：蒸馏版 4 步就完成去噪，>8 步官方明确说质量**下降**。
#      实测佐证（2026-09-20，同一 prompt/seed）：15 步 16s、35 步 164s ——
#      耗时差主要来自队列拥挤而非步数，说明多出的步数在蒸馏模型上几乎不产生有效计算。
#
# 为什么不改 DEFAULT_STEPS 常量了事：那只是把 25 换成 4，**dev 机换回来就又错了**。
# 参数画像必须是**模型属性**，跟着加载的模型走 —— 所以在这里按类名推断。
#
# ⚠️ 这张表是**声明**，不是实测：klein 蒸馏版 4 步/1.0 来自 BFL 官方文档
#    （docs.bfl.ai/flux_2: "Inference steps 4 (step-distilled)"、"guidance 1.0"）。
#    Base 变体的 50 步/4.0 同样来自官方。dev 的 25 步/3.5 是本站历史实测值。
MODEL_PROFILES = {
    # 类名 → (默认步数, 建议上限, 默认 guidance, 备注)
    'Flux2KleinPipeline': (4, 8, 1.0,
                           'klein 4B/9B **蒸馏版**：官方 4 步、guidance 锁 1.0；'
                           '社区实测 >8 步质量下降'),
    'FluxPipeline': (25, 100, 3.5,
                     'FLUX.1-dev：guidance-distilled，步数越多越好（本站历史默认 25）'),
}
# 未识别的类名 → 退回保守画像（宁可慢，不要崩）
FALLBACK_PROFILE = (DEFAULT_STEPS, MAX_STEPS, DEFAULT_GUIDANCE,
                    '未知模型：使用保守默认值')

# ── 图生图能力表（2026-09-21 新增）──────────────────────────────
# 图生图要求 pipeline 的 __call__ 接受 `image` 参数。
#   · Flux2KleinPipeline（klein 系列）接受  → 能跑图生图
#   · FluxPipeline（FLUX.1-dev）**没有这个参数** → 跑了必然报「当前模型不支持图生图」
#     （2026-09-20 实测：edit 任务落在 dev 机上 100% 失败）
#
# ★ 为什么这张表必须在**这里**（而不是让前端按模型名猜）：
#   这是**模型的能力属性**，只有加载它的这一侧真正知道。前端若用
#   `/klein/i.test(id)` 这类名字正则判断，将来加一个名字里不含 "klein"
#   但支持编辑的模型就会误判（反之亦然），而且判断逻辑散落在两个仓库里。
#   现在上游声明 → /api/models 透传 → 前端只消费 `supports_edit` 布尔值。
MODEL_EDIT_CAPABLE = {
    'Flux2KleinPipeline': True,
    'FluxPipeline': False,
}
# 未识别的类名 → False（保守：说不支持，顶多让用户换个模型；
# 说支持却跑不了，用户会白等一整轮才收到失败）
FALLBACK_EDIT_CAPABLE = False


def model_supports_edit(cls_name: str) -> bool:
    """这个 pipeline 能不能跑图生图。按类名查表，未识别 → False（保守）。"""
    return MODEL_EDIT_CAPABLE.get(cls_name or '', FALLBACK_EDIT_CAPABLE)


def model_profile(cls_name: str):
    """按 pipeline 类名取采样参数画像，返回 (default_steps, max_steps, guidance, note)。

    识别不到时退回 FALLBACK_PROFILE —— 与旧行为完全一致，不会让新模型跑不起来。
    """
    return MODEL_PROFILES.get(cls_name, FALLBACK_PROFILE)


# ── 可选模型清单（2026-09-21 新增：界面选模型）──────────────────────────
#
# 【为什么不在客户端硬编码模型列表】
#   「这台机器上有哪些模型」是**机器属性**，会随克隆/下载变化。写死在站点或 manager 里，
#   换台机就得改代码 —— 正是 servers.json 那次事故的同一种病（~/.ssh/config 在仓库外、
#   不在版本控制里 → 克隆实例后 manager 永远看不见那台机器）。
#   所以清单由**服务端扫盘得出**，客户端只做展示与选择。
#
# 【扫描规则】
#   FLUX_MODEL_DIRS（冒号分隔）下的一级子目录，且含 model_index.json（diffusers 格式）。
#   没有 DOWNLOAD_DONE 的**也列出但标 ready=false** —— 半下载的模型要能看见，
#   否则用户选了才发现跑不了，比直接不列更糟。
DEFAULT_MODEL_DIRS = '/root/klein-models:/root/autodl-tmp/models'


def _split_dirs(raw: str) -> list:
    """切分模型目录列表。同时容忍冒号（Linux）与分号（Windows）分隔。

    ⚠️ **不能直接 `raw.split(':')`**：Windows 盘符自带冒号，`C:/a:C:/b` 会被切成
    `['C', '/a', 'C', '/b']` —— 目录全部失效，而且表现出来是「扫不到任何模型」，
    看起来像磁盘/权限问题（本地联调实测踩过）。所以按冒号切完后要把
    「单字母盘符 + 后面那一段」重新粘回去。
    """
    parts = [p.strip() for p in raw.replace(';', ':').split(':')]
    out, i = [], 0
    while i < len(parts):
        cur = parts[i]
        # 盘符特征：恰好 1 个字母，且它是本段最后一个字符（说明冒号被切掉了）
        if len(cur) == 1 and cur.isalpha() and i + 1 < len(parts):
            out.append(f'{cur}:{parts[i + 1]}')     # 粘回 "C:" + "/path"
            i += 2
            continue
        if cur:
            out.append(cur)
        i += 1
    return out


def _model_dirs() -> list:
    raw = os.environ.get('FLUX_MODEL_DIRS') or DEFAULT_MODEL_DIRS
    return [Path(p) for p in _split_dirs(raw)]


def scan_models() -> list:
    """扫出所有可用模型。返回 [{id, path, name, class_name, ready, size_gb, profile}]。

    `id` = 目录名（如 `FLUX.2-klein-4B`），它是**跨层稳定标识**：
    写进 jobs.model → 透传到 /generate 的 model 参数 → 服务端按 id 反查路径。
    用 basename 而不是全路径，是为了让站点不必知道服务器的目录布局。
    """
    out = []
    for d in _model_dirs():
        if not d.is_dir():
            continue
        for sub in sorted(d.iterdir()):
            if not sub.is_dir():
                continue
            mi = sub / 'model_index.json'
            if not mi.is_file():
                continue                     # 不是 diffusers 目录（可能是缓存/分片目录）
            cls_name = ''
            try:
                cls_name = (json.loads(mi.read_text(encoding='utf-8'))
                            .get('_class_name') or '')
            except Exception:                # noqa: BLE001 —— 坏 json 不该让整个清单塌掉
                cls_name = ''
            d_steps, m_steps, guid, note = model_profile(cls_name)
            out.append({
                'id': sub.name,
                'path': str(sub),
                'name': sub.name,
                'class_name': cls_name,
                # ★ 图生图能力（2026-09-21 新增）：由**上游按 pipeline 类名声明**，
                #   前端据此置灰/强制选模型，不要再用 `id` 里有没有 "klein" 去猜。
                'supports_edit': model_supports_edit(cls_name),
                'ready': (sub / 'DOWNLOAD_DONE').is_file(),
                'size_gb': _du_gb(sub),
                'profile': {'default_steps': d_steps, 'max_steps': m_steps,
                            'guidance': guid, 'note': note},
            })
    return out


def _du_gb(p: Path) -> float:
    """目录体积（GB，1 位小数）。只 stat，不读内容 —— 32G 的模型读一遍要好几分钟。

    失败返回 0（体积只是展示信息，不值得为它让整个清单失败）。
    """
    total = 0
    try:
        for f in p.rglob('*'):
            if f.is_file():
                total += f.stat().st_size
    except Exception:                        # noqa: BLE001
        return 0.0
    return round(total / (1024 ** 3), 1)


def resolve_model_dir(model_id: str, models: list = None):
    """按 id 反查模型目录。返回 (path, err)。

    ⚠️ 只接受**清单里存在**的 id，不接受任意路径 —— 否则 model 参数会变成
    一个任意目录读取原语（传 `../../etc` 之类）。这是本期新增的外部输入，
    必须按白名单收口。
    """
    if not model_id:
        return None, None                    # 未指定 → 由调用方用当前模型
    for m in (models if models is not None else scan_models()):
        if m['id'] == model_id:
            if not m['ready']:
                return None, f'模型 {model_id} 尚未下载完成（缺 DOWNLOAD_DONE）'
            return Path(m['path']), None
    return None, f'未知模型 {model_id!r}（本机没有这个模型，GET /models 看可用清单）'


def load_model_async(holder: dict, model_path: str, offload: str, stub: bool):
    """后台加载模型，让 /health 在加载期间就能响应（status=loading）。

    【2026-09-21 改造：支持换模型】加 `switch_to` 参数后，本函数在**已加载**时
    会把新模型换上去（worker 侧串行调用，天然实现「排队等」）。

    ⚠️ 换模型的正确姿势是 **整体替换 holder['pipe']**，而不是就地 __setattr__：
      旧 pipe 必须先释放（del + 显式 gc）再加载新的，否则 32G 的 dev + 15G 的 klein
      同时驻留显存 → OOM。所以下面加载成功后才做「旧值出让」，且强制 gc。
    """
    target = {'path': str(model_path), 'offload': offload, 'stub': stub}
    holder['target'] = target               # 让 /health 能显示「正在切到哪个」

    def _load():
        try:
            if stub:
                holder['pipe'] = StubPipeline()
                holder['offload'] = 'stub'
                holder['model_path'] = str(model_path)
                holder['model_id'] = Path(model_path).name
                # ⚠️ STUB 也要给 profile：否则 /health 的 profile 是空的，
                #    离线端到端测「换模型 → 采样参数跟着变」时读不到值 ——
                #    而这条路径正是**唯一能不上 GPU 验证模型切换**的手段。
                #    按 model_index.json 的真实类名取画像（stub 也要忠实）。
                cls_name = ''
                try:
                    mi = Path(model_path) / 'model_index.json'
                    cls_name = (json.loads(mi.read_text(encoding='utf-8'))
                                .get('_class_name') or '')
                except Exception:            # noqa: BLE001
                    pass
                holder['class_name'] = cls_name
                d_steps, m_steps, guid, note = model_profile(cls_name)
                holder['profile'] = {'default_steps': d_steps, 'max_steps': m_steps,
                                     'guidance': guid, 'note': note}
                holder['ready'] = True
                holder['error'] = ''
                print(f'[fluxd] STUB 模式：未加载真实模型，输出为合成占位图'
                      f'（model_id={holder["model_id"]}, 画像 {d_steps}步/g={guid}）', flush=True)
                return
            import torch
            print(f'[fluxd] 加载模型 {model_path} (offload={offload}) ...', flush=True)
            cls, cls_name = _resolve_pipe_class(model_path)
            # ⚠️ device_map 与 enable_*_cpu_offload() 是**互斥**的装载策略，不能并用：
            #    `device_map` 已自动把层摊到 CPU/GPU；再调 offload 会抛
            #    "you have activated a device mapping strategy ... reset_device_map() first"，
            #    退到 sequential 仍是同一个错 → 整条加载失败（2026-09-18 实测踩过）。
            # 所以 'balanced' 必须在**加载时**就带上 device_map，不能先加载再补 offload。
            if offload == 'balanced':
                pipe = cls.from_pretrained(model_path, torch_dtype=torch.bfloat16,
                                           device_map='balanced')
            else:
                pipe = cls.from_pretrained(model_path, torch_dtype=torch.bfloat16)
                if offload == 'sequential':
                    pipe.enable_sequential_cpu_offload()
                elif offload == 'model':
                    pipe.enable_model_cpu_offload()
                else:  # none / 其余 → 全程显存
                    pipe.to('cuda')

            # ── 新模型就绪，此时才出让旧的（换模型路径的关键）──────────────
            old = holder.get('pipe')
            if old is not None and old is not pipe:
                holder['pipe'] = None        # 先摘掉引用，避免新加载期间被误用
                del old
                try:
                    import gc
                    gc.collect()
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:            # noqa: BLE001 —— 释放失败不该让加载失败
                    pass

            holder['pipe'] = pipe
            holder['offload'] = offload
            holder['class_name'] = cls_name
            holder['model_path'] = str(model_path)
            holder['model_id'] = Path(model_path).name
            # 采样参数画像跟着模型走（见 MODEL_PROFILES 的事故说明）
            d_steps, m_steps, guid, note = model_profile(cls_name)
            holder['profile'] = {'default_steps': d_steps, 'max_steps': m_steps,
                                 'guidance': guid, 'note': note}
            holder['ready'] = True
            holder['error'] = ''             # 换模型成功要清掉上一次的 error
            print(f'[fluxd] 模型常驻就绪 ✅ ({cls_name}, offload={offload}) | '
                  f'画像: steps 默认 {d_steps} 上限 {m_steps}, guidance {guid}', flush=True)
        except Exception as e:
            holder['error'] = f'{type(e).__name__}: {e}'
            # ⚠️ 换模型失败时**必须保持 ready=False**：旧 pipe 已在上面被释放，
            #    此时若还是 ready=True，worker 会拿一个 None 去推理 → 整批任务崩。
            #    ready=False 会让 worker 停在等待循环里，用户看到的是「切模型失败」，
            #    而不是一串莫名其妙的 TypeError。
            holder['ready'] = False
            holder['pipe'] = None
            print(f'[fluxd] 模型加载失败 ❌ {holder["error"]}', flush=True)

    threading.Thread(target=_load, daemon=True, name='flux-model-loader').start()


# ══════════════════════════ HTTP 层 ══════════════════════════
def _norm_dim(v, default: int) -> int:
    """把尺寸归一到 16 的倍数并夹在 [MIN_DIM, MAX_DIM]。"""
    if v in (None, ''):
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise BadRequest(f'尺寸必须是整数，收到 {v!r}') from None
    if n < MIN_DIM or n > MAX_DIM:
        raise BadRequest(f'尺寸需在 {MIN_DIM}~{MAX_DIM} 之间，收到 {n}')
    return int(round(n / DIM_MULTIPLE)) * DIM_MULTIPLE


class Handler(BaseHTTPRequestHandler):
    server_version = 'flux-resident/1.0'
    sys_version = ''                       # 不回传 Python 版本（收敛指纹）
    store: JobStore = None
    worker: 'FluxWorker' = None
    holder: dict = None
    token: str = ''
    started_at: float = 0.0

    # ── 工具 ──
    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str):
        self._json(code, {'error': msg})

    def _auth_ok(self) -> bool:
        if not self.token:
            return True
        if self.headers.get('X-Auth-Token', '') == self.token:
            return True
        self._err(401, 'X-Auth-Token 缺失或不正确')
        return False

    def _query(self, key: str, default=None):
        from urllib.parse import urlparse, parse_qs
        v = parse_qs(urlparse(self.path).query).get(key)
        return v[0] if v else default

    def _body(self, max_bytes: int = MAX_REQ_BYTES) -> dict:
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0:
            return {}
        if n > max_bytes:
            raise BadRequest(f'请求体过大（{n}B > {max_bytes}B）')
        try:
            return json.loads(self.rfile.read(n).decode('utf-8') or '{}')
        except json.JSONDecodeError as e:
            raise BadRequest(f'请求体不是合法 JSON: {e}') from None

    def log_message(self, fmt, *args):     # 收敛默认日志噪音
        pass

    # ── 路由 ──
    def do_GET(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path.rstrip('/') or '/'
        if path == '/health':
            return self._health()
        if not self._auth_ok():
            return
        if path == '/models':
            return self._models()
        if path == '/status':
            return self._status()
        if path == '/image':
            return self._image()
        if path == '/jobs':
            return self._jobs()
        return self._err(404, f'未知路径 {path}')

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path.rstrip('/') or '/'
        if not self._auth_ok():
            return
        try:
            body = self._body(MAX_EDIT_BYTES if path == '/edit' else MAX_REQ_BYTES)
        except BadRequest as e:
            return self._err(400, str(e))
        if path == '/generate':
            return self._generate(body)
        if path == '/edit':
            return self._generate(body, is_edit=True)
        if path == '/cancel':
            return self._cancel(body)
        return self._err(404, f'未知路径 {path}')

    # ── 各端点 ──
    def _gpu_info(self):
        if self.holder.get('offload') == 'stub':
            return 'STUB(no-gpu)'
        try:
            import subprocess
            r = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.used,memory.total',
                                '--format=csv,noheader'],
                               capture_output=True, text=True, timeout=5)
            return r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else 'unknown'
        except Exception:
            return 'unknown'

    def _health(self):
        ready = bool(self.holder.get('ready'))
        err = self.holder.get('error') or ''
        return self._json(200, {
            'status': 'error' if err else ('ok' if ready else 'loading'),
            'model_loaded': ready,
            'model_error': err,
            'class_name': self.holder.get('class_name') or '',
            # 当前加载的是哪个模型（2026-09-21 新增）—— 界面选模型后要能验证「真的换上了」，
            # 而不是只看「有一张图出来了」。
            'model_id': self.holder.get('model_id') or '',
            'model_path': self.holder.get('model_path') or '',
            # 切换目标：非空且与 model_id 不同 = 正在切。界面据此显示「切换中…」
            'switching_to': ((self.holder.get('target') or {}).get('path') or '').split('/')[-1]
                            if (self.holder.get('target') or {}).get('path') else '',
            # 采样参数画像（2026-09-21 新增）—— 让「这个模型该用几步/多少 guidance」
            # 成为**可远程读到的事实**，而不是散落在代码和设备记忆里。
            'profile': self.holder.get('profile') or {},
            'offload': self.holder.get('offload') or '',
            'queue_depth': self.worker.depth,
            'current_job': self.worker.current_job,
            'job_counts': self.store.counts(),
            'gpu': self._gpu_info(),
            'uptime_sec': round(time.time() - self.started_at, 1),
        })

    def _models(self):
        """可用模型清单。供界面渲染选择器 + 校验 model 参数合法性。"""
        try:
            models = scan_models()
        except Exception as e:                   # noqa: BLE001 —— 扫盘失败也要给结构化错误
            return self._json(500, {'error': f'扫描模型目录失败: {e}'})
        return self._json(200, {
            # ⚠️ 这里是**白名单**不是黑名单：只放行这些键，`path`（GPU 上的绝对路径）
            #    绝不能出去（对外站点不该知道服务器目录布局）。
            #    新增可外泄字段时**必须显式加进这个元组** —— 反过来（把 path pop 掉）
            #    将来加字段就会漏出去。
            'models': [{k: m[k] for k in ('id', 'name', 'class_name', 'ready',
                                          'size_gb', 'profile', 'supports_edit')}
                       for m in models],
            'current': self.holder.get('model_id') or '',
            'dirs': [str(d) for d in _model_dirs()],
        })

    def _generate(self, body: dict, is_edit: bool = False):
        """提交生成任务。

        `is_edit=True`（走 POST /edit）时要求带参考图，多接一个 `image` 字段。
        参考图两种传法：
          - `image`     : base64（可带 `data:image/png;base64,` 前缀）
          - `image_url` : 服务端可达的 http(s) URL（本机路径不建议，避免 SSRF 面）
        落盘到 out_dir/refs/ 后把**路径**放进 params —— 队列是异步的，
        不能把 PIL 对象或大 base64 长期留在内存里的 job 记录里。

        `model`（2026-09-21 新增）：**模型 id**（如 `FLUX.2-klein-4B`），来自 GET /models。
        不传 = 用当前已加载的模型（完全兼容老调用方）。传了就由 worker 在
        该任务真正开跑前**排队切换**（见 FluxWorker._ensure_model）。
        """
        prompt = (body.get('prompt') or '').strip()
        if not prompt:
            return self._err(400, 'prompt 不能为空')

        # ── 解析目标模型（先于其它校验：后面的 steps 上限要按**目标**模型算）──
        want_model = (body.get('model') or '').strip()
        model_id = None
        if want_model:
            try:
                models = scan_models()
            except Exception as e:               # noqa: BLE001
                return self._err(500, f'无法读取模型清单: {e}')
            path, err = resolve_model_dir(want_model, models)
            if err:
                return self._err(400, err)
            model_id = want_model
            prof = next((m['profile'] for m in models if m['id'] == model_id), {})
            tgt_edit = next((m['supports_edit'] for m in models
                             if m['id'] == model_id), None)
        else:
            prof = self.holder.get('profile') or {}
            # 「不传 model」= 用当前已加载的模型 → 它的能力要看**当前那个**。
            # 用 id 回查清单而不是另存一份 holder 字段：能力表的唯一来源是类名，
            # 多存一份就会漂移。查不到时给 None（= 不知道，不拦）。
            cur_id = self.holder.get('model_id') or ''
            tgt_edit = None
            if cur_id:
                try:
                    tgt_edit = next((m['supports_edit'] for m in scan_models()
                                     if m['id'] == cur_id), None)
                except Exception:                # noqa: BLE001 —— 扫盘失败不该拦住任务
                    tgt_edit = None

        # ── ★ 图生图硬闸（2026-09-21 新增）──────────────────────────────
        # 图生图要求 pipeline 的 __call__ 接受 `image`。FLUX.1-dev 的
        # FluxPipeline **没有这个参数**，落在 dev 上必然失败（2026-09-20 实测）。
        #
        # 为什么要在**这里**再拦一道（前端已经置灰了非 klein 的模型）：
        #   前端的约束可以被绕过 —— 老版本页面、直调 API、或前端将来改坏。
        #   而这里的判据是**能力表**，不依赖调用方自觉。
        #   报错必须明确说「换模型」，不能说成服务故障 —— 否则用户会一直重试。
        if is_edit and tgt_edit is False:
            return self._err(400, f'当前模型不支持图生图（{model_id or "当前已加载的模型"}）。'
                                  f'请改用支持图生图的模型（klein 系列），或去掉参考图用文生图。')

        # ⚠️ 这里的 ready/error 校验只用「不换模型」时把门。
        #    换模型时，当前 holder 可能就是**另一个**模型的状态（比如当前是 dev、
        #    用户要 klein，而 dev 正加载失败）—— 拿它去拒绝一个本来能跑的任务是错的。
        #    换模型路径下由 worker 的 _ensure_model 负责加载与失败处置。
        if not model_id:
            if self.holder.get('error'):
                return self._err(503, f"模型不可用: {self.holder['error']}")
            if not self.holder.get('ready'):
                return self._err(503, '模型仍在加载，请稍后重试（GET /health 看 model_loaded）')
        try:
            width = _norm_dim(body.get('width'), DEFAULT_WIDTH)
            height = _norm_dim(body.get('height'), DEFAULT_HEIGHT)
            steps = int(body.get('steps') or prof.get('default_steps') or DEFAULT_STEPS)
            max_steps = int(prof.get('max_steps') or MAX_STEPS)
        except BadRequest as e:
            return self._err(400, str(e))
        # 上限跟着**目标模型**走：klein 蒸馏版给 8（官方 4 步，>8 反而劣化），dev 给 100。
        # 这里**仍允许显式超出建议值**（只卡硬上限），因为用户可能确实在试参数；
        # 但不再允许 100 步这种对蒸馏模型毫无意义、只烧 GPU 的请求。
        if not (1 <= steps <= max_steps):
            who = model_id or self.holder.get('class_name') or '当前模型'
            return self._err(
                400,
                f'steps 需在 1~{max_steps} 之间，收到 {steps}'
                + (f'（目标模型：{who}，{(prof.get("note") or "")}）' if prof else ''))
        raw_seed = body.get('seed')
        if raw_seed is not None and raw_seed != '':
            try:
                raw_seed = int(raw_seed)
            except (TypeError, ValueError):
                return self._err(400, f'seed 必须是整数或 null，收到 {raw_seed!r}')
        else:
            raw_seed = None
        # guidance 默认值也必须按**目标模型**取：klein 要 1.0、dev 要 3.5。
        # 若按当前模型取，换模型的第一个任务会用错 guidance（静默劣化，正是
        # MODEL_PROFILES 那次事故的同一类问题）。
        guid_default = float(prof.get('guidance') or DEFAULT_GUIDANCE)
        params = {
            'prompt': prompt,
            'negative_prompt': (body.get('negative_prompt') or '').strip(),
            'width': width,
            'height': height,
            'steps': steps,
            'seed': raw_seed,
            'guidance_scale': float(body.get('guidance_scale') or guid_default),
        }
        if model_id:
            params['model_id'] = model_id           # worker 据此决定要不要换模型
        if is_edit:
            try:
                ref_path = self._save_ref_image(body)
            except BadRequest as e:
                return self._err(400, str(e))
            params['image_path'] = ref_path
        rec = self.store.create(params)
        self.worker.submit(rec['job_id'], int(body.get('priority') or 0))
        return self._json(200, {'job_id': rec['job_id'], 'status': STATUS_QUEUED,
                                'queue_depth': self.worker.depth})

    def _save_ref_image(self, body: dict) -> str:
        """把 /edit 的参考图落盘，返回路径。

        只接受 base64（`image` 字段，可带 data: 前缀）。**不接受任意本机路径** ——
        那等于给调用方一个任意文件读取口子。URL 下载也不做（SSRF 面 + 内网探测风险），
        需要时由调用方自己取回再以 base64 传入。
        """
        import base64
        import binascii
        raw = (body.get('image') or '').strip()
        if not raw:
            raise BadRequest('edit 模式必须提供 image（base64）')
        if raw.startswith('data:'):
            try:
                raw = raw.split(',', 1)[1]
            except IndexError:
                raise BadRequest('data: 前缀格式不对（缺逗号）') from None
        try:
            blob = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as e:
            raise BadRequest(f'image 不是合法 base64: {e}') from None
        if not blob:
            raise BadRequest('image 解码后为空')
        if len(blob) > MAX_REF_BYTES:
            raise BadRequest(f'参考图过大（{len(blob)}B > {MAX_REF_BYTES}B）')
        # 用 PIL 校验确实是图片（不信任扩展名），顺便定后缀
        from io import BytesIO
        from PIL import Image as _Img
        try:
            probe = _Img.open(BytesIO(blob))
            probe.verify()                      # 校验完整性（会消耗对象，需重开）
            fmt = (_Img.open(BytesIO(blob)).format or 'PNG').lower()
        except Exception as e:  # noqa: BLE001
            raise BadRequest(f'image 不是有效图片: {type(e).__name__}') from None
        ext = {'jpeg': 'jpg', 'png': 'png', 'webp': 'webp', 'bmp': 'bmp'}.get(fmt, 'png')
        ref_dir = self.store.out_dir / 'refs'
        ref_dir.mkdir(parents=True, exist_ok=True)
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(4).hex()}.{ext}"
        p = ref_dir / name
        p.write_bytes(blob)
        print(f'[fluxd] 参考图已落盘 {p} ({len(blob)}B, {fmt})', flush=True)
        return str(p)

    def _status(self):
        job_id = self._query('job_id')
        if not job_id:
            return self._err(400, '缺少 job_id')
        rec = self.store.get(job_id)
        if not rec:
            return self._err(404, f'未知 job_id: {job_id}')
        out = {
            'job_id': rec['job_id'],
            'status': rec['status'],
            'error': rec['error'],
            'elapsed': round((rec['finished_at'] or time.time()) - rec['submitted_at'], 2),
            'runtime': rec['runtime'],
            'queue_depth': self.worker.depth,
        }
        if rec['status'] == STATUS_DONE:
            out['image_path'] = rec['image_path']
            out['seed'] = rec.get('seed')
            out['params'] = rec['params']       # 回带实际生效的参数（尺寸可能被归一过）
        return self._json(200, out)

    def _image(self):
        job_id = self._query('job_id')
        if not job_id:
            return self._err(400, '缺少 job_id')
        rec = self.store.get(job_id)
        if not rec:
            return self._err(404, f'未知 job_id: {job_id}')
        if rec['status'] != STATUS_DONE:
            return self._err(409, f"任务未完成（当前 {rec['status']}）")
        p = Path(rec['image_path'])
        if not p.exists():
            return self._err(410, f'图片文件已不存在: {p}')
        data = p.read_bytes()
        if self._query('b64') in ('1', 'true'):
            return self._json(200, {'job_id': job_id,
                                    'b64': base64.b64encode(data).decode('ascii')})
        self.send_response(200)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _jobs(self):
        try:
            limit = max(1, min(200, int(self._query('limit', '20') or 20)))
        except ValueError:
            limit = 20
        return self._json(200, {'jobs': self.store.recent(limit)})

    def _cancel(self, body: dict):
        job_id = (body.get('job_id') or '').strip()
        if not job_id:
            return self._err(400, '缺少 job_id')
        ok = self.worker.cancel_queued(job_id)
        return self._json(200, {'job_id': job_id, 'canceled': ok,
                                'note': '' if ok else '仅可取消排队中的任务；生成中/已完成的不受影响'})


def build_server(host: str, port: int, store: JobStore, worker: FluxWorker,
                 holder: dict, token: str):
    Handler.store = store
    Handler.worker = worker
    Handler.holder = holder
    Handler.token = token
    Handler.started_at = time.time()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main():
    ap = argparse.ArgumentParser(description='FLUX.1 常驻生成服务')
    ap.add_argument('--host', default=os.environ.get('FLUX_HOST', '127.0.0.1'))
    ap.add_argument('--port', type=int, default=int(os.environ.get('FLUX_RESIDENT_PORT', '9630')))
    ap.add_argument('--model', default='/root/autodl-tmp/models/FLUX.1-dev')
    ap.add_argument('--out', default=os.environ.get('FLUX_OUT_DIR', str(SCRIPT_DIR / 'resident_out')))
    # ⚠️ 默认值必须是**安全**的那一档，不能是「最快」的。
    # 2026-09-18 实测：32G 卡上 FLUX.1-dev 权重 31.7 GiB > 卡空闲 ~31.1 GiB，
    #   默认 'none'（= pipe.to('cuda') 全量上卡）**必然 OOM**。
    #   manager 侧（flux_resident_client.py）已默认 'model'，但**直接跑 start_resident.sh
    #   或手起本脚本时走的是这里的默认值** —— 所以这里也必须是 'model'。
    # 'balanced'：交给 accelerate 按可用显存自动摊层（klein 17.3 GB 全程卡上、dev 混合装载），
    #   它是**独立策略**，与 enable_*_cpu_offload() 互斥，不能并用。
    ap.add_argument('--offload', default=os.environ.get('FLUX_OFFLOAD', 'model'),
                    choices=['none', 'model', 'sequential', 'balanced'],
                    help='none=全程显存(最快但吃满卡) / balanced=自动摊层 / '
                         'model|sequential=CPU offload（与 balanced 互斥）')
    ap.add_argument('--stub', action='store_true', default=os.environ.get('FLUX_STUB') == '1',
                    help='不加载真实模型，用合成占位图（无需 GPU，供本地联调/自证）')
    args = ap.parse_args()

    out_dir = Path(args.out)
    store = JobStore(out_dir=out_dir, status_dir=out_dir / 'status')
    # model_id/model_path/target/profile 在 load_model_async 里回填；
    # 这里先给全默认值，让 /health 在模型加载期间也能读到稳定结构（不是 KeyError）。
    holder = {'pipe': None, 'ready': False, 'error': '', 'offload': '',
              'model_id': '', 'model_path': '', 'class_name': '',
              'profile': {}, 'target': {}}
    worker = FluxWorker(store, holder)
    worker.start()
    load_model_async(holder, args.model, args.offload, args.stub)

    mode = 'STUB' if args.stub else f'offload={args.offload}'
    token = os.environ.get('FLUX_RESIDENT_TOKEN', '')
    httpd = build_server(args.host, args.port, store, worker, holder, token)
    print(f'[fluxd] 监听 http://{args.host}:{args.port}  ({mode})'
          f'{"  鉴权: 开" if token else "  鉴权: 关"}', flush=True)
    print(f'[fluxd] 输出目录 {out_dir}', flush=True)
    # 启动时把可用模型打出来 —— 「这台机器上有哪些模型」是最常被问到的问题，
    # 打印一次就省掉一次 ssh 去 ls 目录。
    try:
        found = [m for m in scan_models() if m['ready']]
        print(f'[fluxd] 可用模型（{len(found)} 个已就绪）: '
              f'{", ".join(m["id"] for m in found) or "(无)"}', flush=True)
    except Exception as e:                       # noqa: BLE001
        print(f'[fluxd] 扫描模型目录失败: {e}', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('[fluxd] 收到中断，退出', flush=True)
    finally:
        worker.stop()
        httpd.server_close()


if __name__ == '__main__':
    main()

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
    GET  /health                 → {status, model_loaded, offload, queue_depth, current_job, gpu, ...}
    POST /generate               → {job_id, status:"queued", queue_depth}
    GET  /status?job_id=<id>     → {job_id, status, image_path, error, elapsed, runtime, seed}
    GET  /image?job_id=<id>      → PNG 二进制（加 &b64=1 → JSON {b64}）
    GET  /jobs?limit=20          → 最近任务列表（运维排查用）
    POST /cancel {job_id}        → 取消「排队中」的任务（生成中的不打断）

POST /generate 请求体（除 prompt 外全部可选）：
    {"prompt": "...", "negative_prompt": "", "width": 768, "height": 1024,
     "steps": 25, "seed": null, "guidance_scale": 3.5, "priority": 0}
  · 缺省即沿用 gen_flux.py 原有的固定值（768×1024 / steps 25 / seed 42），
    所以只传 prompt 的老调用方行为不变。
  · seed 传 null 或省略 = 每张随机且返回实际 seed（可复现：拿返回的 seed 再传一次）。

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
    """不加载真实模型的替身。调用签名与 Flux2KleinPipeline.__call__ 对齐，供无 GPU 环境自证链路。

    【2026-09-18 修正】原先没有 `image` 参数 —— 导致 stub 模式下 `/edit` 会在
    「找不到图像参数」的检查处直接失败，**图生图链路无法离线自证**。
    klein 的真实签名里该参数名 = `image`（已实测定死），故替身照此对齐。

    另：替身**只声明 image 这一种**，不声明 images/image_latents —— 这样离线就能区分出
    「按模型签名动态判参名」的逻辑是否真的生效，而不是恒等于某个硬编码名字。
    """

    def __call__(self, prompt, negative_prompt='', num_inference_steps=25,
                 guidance_scale=3.5, width=768, height=1024, generator=None,
                 image=None, **kw):
        seed = 0
        if generator is not None:
            try:
                seed = int(generator.initial_seed())
            except Exception:
                seed = 0
        # 传了参考图 → 用它的颜色影响输出，让「edit 确实读到了图」肉眼/断言可辨
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
            kw = dict(
                negative_prompt=p.get('negative_prompt') or '',
                num_inference_steps=int(p.get('steps') or DEFAULT_STEPS),
                guidance_scale=float(p.get('guidance_scale') or DEFAULT_GUIDANCE),
                width=int(p.get('width') or DEFAULT_WIDTH),
                height=int(p.get('height') or DEFAULT_HEIGHT),
                generator=generator,
            )
            # ── 图生图分支（2026-09-18 新增）────────────────────────────
            # 传了参考图才带图像参数。**参数名按模型签名动态判定**，不硬编码：
            #   klein → `image`（已实测定死）；FLUX.1-dev 没有该参数 → 自动跳过，不 TypeError。
            # 这样同一份队列代码能同时服务 t2i 与 edit，且换模型不会炸。
            ref_path = p.get('image_path')
            if ref_path:
                import inspect
                sig = set(inspect.signature(self.holder['pipe'].__call__).parameters)
                img_param = next((n for n in ('image', 'images', 'image_latents') if n in sig), None)
                if img_param is None:
                    raise RuntimeError(
                        f'当前模型不支持图生图：__call__ 没有 image/images/image_latents 参数'
                        f'（可用参数：{sorted(sig)[:15]}...）'
                    )
                from PIL import Image as _Img
                ref_img = _Img.open(ref_path)
                # 保持宽高比 + 色彩管理（与 klein-setup/ab_klein_dev.py 的 load_ref_image 一致）
                ref_img = _prepare_ref_image(ref_img, int(kw['width']))
                kw['image'] = ref_img
                print(f'[fluxd] edit 模式：参考图 {ref_path} → 参数名 {img_param!r}', flush=True)
            out = self.holder['pipe'](p['prompt'], **kw)
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
    scale = size / min(w, h)
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


def load_model_async(holder: dict, model_path: str, offload: str, stub: bool):
    """后台加载模型，让 /health 在加载期间就能响应（status=loading）。"""

    def _load():
        try:
            if stub:
                holder['pipe'] = StubPipeline()
                holder['offload'] = 'stub'
                holder['ready'] = True
                print('[fluxd] STUB 模式：未加载真实模型，输出为合成占位图', flush=True)
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
            holder['pipe'] = pipe
            holder['offload'] = offload
            holder['class_name'] = cls_name
            holder['ready'] = True
            print(f'[fluxd] 模型常驻就绪 ✅ ({cls_name}, offload={offload})', flush=True)
        except Exception as e:
            holder['error'] = f'{type(e).__name__}: {e}'
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
            'offload': self.holder.get('offload') or '',
            'queue_depth': self.worker.depth,
            'current_job': self.worker.current_job,
            'job_counts': self.store.counts(),
            'gpu': self._gpu_info(),
            'uptime_sec': round(time.time() - self.started_at, 1),
        })

    def _generate(self, body: dict, is_edit: bool = False):
        """提交生成任务。

        `is_edit=True`（走 POST /edit）时要求带参考图，多接一个 `image` 字段。
        参考图两种传法：
          - `image`     : base64（可带 `data:image/png;base64,` 前缀）
          - `image_url` : 服务端可达的 http(s) URL（本机路径不建议，避免 SSRF 面）
        落盘到 out_dir/refs/ 后把**路径**放进 params —— 队列是异步的，
        不能把 PIL 对象或大 base64 长期留在内存里的 job 记录里。
        """
        prompt = (body.get('prompt') or '').strip()
        if not prompt:
            return self._err(400, 'prompt 不能为空')
        if self.holder.get('error'):
            return self._err(503, f"模型不可用: {self.holder['error']}")
        if not self.holder.get('ready'):
            return self._err(503, '模型仍在加载，请稍后重试（GET /health 看 model_loaded）')
        try:
            width = _norm_dim(body.get('width'), DEFAULT_WIDTH)
            height = _norm_dim(body.get('height'), DEFAULT_HEIGHT)
            steps = int(body.get('steps') or DEFAULT_STEPS)
        except BadRequest as e:
            return self._err(400, str(e))
        if not (1 <= steps <= MAX_STEPS):
            return self._err(400, f'steps 需在 1~{MAX_STEPS} 之间，收到 {steps}')
        raw_seed = body.get('seed')
        if raw_seed is not None and raw_seed != '':
            try:
                raw_seed = int(raw_seed)
            except (TypeError, ValueError):
                return self._err(400, f'seed 必须是整数或 null，收到 {raw_seed!r}')
        else:
            raw_seed = None
        params = {
            'prompt': prompt,
            'negative_prompt': (body.get('negative_prompt') or '').strip(),
            'width': width,
            'height': height,
            'steps': steps,
            'seed': raw_seed,
            'guidance_scale': float(body.get('guidance_scale') or DEFAULT_GUIDANCE),
        }
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
    holder = {'pipe': None, 'ready': False, 'error': '', 'offload': ''}
    worker = FluxWorker(store, holder)
    worker.start()
    load_model_async(holder, args.model, args.offload, args.stub)

    mode = 'STUB' if args.stub else f'offload={args.offload}'
    token = os.environ.get('FLUX_RESIDENT_TOKEN', '')
    httpd = build_server(args.host, args.port, store, worker, holder, token)
    print(f'[fluxd] 监听 http://{args.host}:{args.port}  ({mode})'
          f'{"  鉴权: 开" if token else "  鉴权: 关"}', flush=True)
    print(f'[fluxd] 输出目录 {out_dir}', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('[fluxd] 收到中断，退出', flush=True)
    finally:
        worker.stop()
        httpd.server_close()


if __name__ == '__main__':
    main()

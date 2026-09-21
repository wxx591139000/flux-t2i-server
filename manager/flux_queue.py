#!/usr/bin/env python3
"""
FLUX 对外文生图服务 — 队列调度器（核心，对标转录bot orchestrator.py）
优先队列 + 单 worker 线程（GPU 单卡严格串行）。复用 flux_server_manager 的 SSH 操作函数。

提交流程:  去重 → 配额 precheck → 队列上限 → 入队
worker:    弹任务 → 调 FLUX 服务器生成 → 拉图到 web_out/<jobid>/ → 标记完成；服务器 down → 标[SERVER_DOWN]重排队
健康监控:  队列非空且服务器 down → 飞书通知开机

生成路径（FLUX_GEN_MODE，2026-09-16 起）:
  resident（默认）  走 server/flux_resident_server.py 的常驻服务：模型加载一次常驻显存，
                    之后每张图只做推理。旧链路每张图重载 31GB 权重，是吞吐的结构性瓶颈。
  legacy            走 server/start_gen.sh + gen_flux.py 的冷启动链路（保留作逃生口/回退）
"""
import os
import sys
import json
import time
import queue
import logging
import threading
import shutil
import subprocess
import uuid
import base64
from collections import deque
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from manager.flux_db import FluxDB
from manager.feishu_notify import notify_owner, _load_env
from manager.flux_quota import current_ym
from manager.prompt_translator import translate_to_flux_prompt, has_chinese, TranslationError
import manager.flux_server_manager as fsm
import manager.flux_resident_client as fr

# 必须先加载 manager/.env：feishu_notify._load_env 只在函数内惰性调用，
# 不主动调用的话，模块级读的 FLUX_* 配置（GEN_MODE / RESIDENT_* 等）拿不到 .env 的值
_load_env()

logger = logging.getLogger('manager.flux_queue')

WEB_OUT = Path(os.environ.get('FLUX_WEB_OUT', BASE_DIR / 'web_out'))
QUEUE_MAX = int(os.environ.get('FLUX_QUEUE_MAX', '50'))
MAX_RETRY = 3

# 「服务器不可达」时，waiting 任务能等多久才判死（秒）。默认 4 小时。
# 为什么不用 MAX_RETRY 管 waiting（2026-09-20 实测踩到）：
#   waiting 池装的**全是基础设施问题**（机器关机 / SSH 不通），不是生成失败
#   （生成失败走 'failed' 分支直接终态，不进池）。而 AutoDL 开机要 1~3 分钟，
#   期间 SSH 会先 refused 再通。旧逻辑每 30s 重试一次、3 次（≈1 分钟）就判死并退款，
#   于是「用户刚点了开机」的任务反而被杀，机器白开。
#   改成按**等待总时长**判死：机器慢慢开、甚至隔几小时再开都能接上。
WAIT_MAX_SEC = int(os.environ.get('FLUX_WAIT_MAX_SEC', str(4 * 3600)))

# 待用参考图的内存暂存上限（FIFO 淘汰）。参考图只在「提交 → worker 取走」之间存活，
# 不落盘不入库。32 × 本站 6 MiB 上限 ≈ 190 MiB 封顶，防止有人连提交把 web 进程拖 OOM。
REF_CACHE_MAX = int(os.environ.get('FLUX_REF_CACHE_MAX', '32'))

# 'resident' = 常驻服务（默认，见模块 docstring）；'legacy' = 旧的冷启动链路
GEN_MODE = (os.environ.get('FLUX_GEN_MODE') or 'resident').strip().lower()
if GEN_MODE not in ('resident', 'legacy'):
    logger.warning(f'未知 FLUX_GEN_MODE={GEN_MODE!r}，回退为 resident')
    GEN_MODE = 'resident'


def _strip_data_uri(b64: str) -> str:
    """去掉 `data:image/xxx;base64,` 前缀（前端 <input type=file> 常见写法）。"""
    s = (b64 or '').strip()
    if s.startswith('data:') and ',' in s[:64]:
        return s.split(',', 1)[1]
    return s


def _dedup_key(user_id, prompt, seed=None, width=None, height=None,
               original_prompt=None, edit_mode=0) -> str:
    """去重键的**唯一**构造入口（2026-09-17 修，两个 bug 一起治）。

    bug A —— 键格式漂移：入队处拼 `{user}:{prompt}:{seed}:{w}x{h}`，而 worker
    收尾的 `discard()` 只拼 `{user}:{prompt}` —— 两者永远不相等，`_inflight`
    只增不减。三条后果：集合无限增长；`/health` 的 `backend_hint` 恒为 busy
    （队列空也报忙，监控误判）；同用户用固定 seed 重复提交同样内容会被永久拒绝。
    现在 add / discard / 孤儿恢复三处全部走这个函数，格式不可能再漂移。

    bug B —— 用错了文本：中文任务原先拿**翻译后**的英文 prompt 做键。但翻译是
    LLM 调用，同一句中文两次翻译的结果会漂移 —— 实测「一条牛仔材质…瘦腿的男士
    牛仔裤」两次分别译成 "A pair of men's slim-leg jeans…" 与
    "Photorealistic product photography…"，键不同 → 去重直接失效 →
    用户连点两次就出两张重复图，还各计一次费。
    现在中文一律用**用户原始输入** `original_prompt` 做键：语义相同必定命中去重；
    换 seed / 换尺寸仍算不同任务（B 链一次出多张候选靠这条放行）。

    edit_mode（2026-09-18）—— 必须进键：否则「同一句提示词，先文生图、再传参考图
    做图生图」会被判成重复提交而拒绝，而那是**两个完全不同的任务**。
    这里只标「是不是图生图」，不哈希参考图内容 —— 同一提示词配不同参考图若被算作
    同一任务会被误拒；代价是同提示词多张参考图**不参与去重**（宁可少去重，
    不可误拒用户）。
    """
    base = original_prompt or prompt
    return f'{user_id}:{base}:{seed}:{width}x{height}:e{1 if edit_mode else 0}'


class FluxQueueScheduler:
    def __init__(self, db: FluxDB, quota, interval=120):
        self.db = db
        self.quota = quota
        self.interval = interval
        self._pq = queue.PriorityQueue()
        self._seq = 0
        self._inflight = set()          # 去重键集合（构造见 _dedup_key）
        self._waiting = set()           # 服务器 down 等待恢复池
        # 待用参考图暂存（job_id → base64）。**内存态、进程重启即失**，这是刻意的：
        # 参考图可达 MiB 级，SQLite 不该背这个；重启后孤儿 edit 任务会明确失败。
        self._refs = {}
        self._order = deque()           # 仅用于 FIFO 淘汰，存 job_id
        # 参考图暂存**独立**一把锁：绝不与 _submit_lock 嵌套。
        # 嵌套过 → 自死锁（见 _put_ref docstring），这个坑值得单独一把锁来永久规避。
        self._ref_lock = threading.Lock()
        # web 层是 ThreadingHTTPServer：连点 / 并发提交会落在**不同线程**上，
        # 「检查去重 → 入队 → 登记 inflight」若不原子，两个线程会同时通过检查、
        # 各入一个队 → 出两张重复图，还各计一次费。
        self._submit_lock = threading.Lock()
        self._stop = threading.Event()
        self._worker_thread = None
        self._health_thread = None
        self._started_at = 0.0           # start() 时置为真实启动时刻，供 /health 报 uptime
        self._last_notify = 0.0

    # ── 提交（入口）──
    def submit(self, user_id: str, prompt: str, priority: int = 0,
               width=None, height=None, seed=None, steps=None, negative_prompt=None,
               ref_image: str = None) -> dict:
        """提交一个生成任务。成功返回 job dict，失败返回 {error: reason}。

        width/height/seed/steps/negative_prompt 为可选生图参数，透传到底层常驻服务
        （legacy 链路固定尺寸，这些仅 resident 模式生效）。

        `ref_image` = base64 参考图（可带 `data:image/...;base64,` 前缀）。
        传了它 = 图生图（走 resident `POST /edit`）；不传 = 文生图。
        """
        prompt = (prompt or '').strip()
        if not prompt:
            return {'error': '提示词不能为空'}

        # 0. 提示词处理：**只有中文才走翻译层；英文原样直传，一个字符都不改**。
        #
        #    （2026-09-21 明确为契约，此前只是"碰巧成立"）
        #    「英文直传」不是省钱省事的偷懒，而是**必须**的：
        #      a) 用户/调用方写的英文提示词往往已经针对 FLUX 调过（含质量词、构图词），
        #         再过一层 LLM 改写 = 把用户精心写的 prompt 换成一个"看起来差不多"的版本，
        #         出图与预期不符却查不出原因；
        #      b) 翻译是有损的：LLM 会"顺手"补充它认为合理的元素（把 mug 译成
        #         "coffee mug on wooden table"），而用户只说了杯；
        #      c) 直传才能保证**可复现**：同一 prompt+seed 必须出同一张图，
        #         经过 LLM 改写就不可能稳定（翻译结果会漂移，见 _dedup_key 的 bug B）。
        #
        #    中文为什么要翻译：klein 的 Qwen3 编码器 / dev 的 CLIP-L+T5-XXL 都是
        #    以英文语料为主训练的，中文直发出图会跑偏（实测：直发中文把「白色马克杯」
        #    画成了动漫少女）。翻译失败**不降级为中文**，而是拒绝任务并说明原因 ——
        #    静默降级会让用户拿到一张完全无关的图，比直接失败更糟。
        #
        #    ⚠️ 改动这里的任何逻辑前先想清楚：**这条 if 就是"英文直传"的唯一保证**。
        #       把它去掉（无条件翻译）会让所有英文提示词被 LLM 悄悄改写。
        #       tests/test_model_profile.py 与 test_prompt_translator.py 有断言钉住。
        original_prompt = prompt if has_chinese(prompt) else None
        if original_prompt:
            try:
                prompt = translate_to_flux_prompt(prompt)
            except TranslationError as e:
                logger.warning(f'❌ 中文翻译失败，拒绝任务: {e}')
                return {'error': '中文提示词翻译失败（翻译服务繁忙），请稍后重试，或改用英文提示词'}
            if prompt != original_prompt:
                logger.info(f'🌐 中文已转换: {original_prompt[:30]} → {prompt[:50]}...')
        else:
            # 英文直传：显式记一笔，便于排查时确认"这次确实没经过翻译层"
            logger.debug('🌐 英文提示词直传（未经过翻译层）')

        # 1~3 全部收进同一把锁：去重检查、配额、队列上限、入队、计费、登记
        #    必须是一个原子动作。web 层是多线程的，拆开会留出竞态窗口 ——
        #    连点两次就能各过一次检查，出两张重复图还各计一次费。
        #    （锁内只调 self.db 的原子方法，不嵌套别的锁，无死锁风险）
        with self._submit_lock:
            # 1. 去重（同用户同提示词同参数在排队/生成中）
            #    key 纳入 seed/size：同一提示词换 seed 或换尺寸 = 不同任务
            #    （B 链一次出多张候选靠这条放行）。
            #    中文必须传 original_prompt —— 翻译结果会漂移，见 _dedup_key。
            key = _dedup_key(user_id, prompt, seed, width, height,
                             original_prompt=original_prompt,
                             edit_mode=1 if ref_image else 0)
            if key in self._inflight:
                return {'error': '相同提示词与参数正在排队/生成中，请勿重复提交'}
            # 2. 配额
            ok, reason = self.quota.precheck(user_id)
            if not ok:
                return {'error': reason}
            # 3. 队列上限
            if self._pq.qsize() >= QUEUE_MAX:
                return {'error': f'队列已满（{QUEUE_MAX}），请稍后再试'}

            # 原先用毫秒时间戳当主键：同毫秒两次并发提交会 INSERT 冲突/互相覆盖。
            # 改 uuid4 前 16 位（32bit 十六进制 ≈ 64bit 熵），冲突概率可忽略。
            job_id = uuid.uuid4().hex[:16]
            is_edit = bool(ref_image)
            self.db.job_insert(job_id, user_id, prompt, priority, original_prompt,
                               width, height, seed, steps, negative_prompt,
                               edit_mode=1 if is_edit else 0, has_ref=1 if is_edit else 0)
            self.db.usage_add(user_id, current_ym(), 1)  # 入队即计费
            self._inflight.add(key)
            self._seq += 1
            self._pq.put((priority, -self._seq, job_id))
        # 参考图**只放内存**（base64 可达 MiB 级，不该进 SQLite），worker 取用后 pop。
        # 放在锁外：既避开非可重入锁的自死锁，也避免这段（可能淘汰、可能抛异常）的逻辑
        # 在锁内把整把 _submit_lock 拖住 —— 那会让**所有**后续提交一起挂死。
        if is_edit:
            self._put_ref(job_id, ref_image)
        logger.info(f'📥 {user_id} 入队 {job_id}{"[edit]" if ref_image else ""}: {prompt[:40]}')
        return {'job_id': job_id, 'status': 'queued'}

    # ── 参考图暂存（内存，不入库）──
    def _put_ref(self, job_id: str, ref_image: str):
        """登记待用参考图，并按 FIFO 淘汰，防止内存被拖爆。

        ⚠️ **绝不能在 `_submit_lock` 内被调用**。这里曾经写成
        `with self._submit_lock:` —— 而 `submit` 已经持有同一把**非可重入**锁
        → 自死锁。表现：「POST /api/edit 带 image 时永久挂起、连接不断、
        日志里一行都不打」，且**只在带 image 时触发**（t2i 路径完全正常），
        极易被误判成网络 / 代理问题（2026-09-18 实际排查了 4 轮才定位）。
        所以这里自己用一把**独立的** `_ref_lock`，与 `_submit_lock` 无嵌套关系。

        上限 N 是按「单张 base64 ≤ 8 MiB（上游 MAX_REF_BYTES）+ 本站 6 MiB 上限」算的
        粗口径：N 张 × 6 MiB ≈ N×6 MiB 内存占用，N 取 32 时约 190 MiB 上限。
        超出说明「提交了但 worker 还没跑到」的积压过多 —— 宁可丢最老的（让那个任务
        明确失败，用户重提），也不能让 web 进程 OOM。
        """
        with self._ref_lock:
            self._order.append(job_id)
            self._refs[job_id] = ref_image
            while len(self._order) > REF_CACHE_MAX:
                old = self._order.popleft()
                if old != job_id:
                    self._refs.pop(old, None)
        # 落盘（2026-09-20 补）：只放内存时，manager 一重启 → 所有在途编辑任务必失败
        # 「参考图已失效，请重新提交图生图任务」；FIFO 淘汰也会静默吃掉参考图。
        # 现在同时写一份到 web_out/<job_id>/ref.png：内存快路径仍在，
        # 重启 / 被淘汰后从磁盘读回，编辑任务不会因为 infra 抖动白扣一张配额。
        try:
            raw = base64.b64decode(_strip_data_uri(ref_image))
            p = self._ref_dir(job_id)
            # ⚠️ 必须 mkdir：WEB_OUT/<job_id>/ 此时**还不存在**（目录是生成阶段才建的）。
            # 少了这一行就是 FileNotFoundError，被下面 except 吞成一条 warning ——
            # 于是「重启不丢参考图」的承诺**从来没真正兑现过**（2026-09-20 查卡死任务
            # ceff1b2c 时才发现：web_out/ceff1b2c116b4c95/ 整个目录都不存在）。
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
        except Exception as e:                       # noqa: BLE001 落盘失败不该阻断提交
            logger.warning(f'参考图落盘失败（仅内存可用，重启后会失效）: '
                           f'{type(e).__name__}: {e}')

    # 参考图：路径 / 取用 / 清理
    def _ref_dir(self, job_id: str) -> Path:
        return WEB_OUT / job_id / 'ref.png'

    def _get_ref(self, job_id: str):
        """取参考图 base64：内存优先；没有则从磁盘读回（重启 / FIFO 淘汰后仍可用）。"""
        with self._ref_lock:
            b64 = self._refs.pop(job_id, None)
        if b64:
            return b64
        f = self._ref_dir(job_id)
        try:
            if f.exists():
                return base64.b64encode(f.read_bytes()).decode('ascii')
        except Exception as e:                       # noqa: BLE001
            logger.warning(f'读取磁盘参考图失败: {type(e).__name__}: {e}')
        return None

    def _drop_ref(self, job_id: str):
        """终态清理：内存 + 磁盘各删一份，避免 ref.png 无限堆积。"""
        with self._ref_lock:
            self._refs.pop(job_id, None)
        try:
            self._ref_dir(job_id).unlink(missing_ok=True)
        except Exception:                            # noqa: BLE001
            pass

    # ── worker ──
    def start(self):
        self._recover_stale_waiting()   # 启动时恢复上次遗留的 waiting 任务
        self._recover_orphaned_jobs()   # 启动时恢复重启丢掉的 queued/generating 任务（防孤儿）
        self._worker_thread = threading.Thread(target=self._run_worker, daemon=True, name='flux-worker')
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True, name='flux-health')
        self._started_at = time.time()
        self._worker_thread.start()
        self._health_thread.start()
        logger.info(f'🚀 队列调度器启动（单 worker 串行，生成路径={GEN_MODE}）')

    def stop(self):
        self._stop.set()

    def stats(self) -> dict:
        """进程内状态快照，供 GET /health 浅探针使用。**只读，不外呼**。

        为什么重要：worker 线程若异常退出，web 仍会照常返回 200 —— 用户提交得到
        `queued` 但永远不出图，从外部完全看不出来。`worker_alive` 是唯一能区分
        「活着」与「僵尸」的信号，且不需要碰网络/GPU。

        竞态说明：这里是**best-effort 快照**（不加 `_lock`）—— qsize()/len() 在 GIL 下
        足够原子，探针拿到的是某一瞬间的近似值，不是一致性事务。若为它加锁，反而
        可能与正在持锁的 submit 互相排队，把「快探针」变成慢请求。
        """
        wt = self._worker_thread
        ht = self._health_thread
        return {
            'gen_mode': GEN_MODE,
            'queue_depth': self._pq.qsize(),
            'inflight': len(self._inflight),
            'waiting': len(self._waiting),
            'worker_alive': bool(wt is not None and wt.is_alive()),
            'health_alive': bool(ht is not None and ht.is_alive()),
            'stopped': self._stop.is_set(),
            'uptime_sec': round(time.time() - self._started_at, 1) if self._started_at else None,
        }

    def _run_worker(self):
        while not self._stop.is_set():
            try:
                _, _, job_id = self._pq.get(timeout=2)
            except queue.Empty:
                continue
            self._process(job_id)

    def _process(self, job_id: str):
        job = self.db.job_get(job_id)
        if not job:
            return
        # job_get() 返回的是 sqlite3.Row —— 它支持 [] 索引但**没有 .get()**。
        # 统一转成 dict 再用 .get()：既兼容 Row，缺列时也不会抛 IndexError。
        job = dict(job)
        user_id = job['user_id']
        # 必须与入队时同一把钥匙，否则 discard() 永远清不掉（详见 _dedup_key 的 bug 说明）
        key = _dedup_key(user_id, job.get('prompt'), job.get('seed'),
                         job.get('width'), job.get('height'),
                         original_prompt=job.get('original_prompt'),
                         edit_mode=job.get('edit_mode'))
        try:
            self.db.job_update(job_id, status='generating')
            ok, err = self._generate(job)
            if ok:
                self.db.job_update(job_id, status='done', completed_at=int(time.time()))
                self._drop_ref(job_id)
                logger.info(f'✅ {job_id} 完成')
            else:
                # 服务器 down → 进等待恢复池（不失败、不立即重排，等健康监控检测到恢复后统一入队）
                if '[SERVER_DOWN]' in err:
                    retry = self._count_retry(job['error'] or '')
                    new_err = f'{err} [RETRY:{retry+1}]'
                    self.db.job_update(job_id, status='waiting', error=new_err)
                    self._waiting.add(job_id)
                    logger.warning(f'⏸ {job_id} 服务器down，进入等待恢复池 (retry {retry+1})')
                else:
                    self.db.job_update(job_id, status='failed', error=err, completed_at=int(time.time()))
                    self._drop_ref(job_id)
                    logger.error(f'❌ {job_id} 失败: {err[:120]}')
                    self._refund_quota(job, '业务失败')
        finally:
            # finally 而非写在末尾：_generate 抛异常时也必须释放去重键，
            # 否则这组参数会被永久占用 —— 同用户再也提交不了同样内容。
            self._inflight.discard(key)

    def _refund_quota(self, job, reason: str):
        """终态失败时退还配额（幂等，靠 jobs.refunded_at 防重复退）。

        只在「真正 failed」时调用。waiting（服务器 down 等恢复）**不退**：
        它不是终态，任务会被重新入队继续跑；若在 waiting 就退，等于同一张图免费出。
        退款额度按 token 落账（与 usage_add 同口径），账户聚合由 account_usage 负责。
        """
        job_id, user_id = job['job_id'], job['user_id']
        try:
            if self.db.refund_job_once(job_id, user_id, current_ym(), 1):
                logger.info(f'↩️ {job_id} 终态失败[{reason}]，已退还 1 张配额（{user_id}）')
            else:
                logger.info(f'↩️ {job_id} 配额此前已退还过，跳过（{reason}）')
        except Exception as e:      # 退还异常不得影响任务状态机，但必须留痕
            logger.error(f'⚠️ 配额退还异常 {job_id}: {type(e).__name__}: {e}')

    def _count_retry(self, err: str) -> int:
        import re
        m = re.search(r'\[RETRY:(\d+)\]', err or '')
        return int(m.group(1)) if m else 0

    # ── 生成入口：按 FLUX_GEN_MODE 分发 ──
    def _generate(self, job) -> tuple:
        if job.get('edit_mode'):
            return self._generate_resident(job, is_edit=True)
        if GEN_MODE == 'legacy':
            return self._generate_legacy(job)
        return self._generate_resident(job)

    # ── 生成（常驻服务：模型加载一次常驻显存）──
    def _generate_resident(self, job, is_edit: bool = False) -> tuple:
        """提交到 GPU 机上的常驻服务并拉图。返回 (ok, err)。

        `is_edit=True` → 打 resident 的 `POST /edit`（body 多一个 base64 `image`），
        否则打 `POST /generate`。两条路径**除这一个字段外完全同构**，所以共用本函数。

        错误分流靠 fr.TransportError.kind：
          'server_down' → 加 '[SERVER_DOWN]' 前缀，_process 会放进等待恢复池（等机器回来再试）
          'failed'      → 业务失败，直接标记 failed（重试也没意义）
        """
        job_id = job['job_id']
        try:
            # need_edit：图生图只有装了 klein 的机器能跑（dev 的 FluxPipeline 没有 image 参数），
            # 让选机阶段就避开不支持的机器，而不是等 GPU 上跑一遍才报错。
            server, p = fr.find_available_server(need_edit=is_edit)
            if not server:
                return False, '[SERVER_DOWN] 无可用 flux 服务器（均关机 / SSH 不通）'

            r = fr.ensure_resident(server, p)          # 幂等：在跑则直接返回
            if not r['ok']:
                prefix = '[SERVER_DOWN] ' if r['kind'] == 'server_down' else ''
                return False, f'{prefix}{r["msg"]}'

            ref_b64 = None
            if is_edit:
                # 参考图只存在于「提交那一刻」的内存里（不入库，见 flux_db 的 edit_mode 注释）。
                # 进程重启后孤儿任务恢复会走到这里且 ref_b64 为 None → **明确失败**，
                # 绝不静默降级成文生图：那会照常出图、照常扣费，但完全不是用户要的东西。
                ref_b64 = self._get_ref(job_id)
                if not ref_b64:
                    return False, ('参考图已失效（内存与磁盘均无留存），'
                                   '请重新提交图生图任务')

            dest = WEB_OUT / job_id / f'{job_id}.png'
            gen_kwargs = {}
            for k in ('width', 'height', 'seed', 'steps', 'negative_prompt'):
                v = job[k]          # job 是 sqlite3.Row，下标访问（无 .get）
                if v not in (None, ''):
                    gen_kwargs[k] = v
            st = fr.generate_via_resident(server, job['prompt'], dest,
                                          ref_image_b64=ref_b64, **gen_kwargs)
            seed_out = st.get('seed')
            self.db.job_update(job_id, image_path=str(dest), server=server['name'], seed=seed_out)
            logger.info(f'🗄️  任务 {job_id} 由 {server["name"]} 常驻服务完成'
                        f'（推理 {st.get("runtime")}s / 端到端 {st.get("total_elapsed")}s'
                        f' / seed {seed_out}）')
            return True, ''
        except fr.TransportError as e:
            if e.kind == 'server_down':
                return False, f'[SERVER_DOWN] {e}'
            return False, str(e)
        except Exception as e:                          # 非预期异常也要落到任务上，不静默
            logger.exception(f'常驻生成未预期异常 {job_id}')
            return False, f'{type(e).__name__}: {e}'

    # ── 生成（旧链路：每张图冷启动 gen_flux.py；保留作逃生口）──
    def _generate_legacy(self, job) -> tuple:
        """生成单图并拉回 web_out/<jobid>/。挑任意一台可达+带卡+模型就绪的 flux 服务器执行。返回 (ok, err)"""
        job_id = job['job_id']
        # 多服务器支持：挑第一台就绪的（自动跳过关机/无卡/模型未就绪的）
        server = fsm.find_ready_server()
        if not server:
            return False, '[SERVER_DOWN] 无可用 flux 服务器（均关机 / 无卡 / 模型未就绪）'
        alias = server['alias']
        rem_out = f"{server['remote_base']}/out"

        # 写单图 prompts.json
        prompts = {
            'notes': [{'note': 'web', 'images': [{'key': job_id, 'prompt': job['prompt']}]}]
        }
        prompts_path = BASE_DIR / 'manager' / 'tmp_web_prompts.json'
        prompts_path.write_text(json.dumps(prompts, ensure_ascii=False), encoding='utf-8')

        # 上传（prompts + gen_flux.py）——本地 Windows 路径转正斜杠，避免 bash 把反斜杠当转义
        local_prompts = str(prompts_path).replace('\\', '/')
        local_gen = str(fsm.LOCAL_GEN).replace('\\', '/')
        ok1, _ = fsm.run(f'scp {local_prompts} {alias}:{server["remote_base"]}/prompts.json', 30)
        ok2, _ = fsm.run(f'scp {local_gen} {alias}:{server["remote_base"]}/gen_flux.py', 30)
        if not (ok1 and ok2):
            return False, '上传 prompts 失败'

        # 干净重启：杀掉残留 fluxgen 会话 + wipe 清僵尸 + 清空 out/（僵尸 Dead 会话会被 start_gen 误判为运行中，必须 wipe）
        fsm.run(f'ssh {alias} "screen -S fluxgen -X quit 2>/dev/null; screen -wipe 2>/dev/null; pkill -f gen_fl[u]x.py 2>/dev/null; rm -rf {rem_out}/* 2>/dev/null; true"', 15)
        time.sleep(2)

        if not fsm.start_generation(server):
            return False, '启动生成失败'

        # 等待完成（1 张）
        if not fsm.wait_generation(1, server, timeout_sec=1800):
            return False, '生成超时'

        # 拉回 web_out/<jobid>/
        dest = WEB_OUT / job_id
        dest.mkdir(parents=True, exist_ok=True)
        dest_posix = str(dest).replace('\\', '/')
        ok, _ = fsm.run(f'scp -r {alias}:{rem_out}/. "{dest_posix}" 2>/dev/null', 120)
        # gen_flux 输出在 out/00_web/<jobid>.png，上移一层
        for sub in list(dest.iterdir()):
            if sub.is_dir():
                for f in sub.glob('*.png'):
                    shutil.move(str(f), str(dest / f.name))
                shutil.rmtree(sub, ignore_errors=True)
        pngs = list(dest.glob('*.png'))
        if not pngs:
            return False, '未拉回图片'
        self.db.job_update(job_id, image_path=str(pngs[0]), server=server['name'])
        logger.info(f'🗄️  任务 {job_id} 由服务器 {server["name"]} 完成')
        return True, ''

    # ── 健康监控（对标转录bot _health_monitor_loop + _recover_failed_tasks）──
    def _any_ready(self) -> bool:
        """是否有任意一台服务器「能干活」（可达 + 有卡 + 模型文件就绪）。

        resident 模式用 fr.any_usable()：**刻意不要求常驻服务已在跑**。
        换机 / 克隆实例后机器刚开机时常驻还没起来，但 _generate_resident →
        ensure_resident 会自动拉起；若拿「常驻已跑」当门槛，waiting 任务会一直卡住死等。
        探测走 fsm.probe_full：每台固定 1 次 SSH 往返 + TTL 缓存
        （旧路径每台要 3~4 次往返：echo + nvidia-smi + test -f [+ curl]）。
        """
        if GEN_MODE == 'legacy':
            return fsm.any_ready()
        return fr.any_usable()

    def _health_loop(self):
        while not self._stop.is_set():
            try:
                if not self._pq.empty() and not self._any_ready():
                    now = time.time()
                    if now - self._last_notify > 600:   # 节流 10 分钟
                        self._last_notify = now
                        logger.warning('🔴 FLUX 服务器均不可用且有任务排队，通知开机')
                        try:
                            notify_owner(f'🔴 FLUX 文生图服务器均不可用，有 {self._pq.qsize()} 个任务排队。\n'
                                         f'请到 AutoDL 控制台给任一台开机（带卡模式）。')
                        except Exception as e:
                            logger.error(f'飞书通知失败: {e}')
                elif self._any_ready() and self._waiting:
                    # 任一台可用服务器恢复 → 才尝试重入队等待恢复的任务（否则全关机会每 30s 打退一次 retry）
                    self._recover_waiting_tasks()
            except Exception as e:
                logger.error(f'健康监控异常: {e}')
            time.sleep(30)

    def _recover_waiting_tasks(self):
        """服务器恢复时，把 waiting 的 [SERVER_DOWN] 任务重新入队。

        判死口径 = **等待总时长**（WAIT_MAX_SEC），不是重试次数：
        池里全是「机器关机 / SSH 不通」这类基础设施等待，AutoDL 开机要 1~3 分钟，
        期间 SSH 先 refused 再通。按次数判死会把「用户刚点开机」的任务误杀
        （2026-09-20 实测：3 次重试 ≈1 分钟耗尽，机器开了但任务已 failed 并退款）。
        真正生成失败（kind='failed'）不进池，直接终态，不受这里影响。

        ⚠️ 两个曾经的真实事故（2026-09-20，站点任务 ceff1b2c 卡死 49 分钟的叠加根因）：
          1. `job_get()` 返回 sqlite3.Row，Row **没有 .get()** —— 旧代码
             `job.get('created_at')` 每次必抛 AttributeError，被 health_loop 的
             except 吞掉 → **每次恢复窗口都一个任务也恢复不了**；
          2. 更糟的是异常发生在 `self._waiting.pop()` **之后** → 任务被弹出池子
             却没入队也没判死 → 内存里彻底丢失，DB 还显示 waiting →
             **永远卡住、连 4 小时超时判死都等不到**（用户看到「已等待 49 分 53 秒」）。
        修法：拿到 Row 先 `dict()` 再碰；单个任务异常绝不吞掉 —— 标 failed 退款，
        让用户能重提，而不是无声消失。
        """
        while self._waiting:
            job_id = self._waiting.pop()
            try:
                job = self.db.job_get(job_id)
                if not job:
                    continue
                job = dict(job)          # ⚠️ Row 没有 .get()，先转 dict（坑 1）
                retry = self._count_retry(job['error'] or '')
                waited = int(time.time()) - int(job.get('created_at') or time.time())
                if waited > WAIT_MAX_SEC:
                    self.db.job_update(job_id, status='failed',
                                       error=f'{job["error"] or ""} [WAIT_TIMEOUT '
                                             f'{waited}s>{WAIT_MAX_SEC}s]',
                                       completed_at=int(time.time()))
                    logger.warning(f'⛔ {job_id} 等待服务器 {waited}s 超过上限，标记失败')
                    self._refund_quota(job, f'等待服务器超时({waited//60}分钟)')
                    continue
                self.db.job_update(job_id, status='queued')
                self._pq.put((job['priority'], -self._seq, job_id))
                self._seq += 1
                logger.info(f'♻️ {job_id} 服务器恢复，重新入队 (retry {retry+1})')
            except Exception as e:                        # noqa: BLE001 坑 2：绝不让任务无声消失
                logger.exception(f'♻️ 恢复 waiting 任务 {job_id} 异常，标记失败并退款')
                try:
                    self.db.job_update(job_id, status='failed',
                                       error=f'[RECOVER_ERROR] {type(e).__name__}: {e}',
                                       completed_at=int(time.time()))
                    self._refund_quota(dict(self.db.job_get(job_id) or {}), '恢复异常')
                except Exception:                         # noqa: BLE001 退款失败也不许再炸
                    logger.exception(f'♻️ {job_id} 异常收尾再失败')

    def _recover_orphaned_jobs(self):
        """重启后把 DB 里残留 queued/generating 的任务重新入队。
        原缺陷：任务队列 _pq 在内存，重启即清零，DB 的 queued/generating 会成孤儿永远无人处理。
        现：启动时把这两类重入队（generating 改回 queued 重新生成，最稳）。"""
        for j in self.db.jobs_queued():
            j = dict(j)          # 同上：jobs_queued() 返回 sqlite3.Row，没有 .get()
            jid = j['job_id']
            key = _dedup_key(j['user_id'], j.get('prompt'), j.get('seed'),
                             j.get('width'), j.get('height'),
                             original_prompt=j.get('original_prompt'),
                             edit_mode=j.get('edit_mode'))
            if j['status'] == 'generating':
                self.db.job_update(jid, status='queued')
            self._inflight.add(key)
            self._pq.put((j['priority'], -self._seq, jid))
            self._seq += 1
            logger.info(f'🔁 重启恢复孤儿任务重入队: {jid} (原 {j["status"]})')

    def _recover_stale_waiting(self):
        """启动时扫描 DB 里残留的 waiting 任务，加入等待恢复池（防重启丢失）。"""
        stale = self.db.jobs_waiting()
        if not stale:
            return
        for j in stale:
            self._waiting.add(j['job_id'])
        logger.info(f'🔧 启动恢复 {len(stale)} 个遗留 waiting 任务，等待服务器恢复重试')


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--interval', type=int, default=120)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[logging.FileHandler(BASE_DIR / 'manager' / 'flux_service.log', encoding='utf-8'),
                  logging.StreamHandler(sys.stdout)])
    from manager.flux_quota import QuotaService
    db = FluxDB()
    quota = QuotaService(db)
    sched = FluxQueueScheduler(db, quota, args.interval)
    sched.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        sched.stop()


if __name__ == '__main__':
    main()
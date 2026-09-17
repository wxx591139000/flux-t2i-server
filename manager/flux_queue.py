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

# 'resident' = 常驻服务（默认，见模块 docstring）；'legacy' = 旧的冷启动链路
GEN_MODE = (os.environ.get('FLUX_GEN_MODE') or 'resident').strip().lower()
if GEN_MODE not in ('resident', 'legacy'):
    logger.warning(f'未知 FLUX_GEN_MODE={GEN_MODE!r}，回退为 resident')
    GEN_MODE = 'resident'


class FluxQueueScheduler:
    def __init__(self, db: FluxDB, quota, interval=120):
        self.db = db
        self.quota = quota
        self.interval = interval
        self._pq = queue.PriorityQueue()
        self._seq = 0
        self._inflight = set()          # 去重 user:prompt
        self._waiting = set()           # 服务器 down 等待恢复池
        self._stop = threading.Event()
        self._worker_thread = None
        self._health_thread = None
        self._started_at = 0.0           # start() 时置为真实启动时刻，供 /health 报 uptime
        self._last_notify = 0.0

    # ── 提交（入口）──
    def submit(self, user_id: str, prompt: str, priority: int = 0,
               width=None, height=None, seed=None, steps=None, negative_prompt=None) -> dict:
        """提交一个生成任务。成功返回 job dict，失败返回 {error: reason}。

        width/height/seed/steps/negative_prompt 为可选生图参数，透传到底层常驻服务
        （legacy 链路固定尺寸，这些仅 resident 模式生效）。"""
        prompt = (prompt or '').strip()
        if not prompt:
            return {'error': '提示词不能为空'}

        # 0. 中文提示词 → FLUX 友好英文提示词（借鉴短剧 FLUX 方法论）
        #    翻译失败**不降级为中文**：FLUX.1-dev 是双英文编码器（CLIP-L + T5-XXL），
        #    中文直发出图会完全跑偏 —— 实测直发中文把「白色马克杯」画成了动漫少女。
        #    所以失败就把任务拒掉并说明原因，让用户知道该重试/改用英文。
        original_prompt = prompt if has_chinese(prompt) else None
        if original_prompt:
            try:
                prompt = translate_to_flux_prompt(prompt)
            except TranslationError as e:
                logger.warning(f'❌ 中文翻译失败，拒绝任务: {e}')
                return {'error': '中文提示词翻译失败（翻译服务繁忙），请稍后重试，或改用英文提示词'}
            if prompt != original_prompt:
                logger.info(f'🌐 中文已转换: {original_prompt[:30]} → {prompt[:50]}...')

        # 1. 去重（同用户同提示词同参数在排队/生成中）
        #    key 纳入 seed/size：同一提示词换 seed 或换尺寸 = 不同任务（B 链多张候选靠这绕过去重）。
        key = f'{user_id}:{prompt}:{seed}:{width}x{height}'
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
        self.db.job_insert(job_id, user_id, prompt, priority, original_prompt,
                           width, height, seed, steps, negative_prompt)
        self.db.usage_add(user_id, current_ym(), 1)  # 入队即计费
        self._inflight.add(key)
        self._seq += 1
        self._pq.put((priority, -self._seq, job_id))
        logger.info(f'📥 {user_id} 入队 {job_id}: {prompt[:40]}')
        return {'job_id': job_id, 'status': 'queued'}

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
        user_id = job['user_id']
        key = f'{user_id}:{job["prompt"]}'
        self.db.job_update(job_id, status='generating')
        ok, err = self._generate(job)
        if ok:
            self.db.job_update(job_id, status='done', completed_at=int(time.time()))
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
                logger.error(f'❌ {job_id} 失败: {err[:120]}')
                self._refund_quota(job, '业务失败')
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
        if GEN_MODE == 'legacy':
            return self._generate_legacy(job)
        return self._generate_resident(job)

    # ── 生成（常驻服务：模型加载一次常驻显存）──
    def _generate_resident(self, job) -> tuple:
        """提交到 GPU 机上的常驻服务并拉图。返回 (ok, err)。

        错误分流靠 fr.TransportError.kind：
          'server_down' → 加 '[SERVER_DOWN]' 前缀，_process 会放进等待恢复池（等机器回来再试）
          'failed'      → 业务失败，直接标记 failed（重试也没意义）
        """
        job_id = job['job_id']
        try:
            server, p = fr.find_available_server()
            if not server:
                return False, '[SERVER_DOWN] 无可用 flux 服务器（均关机 / SSH 不通）'

            r = fr.ensure_resident(server, p)          # 幂等：在跑则直接返回
            if not r['ok']:
                prefix = '[SERVER_DOWN] ' if r['kind'] == 'server_down' else ''
                return False, f'{prefix}{r["msg"]}'

            dest = WEB_OUT / job_id / f'{job_id}.png'
            gen_kwargs = {}
            for k in ('width', 'height', 'seed', 'steps', 'negative_prompt'):
                v = job[k]          # job 是 sqlite3.Row，下标访问（无 .get）
                if v not in (None, ''):
                    gen_kwargs[k] = v
            st = fr.generate_via_resident(server, job['prompt'], dest, **gen_kwargs)
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
        """服务器恢复时，把 waiting 的 [SERVER_DOWN] 任务重新入队。每任务最多恢复 MAX_RETRY 次。"""
        while self._waiting:
            job_id = self._waiting.pop()
            job = self.db.job_get(job_id)
            if not job:
                continue
            retry = self._count_retry(job['error'] or '')
            if retry >= MAX_RETRY:
                self.db.job_update(job_id, status='failed',
                                   error=f'{job["error"] or ""} [RECOVER_SKIP]',
                                   completed_at=int(time.time()))
                logger.warning(f'⛔ {job_id} 恢复超限({retry})，标记失败')
                self._refund_quota(job, f'重试超限({retry})')
                continue
            self.db.job_update(job_id, status='queued')
            self._pq.put((job['priority'], -self._seq, job_id))
            self._seq += 1
            logger.info(f'🔄 {job_id} 服务器恢复，重新入队 (retry {retry+1})')

    def _recover_orphaned_jobs(self):
        """重启后把 DB 里残留 queued/generating 的任务重新入队。
        原缺陷：任务队列 _pq 在内存，重启即清零，DB 的 queued/generating 会成孤儿永远无人处理。
        现：启动时把这两类重入队（generating 改回 queued 重新生成，最稳）。"""
        for j in self.db.jobs_queued():
            jid = j['job_id']
            key = f"{j['user_id']}:{j['prompt']}"
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
#!/usr/bin/env python3
"""
FLUX 对外文生图服务 — 队列调度器（核心，对标转录bot orchestrator.py）
优先队列 + 单 worker 线程（GPU 单卡严格串行）。复用 flux_server_manager 的 SSH 操作函数。

提交流程:  去重 → 配额 precheck → 队列上限 → 入队
worker:    弹任务 → 调 FLUX 服务器生成 → 拉图到 web_out/<jobid>/ → 标记完成；服务器 down → 标[SERVER_DOWN]重排队
健康监控:  队列非空且服务器 down → 飞书通知开机
"""
import os
import sys
import json
import time
import queue
import logging
import threading
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from manager.flux_db import FluxDB
from manager.feishu_notify import notify_owner
from manager.flux_quota import current_ym
from manager.prompt_translator import translate_to_flux_prompt, has_chinese
import manager.flux_server_manager as fsm

logger = logging.getLogger('manager.flux_queue')

WEB_OUT = Path(os.environ.get('FLUX_WEB_OUT', BASE_DIR / 'web_out'))
QUEUE_MAX = int(os.environ.get('FLUX_QUEUE_MAX', '50'))
MAX_RETRY = 3


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
        self._last_notify = 0.0

    # ── 提交（入口）──
    def submit(self, user_id: str, prompt: str, priority: int = 0) -> dict:
        """提交一个生成任务。成功返回 job dict，失败返回 {error: reason}"""
        prompt = (prompt or '').strip()
        if not prompt:
            return {'error': '提示词不能为空'}

        # 0. 中文提示词 → FLUX 友好英文提示词（借鉴短剧 FLUX 方法论；LLM 失败返回原文）
        original_prompt = prompt if has_chinese(prompt) else None
        if original_prompt:
            prompt = translate_to_flux_prompt(prompt)
            if prompt != original_prompt:
                logger.info(f'🌐 中文已转换: {original_prompt[:30]} → {prompt[:50]}...')

        # 1. 去重（同用户同提示词在排队/生成中）
        key = f'{user_id}:{prompt}'
        if key in self._inflight:
            return {'error': '相同提示词正在排队/生成中，请勿重复提交'}
        # 2. 配额
        ok, reason = self.quota.precheck(user_id)
        if not ok:
            return {'error': reason}
        # 3. 队列上限
        if self._pq.qsize() >= QUEUE_MAX:
            return {'error': f'队列已满（{QUEUE_MAX}），请稍后再试'}

        job_id = f'{int(time.time()*1000)}'
        self.db.job_insert(job_id, user_id, prompt, priority, original_prompt)
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
        self._worker_thread.start()
        self._health_thread.start()
        logger.info('🚀 队列调度器启动（单 worker 串行）')

    def stop(self):
        self._stop.set()

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
        self._inflight.discard(key)

    def _count_retry(self, err: str) -> int:
        import re
        m = re.search(r'\[RETRY:(\d+)\]', err or '')
        return int(m.group(1)) if m else 0

    # ── 生成（复用 flux_server_manager SSH 操作，任一台可用服务器）──
    def _generate(self, job) -> tuple:
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
        import shutil
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
    def _health_loop(self):
        while not self._stop.is_set():
            try:
                if not self._pq.empty() and not fsm.any_ready():
                    now = time.time()
                    if now - self._last_notify > 600:   # 节流 10 分钟
                        self._last_notify = now
                        logger.warning('🔴 FLUX 服务器均不可用且有任务排队，通知开机')
                        try:
                            notify_owner(f'🔴 FLUX 文生图服务器均不可用，有 {self._pq.qsize()} 个任务排队。\n'
                                         f'请到 AutoDL 控制台给任一台开机（带卡模式）。')
                        except Exception as e:
                            logger.error(f'飞书通知失败: {e}')
                elif fsm.any_ready() and self._waiting:
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
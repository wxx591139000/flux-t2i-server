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
import manager.flux_server_manager as fsm

logger = logging.getLogger('manager.flux_queue')

WEB_OUT = Path(os.environ.get('FLUX_WEB_OUT', BASE_DIR / 'web_out'))
QUEUE_MAX = int(os.environ.get('FLUX_QUEUE_MAX', '50'))
MAX_RETRY = 3
REMOTE_BASE = '/root/autodl-tmp/flux-t2i'
REMOTE_OUT = f'{REMOTE_BASE}/out'


class FluxQueueScheduler:
    def __init__(self, db: FluxDB, quota, interval=120):
        self.db = db
        self.quota = quota
        self.interval = interval
        self._pq = queue.PriorityQueue()
        self._seq = 0
        self._inflight = set()          # 去重 user:prompt
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
        self.db.job_insert(job_id, user_id, prompt, priority)
        self.db.usage_add(user_id, current_ym(), 1)  # 入队即计费
        self._inflight.add(key)
        self._seq += 1
        self._pq.put((priority, -self._seq, job_id))
        logger.info(f'📥 {user_id} 入队 {job_id}: {prompt[:40]}')
        return {'job_id': job_id, 'status': 'queued'}

    # ── worker ──
    def start(self):
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
            # 服务器 down → 重排队（带重试计数）
            if '[SERVER_DOWN]' in err:
                retry = self._count_retry(job['error'] or '')
                new_err = f'{err} [RETRY:{retry+1}]'
                if retry < MAX_RETRY:
                    self.db.job_update(job_id, status='queued', error=new_err)
                    self._pq.put((job['priority'], -self._seq, job_id))
                    self._seq += 1
                    logger.warning(f'↻ {job_id} 服务器down，重排队({retry+1}/{MAX_RETRY})')
                else:
                    self.db.job_update(job_id, status='failed', error=new_err, completed_at=int(time.time()))
            else:
                self.db.job_update(job_id, status='failed', error=err, completed_at=int(time.time()))
                logger.error(f'❌ {job_id} 失败: {err[:120]}')
        self._inflight.discard(key)

    def _count_retry(self, err: str) -> int:
        import re
        m = re.search(r'\[RETRY:(\d+)\]', err or '')
        return int(m.group(1)) if m else 0

    # ── 生成（复用 flux_server_manager SSH 操作）──
    def _generate(self, job) -> tuple:
        """生成单图并拉回 web_out/<jobid>/。返回 (ok, err)"""
        job_id = job['job_id']
        if not fsm.server_reachable():
            return False, '[SERVER_DOWN] FLUX 服务器不可达'
        if not fsm.gpu_ready()[0]:
            return False, '[SERVER_DOWN] 服务器无卡模式'
        if not fsm.model_ready():
            return False, '模型未就绪'

        # 写单图 prompts.json
        prompts = {
            'notes': [{'note': 'web', 'images': [{'key': job_id, 'prompt': job['prompt']}]}]
        }
        prompts_path = BASE_DIR / 'manager' / 'tmp_web_prompts.json'
        prompts_path.write_text(json.dumps(prompts, ensure_ascii=False), encoding='utf-8')

        # 上传（prompts + gen_flux.py）——本地 Windows 路径转正斜杠，避免 bash 把反斜杠当转义
        local_prompts = str(prompts_path).replace('\\', '/')
        local_gen = str(fsm.LOCAL_GEN).replace('\\', '/')
        ok1, _ = fsm.run(f'scp {local_prompts} {fsm.SSH_ALIAS}:{REMOTE_BASE}/prompts.json', 30)
        ok2, _ = fsm.run(f'scp {local_gen} {fsm.SSH_ALIAS}:{REMOTE_BASE}/gen_flux.py', 30)
        if not (ok1 and ok2):
            return False, '上传 prompts 失败'

        # 干净重启：杀掉残留 fluxgen 会话 + 清空 out/（保证只拉回本次生成图，避免历史图污染）
        fsm.run(f'ssh {fsm.SSH_ALIAS} "screen -S fluxgen -X quit 2>/dev/null; pkill -f gen_fl[u]x.py 2>/dev/null; rm -rf {REMOTE_OUT}/* 2>/dev/null; true"', 15)
        time.sleep(2)

        if not fsm.start_generation():
            return False, '启动生成失败'

        # 等待完成（1 张）
        if not fsm.wait_generation(1, timeout_sec=1800):
            return False, '生成超时'

        # 拉回 web_out/<jobid>/
        dest = WEB_OUT / job_id
        dest.mkdir(parents=True, exist_ok=True)
        dest_posix = str(dest).replace('\\', '/')
        ok, _ = fsm.run(f'scp -r {fsm.SSH_ALIAS}:{REMOTE_OUT}/. "{dest_posix}" 2>/dev/null', 120)
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
        self.db.job_update(job_id, image_path=str(pngs[0]))
        return True, ''

    # ── 健康监控（对标转录bot _health_monitor_loop）──
    def _health_loop(self):
        while not self._stop.is_set():
            try:
                if not self._pq.empty() and not fsm.server_reachable():
                    now = time.time()
                    if now - self._last_notify > 600:   # 节流 10 分钟
                        self._last_notify = now
                        logger.warning('🔴 FLUX 服务器不可达且有任务排队，通知开机')
                        try:
                            notify_owner(f'🔴 FLUX 文生图服务器不可达，有 {self._pq.qsize()} 个任务排队。\n'
                                         f'请到 AutoDL 控制台开机（带卡模式）。')
                        except Exception as e:
                            logger.error(f'飞书通知失败: {e}')
            except Exception as e:
                logger.error(f'健康监控异常: {e}')
            time.sleep(30)


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
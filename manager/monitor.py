#!/usr/bin/env python3
"""FLUX 出图链路监控器（只读 + 可告警）

为什么需要它：
  A 链（manager，本机 9620）→ 远端 GPU 机（常驻服务 9630）这条链路有 4 个环节会静默卡住：
    1) manager 进程没跑 / 挂了
    2) 远端机器关机或 SSH 不通 → 任务进 waiting 池
    3) 常驻服务没起来 / 模型加载失败
    4) 推理本身报错
  出问题时的现象都一样：**网站上一个圈一直转**。所以需要一个能一眼区分这四种情况的探针。

用法：
  python manager/monitor.py                       # 打一次快照
  python manager/monitor.py --watch 30            # 每 30s 一次，一直跑（Ctrl+C 停）
  python manager/monitor.py --job <job_id> --watch 20 --rounds 60
                                                  # 盯某个任务，done/failed 就退出（退出码 0/1）
  python manager/monitor.py --job <job_id> --watch 20 --notify   # 完成后飞书通知机主

判定口径（重要，别混淆）：
  reachable=✗  → 远端 SSH 不通（关机 / 注册表里 host:port 写错 / 密钥不对）
  model_loaded=✗→ 常驻在，但模型还没加载完（首次冷启动要 1~3 分钟，属正常）
  inflight>0 且 waiting=0 → 正在出图
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / 'manager'))

DB = BASE_DIR / 'data' / 'flux_service.db'
LOG = BASE_DIR / 'manager' / 'flux_manager.log'
HEALTH = 'http://127.0.0.1:9620/health'

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕开系统代理，见 PITFALLS


def health():
    try:
        with _opener.open(HEALTH, timeout=8) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:                                  # noqa: BLE001
        return {'_error': f'{type(e).__name__}: {e}'}


def gpu():
    """候选机探活。import 失败/没 bash 时 graceful 降级，绝不让监控器自己崩。"""
    try:
        import manager.flux_resident_client as rc
        cands = rc.probe_all()
        return [(s.get('name'), p.get('reachable'), p.get('gpu_ok'),
                 p.get('model_ok'), p.get('resident'), p.get('model_loaded'),
                 (p.get('error') or '')[:60]) for s, p in cands]
    except Exception as e:                                  # noqa: BLE001
        return [('_', f'probe 失败: {type(e).__name__}: {e}')]


def job(job_id):
    if not job_id or not DB.exists():
        return None
    try:
        c = sqlite3.connect(str(DB))
        c.row_factory = sqlite3.Row
        r = c.execute('select * from jobs where job_id=?', (job_id,)).fetchone()
        return dict(r) if r else None
    except Exception as e:                                  # noqa: BLE001
        return {'_error': str(e)}


def new_log_lines(since: int):
    try:
        lines = LOG.read_text(encoding='utf-8', errors='replace').splitlines()
    except Exception:                                       # noqa: BLE001
        return [], since
    if since == 0:
        return lines[-8:], len(lines)
    return lines[since:], len(lines)


def snapshot(job_id=None, since=0, show_gpu=True):
    h = health()
    print(f'──────── {time.strftime("%H:%M:%S")} ────────')
    if '_error' in h:
        print(f'❌ manager 无响应: {h["_error"]}')
        print('   → 检查: nohup python manager/flux_service.py &')
    else:
        print(f'manager: status={h.get("status")} worker={h.get("worker_alive")} '
              f'队列={h.get("queue_depth")} 进行中={h.get("inflight")} '
              f'等待={h.get("waiting")} 后端={h.get("backend_hint")}')
    if show_gpu:
        for row in gpu():
            if len(row) == 2:
                print(f'  GPU: {row[1]}')
                continue
            name, reach, g, m, res, loaded, err = row
            print(f'  {name:7s} 可达={"✓" if reach else "✗"} 带卡={"✓" if g else "✗"} '
                  f'模型={"✓" if m else "✗"} 常驻={"✓" if res else "✗"} '
                  f'已加载={"✓" if loaded else "✗"} {err}')
    if job_id:
        j = job(job_id)
        if j is None:
            print(f'  job {job_id}: 未找到')
        elif '_error' in j:
            print(f'  job {job_id}: 读取失败 {j["_error"]}')
        else:
            print(f'  job {job_id}: status={j.get("status")} server={j.get("server")} '
                  f'error={(j.get("error") or "")[:80]}')
    lines, n = new_log_lines(since)
    for ln in lines:
        print('  │ ' + ln[:150])
    return n, (h.get('waiting'), h.get('inflight'), (job(job_id) or {}).get('status'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--job', default=None)
    ap.add_argument('--watch', type=int, default=0, help='>0 则循环，间隔秒数')
    ap.add_argument('--rounds', type=int, default=0, help='最多跑几轮（0=不限）')
    ap.add_argument('--no-gpu', action='store_true', help='跳过候选机探活（省 SSH 往返）')
    ap.add_argument('--notify', action='store_true', help='任务终态时飞书通知机主')
    a = ap.parse_args()

    since = 0
    rounds = 0
    while True:
        since, (waiting, inflight, status) = snapshot(a.job, since, show_gpu=not a.no_gpu)
        if a.job and status in ('done', 'failed', 'canceled'):
            msg = f'{"✅" if status == "done" else "❌"} 任务 {a.job} 终态: {status}'
            print(msg)
            if a.notify:
                try:
                    from manager.feishu_notify import notify_owner
                    notify_owner('FLUX 任务监控', msg)
                except Exception as e:                      # noqa: BLE001
                    print(f'（飞书通知失败: {e}）')
            return 0 if status == 'done' else 1
        if not a.watch:
            return 0
        rounds += 1
        if a.rounds and rounds >= a.rounds:
            print(f'（已跑满 {a.rounds} 轮，退出）')
            return 0
        try:
            time.sleep(a.watch)
        except KeyboardInterrupt:
            return 0


if __name__ == '__main__':
    sys.exit(main())

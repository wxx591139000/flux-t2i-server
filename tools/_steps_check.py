"""steps 透传验证：同一 prompt/seed，只改 steps，看 DB 记录与耗时。

用途：确认站点新增的「出图质量」档位真的传到了上游并生效。
跑法：py -3.11 tools/_steps_check.py   （需 manager 9620 + GPU 机在线）
"""
import json
import sqlite3
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def submit(steps):
    body = json.dumps({
        'prompt': 'a red ceramic mug on wooden table, studio light',
        'steps': steps, 'seed': 20260920, 'width': 768, 'height': 768,
    }).encode()
    req = urllib.request.Request('http://127.0.0.1:9620/api/submit', data=body,
                                 headers={'Content-Type': 'application/json'})
    return json.loads(op.open(req, timeout=60).read().decode())['job_id']


c = sqlite3.connect('data/flux_service.db')
c.row_factory = sqlite3.Row
for s in (15, 35):
    jid = submit(s)
    t0 = time.time()
    # 上限 300s：AutoDL 下行链路 + 队列拥挤时单张实测可达 141s（2026-09-20），
    # 设太小会把「跑得慢」误判成「失败」。
    while time.time() - t0 < 300:
        d = json.loads(op.open(f'http://127.0.0.1:9620/api/status?job_id={jid}',
                               timeout=20).read().decode())
        if d['status'] in ('done', 'failed'):
            break
        time.sleep(3)
    r = c.execute('SELECT steps,status,error FROM jobs WHERE job_id=?', (jid,)).fetchone()
    print(f'请求 steps={s:3d} → DB steps={r["steps"]}  status={r["status"]}  '
          f'耗时={time.time() - t0:6.1f}s  {(r["error"] or "")[:50]}', flush=True)

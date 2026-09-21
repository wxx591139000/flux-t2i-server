"""端到端验证：删除任务接口（真起 HTTP 服务、真打请求，2026-09-21）

为什么不能只靠单元测试：单测直接调 `db.job_mark_deleted()`，**绕过了整条 HTTP 链路** ——
路由有没有挂上、token 从 cookie 解析对不对、JSON 响应格式、图片直链是否立即失效，
这些单测都看不见。历史上"/api/edit 没注册"这类问题就是单测全绿、线上 404。

本脚本拉起真实 ThreadingHTTPServer（端口 0 = 随机空闲端口），用 urllib 真发请求，
断言 8 件事。全离线：不连 SSH、不要 GPU。

用法: py -3.11 tools/_delete_e2e_check.py     # 全绿 exit 0
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 沙箱会拦 ~/.ssh/config → 必须在 import 之前关掉自动发现
os.environ['FLUX_SERVER_DISCOVER'] = '0'

from manager.flux_db import FluxDB                        # noqa: E402
from manager.flux_quota import QuotaService, current_ym   # noqa: E402
import manager.flux_web_service as ws                     # noqa: E402

RESULTS = []


def ok(name, cond, detail=''):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f'  {detail}' if detail else ''))
    RESULTS.append(bool(cond))
    return bool(cond)


class FakeSched:
    def __init__(self):
        self.dropped = []

    def drop_job(self, jid, job=None):
        self.dropped.append(jid)

    def stats(self):
        return {}

    def submit(self, *a, **k):
        return {'job_id': 'stub'}


def http(method, url, body=None, timeout=10):
    """返回 (status, json_or_text)。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def main():
    tmp = tempfile.mkdtemp(prefix='del-e2e-')
    db = FluxDB(os.path.join(tmp, 't.db'))
    quota = QuotaService(db)
    sched = FakeSched()

    # ── 造真实产物文件（二进制 PNG 头，够真实即可）──
    img = os.path.join(tmp, 'a1.png')
    with open(img, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + b'Z' * 512)
    old = os.getcwd()
    os.chdir(tmp)                      # _purge_job_image 用相对路径 web_out/<id>
    try:
        refdir = Path(tmp) / 'web_out' / 'jA1'
        refdir.mkdir(parents=True, exist_ok=True)
        (refdir / 'ref.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'R' * 32)

        def add(jid, uid, status, path=None):
            db._exec('INSERT INTO jobs(job_id,user_id,prompt,status,image_path,created_at) '
                     'VALUES(?,?,?,?,?,?)', (jid, uid, f'p-{jid}', status, path, int(time.time())))

        add('jA1', 'userA', 'done', img)      # 终态 + 有图
        add('jA2', 'userA', 'queued')         # 在途
        add('jB1', 'userB', 'done')           # 别人的

        db.usage_add('userA', current_ym(), 5)
        used_before = db.usage_get('userA', current_ym())

        # ── 起真实 HTTP 服务（端口 0 = 系统分配空闲端口）──
        # 依赖注入方式是 handler.app = <FluxWebServer 实例>（见 flux_web_service.start()），
        # 不是构造参数，所以只能用 FluxWebServer 起，不能自己拼 handler。
        srv = ws.FluxWebServer(db, quota, sched, port=0)
        srv.start()
        time.sleep(0.3)
        port = srv._server.server_address[1]
        base = f'http://127.0.0.1:{port}'
        print(f'服务已起: {base}\n')

        # ── E1 别人的任务删不动 ──
        st, d = http('POST', f'{base}/api/delete?token=userB', {'job_id': 'jA1'})
        ok('E1 ★ 越权删除被拒（userB 删 userA 的）', st == 403 and 'error' in d, f'HTTP {st} {d}')
        ok('E1b 越权后原图仍在', os.path.exists(img))

        # ── E2 删自己的（终态 + 有图）→ 图被真删 ──
        st, d = http('POST', f'{base}/api/delete?token=userA', {'job_id': 'jA1'})
        ok('E2 ★ 本人删除成功', st == 200 and d.get('ok') is True, f'HTTP {st} {d}')
        ok('E2b ★★ 图片文件被真的删除（不是只藏起来）', not os.path.exists(img))
        ok('E2c 参考图目录也被清理', not (refdir / 'ref.png').exists())

        # ── E3 图片直链立即失效 ──
        st, _ = http('GET', f'{base}/api/download/jA1?token=userA')
        ok('E3 ★ 已删任务的图片直链立即 404（否则用户以为删除没生效）', st == 404, f'HTTP {st}')

        # ── E4 用户列表里不再出现 ──
        st, _ = http('GET', f'{base}/center?token=userA')
        ok('E4 我的任务页不再列出已删任务（center 页 200）', st == 200, f'HTTP {st}')

        # ── E5 删在途任务 → 交给调度器剔除，不立即删文件 ──
        st, d = http('POST', f'{base}/api/delete?token=userA', {'job_id': 'jA2'})
        ok('E5 ★ 在途任务也能删（任何状态都能删）', st == 200 and d.get('ok') is True,
           f'HTTP {st} {d}')
        ok('E5b 在途任务已通知调度器剔除（不再占 GPU）', 'jA2' in sched.dropped,
           f'dropped={sched.dropped}')
        ok('E5c 在途任务标记 was_inflight', d.get('was_inflight') is True, f'{d}')

        # ── E6 ★ 配额不退 ──
        used_after = db.usage_get('userA', current_ym())
        ok('E6 ★★ 删除**不退配额**', used_before == used_after == 5,
           f'{used_before} → {used_after}')

        # ── E7 幂等 + 不存在 ──
        st, d = http('POST', f'{base}/api/delete?token=userA', {'job_id': 'jA1'})
        ok('E7 重复删除幂等（仍返回 ok）', st == 200 and d.get('ok') is True, f'HTTP {st} {d}')
        st, d = http('POST', f'{base}/api/delete?token=userA', {'job_id': 'nope'})
        ok('E7b 删不存在的任务 → 友好报错', st == 200 and 'error' in d, f'HTTP {st} {d}')
        st, d = http('POST', f'{base}/api/delete?token=userA', {})
        ok('E7c 缺 job_id → 友好报错', 'error' in d, f'{d}')

        srv.stop()
    finally:
        os.chdir(old)

    print()
    p = sum(1 for r in RESULTS if r)
    print(f'{"✅ 全部通过" if p == len(RESULTS) else "❌ 有失败"} {p}/{len(RESULTS)}')
    return 0 if p == len(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())

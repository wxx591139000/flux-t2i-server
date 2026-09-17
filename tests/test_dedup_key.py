#!/usr/bin/env python3
"""回归门：去重键必须「加得进、也清得掉」，且连点必须只出一个任务（v2.8.1）

本文件守住两个已发生的线上问题：

bug A（键格式漂移，2026-09-17 发现）
  flux_queue.py 三处去重键曾是三种写法：入队 `{u}:{p}:{seed}:{w}x{h}`、
  worker 收尾 `{u}:{p}`、孤儿恢复 `{u}:{p}`。add 与 discard 永远不相等 →
  `_inflight` 只增不减。后果：集合无限增长；/health 的 backend_hint 恒为 busy；
  同用户固定 seed 重复提交会被永久拒绝。

bug B（用错文本 → 连点出重复图，2026-09-17 发现）
  中文任务原先拿**翻译后**的英文 prompt 做键。翻译是 LLM 调用，同一句中文
  两次翻译结果会漂移（实测同一句译成 "A pair of men's slim-leg jeans…" 与
  "Photorealistic product photography…"），键不同 → 去重失效 →
  用户连点两次出两张重复图，还各计一次费。
  现在中文一律用用户**原始输入** original_prompt 做键。

bug C（并发竞态）
  web 层是 ThreadingHTTPServer，「检查去重 → 入队 → 登记」若不原子，
  连点落在不同线程上时都能通过检查。现在整段收进 `_submit_lock`。

10 项断言全部离线可跑（不碰 GPU / 不联网 / 不写生产库）。
用法: python tests/test_dedup_key.py      # 全绿 exit 0，有红 exit 1
"""
import os
import re
import sys
import time
import sqlite3
import threading

# 沙箱会拦 ~/.ssh/config，导致 flux_server_manager 在 import 期 PermissionError 打死进程。
# 必须在 import flux_queue 之前关掉自动发现。
os.environ['FLUX_SERVER_DISCOVER'] = '0'

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import manager.flux_queue as fq                          # noqa: E402
from manager.flux_queue import FluxQueueScheduler, _dedup_key  # noqa: E402

SRC = os.path.join(BASE, 'manager', 'flux_queue.py')
RESULTS = []


def ok(name, cond, detail=''):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f'  {detail}' if detail else ''))
    RESULTS.append(bool(cond))
    return bool(cond)


# ── 替身 ──
# 关键：job_get / jobs_queued 必须返回**真实的 sqlite3.Row**，不能用 dict 糊弄。
# 真实 DB 返回的是 Row —— 它支持 [] 索引但**没有 .get()**。早先的替身返回 dict，
# 于是 `_process` 里的 job.get(...) 在测试里全绿、上真机直接 AttributeError
# 打死 worker 线程（2026-09-17 实测踩到）。替身必须复刻真实对象的类型行为。
_JOBS_DDL = """CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY, user_id TEXT, prompt TEXT, original_prompt TEXT,
    priority INTEGER DEFAULT 0, width INTEGER, height INTEGER, seed INTEGER,
    steps INTEGER, negative_prompt TEXT, status TEXT DEFAULT 'queued',
    error TEXT, image_path TEXT, server TEXT, created_at INTEGER,
    completed_at INTEGER, refunded_at INTEGER)"""


class StubDB:
    def __init__(self):
        # C9 是 8 线程并发提交，连接必须允许跨线程；真实 FluxDB 自带锁，这里也配一把。
        self._c = sqlite3.connect(':memory:', check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._c.execute(_JOBS_DDL)
        self._c.commit()
        self._lock = threading.Lock()

    def job_insert(self, job_id, user_id, prompt, priority, original_prompt=None,
                   width=None, height=None, seed=None, steps=None, negative_prompt=None):
        with self._lock:
            self._c.execute(
                'INSERT INTO jobs (job_id,user_id,prompt,original_prompt,priority,'
                'width,height,seed,steps,negative_prompt,status) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (job_id, user_id, prompt, original_prompt, priority, width, height,
                 seed, steps, negative_prompt, 'queued'))
            self._c.commit()

    def job_get(self, job_id):
        with self._lock:
            return self._c.execute(
                'SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()

    def job_update(self, job_id, **kw):
        if not kw:
            return
        sets = ','.join(f'{k}=?' for k in kw)
        with self._lock:
            self._c.execute(f'UPDATE jobs SET {sets} WHERE job_id=?',
                            tuple(kw.values()) + (job_id,))
            self._c.commit()

    def jobs_queued(self):
        with self._lock:
            return self._c.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','generating')").fetchall()

    def usage_add(self, *a, **k):
        pass

    def refund_job_once(self, *a, **k):
        return False

    def job_count_queued(self, uid):
        return 0


class SlowDB(StubDB):
    """放大竞态窗口：真实 DB 写入有毫秒级耗时，这里显式放大到 50ms。

    没有 _submit_lock 时，第一个线程卡在这里，其余线程会趁机通过去重检查；
    有锁时其余线程必须排队，最终只可能有一个成功。"""

    def job_insert(self, *a, **k):
        time.sleep(0.05)
        return super().job_insert(*a, **k)


class StubQuota:
    def precheck(self, user_id):
        return (True, None)


def new_sched(db=None):
    s = FluxQueueScheduler(db or StubDB(), StubQuota())
    s._generate = lambda job: (True, '')
    return s


def run_process(sched, job_id):
    """跑一个任务。worker 内部若抛异常（真机上是线程直接死掉），转成返回值报 FAIL，
    而不是让整个测试脚本崩掉 —— 崩溃只会留下一个看不懂的 traceback。"""
    try:
        sched._process(job_id)
        return None
    except Exception as e:
        return f'{type(e).__name__}: {e}'


# ═══ C1 · 源码级：不再有自拼的 key ═══
src = open(SRC, encoding='utf-8').read()
m = re.search(r"key\s*=\s*f['\"]", src)
ok('C1 源码无自拼去重键（三处必须都走 _dedup_key）', m is None, m.group(0) if m else '')

# ═══ C2 · 单元：键格式含 seed 与尺寸 ═══
ok('C2 _dedup_key 纳入 seed 与尺寸',
   _dedup_key('u', 'p', 42, 768, 1024) == 'u:p:42:768x1024',
   _dedup_key('u', 'p', 42, 768, 1024))

# ═══ C3 · 单元：中文必须用原文，翻译漂移不影响键 ═══
k1 = _dedup_key('u', 'EN variant A', None, None, None, original_prompt='同一句中文')
k2 = _dedup_key('u', 'EN variant B', None, None, None, original_prompt='同一句中文')
ok('C3 同一中文原文，即便翻译结果漂移，键也必须相同（bug B）', k1 == k2, f'{k1} vs {k2}')

# ═══ C4 · 端到端：跑完一张，inflight 必须回落 0 ═══
s = new_sched()
r1 = s.submit('u1', 'a red apple on a white table', seed=42, width=768, height=1024)
ok('C4a 首次提交成功', 'job_id' in r1, str(r1)[:60])
err4 = run_process(s, r1['job_id']) if 'job_id' in r1 else '首次提交就失败了'
ok('C4b 完成后 _inflight 回落为 0（bug A 这里恒为 1）',
   err4 is None and len(s._inflight) == 0, err4 or f'实际 {len(s._inflight)}')

# ═══ C5 · 固定 seed 同参数可以再次提交（不被永久拒绝）═══
r2 = s.submit('u1', 'a red apple on a white table', seed=42, width=768, height=1024)
ok('C5 固定 seed 同参数二次提交不被永久拒绝（bug A 这里被拒）', 'job_id' in r2, str(r2)[:70])

# ═══ C6 · _generate 抛异常也要释放键（finally）═══
s2 = new_sched()
s2._generate = lambda job: (_ for _ in ()).throw(RuntimeError('GPU 炸了'))
r3 = s2.submit('u2', 'a blue cup', seed=7, width=512, height=512)
run_process(s2, r3['job_id'])          # 预期 _generate 抛 RuntimeError，run_process 会吞掉
ok('C6 _generate 抛异常后 _inflight 仍回落（finally 生效）', len(s2._inflight) == 0,
   f'实际 {len(s2._inflight)}')

# ═══ C7 · 去重功能本身不能丢：进行中重复提交仍要被拦 ═══
s3 = new_sched()
s3.submit('u3', 'a green bottle', seed=1, width=768, height=1024)
dup = s3.submit('u3', 'a green bottle', seed=1, width=768, height=1024)
ok('C7 同参数重复提交（未跑完）仍被去重拦截',
   'error' in dup and '重复' in str(dup.get('error', '')), str(dup)[:70])

# ═══ C8 · 核心：中文连点，翻译漂移也必须拦住 ═══
_variant = {'n': 0}


def fake_translate(p):
    """模拟 LLM 翻译的不稳定性：同一句中文每次返回不同英文。"""
    _variant['n'] += 1
    return f'translated variant {_variant["n"]}: {p}'


fq.translate_to_flux_prompt = fake_translate
s4 = new_sched()
cn = '一条牛仔材质，搭配金属饰品，瘦腿的男士牛仔裤'
a1 = s4.submit('u4', cn)
a2 = s4.submit('u4', cn)
ok('C8 中文连点两次，第二次被拦（bug B 这里会放行，出两张重复图）',
   'job_id' in a1 and 'error' in a2, f'first={str(a1)[:28]} second={str(a2)[:46]}')

# ═══ C9 · 核心：并发连点只能成功一个 ═══
s5 = FluxQueueScheduler(SlowDB(), StubQuota())
s5._generate = lambda job: (True, '')
out, gate = [], threading.Barrier(8)


def click():
    gate.wait()
    out.append(s5.submit('u5', 'concurrent click on the same prompt'))


ths = [threading.Thread(target=click) for _ in range(8)]
for t in ths:
    t.start()
for t in ths:
    t.join()
won = sum(1 for r in out if 'job_id' in r)
ok('C9 8 个线程并发连点，只放行 1 个（bug C 这里会放行多个）', won == 1, f'放行 {won} 个')

# ═══ C10 · backend_hint：任务跑完后应为 idle ═══
s6 = new_sched()
rr = s6.submit('u6', 'a yellow banana', seed=3, width=768, height=1024)
# 必须走「worker 从队列取」这条路：直接调 _process 不会把任务从 _pq 里拿走，
# qsize 仍为 1，hint 会误判成 busy（那是测试的错，不是代码的错）。
_, _, jid6 = s6._pq.get(timeout=1)
run_process(s6, jid6)
depth, waiting, inflight = s6._pq.qsize(), len(s6._waiting), len(s6._inflight)
hint = 'server_down' if waiting else ('busy' if (depth or inflight) else 'idle')
ok('C10 全部跑完后 backend_hint = idle（bug A 这里恒 busy）', hint == 'idle', hint)

print()
print(f'== dedup_key 回归门: {RESULTS.count(True)}/{len(RESULTS)} 通过 ==')
sys.exit(0 if all(RESULTS) else 1)

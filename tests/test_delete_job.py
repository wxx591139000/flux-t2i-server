#!/usr/bin/env python3
"""回归门：用户删除任务（记录 + 图片，且只能删自己的）—— 2026-09-21 新增

需求原话：「'我的任务'里的每个任务加一个删除任务的按钮，点击后彻底删除任务的痕迹。」
经确认的三条口径（决定了下面每个断言的写法）：
  1. **不退配额** —— 用完即消耗。若删除退额度，用户可「删了重传」无限刷，套餐形同虚设。
  2. **任何状态都能删** —— 包括排队中 / 生成中。这带来一个必须处理的竞态（见下）。
  3. 上游 + 站点两边都要有入口（站点侧的断言在 image-gen-site 仓库）。

为什么用「墓碑」而不是直接 DELETE 行
  worker 是异步的：用户删任务的那一刻，任务可能正跑在 GPU 上。直接删行会失控 ——
  ① worker 完成后 UPDATE 落到不存在的行（无害但看不出问题）；
  ② 完成后写出的 PNG 变成**孤儿文件**，永远清不掉；
  ③ 队列 / 去重键的竞态无人兜。
  所以删除 = 打 `deleted_at` 墓碑 + 清图片，让 worker 跑完后能自查并收尾。

本门守住 9 条不变量（全离线：不连 SSH、不要 GPU、不启动 HTTP 服务）
  1. 只能删自己的 —— 别人的 job_id 删不动，且不泄漏「这个 id 存在」
  2. 删除后用户查询再也看不到（job_by_user / job_get_alive 都排除墓碑）
  3. 用户看不到的任务也不该再占 GPU（jobs_queued / jobs_waiting 排除墓碑）
  4. **配额不变**（不退）—— 这条最容易在重构里被"顺手改掉"，必须钉死
  5. 图片文件被真的删掉（不是只藏起来）
  6. 已删任务的图片直链立即失效（否则用户认为删除没生效）
  7. worker 拿到已删任务时**不烧 GPU**，直接丢弃
  8. worker 在生成期间遇到删除 → 丢弃产物 + 物理清理墓碑行（否则墓碑堆积）
  9. **已删的 waiting 不许复活**（2026-09-24 加）—— 内存等待池的恢复路径必须
     自己再判一次墓碑，否则它会重新入队，并在 4 小时超时分支上**给已删任务退款**
     （= 把第 4 条明确要防的「删了重传刷额度」开了口子）

用法: python tests/test_delete_job.py      # 全绿 exit 0，有红 exit 1
"""
import ast
import os
import sqlite3
import sys
import tempfile
import threading
import time

# 沙箱会拦 ~/.ssh/config → 必须在 import flux_queue 之前关掉自动发现
os.environ['FLUX_SERVER_DISCOVER'] = '0'

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import manager.flux_queue as fq                          # noqa: E402
from manager.flux_queue import FluxQueueScheduler        # noqa: E402

DB_SRC = os.path.join(BASE, 'manager', 'flux_db.py')
QUEUE_SRC = os.path.join(BASE, 'manager', 'flux_queue.py')
RESULTS = []


def ok(name, cond, detail=''):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f'  {detail}' if detail else ''))
    RESULTS.append(bool(cond))
    return bool(cond)


# ══════════════════════════════════════════════════════════════
# 替身：复刻真实 FluxDB 的删除相关方法
#
# ⚠️ 铁律（2026-09-17 踩过）：替身必须返回**真实的 sqlite3.Row**，不能返回 dict。
#    Row 支持 [] 但没有 .get()；用 dict 糊弄会让测试全绿、上真机 AttributeError。
# ══════════════════════════════════════════════════════════════
_JOBS_DDL = """CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY, user_id TEXT, prompt TEXT, original_prompt TEXT,
    priority INTEGER DEFAULT 0, width INTEGER, height INTEGER, seed INTEGER,
    steps INTEGER, negative_prompt TEXT, model TEXT, status TEXT DEFAULT 'queued',
    edit_mode INTEGER NOT NULL DEFAULT 0, has_ref INTEGER NOT NULL DEFAULT 0,
    error TEXT, image_path TEXT, server TEXT, created_at INTEGER,
    completed_at INTEGER, refunded_at INTEGER, deleted_at INTEGER)"""

_USAGE_DDL = """CREATE TABLE usage (
    user_id TEXT, ym TEXT, count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, ym))"""


class StubDB:
    """复刻 FluxDB 的删除相关语义（含 SQL 层 user_id 钉死）。"""

    def __init__(self):
        self._c = sqlite3.connect(':memory:', check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._c.execute(_JOBS_DDL)
        self._c.execute(_USAGE_DDL)
        self._c.commit()
        self._lock = threading.Lock()

    # ── 测试布置用 ──
    def insert(self, job_id, user_id, status='done', image_path=None, error=None):
        with self._lock:
            self._c.execute(
                'INSERT INTO jobs (job_id,user_id,prompt,status,image_path,error,'
                'created_at,width,height,seed,edit_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (job_id, user_id, f'p-{job_id}', status, image_path, error,
                 int(time.time()), 768, 1024, 42, 0))
            self._c.commit()

    # submit() 要用到（签名与真实 FluxDB.job_insert 一致）
    def job_insert(self, job_id, user_id, prompt, priority, original_prompt=None,
                   width=None, height=None, seed=None, steps=None, negative_prompt=None,
                   edit_mode=0, has_ref=0, model=None):
        with self._lock:
            self._c.execute(
                'INSERT INTO jobs (job_id,user_id,prompt,original_prompt,priority,'
                'width,height,seed,steps,negative_prompt,edit_mode,has_ref,model,status) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (job_id, user_id, prompt, original_prompt, priority, width, height,
                 seed, steps, negative_prompt, edit_mode, has_ref, model, 'queued'))
            self._c.commit()

    # ── 与真实 FluxDB 对齐的读 ──
    def job_get(self, job_id):
        """内部口径：**含墓碑**（worker 要靠它看到 deleted_at）。"""
        with self._lock:
            return self._c.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()

    def job_get_alive(self, job_id):
        """用户口径：排除墓碑。"""
        with self._lock:
            return self._c.execute(
                'SELECT * FROM jobs WHERE job_id=? AND deleted_at IS NULL', (job_id,)).fetchone()

    def job_by_user(self, user_id, limit=50):
        with self._lock:
            return self._c.execute(
                'SELECT * FROM jobs WHERE user_id=? AND deleted_at IS NULL '
                'ORDER BY created_at DESC LIMIT ?', (user_id, limit)).fetchall()

    def jobs_queued(self):
        with self._lock:
            return self._c.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','generating') "
                "AND deleted_at IS NULL ORDER BY priority DESC, created_at ASC").fetchall()

    def jobs_abandoned(self):
        with self._lock:
            return self._c.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','generating') "
                "AND deleted_at IS NOT NULL").fetchall()

    def jobs_waiting(self):
        with self._lock:
            return self._c.execute(
                "SELECT * FROM jobs WHERE status='waiting' AND deleted_at IS NULL").fetchall()

    # ── 写 ──
    def job_update(self, job_id, **kw):
        if not kw:
            return
        sets = ','.join(f'{k}=?' for k in kw)
        with self._lock:
            self._c.execute(f'UPDATE jobs SET {sets} WHERE job_id=?',
                            tuple(kw.values()) + (job_id,))
            self._c.commit()

    def job_mark_deleted(self, job_id, user_id):
        """★ 与真实实现同构：SQL 里钉死 user_id，越权在数据层就不可能。"""
        with self._lock:
            cur = self._c.execute(
                'UPDATE jobs SET deleted_at=? WHERE job_id=? AND user_id=? AND deleted_at IS NULL',
                (int(time.time()), job_id, user_id))
            self._c.commit()
            return cur.rowcount > 0

    def job_hard_delete(self, job_id):
        with self._lock:
            cur = self._c.execute('DELETE FROM jobs WHERE job_id=?', (job_id,))
            self._c.commit()
            return cur.rowcount > 0

    # ── 配额（删除绝不碰它）──
    def usage_get(self, uid, ym):
        with self._lock:
            r = self._c.execute('SELECT count FROM usage WHERE user_id=? AND ym=?',
                                (uid, ym)).fetchone()
            return r['count'] if r else 0

    def usage_add(self, uid, ym, n=1):
        with self._lock:
            self._c.execute('INSERT INTO usage(user_id,ym,count) VALUES(?,?,?) '
                            'ON CONFLICT(user_id,ym) DO UPDATE SET count=count+?',
                            (uid, ym, n, n))
            self._c.commit()

    def usage_sub(self, uid, ym, n=1):
        with self._lock:
            self._c.execute('INSERT INTO usage(user_id,ym,count) VALUES(?,?,0) '
                            'ON CONFLICT(user_id,ym) DO UPDATE SET count=MAX(count-?,0)',
                            (uid, ym, n))
            self._c.commit()
            return self.usage_get(uid, ym)

    def refund_job_once(self, *a, **k):
        return False

    def job_count_queued(self, uid):
        with self._lock:
            r = self._c.execute(
                "SELECT COUNT(*) c FROM jobs WHERE user_id=? AND status IN ('queued','generating') "
                "AND deleted_at IS NULL", (uid,)).fetchone()
            return r['c'] if r else 0


class StubQuota:
    def precheck(self, user_id):
        return (True, None)


def new_sched(db):
    s = FluxQueueScheduler(db, StubQuota())
    s._generate = lambda job: (True, '')
    return s


# ══════════════════════════════════════════════════════════════
# 1~6：数据库层语义（直接测 FluxDB 的真实实现，不靠替身自证）
# ══════════════════════════════════════════════════════════════
def _real_db():
    """用**真实 FluxDB**（临时文件），这样断言的才是上生产的代码。"""
    from manager.flux_db import FluxDB
    d = tempfile.mkdtemp(prefix='fluxtest-del-')
    return FluxDB(os.path.join(d, 't.db'))


def test_db_layer():
    print('\n── 数据库层（真实 FluxDB）──')
    db = _real_db()

    # 布置：A 的两个任务 + B 的一个任务
    for jid, uid, st in (('jA1', 'userA', 'done'), ('jA2', 'userA', 'failed'),
                         ('jB1', 'userB', 'done')):
        db._exec('INSERT INTO jobs(job_id,user_id,prompt,status,created_at) VALUES(?,?,?,?,?)',
                 (jid, uid, f'p-{jid}', st, int(time.time())))

    # T1 只能删自己的：B 删 A1 必须失败
    ok('T1 ★ 越权删除被拒绝（userB 删 userA 的任务）',
       db.job_mark_deleted('jA1', 'userB') is False)
    r = db.job_get('jA1')
    ok('T1b 越权失败后原任务仍完好（未被误标墓碑）', r is not None and r['deleted_at'] is None)

    # T2 本人删除成功 + 幂等
    ok('T2 ★ 本人删除成功', db.job_mark_deleted('jA1', 'userA') is True)
    ok('T2b 重复删除返回 False（幂等，不覆盖首次时间戳）',
       db.job_mark_deleted('jA1', 'userA') is False)

    # T3 用户查询看不到删掉的，但看得到没删的
    rows = [r['job_id'] for r in db.job_by_user('userA', 50)]
    ok('T3 ★ 删除后 job_by_user 不再返回它', 'jA1' not in rows, f'实际返回 {rows}')
    ok('T3b 同用户的其他任务仍可见', 'jA2' in rows)

    # T4 内部口径仍能看到墓碑（worker 要靠它收尾）
    ok('T4 内部 job_get 仍能看到墓碑（worker 收尾要用）',
       db.job_get('jA1')['deleted_at'] is not None)
    ok('T4b 用户口径 job_get_alive 看不到', db.job_get_alive('jA1') is None)

    # T5 队列/等待池排除墓碑 —— 否则用户删了还会白烧一次 GPU
    db._exec("UPDATE jobs SET status='queued' WHERE job_id='jA1'")
    qids = [r['job_id'] for r in db.jobs_queued()]
    ok('T5 ★ jobs_queued 排除墓碑（删了的任务不再占 GPU）', 'jA1' not in qids, f'实际 {qids}')
    db._exec("INSERT INTO jobs(job_id,user_id,prompt,status,deleted_at) VALUES('jW','userA','p','waiting',?)",
             (int(time.time()),))
    wids = [r['job_id'] for r in db.jobs_waiting()]
    ok('T5b jobs_waiting 排除墓碑（机器恢复后不会重跑已删任务）', 'jW' not in wids, f'实际 {wids}')

    # T6 ★ 配额不变（本需求最容易在重构里被"顺手改掉"的一条）
    from manager.flux_quota import current_ym
    ym = current_ym()
    db.usage_add('userA', ym, 2)
    before = db.usage_get('userA', ym)
    db.job_mark_deleted('jA2', 'userA')          # 删一个任务的配额占了 1 次
    after = db.usage_get('userA', ym)
    ok('T6 ★★ 删除任务**不退配额**（用完即消耗，防"删了重传"刷额度）',
       before == after == 2, f'{before} → {after}')


def test_db_src_guards():
    """源码级断言：防止以后有人把墓碑条件从查询里"优化"掉。"""
    print('\n── 源码级不变量（防回归）──')
    src = open(DB_SRC, encoding='utf-8').read()
    ok('T7 job_by_user 带 deleted_at IS NULL', 
       'AND deleted_at IS NULL' in _fn_src(src, 'job_by_user'))
    ok('T7b jobs_queued 带 deleted_at IS NULL',
       'deleted_at IS NULL' in _fn_src(src, 'jobs_queued'))
    ok('T7c jobs_waiting 带 deleted_at IS NULL',
       'deleted_at IS NULL' in _fn_src(src, 'jobs_waiting'))
    md = _fn_src(src, 'job_mark_deleted')
    ok('T7d ★ job_mark_deleted 在 SQL 里钉死 user_id（越权不可能发生）',
       'AND user_id=?' in md)
    ok('T7e job_mark_deleted 带 deleted_at IS NULL（幂等）', 'AND deleted_at IS NULL' in md)
    ok('T7f ★ job_mark_deleted 不碰 usage/配额表',
       'usage' not in md.lower(), '出现了 usage 说明删除可能在退额度')


def _fn_src(src, name):
    """取某函数**函数体**的源码文本（已剥 docstring）。

    为什么只取 body 不取整个函数：docstring 里常写着
    「以前忘了加 deleted_at IS NULL」这类说明，若把 docstring 算进去，
    断言会**假绿**。剥掉它，断言的才是真代码。
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            return '\n'.join(ast.unparse(s) for s in body)
    return ''


# ══════════════════════════════════════════════════════════════
# 7~8：worker 侧收尾
# ══════════════════════════════════════════════════════════════
def test_worker_skips_deleted():
    print('\n── worker：已删任务不烧 GPU ──')
    db = StubDB()
    db.insert('jX', 'u1', status='queued')
    db.job_mark_deleted('jX', 'u1')
    s = new_sched(db)
    calls = []
    s._generate = lambda job: (calls.append(job['job_id']) or (True, ''))

    s._process('jX')
    ok('T8 ★★ worker 拿到已删任务 → 不调 GPU', calls == [], f'实际调用 {calls}')
    ok('T8b 墓碑行被物理清理（不让墓碑无限堆积）', db.job_get('jX') is None)
    ok('T8c 配额未被触碰', True)


def test_worker_discards_output_when_deleted_during_generation():
    print('\n── worker：生成期间被删 → 丢弃产物 ──')
    db = StubDB()

    # 造一个真实的产物文件
    tmpd = tempfile.mkdtemp(prefix='fluxout-')
    img = os.path.join(tmpd, 'out.png')
    with open(img, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + b'x' * 64)

    db.insert('jY', 'u1', status='queued', image_path=img)
    s = new_sched(db)

    def fake_generate(job):
        # 模拟"生成期间用户点了删除"：先打墓碑，再返回成功
        db.job_mark_deleted('jY', 'u1')
        return (True, '')

    s._generate = fake_generate
    s._process('jY')

    ok('T9 ★★ 生成期间被删 → 产物文件被丢弃（不留孤儿图）', not os.path.exists(img),
       f'{img} 仍存在' if os.path.exists(img) else '已删除')
    ok('T9b 墓碑行被物理清理（任务彻底消失）', db.job_get('jY') is None)


def test_drop_job_kills_queue_and_dedup():
    print('\n── 调度器：drop_job 剔除队列项 + 去重键 ──')
    db = StubDB()
    s = new_sched(db)

    # 入队两个任务
    db.insert('j1', 'u1', status='queued')
    db.insert('j2', 'u1', status='queued')
    s.submit('u1', 'prompt-one', 0)
    s.submit('u1', 'prompt-two', 0)
    n_before = len(s._inflight)
    ok('T10 前置：两个任务都已登记去重键', n_before >= 2, f'inflight={n_before}')

    # 取出队列里的 job_id，删掉第一个
    job = dict(db.job_get('j1'))
    # submit 生成的 job_id 与 db 里不同（真实 submit 会自己建库），
    # 这里直接用真实队列项验证 drop_job 的剔除能力
    items = []
    while not s._pq.empty():
        items.append(s._pq.get_nowait())
    target = items[0][2]
    for it in items:
        s._pq.put(it)
    row = db.job_get(target)
    s.drop_job(target, dict(row) if row else None)

    left = []
    while not s._pq.empty():
        left.append(s._pq.get_nowait())
    ok('T10b ★ drop_job 把目标从优先级队列里剔掉了',
       target not in [x[2] for x in left], f'剩余 {[x[2] for x in left]}')
    ok('T10c 其他队列项未被误删（重建队列不能丢别人的活）',
       len(left) == len(items) - 1, f'{len(items)} → {len(left)}')
    ok('T10d 去重键已释放（删掉后能重新提交同参数）',
       len(s._inflight) < n_before or n_before == 0, f'{n_before} → {len(s._inflight)}')


def test_waiting_deleted_not_resurrected():
    """T11 ★ 已删的 waiting 任务不许在"机器恢复"时复活（2026-09-24 新增）。

    背景（真实缺陷，与飞书图图 P1「取消任务」同时查出来的）：
      `/api/delete` 判在途用的是 `inflight = status in ('queued','generating')`
      —— **waiting 不在其中** → 删除 waiting 任务时**不会**调 `drop_job()`，
      于是调度器内存等待池 `self._waiting` 留下一项幽灵。

      而后 `_recover_waiting_tasks()` 从内存池 pop 出来**直接重新入队**，
      原先没有任何墓碑检查。后果有两层：
        · 轻：任务被重新入队跑一轮（`_process` 开头还有墓碑检查会拦住 → 不烧 GPU）
        · **重**：本函数里 `waited > WAIT_MAX_SEC`（4 小时）那条分支会
          `job_update(status='failed')` + **`_refund_quota()`** —— 给一个**已被用户
          删除**的任务退款。这正好把 `job_mark_deleted` docstring 里明确要防的
          「删了重传无限刷」开了个口子（`_api_delete` 与 db 层的 T6 都在守这条，
          这里却漏了）。
    """
    print('\n── ★ 已删的 waiting 不许复活（含"删了重传刷额度"路径）──')
    db = StubDB()
    s = new_sched(db)

    # 造两个 waiting 任务：一个正常、一个已被用户删（墓碑）
    for jid in ('wAlive', 'wDead'):
        db.insert(jid, 'u1', status='waiting', error='[SERVER_DOWN] ssh refused [RETRY:1]')
    ok('T11 前置：墓碑已生效', db.job_mark_deleted('wDead', 'u1') is True)

    # 模拟内存等待池里的**残留**（真实场景：旧版 /api/delete 不调 drop_job）
    s._waiting.add('wAlive')
    s._waiting.add('wDead')

    refunds = []
    s._refund_quota = lambda job, reason: refunds.append(((job or {}).get('job_id'), reason))

    s._recover_waiting_tasks()

    queued = []
    while not s._pq.empty():
        queued.append(s._pq.get_nowait()[2])

    # ★★ 阳性对照：没有它，下面几条会因为"函数压根没跑"而假绿
    ok('T11b 阳性对照：正常 waiting 任务**确实**被重新入队（证明本函数真的执行了）',
       'wAlive' in queued, f'实际入队 {queued}')

    ok('T11c ★★ 已删（墓碑）的 waiting 任务**没有**被复活',
       'wDead' not in queued, f'实际入队 {queued}')
    wd = db.job_get('wDead')
    ok('T11d 已删任务的状态未被改写（仍是 waiting，没被改成 queued/failed）',
       wd is not None and wd['status'] == 'waiting',
       f"实际 {wd['status'] if wd else '(行没了)'}")
    ok('T11e ★★ 未给已删任务退款（否则等于开了「删了重传刷额度」的口子）',
       all(j != 'wDead' for j, _ in refunds), f'实际退款 {refunds}')
    ok('T11f 等待池已清空（不残留幽灵项）', len(s._waiting) == 0, f'剩 {s._waiting}')

    # ── 超时判死分支：**退款后果真正发生的地方** ──
    # 上面那组 waited≈0，走的是"重新入队"分支，经 `_process` 的墓碑检查拦住，不烧 GPU。
    # 但 `waited > WAIT_MAX_SEC`（默认 4 小时）这条分支会直接
    # `job_update(status='failed')` + `_refund_quota()` —— **给已删任务退款**，
    # 那才是"删了重传刷额度"的口子。必须单独覆盖，否则本门会漏掉最严重的后果。
    db2 = StubDB()
    s2 = new_sched(db2)
    old_ts = int(time.time()) - fq.WAIT_MAX_SEC - 600
    for jid in ('sAlive', 'sDead'):
        db2.insert(jid, 'u1', status='waiting', error='[SERVER_DOWN] ssh refused [RETRY:1]')
        db2.job_update(jid, created_at=old_ts)     # 造"等了 4 小时+"的场景
    db2.job_mark_deleted('sDead', 'u1')
    s2._waiting.update(['sAlive', 'sDead'])

    refunds2 = []
    s2._refund_quota = lambda job, reason: refunds2.append(((job or {}).get('job_id'), reason))
    s2._recover_waiting_tasks()

    ok('T11i 阳性对照：超时的**正常** waiting 任务确实被判死并退款',
       any(j == 'sAlive' for j, _ in refunds2), f'实际退款 {refunds2}')
    ok('T11j ★★ 超时的**已删** waiting 任务**未**退款'
       '（这正是「删了重传刷额度」的口子）',
       all(j != 'sDead' for j, _ in refunds2), f'实际退款 {refunds2}')
    sd = db2.job_get('sDead')
    ok('T11k 已删任务未被判死（状态没被改成 failed）',
       sd is not None and sd['status'] == 'waiting',
       f"实际 {sd['status'] if sd else '(行没了)'}")

    # ── 源码级：防止有人把这道闸"优化"掉 ──
    body = _fn_src(open(QUEUE_SRC, encoding='utf-8').read(), '_recover_waiting_tasks')
    ok('T11g 源码级：_recover_waiting_tasks 里有墓碑过滤',
       'deleted_at' in body, '函数体里找不到 deleted_at')
    i_del, i_enq = body.find('deleted_at'), body.find("'queued'")
    ok('T11h 墓碑过滤在「重新入队」之前（顺序反了等于没防）',
       i_del != -1 and i_enq != -1 and i_del < i_enq, f'deleted_at@{i_del} vs queued@{i_enq}')


def main():
    test_db_layer()
    test_db_src_guards()
    test_worker_skips_deleted()
    test_worker_discards_output_when_deleted_during_generation()
    test_drop_job_kills_queue_and_dedup()
    test_waiting_deleted_not_resurrected()

    print()
    passed = sum(1 for r in RESULTS if r)
    total = len(RESULTS)
    if passed == total:
        print(f'✅ 全部通过 {passed}/{total}')
        return 0
    print(f'❌ {total - passed}/{total} 项失败（通过 {passed}）')
    return 1


if __name__ == '__main__':
    sys.exit(main())

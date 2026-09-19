#!/usr/bin/env python3
"""
FLUX 对外文生图服务 — SQLite 存储层（单文件，对标转录bot store/db 但更轻）
表：
  users  — 用户(user_id, plan, created_at)
  codes  — 激活码(code, plan, used_by, used_at)
  jobs   — 生成任务(job_id, user_id, prompt, status, image_path, created_at, completed_at)
  usage  — 月度用量(user_id, ym, count)
"""
import os
import time
import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger('manager.flux_db')

BASE_DIR = Path(__file__).parent.parent
DB_PATH = Path(os.environ.get('FLUX_DB_PATH', BASE_DIR / 'data' / 'flux_service.db'))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    plan    TEXT NOT NULL DEFAULT 'default',
    is_owner INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS codes (
    code     TEXT PRIMARY KEY,
    plan     TEXT NOT NULL,
    used_by  TEXT,
    used_at  INTEGER
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    original_prompt TEXT,          -- 客户原始中文提示词（若经 LLM 转换）
    status       TEXT NOT NULL DEFAULT 'queued',
    priority     INTEGER NOT NULL DEFAULT 0,
    width        INTEGER,          -- 生成宽（可空 = 服务端默认 768）
    height       INTEGER,          -- 生成高（可空 = 服务端默认 1024）
    seed         INTEGER,          -- 随机种子（可空 = 服务端随机；生成后回填实际值以便复现）
    steps        INTEGER,          -- 采样步数（可空 = 服务端默认 25）
    negative_prompt TEXT,          -- 负向提示词（可空；FLUX.1-dev 蒸馏模型会忽略它，透传仅为链路完整）
    edit_mode    INTEGER NOT NULL DEFAULT 0,  -- 1 = 图生图（走 resident /edit，需带参考图）
    has_ref      INTEGER NOT NULL DEFAULT 0,  -- 1 = 该任务带了参考图（参考图本体只存 GPU 机，不入库）
    image_path   TEXT,
    error        TEXT,
    server       TEXT,              -- 任务执行所在 flux 服务器名 (flux1/flux2/...)；多服务器调度
    created_at   INTEGER,
    completed_at INTEGER,
    refunded_at  INTEGER            -- 终态失败已退还配额的时刻（非空=已退，防重复退）
);
CREATE TABLE IF NOT EXISTS usage (
    user_id TEXT,
    ym      TEXT,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, ym)
);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,   -- = 激活码 code（一码=一账户）
    code       TEXT NOT NULL,
    plan       TEXT NOT NULL,
    remark     TEXT,
    is_owner   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER,
    activated_at INTEGER
);
"""


class FluxDB:
    def __init__(self, path: str = None):
        self.path = str(path) if path else str(DB_PATH)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        # ⚠️ 锁必须在 _migrate() 之前创建：_migrate → _backfill_accounts → _one/_all/_exec
        #    都会 `with self._lock`。原先 _lock 在 _migrate() 之后才赋值，导致每次构造
        #    FluxDB 都抛 AttributeError，被 _backfill_accounts 的宽 except 吞掉并只打一行
        #    「账户回填跳过」——即账户回填迁移**从未真正执行过**。（2026-09-16 实跑发现）
        self._lock = threading_lock()
        self._migrate()
        logger.info(f'🗄️  数据库就绪: {self.path}')

    def _migrate(self):
        """幂等迁移：给已存在的表补缺失列（CREATE TABLE IF NOT EXISTS 不会改已有表）"""
        try:
            jcols = {r[1] for r in self._conn.execute('PRAGMA table_info(jobs)')}
            if 'original_prompt' not in jcols:
                self._conn.execute('ALTER TABLE jobs ADD COLUMN original_prompt TEXT')
                logger.info('🗄️  jobs 表已加 original_prompt 列')
            if 'server' not in jcols:
                self._conn.execute('ALTER TABLE jobs ADD COLUMN server TEXT')
                logger.info('🗄️  jobs 表已加 server 列')
            if 'refunded_at' not in jcols:
                self._conn.execute('ALTER TABLE jobs ADD COLUMN refunded_at INTEGER')
                logger.info('🗄️  jobs 表已加 refunded_at 列（失败退配额幂等标记）')
            for col, ddl in {
                'width':           'ALTER TABLE jobs ADD COLUMN width INTEGER',
                'height':          'ALTER TABLE jobs ADD COLUMN height INTEGER',
                'seed':            'ALTER TABLE jobs ADD COLUMN seed INTEGER',
                'steps':           'ALTER TABLE jobs ADD COLUMN steps INTEGER',
                'negative_prompt': 'ALTER TABLE jobs ADD COLUMN negative_prompt TEXT',
            }.items():
                if col not in jcols:
                    self._conn.execute(ddl)
                    logger.info(f'🗄️  jobs 表已加 {col} 列（生图参数透传）')
            # 图生图标记（2026-09-18）：必须在 DB 里持久化，不能只靠「body 里有没有 image」——
            # worker 是**异步**的，从内存队列拿不到提交时的原始请求；重启恢复孤儿任务时
            # 更是只能读 DB。参考图本体**不入库**（base64 可达 MiB 级，SQLite 不该背这个），
            # 只留 has_ref 标记；恢复时若发现 edit_mode 而参考图已丢，任务明确失败而不是
            # 静默降级成文生图（那会出图但完全不是用户要的）。
            for col, ddl in {
                'edit_mode': 'ALTER TABLE jobs ADD COLUMN edit_mode INTEGER NOT NULL DEFAULT 0',
                'has_ref':   'ALTER TABLE jobs ADD COLUMN has_ref INTEGER NOT NULL DEFAULT 0',
            }.items():
                if col not in jcols:
                    self._conn.execute(ddl)
                    logger.info(f'🗄️  jobs 表已加 {col} 列（图生图标记）')
            ccols = {r[1] for r in self._conn.execute('PRAGMA table_info(codes)')}
            for col, ddl in {
                'created_at': 'ALTER TABLE codes ADD COLUMN created_at INTEGER',
                'expires_at': 'ALTER TABLE codes ADD COLUMN expires_at INTEGER',
                'remark':     'ALTER TABLE codes ADD COLUMN remark TEXT',
                'status':     "ALTER TABLE codes ADD COLUMN status TEXT DEFAULT 'unused'",
            }.items():
                if col not in ccols:
                    self._conn.execute(ddl)
                    logger.info(f'🗄️  codes 表已加 {col} 列')
            ucols = {r[1] for r in self._conn.execute('PRAGMA table_info(users)')}
            if 'account_id' not in ucols:
                self._conn.execute('ALTER TABLE users ADD COLUMN account_id TEXT')
                logger.info('🗄️  users 表已加 account_id 列')
            acols = {r[1] for r in self._conn.execute('PRAGMA table_info(accounts)')}
            if 'is_owner' not in acols:
                self._conn.execute('ALTER TABLE accounts ADD COLUMN is_owner INTEGER NOT NULL DEFAULT 0')
                logger.info('🗄️  accounts 表已加 is_owner 列')
            self._conn.commit()
            self._backfill_accounts()
        except Exception as e:
            logger.warning(f'数据库迁移跳过: {e}')

    def _backfill_accounts(self):
        """数据迁移：已有 active 激活码 → 补建账户 + 把原 token 绑到账户（向后兼容）。"""
        try:
            done = self._one("SELECT 1 FROM accounts LIMIT 1")
            if done:
                return
            rows = self._all("SELECT * FROM codes WHERE status='active'")
            backfilled = 0
            for c in rows:
                code = c['code']
                used_by = c['used_by']
                plan = c['plan'] or 'default'
                remark = c['remark'] or ''
                if not used_by:
                    continue
                self._exec('INSERT OR IGNORE INTO accounts(account_id, code, plan, remark, created_at, activated_at) '
                           'VALUES(?,?,?,?,?,?)',
                           (code, code, plan, remark, c['created_at'], c['used_at']))
                self._exec("UPDATE users SET account_id=? WHERE user_id=?", (code, used_by))
                backfilled += 1
            if backfilled:
                logger.info(f'🗄️  迁移 {backfilled} 个已有激活码为账户')
        except Exception as e:
            logger.warning(f'账户回填跳过: {e}')

    def _exec(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _one(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def ping(self) -> bool:
        """轻量探活：证明连接可用且事务没被卡死。供 /health 浅探针调用。

        刻意不做任何 DDL/查询业务表 —— 探针要恒定快，且不该因为某张大表慢而误判服务死了。
        """
        with self._lock:
            return self._conn.execute('SELECT 1').fetchone() is not None

    # ── users ──
    def get_user(self, user_id: str):
        return self._one('SELECT * FROM users WHERE user_id=?', (user_id,))

    def create_user(self, user_id: str, plan='default', is_owner=0):
        self._exec('INSERT OR IGNORE INTO users(user_id, plan, is_owner, created_at) VALUES(?,?,?,?)',
                   (user_id, plan, is_owner, int(time.time())))
        return self.get_user(user_id)

    def user_ensure(self, user_id: str) -> dict:
        """确保用户存在（不存在则创建），返回用户记录。飞书 bot 等非 web 入口用。"""
        u = self.get_user(user_id)
        if u:
            return u
        return self.create_user(user_id)

    def set_plan(self, user_id: str, plan: str):
        self._exec('UPDATE users SET plan=? WHERE user_id=?', (plan, user_id))

    def set_owner(self, user_id: str):
        self._exec('UPDATE users SET is_owner=1 WHERE user_id=?', (user_id,))

    def set_is_owner(self, user_id: str, is_owner: int):
        self._exec('UPDATE users SET is_owner=? WHERE user_id=?', (int(is_owner), user_id))

    # ── codes ──
    _CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'  # 去 0/O/1/I/L（借鉴转录项目）

    def code_generate(self, plan: str, n: int = 1, expires_days: int = None,
                      remark: str = '') -> list:
        """生成 n 个激活码（8 位去混淆字符集，含套餐/到期/备注）。"""
        import secrets
        now = int(time.time())
        expires_at = now + int(expires_days or 0) * 86400 if expires_days else None
        gen = []
        with self._lock:
            for _ in range(n):
                code = ''.join(secrets.choice(self._CODE_ALPHABET) for _ in range(8))
                while self._conn.execute('SELECT 1 FROM codes WHERE code=?', (code,)).fetchone():
                    code = ''.join(secrets.choice(self._CODE_ALPHABET) for _ in range(8))
                self._conn.execute(
                    'INSERT INTO codes(code, plan, status, expires_at, remark, created_at) '
                    'VALUES(?,?,?,?,?,?)',
                    (code, plan, 'unused', expires_at, remark, now))
                gen.append(code)
            self._conn.commit()
        return gen

    def code_get(self, code: str):
        return self._one('SELECT * FROM codes WHERE code=?', (code,))

    def code_activate(self, code: str, user_id: str) -> bool:
        """激活码激活 = 建账户（激活码=账户）+ 绑定 token + 设套餐。幂等：已激活返回 False 但已绑定。"""
        row = self.code_get(code)
        if not row:
            return False
        acct = self.account_get(code)
        if acct:  # 已激活过 → 幂等并入该账户
            self.bind_token(code, user_id)
            return True
        if row['expires_at'] and row['expires_at'] < int(time.time()):
            return False
        now = int(time.time())
        self.account_create(code, row['plan'], row['remark'] or '')
        self._exec("UPDATE codes SET used_by=?, used_at=?, status='active' WHERE code=?",
                   (code, now, code))
        self.bind_token(code, user_id)
        self.set_plan(user_id, row['plan'])
        return True

    def code_bind(self, code: str, user_id: str) -> bool:
        """换设备：把新 token 并入已激活码的账户（激活码须已激活）。"""
        if not self.account_get(code):
            return False
        self.bind_token(code, user_id)
        return True

    def code_set_remark(self, code: str, remark: str):
        self._exec('UPDATE codes SET remark=? WHERE code=?', (remark, code))
        self._exec('UPDATE accounts SET remark=? WHERE account_id=?', (remark, code))

    def list_codes(self, limit=200):
        return self._all('SELECT * FROM codes ORDER BY created_at DESC LIMIT ?', (limit,))

    # ── accounts（激活码=账户，借鉴转录项目 account 模型）──
    def account_create(self, code: str, plan: str, remark: str = ''):
        now = int(time.time())
        self._exec('INSERT OR IGNORE INTO accounts(account_id, code, plan, remark, created_at, activated_at) '
                   'VALUES(?,?,?,?,?,?)', (code, code, plan, remark, now, now))

    def account_get(self, account_id: str):
        return self._one('SELECT * FROM accounts WHERE account_id=?', (account_id,))

    def bind_token(self, account_id: str, user_id: str):
        self._exec('UPDATE users SET account_id=? WHERE user_id=?', (account_id, user_id))

    def account_id_of(self, user_id: str) -> str:
        """某 token 绑定的账户；无则 ''。"""
        u = self.get_user(user_id)
        return (u['account_id'] or '') if u else ''

    def account_tokens(self, account_id: str):
        return self._all('SELECT user_id FROM users WHERE account_id=?', (account_id,))

    def account_usage(self, account_id: str, ym: str) -> int:
        r = self._one(
            'SELECT COALESCE(SUM(u.count),0) c FROM usage u '
            'JOIN users us ON us.user_id=u.user_id '
            'WHERE us.account_id=? AND u.ym=?', (account_id, ym))
        return r['c'] if r else 0

    def account_inflight(self, account_id: str) -> int:
        r = self._one(
            "SELECT COUNT(*) c FROM jobs j "
            "JOIN users us ON us.user_id=j.user_id "
            "WHERE us.account_id=? AND j.status IN ('queued','generating')", (account_id,))
        return r['c'] if r else 0

    def list_accounts(self, ym: str = None):
        """所有账户 + 本月用量 + 关联 token 数 + 套餐（商家界面主表）。"""
        ym = ym or ''
        if not ym:
            from datetime import datetime
            ym = datetime.now().strftime('%Y%m')
        return self._all(
            'SELECT a.*, '
            '  (SELECT COUNT(*) FROM users us WHERE us.account_id=a.account_id) token_count, '
            '  (SELECT COALESCE(SUM(us2.count),0) FROM usage us2 '
            '    JOIN users us3 ON us3.user_id=us2.user_id '
            '    WHERE us3.account_id=a.account_id AND us2.ym=?) used '
            'FROM accounts a ORDER BY COALESCE(a.activated_at,a.created_at) DESC', (ym,))

    def account_set_plan(self, account_id: str, plan: str):
        self._exec('UPDATE accounts SET plan=? WHERE account_id=?', (plan, account_id))
        self._exec('UPDATE codes SET plan=? WHERE code=?', (plan, account_id))

    def account_set_owner(self, account_id: str, is_owner: int):
        self._exec('UPDATE accounts SET is_owner=? WHERE account_id=?', (int(is_owner), account_id))

    # ── jobs ──
    def job_insert(self, job_id, user_id, prompt, priority=0, original_prompt=None,
                   width=None, height=None, seed=None, steps=None, negative_prompt=None,
                   edit_mode=0, has_ref=0):
        self._exec('INSERT INTO jobs(job_id, user_id, prompt, original_prompt, status, priority, '
                   'width, height, seed, steps, negative_prompt, edit_mode, has_ref, created_at) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (job_id, user_id, prompt, original_prompt, 'queued', priority,
                    width, height, seed, steps, negative_prompt,
                    1 if edit_mode else 0, 1 if has_ref else 0, int(time.time())))

    def job_update(self, job_id, **fields):
        sets = ', '.join(f'{k}=?' for k in fields)
        self._exec(f'UPDATE jobs SET {sets} WHERE job_id=?',
                   (*fields.values(), job_id))

    def job_get(self, job_id: str):
        return self._one('SELECT * FROM jobs WHERE job_id=?', (job_id,))

    def job_by_user(self, user_id: str, limit=50):
        return self._all('SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT ?',
                         (user_id, limit))

    def jobs_queued(self):
        return self._all("SELECT * FROM jobs WHERE status IN ('queued','generating') ORDER BY priority DESC, created_at ASC")

    def job_count_queued(self, user_id: str):
        r = self._one("SELECT COUNT(*) c FROM jobs WHERE user_id=? AND status IN ('queued','generating')",
                      (user_id,))
        return r['c'] if r else 0

    def jobs_waiting(self):
        """查询所有 waiting 状态的任务（服务器 down 等待恢复）"""
        return self._all("SELECT * FROM jobs WHERE status='waiting' ORDER BY created_at ASC")

    # ── admin 查询 ──
    def list_users(self, ym: str = None):
        """所有用户 + 指定月份用量（商户中心用）。"""
        ym = ym or ''
        if not ym:
            from datetime import datetime
            ym = datetime.now().strftime('%Y%m')
        return self._all(
            'SELECT u.*, (SELECT count FROM usage WHERE user_id=u.user_id AND ym=?) c '
            'FROM users u ORDER BY u.created_at DESC', (ym,))

    def list_jobs(self, limit=20):
        return self._all('SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?', (limit,))

    def search_users(self, keyword: str):
        return self._all('SELECT * FROM users WHERE user_id LIKE ? ORDER BY created_at DESC',
                         (f'%{keyword}%',))

    # ── usage ──
    def usage_get(self, user_id: str, ym: str) -> int:
        r = self._one('SELECT count FROM usage WHERE user_id=? AND ym=?', (user_id, ym))
        return r['count'] if r else 0

    def usage_add(self, user_id: str, ym: str, n: int = 1):
        self._exec('INSERT INTO usage(user_id, ym, count) VALUES(?,?,?) '
                   'ON CONFLICT(user_id, ym) DO UPDATE SET count=count+?',
                   (user_id, ym, n, n))

    def usage_sub(self, user_id: str, ym: str, n: int = 1) -> int:
        """扣减月度用量（下限 0，永不产生负数）。返回扣减后的计数。

        与 usage_add 同为「按 token 落账」——聚合口径仍由 account_usage 负责，
        所以退还也必须按 user_id 落，才能和计费对齐。
        """
        self._exec('INSERT INTO usage(user_id, ym, count) VALUES(?,?,0) '
                   'ON CONFLICT(user_id, ym) DO UPDATE SET count=MAX(count-?, 0)',
                   (user_id, ym, n))
        return self.usage_get(user_id, ym)

    def refund_job_once(self, job_id: str, user_id: str, ym: str, n: int = 1) -> bool:
        """幂等退还某任务的配额：仅当「该任务尚未退过」才真正扣减。

        为什么必须幂等：终态失败有多条落点（业务失败 / 重试超限），将来还可能加重试路径；
        没有这层标记就会重复退，等于白送额度。标记落在 jobs.refunded_at。

        返回 True=本次确实退了；False=之前已退过（或任务不存在）。
        """
        with self._lock:            # ⚠️ Lock 不可重入：块内只能用 _conn 裸执行，不能再调 _exec
            row = self._conn.execute(
                'SELECT refunded_at FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if row is None or row['refunded_at']:
                return False
            self._conn.execute('UPDATE jobs SET refunded_at=? WHERE job_id=?',
                               (int(time.time()), job_id))
            self._conn.execute(
                'INSERT INTO usage(user_id, ym, count) VALUES(?,?,0) '
                'ON CONFLICT(user_id, ym) DO UPDATE SET count=MAX(count-?, 0)',
                (user_id, ym, n))
            self._conn.commit()
            return True


def threading_lock():
    import threading
    return threading.Lock()
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
import uuid
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
    image_path   TEXT,
    error        TEXT,
    created_at   INTEGER,
    completed_at INTEGER
);
CREATE TABLE IF NOT EXISTS usage (
    user_id TEXT,
    ym      TEXT,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, ym)
);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


class FluxDB:
    def __init__(self, path: str = None):
        self.path = str(path) if path else str(DB_PATH)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._lock = threading_lock()
        logger.info(f'🗄️  数据库就绪: {self.path}')

    def _migrate(self):
        """幂等迁移：给已存在的表补缺失列（CREATE TABLE IF NOT EXISTS 不会改已有表）"""
        try:
            cols = {r[1] for r in self._conn.execute('PRAGMA table_info(jobs)')}
            if 'original_prompt' not in cols:
                self._conn.execute('ALTER TABLE jobs ADD COLUMN original_prompt TEXT')
                self._conn.commit()
                logger.info('🗄️  jobs 表已加 original_prompt 列')
        except Exception as e:
            logger.warning(f'数据库迁移跳过: {e}')

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

    # ── users ──
    def get_user(self, user_id: str):
        return self._one('SELECT * FROM users WHERE user_id=?', (user_id,))

    def create_user(self, user_id: str, plan='default', is_owner=0):
        self._exec('INSERT OR IGNORE INTO users(user_id, plan, is_owner, created_at) VALUES(?,?,?,?)',
                   (user_id, plan, is_owner, int(time.time())))
        return self.get_user(user_id)

    def set_plan(self, user_id: str, plan: str):
        self._exec('UPDATE users SET plan=? WHERE user_id=?', (plan, user_id))

    def set_owner(self, user_id: str):
        self._exec('UPDATE users SET is_owner=1 WHERE user_id=?', (user_id,))

    # ── codes ──
    def code_generate(self, plan: str, n: int = 1) -> list:
        gen = []
        for _ in range(n):
            code = uuid.uuid4().hex[:12].upper()
            self._exec('INSERT INTO codes(code, plan) VALUES(?,?)', (code, plan))
            gen.append(code)
        return gen

    def code_get(self, code: str):
        return self._one('SELECT * FROM codes WHERE code=?', (code,))

    def code_activate(self, code: str, user_id: str) -> bool:
        row = self.code_get(code)
        if not row or row['used_by']:
            return False
        self._exec("UPDATE codes SET used_by=?, used_at=? WHERE code=?",
                   (user_id, int(time.time()), code))
        self.set_plan(user_id, row['plan'])
        return True

    # ── jobs ──
    def job_insert(self, job_id, user_id, prompt, priority=0, original_prompt=None):
        self._exec('INSERT INTO jobs(job_id, user_id, prompt, original_prompt, status, priority, created_at) '
                   'VALUES(?,?,?,?,?,?,?)',
                   (job_id, user_id, prompt, original_prompt, 'queued', priority, int(time.time())))

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

    # ── usage ──
    def usage_get(self, user_id: str, ym: str) -> int:
        r = self._one('SELECT count FROM usage WHERE user_id=? AND ym=?', (user_id, ym))
        return r['count'] if r else 0

    def usage_add(self, user_id: str, ym: str, n: int = 1):
        self._exec('INSERT INTO usage(user_id, ym, count) VALUES(?,?,?) '
                   'ON CONFLICT(user_id, ym) DO UPDATE SET count=count+?',
                   (user_id, ym, n, n))


def threading_lock():
    import threading
    return threading.Lock()
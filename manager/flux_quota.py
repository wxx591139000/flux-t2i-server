#!/usr/bin/env python3
"""
FLUX 对外文生图服务 — 配额服务（对标转录bot quota.py）
月度图片配额：owner 无限；否则 usage_count >= plan.monthly_images → 拒绝
"""
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger('manager.flux_quota')

BASE_DIR = Path(__file__).parent.parent
PLANS_FILE = Path(__file__).parent / 'plans.yaml'

DEFAULT_PLANS = {
    'default': {'monthly_images': 10},
    'basic': {'monthly_images': 50},
    'pro': {'monthly_images': 200},
}


def load_plans() -> dict:
    try:
        import yaml
        with open(PLANS_FILE, encoding='utf-8') as f:
            data = yaml.safe_load(f)
        plans = data.get('plans', {})
        if plans:
            return plans
    except Exception as e:
        logger.warning(f'plans.yaml 读取失败，用默认: {e}')
    return DEFAULT_PLANS


def current_ym() -> str:
    return datetime.now().strftime('%Y%m')


class QuotaService:
    def __init__(self, db):
        self.db = db
        self.plans = load_plans()

    def get_plan(self, user_id: str) -> dict:
        u = self.db.get_user(user_id)
        plan_name = u['plan'] if u else 'default'
        return self.plans.get(plan_name, self.plans.get('default', {}))

    def precheck(self, user_id: str) -> tuple:
        """检查是否可提交新任务。返回 (ok, reason)"""
        u = self.db.get_user(user_id)
        if u and u['is_owner']:
            return True, ''
        plan = self.get_plan(user_id)
        limit = plan.get('monthly_images', -1)
        if limit is None or limit < 0:
            return True, ''
        used = self.db.usage_get(user_id, current_ym())
        inflight = self.db.job_count_queued(user_id)
        if used + inflight >= limit:
            return False, f'本月配额已用 {used}/{limit} 张，可升级套餐或下月再试'
        return True, ''

    def record_enqueued(self, user_id: str):
        self.db.usage_add(user_id, current_ym(), 1)

    def usage_summary(self, user_id: str) -> str:
        u = self.db.get_user(user_id)
        if u and u['is_owner']:
            return 'owner 无限量'
        plan = self.get_plan(user_id)
        limit = plan.get('monthly_images', -1)
        used = self.db.usage_get(user_id, current_ym())
        if limit is None or limit < 0:
            return f'本月已用 {used} 张（无限）'
        return f'本月已用 {used}/{limit} 张'
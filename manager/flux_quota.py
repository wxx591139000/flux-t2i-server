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

    def effective(self, user_id: str) -> dict:
        """账户聚合口径：绑账户则套餐/owner/用量按账户；否则按 token 自身（自账户）。"""
        acct = None
        acct_id = self.db.account_id_of(user_id)
        if acct_id:
            acct = self.db.account_get(acct_id)
        if acct:
            return {'account_id': acct['account_id'], 'plan': acct['plan'],
                    'is_owner': acct['is_owner'], 'used': self.db.account_usage(acct_id, current_ym()),
                    'inflight': self.db.account_inflight(acct_id)}
        u = self.db.get_user(user_id)
        return {'account_id': '', 'plan': u['plan'] if u else 'default',
                'is_owner': bool(u and u['is_owner']),
                'used': self.db.usage_get(user_id, current_ym()),
                'inflight': self.db.job_count_queued(user_id)}

    def get_plan(self, user_id: str) -> dict:
        return self.plans.get(self.effective(user_id)['plan'], self.plans.get('default', {}))

    def precheck(self, user_id: str) -> tuple:
        """检查是否可提交新任务（账户聚合口径）。返回 (ok, reason)"""
        eff = self.effective(user_id)
        if eff['is_owner']:
            return True, ''
        limit = self.plans.get(eff['plan'], {}).get('monthly_images', -1)
        if limit is None or limit < 0:
            return True, ''
        # 只用 used 判定：usage 在「入队时」已 +1（见 flux_queue.py submit 里的 usage_add），
        # 因此 used 天然包含排队中/生成中的任务。若再叠加 inflight（DB 里
        # status in ('queued','generating') 的计数），同一张在途图会被算两次，
        # 用户实际可用额度约为套餐的一半（basic=50 实际只能出 ~25 张）。
        used = eff['used']
        if used >= limit:
            return False, f'本月配额已用 {used}/{limit} 张，可升级套餐或下月再试'
        return True, ''

    # 2026-09-16 删除 record_enqueued()。它做的是 db.usage_add(user_id, current_ym(), 1)，
    # 而实际计费已经发生在 flux_queue.submit() 里（入队即 usage_add），全仓零调用。
    # 保留它 = 留一个「看起来更分层」的诱饵：谁把它接回 submit，就会变成真的双重计费。
    # 因此计费入口唯一化为 flux_queue.submit() 的 usage_add；退还入口为
    # FluxDB.refund_job_once()（幂等，见 flux_queue._refund_quota）。

    def usage_summary(self, user_id: str) -> str:
        eff = self.effective(user_id)
        if eff['is_owner']:
            return 'owner 无限量'
        limit = self.plans.get(eff['plan'], {}).get('monthly_images', -1)
        used = eff['used']
        if limit is None or limit < 0:
            return f'本月已用 {used} 张（无限）'
        return f'本月已用 {used}/{limit} 张'
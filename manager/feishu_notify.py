#!/usr/bin/env python3
"""
FLUX 专用飞书通知模块（独立于转录bot"小白"）
用新建的独立飞书机器人发私信通知 owner。
API 调用范式复制借鉴自 server-pdf-converter（转录bot），但不触碰其任何文件。

用法:
  from manager.feishu_notify import notify_owner
  notify_owner("你的消息")
"""
import os
import json
import time
import logging
import requests
from pathlib import Path

logger = logging.getLogger('manager.feishu_notify')

API = 'https://open.feishu.cn'

# 凭证从 manager/.env 或环境变量读取
_ENV_FILE = Path(__file__).parent / '.env'


def _load_env():
    """读取 .env 到环境变量（若未设置）"""
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k, v)


def get_app_id() -> str:
    _load_env()
    return os.environ.get('FEISHU_APP_ID', '')


def get_app_secret() -> str:
    _load_env()
    return os.environ.get('FEISHU_APP_SECRET', '')


def get_owner_open_id() -> str:
    _load_env()
    return os.environ.get('FEISHU_OWNER_OPEN_ID', '')


class FeishuNotifier:
    """飞书私信通知器（独立新机器人）"""

    def __init__(self, app_id: str = None, app_secret: str = None, owner_open_id: str = None):
        self.app_id = app_id or get_app_id()
        self.app_secret = app_secret or get_app_secret()
        self.owner_open_id = owner_open_id or get_owner_open_id()
        self._token = ''
        self._token_expire = 0.0

    # ── token 获取（复制借鉴转录bot feishu_channel.get_tenant_token）──
    def get_tenant_token(self) -> str:
        if self._token and self._token_expire > time.time():
            return self._token
        resp = requests.post(
            f'{API}/open-apis/auth/v3/tenant_access_token/internal',
            json={'app_id': self.app_id, 'app_secret': self.app_secret},
            timeout=10)
        data = resp.json()
        if data.get('code') != 0:
            raise RuntimeError(f'获取飞书 token 失败: {data.get("msg")}')
        self._token = data['tenant_access_token']
        self._token_expire = time.time() + data.get('expire', 7200) - 60
        return self._token

    def _post(self, path: str, data: dict):
        headers = {'Authorization': f'Bearer {self.get_tenant_token()}'}
        headers['Content-Type'] = 'application/json; charset=utf-8'
        resp = requests.post(f'{API}{path}', headers=headers, json=data, timeout=10)
        return resp.json()

    # ── 私聊发送（复制借鉴转录bot feishu_channel.send_direct）──
    def send_direct(self, user_id: str, text: str) -> bool:
        if not get_owner_open_id():
            logger.warning('FEISHU_OWNER_OPEN_ID 未配置，无法私信通知')
            raise RuntimeError('FEISHU_OWNER_OPEN_ID 未配置')
        if len(text) > 1900:
            text = text[:1850] + '\n\n...（内容较长，已截断）'
        result = self._post(
            '/open-apis/im/v1/messages?receive_id_type=open_id',
            {'receive_id': user_id, 'msg_type': 'text',
             'content': json.dumps({'text': text}, ensure_ascii=False)})
        if result.get('code') == 0:
            return True
        logger.error(f'飞书私聊发送失败: {result}')
        return False

    def notify_owner(self, text: str) -> bool:
        """给 owner 发私信通知"""
        oid = self.owner_open_id
        if not oid:
            raise RuntimeError('FEISHU_OWNER_OPEN_ID 未配置')
        return self.send_direct(oid, text)


def notify_owner(text: str) -> bool:
    """便捷函数：发飞书私信给 owner"""
    n = FeishuNotifier()
    return n.notify_owner(text)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    ok = notify_owner('✅ FLUX 配图助手飞书通知测试成功！')
    print('发送成功' if ok else '发送失败')
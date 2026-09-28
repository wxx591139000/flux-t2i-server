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


def get_bot_open_id() -> str:
    """机器人自己的 open_id（群聊判断「@的是不是我」用）。

    ⚠️ 没配 `FEISHU_BOT_OPEN_ID` 时返回 ''，此时群聊的 @ 判定会**退化为
      "任意 @ 即响应"**（见 `feishu_bot._mentioned_bot`）。白名单群内可接受，
      但严格来说会误响应"@别人"的消息 —— 所以配了更好。
    """
    _load_env()
    return os.environ.get('FEISHU_BOT_OPEN_ID', '')


def get_allowed_chats() -> set:
    """允许服务的**群** chat_id 集合（逗号分隔）。

    决策 3（2026-09-23 用户拍板）：「只服务 owner 自己 + owner 自己的群」。
    **默认空集合 = 一个群都不服务**（连 owner 的群也要显式登记）——
    这是刻意的安全默认：宁可"配了才生效"，也不要"忘了配就对外裸奔"。
    """
    _load_env()
    raw = os.environ.get('FEISHU_ALLOWED_CHATS', '') or ''
    return {x.strip() for x in raw.replace(';', ',').split(',') if x.strip()}


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
            # ⚠️ 2026-09-28：这里原先 **raise**（一个"奇怪守卫"，见设计方案 §0.2）——
            #    它检查的是"owner 配了没"，却**对发给别人的消息也一样生效**，
            #    于是没配 owner 时连普通回复都发不出去。改成只告警不抛：
            #    真正需要在意的（收件人是否合法）由调用方与白名单负责。
            logger.warning('FEISHU_OWNER_OPEN_ID 未配置（不影响给指定 user_id 发消息）')
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

    # ── 群聊发送（2026-09-28 P2）──
    def send_to(self, chat_id: str, text: str) -> bool:
        """往**群**（或任意 chat）发文本。`receive_id_type=chat_id`。

        与 `send_direct` 的唯一差别就是 receive_id_type —— 飞书要求"发群"必须用
        chat_id 口径，给 open_id 口径传群 id 会报 `receive_id invalid`。
        """
        if not chat_id:
            logger.error('飞书群消息发送失败：chat_id 为空')
            return False
        if len(text) > 1900:
            text = text[:1850] + '\n\n...（内容较长，已截断）'
        result = self._post(
            '/open-apis/im/v1/messages?receive_id_type=chat_id',
            {'receive_id': chat_id, 'msg_type': 'text',
             'content': json.dumps({'text': text}, ensure_ascii=False)})
        if result.get('code') == 0:
            return True
        logger.error(f'飞书群消息发送失败: {result}')
        return False

    def send_image_to(self, chat_id: str, image_key: str) -> bool:
        """往群发图片消息。"""
        if not chat_id or not image_key:
            return False
        result = self._post(
            '/open-apis/im/v1/messages?receive_id_type=chat_id',
            {'receive_id': chat_id, 'msg_type': 'image',
             'content': json.dumps({'image_key': image_key})})
        if result.get('code') == 0:
            return True
        logger.error(f'飞书群图片发送失败: {result}')
        return False

    @staticmethod
    def at_text(open_id: str, name: str = '') -> str:
        """群里 @某人 的文本片段（飞书 text 消息用 `<at user_id="ou_xxx"></at>`）。

        `name` 只是给不渲染 at 的客户端看的兜底文案。
        """
        if not open_id:
            return (f'@{name} ' if name else '')
        return f'<at user_id="{open_id}">{name or ""}</at> '

    def notify_owner(self, text: str) -> bool:
        """给 owner 发私信通知"""
        oid = self.owner_open_id
        if not oid:
            raise RuntimeError('FEISHU_OWNER_OPEN_ID 未配置')
        return self.send_direct(oid, text)

    # ── 图片发送（飞书 API：先上传 /open-apis/im/v1/images → 再发图片消息）──
    def upload_image(self, image_path: str):
        """上传本地图片到飞书，返回 image_key；失败返回 None"""
        path = Path(image_path)
        if not path.exists():
            logger.error(f'图片文件不存在: {path}')
            return None
        headers = {'Authorization': f'Bearer {self.get_tenant_token()}'}
        try:
            with open(str(path), 'rb') as f:
                resp = requests.post(
                    f'{API}/open-apis/im/v1/images',
                    headers=headers,
                    files={'image': (path.name, f)},
                    data={'image_type': 'message'},
                    timeout=120)
            result = resp.json()
            if result.get('code') == 0:
                return result['data']['image_key']
            logger.error(f'飞书图片上传失败: {result}')
        except Exception as e:
            logger.error(f'飞书图片上传异常: {e}')
        return None

    def send_image(self, user_id: str, image_key: str) -> bool:
        """发送图片消息给指定用户（私聊）"""
        if not image_key:
            return False
        result = self._post(
            '/open-apis/im/v1/messages?receive_id_type=open_id',
            {'receive_id': user_id, 'msg_type': 'image',
             'content': json.dumps({'image_key': image_key})})
        if result.get('code') == 0:
            return True
        logger.error(f'飞书图片消息发送失败: {result}')
        return False

    def send_image_direct(self, user_id: str, image_path: str) -> bool:
        """便捷：上传图片 + 发送一条图片消息"""
        key = self.upload_image(image_path)
        if not key:
            return False
        return self.send_image(user_id, key)


def notify_owner(text: str) -> bool:
    """便捷函数：发飞书私信给 owner"""
    n = FeishuNotifier()
    return n.notify_owner(text)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    ok = notify_owner('✅ FLUX 配图助手飞书通知测试成功！')
    print('发送成功' if ok else '发送失败')
#!/usr/bin/env python3
"""
FLUX 飞书"图图"对话式出图机器人（借鉴转录bot feishu_channel.py 的 WS 长连接范式）
WebSocket 监听 P2P 私聊消息 → 提取提示词 → 提交 FLUX 队列 → 完成后上传图片并回传。

与转录bot"小白"的差异：
  - 小白：群聊轮询 + P2P WS；收到文件/图片做转录/OCR，回文本
  - 图图：仅 P2P WS；收到文本当提示词，出图后回图片消息

用法: 由 manager/flux_service.py 装配启动（同进程内嵌，直接调用 scheduler）
"""
import os
import sys
import json
import time
import logging
import threading
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from manager.feishu_notify import FeishuNotifier, get_app_id, get_app_secret, get_owner_open_id

logger = logging.getLogger('manager.feishu_bot')

POLL_INTERVAL = 5   # 任务完成轮询间隔（秒）
MAX_ACTIVE = 50     # 同时跟踪的在途任务上限（超出不跟踪，网页可查看）


class FeishuBot:
    """飞书对话式出图机器人：open_id 即 user_id，复用现有 FLUX 队列与配额。"""

    def __init__(self, scheduler, db, quota):
        self.n = FeishuNotifier()
        self.scheduler = scheduler
        self.db = db
        self.quota = quota
        self.owner_open_id = get_owner_open_id()
        self._active = {}   # job_id -> {open_id, prompt, notified_waiting}
        self._lock = threading.Lock()

    # ── 启动 ──
    def start(self) -> bool:
        if not self.n.app_id or not self.n.app_secret:
            logger.warning('🤖 FEISHU_APP_ID/SECRET 未配置，飞书图图机器人不启动')
            return False
        # owner（自己）通过机器人生成无限量
        if self.owner_open_id:
            self.db.user_ensure(self.owner_open_id)
            self.db.set_owner(self.owner_open_id)
        threading.Thread(target=self._ws_listen, daemon=True, name='feishu-bot-ws').start()
        threading.Thread(target=self._poll_loop, daemon=True, name='feishu-bot-poll').start()
        logger.info('🤖 飞书图图机器人已启动（WebSocket 监听 + 任务轮询）')
        return True

    # ── WebSocket 监听（借鉴转录bot feishu_channel._start_ws_listener）──
    def _ws_listen(self):
        try:
            import lark_oapi as lark
        except ImportError:
            logger.error('🤖 lark_oapi 未安装，图图机器人无法监听（pip install lark-oapi）')
            return
        handler = lark.EventDispatcherHandler.builder('', '') \
            .register_p2_im_message_receive_v1(self._on_message).build()
        client = lark.ws.Client(self.n.app_id, self.n.app_secret,
                                event_handler=handler,
                                log_level=lark.LogLevel.WARNING)
        try:
            client.start()
        except Exception as e:
            logger.error(f'🤖 飞书 WS 连接异常: {e}')

    # ── 消息处理 ──
    def _on_message(self, data):
        try:
            ev = data.event
            sender = ev.sender
            if not sender or sender.sender_type == 'app':
                return
            open_id = sender.sender_id.open_id if sender.sender_id else ''
            message = ev.message
            if not open_id or not message:
                return
            # 首版仅私聊（对齐转录bot P2P 过滤，避免群聊刷屏）
            if getattr(message, 'chat_type', '') not in ('p2p', ''):
                logger.info(f'🤖 忽略非私聊消息 chat_type={message.chat_type}')
                return
            if message.message_type != 'text':
                logger.info(f'🤖 忽略非文本消息 type={message.message_type}')
                return
            try:
                content = json.loads(message.content or '{}')
                text = (content.get('text') or '').strip()
            except Exception:
                text = ''
            if not text:
                return
            logger.info(f'📩 图图收到 {open_id}: {text[:60]}')
            self._handle_prompt(open_id, text)
        except Exception as e:
            logger.error(f'🤖 消息处理异常: {e}')

    def _handle_prompt(self, open_id: str, prompt: str):
        """提交提示词到队列，并回确认消息。"""
        # 斜杠开头 → 提示语（首版不实现命令）
        if prompt.startswith('/'):
            self.n.send_direct(open_id, '📝 直接发提示词即可出图，例如：一只橘猫坐在窗台上，阳光洒落')
            return
        # 每用户最多 1 个在途任务
        with self._lock:
            if any(info['open_id'] == open_id for info in self._active.values()):
                self.n.send_direct(open_id, '⏳ 你有一个任务还在生成中，请稍候再发～')
                return
        # 确保用户存在（飞书 open_id 即 user_id）
        self.db.user_ensure(open_id)
        result = self.scheduler.submit(open_id, prompt)
        if 'error' in result:
            self.n.send_direct(open_id, f'❌ {result["error"]}')
            return
        job_id = result['job_id']
        usage = self.quota.usage_summary(open_id)
        self.n.send_direct(open_id,
                           f'✅ 收到！正在排队生成「{prompt[:40]}」\n'
                           f'任务号: {job_id}\n💳 {usage}')
        with self._lock:
            if len(self._active) < MAX_ACTIVE:
                self._active[job_id] = {'open_id': open_id, 'prompt': prompt,
                                        'notified_waiting': False}
            else:
                logger.warning(f'🤖 在途任务超上限({MAX_ACTIVE})，{job_id} 不跟踪（网页可查看）')

    # ── 任务完成轮询 ──
    def _poll_loop(self):
        while True:
            try:
                self._poll_once()
            except Exception as e:
                logger.error(f'🤖 任务轮询异常: {e}')
            time.sleep(POLL_INTERVAL)

    def _poll_once(self):
        with self._lock:
            jobs = list(self._active.items())
        for job_id, info in jobs:
            job = self.db.job_get(job_id)
            if not job:
                with self._lock:
                    self._active.pop(job_id, None)
                continue
            st = job['status']
            open_id = info['open_id']
            if st == 'done':
                self._send_result(open_id, job)
                with self._lock:
                    self._active.pop(job_id, None)
            elif st == 'failed':
                err = (job['error'] or '未知错误')[:200]
                self.n.send_direct(open_id, f'❌ 生成失败: {err}')
                with self._lock:
                    self._active.pop(job_id, None)
            elif st == 'waiting' and not info['notified_waiting']:
                self.n.send_direct(open_id, '🔴 服务器暂不可达，任务已进入等待恢复队列，恢复后自动继续～')
                info['notified_waiting'] = True

    def _send_result(self, open_id: str, job: dict):
        """生成完成：上传图片到飞书并回传。"""
        img = job.get('image_path') or ''
        if img and Path(img).exists():
            key = self.n.upload_image(img)
            if key:
                if self.n.send_image(open_id, key):
                    self.n.send_direct(open_id, '✨ 已生成，请查收！')
                    return
        self.n.send_direct(open_id, '❌ 生成完成但图片回传失败，可到网页 flux.zhuanlu.xyz 下载')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')
    from manager.flux_db import FluxDB
    from manager.flux_quota import QuotaService
    from manager.flux_queue import FluxQueueScheduler
    db = FluxDB()
    quota = QuotaService(db)
    sched = FluxQueueScheduler(db, quota)
    bot = FeishuBot(sched, db, quota)
    bot.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass

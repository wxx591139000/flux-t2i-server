#!/usr/bin/env python3
"""
FLUX 飞书"图图"对话式出图机器人（借鉴转录bot feishu_channel.py 的 WS 长连接范式）
WebSocket 监听 P2P 私聊消息 → 提取提示词 → 提交 FLUX 队列 → 完成后上传图片并回传。

与转录bot"小白"的差异：
  - 小白：群聊轮询 + P2P WS；收到文件/图片做转录/OCR，回文本
  - 图图：仅 P2P WS；收到文本当提示词，出图后回图片消息

用法: 由 manager/flux_service.py 装配启动（同进程内嵌，直接调用 scheduler）

════════════════════════════════════════════════════════════════════════════
2026-09-23 P0 修复（设计方案 `docs/图图-飞书完整互动-设计方案.md` §6）
修补四个缺陷 —— 前三个是设计文档里点名的，**第 4 个是动手时新查出来的，
而且是其中最严重的一个（核心功能一直是坏的）**：

**P0-1 ★ 图片从来没发出去过（新发现，最严重）**
    原 `_send_result` 写的是 `job.get('image_path')`，而 `job` 是 `sqlite3.Row`
    —— **Row 没有 `.get()` 方法**，实测抛
    `AttributeError: 'sqlite3.Row' object has no attribute 'get'`。
    它被 `_poll_loop` 的宽 `except` 吞掉，只打一行「任务轮询异常」，
    于是：① 图片永远不发给用户；② `_active` 里那条记录**永远不会被 pop**，
    此后每 5 秒重试一次、每次都失败，永不收敛。
    → 本版所有行对象一律先 `dict()` 再取值。

**P0-2 事件去重（飞书会重放事件）**
    重放一次 = **重复出图 + 重复扣额度**，用户完全看不出来。
    → 加 `feishu_dedup` 表 + `db.feishu_seen()`；用 `INSERT OR IGNORE` + `rowcount`
    做**原子**判重（"先查再插"在并发下会双放行，等于假防护）。

**P0-3 在途任务只在内存 → 重启即丢**
    原 `self._active` 是内存 dict。而 9620 **每次改代码都要重启**，
    重启后这些任务即使出完图也**永远不会回传给用户**（额度已扣）。
    → 在途跟踪落 `feishu_tracks` 表；`start()` 时自动恢复未终态任务；
    `notified` 也持久化，避免重启后把「服务器暂不可达」之类的通知重发一遍。

**P0-4 超出在途上限时静默不跟踪**
    原逻辑：`submit()` 之后发现超过 `MAX_ACTIVE` 就**只是不跟踪** ——
    任务照跑、额度照扣，而用户**永远收不到图也不知道为什么**。
    → 改成：**提交之前**判断上限，超了就不提交并**明确回执**（不浪费额度）；
    上限本身也从 50 提到 200（跟踪已落库，内存不再是约束）。
════════════════════════════════════════════════════════════════════════════
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

POLL_INTERVAL = 5          # 任务完成轮询间隔（秒）
MAX_INFLIGHT_GLOBAL = 200  # 全局在途上限（超出 = 拒绝提交 + 明确回执，绝不静默丢弃）
MAX_INFLIGHT_PER_USER = 1  # 每用户在途上限
TRACK_BATCH = 50           # 单轮最多处理多少条在途（防止积压把轮询线程拖住）
DEDUP_KEEP_DAYS = 7        # 去重记录保留天数


class FeishuBot:
    """飞书对话式出图机器人：open_id 即 user_id，复用现有 FLUX 队列与配额。"""

    def __init__(self, scheduler, db, quota, notifier=None):
        """`notifier` 可注入 —— 测试用它替身，避免真发飞书消息（离线可验）。"""
        self.n = notifier if notifier is not None else FeishuNotifier()
        self.scheduler = scheduler
        self.db = db
        self.quota = quota
        self.owner_open_id = get_owner_open_id()
        # ⚠️ 这里**不再有内存 _active**：在途状态唯一事实来源是 `feishu_tracks` 表。
        #    内存里只留一个"正在提交中"的短锁，用来堵住
        #    「查在途 → 提交」之间的竞态窗口（两条消息几乎同时到达会双双通过检查）。
        self._submitting = set()
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

        # ★ P0-3：启动即恢复 —— 上次进程死掉时留下的未终态任务，这次继续盯到出图
        try:
            pending = self.db.feishu_tracks_pending(limit=1000)
            if pending:
                logger.info(f'🤖 恢复 {len(pending)} 个在途任务（上次进程遗留），继续回传结果')
        except Exception as e:
            logger.error(f'🤖 在途任务恢复失败: {e}')

        # 顺手清理（去重记录 7 天、已终态跟踪 30 天）—— 避免表只增不减
        try:
            n1 = self.db.feishu_dedup_gc(DEDUP_KEEP_DAYS * 86400)
            n2 = self.db.feishu_track_gc(30 * 86400)
            if n1 or n2:
                logger.info(f'🤖 清理过期记录：去重 {n1} 条、已终态跟踪 {n2} 条')
        except Exception as e:
            logger.warning(f'🤖 过期记录清理跳过: {e}')

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

    # ── 事件键（去重用）──
    @staticmethod
    def event_key(data, message) -> str:
        """给一条飞书事件算去重键。

        优先 `header.event_id`（飞书官方的**事件**唯一 id，重放时不变），
        退回 `message.message_id`（同一消息 id 也够用）。
        两者都拿不到 → 返回空串，调用方会**明确记日志**而不是假装去重过了。

        ★ 文件场景要另加 `filekey:{file_key}`（借鉴小白 PITFALLS：只按 event_id
          去重会漏掉"同一文件被两条事件引用"的重放），P3 接图片时补上。
        """
        try:
            hdr = getattr(data, 'header', None)
            eid = getattr(hdr, 'event_id', '') if hdr is not None else ''
            if eid:
                return f'evt:{eid}'
            mid = getattr(message, 'message_id', '') or ''
            if mid:
                return f'msg:{mid}'
        except Exception:
            pass
        return ''

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

            # ★ P0-2：事件去重。必须放在所有副作用之前 —— 一旦提交出去就来不及了。
            key = self.event_key(data, message)
            if key:
                if self.db.feishu_seen(key):
                    logger.info(f'🤖 重复事件，已忽略（{key}）')
                    return
            else:
                logger.warning('🤖 事件既无 event_id 也无 message_id，本次**未去重**，请检查上游')

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

        # ★ P0-4：**先**判断上限再提交 —— 原来的顺序（先提交、超限就不跟踪）
        #    会让用户白扣一次额度却永远收不到图。
        with self._lock:
            if open_id in self._submitting:
                self.n.send_direct(open_id, '⏳ 你的上一条还在处理，请稍等 1 秒再发～')
                return
            self._submitting.add(open_id)
        try:
            self._submit_locked(open_id, prompt)
        finally:
            with self._lock:
                self._submitting.discard(open_id)

    def _submit_locked(self, open_id: str, prompt: str):
        """真正提交（已被 `_submitting` 短锁保护，同一用户不会并发进来）。"""
        try:
            # 每用户在途上限：**读库口径**，所以 9620 重启后依然准确
            per_user = self.db.feishu_tracks_inflight(sender_id=open_id)
            if per_user >= MAX_INFLIGHT_PER_USER:
                self.n.send_direct(open_id, '⏳ 你有一个任务还在生成中，请稍候再发～')
                return
            inflight = self.db.feishu_tracks_inflight()
            if inflight >= MAX_INFLIGHT_GLOBAL:
                # 明确回执，且**没有提交**（不浪费额度）
                self.n.send_direct(
                    open_id,
                    f'⚠️ 当前在途任务较多（{inflight} 个），为免你白等，这次**没有提交**。\n'
                    f'稍后再发一次即可。')
                return

            # 确保用户存在（飞书 open_id 即 user_id）
            self.db.user_ensure(open_id)
            result = self.scheduler.submit(open_id, prompt)
            if 'error' in result:
                self.n.send_direct(open_id, f'❌ {result["error"]}')
                return
            job_id = result['job_id']

            # ★ P0-3：先落库再回执 —— 万一进程在回执后立刻挂掉，重启也能靠这张表把图送到
            try:
                self.db.feishu_track_add(job_id, open_id, prompt=prompt, phase='queued')
            except Exception as e:
                # 落库失败不能吞：这意味着"重启就丢结果"的风险回来了
                logger.error(f'🤖 在途任务落库失败（重启将无法回传）: {e}')

            usage = self.quota.usage_summary(open_id)
            self.n.send_direct(open_id,
                               f'✅ 收到！正在排队生成「{prompt[:40]}」\n'
                               f'任务号: {job_id}\n💳 {usage}')
        except Exception as e:
            logger.error(f'🤖 提交异常: {e}')
            try:
                self.n.send_direct(open_id, f'❌ 提交失败：{str(e)[:120]}')
            except Exception:
                pass

    # ── 任务完成轮询 ──
    def _poll_loop(self):
        while True:
            try:
                self._poll_once()
            except Exception as e:
                # 这一层宽 except 是必要的（轮询线程绝不能死），
                # 但要打全异常类型，否则又会像 P0-1 那样"只报异常不说原因"藏很久
                logger.error(f'🤖 任务轮询异常: {type(e).__name__}: {e}', exc_info=True)
            time.sleep(POLL_INTERVAL)

    def _poll_once(self):
        # ★ 从 DB 读在途（不再读内存 dict）—— 这就是重启恢复的实现方式：
        #   新进程起来后同样能查到上次遗留的任务，继续把结果送到用户手上。
        tracks = self.db.feishu_tracks_pending(limit=TRACK_BATCH)
        for t in tracks:
            job_id = t['job_id']
            sender = t['sender_id']
            notified = self._notified(t)
            # ⚠️ 必须 dict()：sqlite3.Row 没有 .get()，直接用会 AttributeError（P0-1 就是这么坏的）
            job = self.db.job_get(job_id)
            if not job:
                # 任务行没了（被清理）→ 记录终态，别再每轮空转
                self.db.feishu_track_set(job_id, phase='failed')
                continue
            job = dict(job)
            st = job.get('status') or ''

            if st == 'done':
                self._send_result(sender, job)
                self.db.feishu_track_set(job_id, phase='done')
            elif st == 'failed':
                err = (job.get('error') or '未知错误')[:200]
                if not notified.get('failed'):
                    self.n.send_direct(sender, f'❌ 生成失败: {err}')
                    notified['failed'] = True
                self.db.feishu_track_set(job_id, phase='failed',
                                         notified=json.dumps(notified, ensure_ascii=False))
            elif st == 'waiting':
                if not notified.get('waiting'):
                    self.n.send_direct(sender, '🔴 服务器暂不可达，任务已进入等待恢复队列，恢复后自动继续～')
                    notified['waiting'] = True
                    self.db.feishu_track_set(job_id, notified=json.dumps(notified, ensure_ascii=False))
            elif st in ('queued', 'generating'):
                # 推进 phase（便于运维看进度），但不打扰用户 —— 进度播报是 P1/P4 的事
                if t['phase'] != st:
                    self.db.feishu_track_set(job_id, phase=st)

    @staticmethod
    def _notified(track) -> dict:
        try:
            return json.loads(track['notified'] or '{}')
        except Exception:
            return {}

    def _send_result(self, open_id: str, job: dict):
        """生成完成：上传图片到飞书并回传。

        ⚠️ `job` 必须是 **dict**（P0-1 的根因就是这里传了 sqlite3.Row 又用 .get()）。
        """
        img = job.get('image_path') or ''
        if img and Path(img).exists():
            key = self.n.upload_image(img)
            if key and self.n.send_image(open_id, key):
                self.n.send_direct(open_id, '✨ 已生成，请查收！')
                return
        else:
            logger.error(f'🤖 任务 {job.get("job_id")} 状态 done 但图片不存在: {img!r}')
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

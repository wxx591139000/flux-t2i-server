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
import re
import sys
import json
import time
import queue
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
MAX_QUEUE = 200            # 待处理消息队列上限（满了明确回绝，不无声丢弃）
LIST_LIMIT = 20            # 「我的任务」最多列几条
SEQ_MAX_LEN = 3            # 序号最多几位（超过这个长度的纯数字按任务短码解释）

# ── 命令面（2026-09-24 P1，对齐小白的中文裸命令用法）──────────────────────
# ★ 这里是命令的**唯一事实源**。新增命令必须登记进 `_match_command`，
#   否则它会被当提示词提交 → **真的扣额度并出一张毫不相干的图**。
#   （小白的同款坑：命令前缀没登记会被状态机吞掉，见其 commands.py 注释。）
#
# 状态 → 图标/中文名（列表与回执共用）
STATUS_LABELS = {
    'queued':     ('⏳', '排队中'),
    'generating': ('🎨', '生成中'),
    'waiting':    ('🔴', '等机器恢复'),
    'done':       ('✅', '已完成'),
    'failed':     ('❌', '已失败'),
}


HELP_TEXT = (
    '📸 图图 · 生图助手\n'
    '\n'
    '🖼 出图\n'
    '• 直接发一句话即可，例如：一只橘猫坐在窗台上，阳光洒落\n'
    '• 出好后我把图片直接发给你（中文提示词会先自动翻译成英文）\n'
    '\n'
    '📋 任务\n'
    '• 发「我的任务」或裸发「取消」→ 列出你在途的任务（带序号）\n'
    '• 发「取消 01」→ 取消列表里第 1 个\n'
    '• 发「取消 3ada124e」→ 按任务号取消（任务号见列表括号内）\n'
    '\n'
    '⚠️ 说明\n'
    '• 取消不退还额度（与网页端 flux.zhuanlu.xyz 一致）\n'
    '• 「生成中」的任务撤不回来，但我会让后台跑完后丢弃产物\n'
    '• 「等机器恢复」的任务也能取消，机器上线后不会再跑它'
)


def _strip_noise(text: str) -> str:
    """去掉飞书 @提及 与斜杠前缀，便于统一匹配命令。"""
    t = re.sub(r'@_user_\d+', '', text or '')
    t = t.strip()
    if t.startswith('/'):
        t = t[1:].strip()
    return t


def _match_command(text: str):
    """把一条文本解析成命令，返回 `(name, arg)`；**不是命令则返回 None**。

    `name` ∈ {'help', 'list', 'cancel'}

    ⚠️ 判据必须**严格**，因为图图的主用法是"发一句话出图"：
      误判成命令 = 吞掉用户的提示词；漏判 = 把命令当提示词提交（扣额度出废图）。
        · 用户提示词「取消一切杂乱背景，画面干净」以"取消"二字开头 —— 这是**真实**
          的误判风险。所以「取消」后面**必须**是序号或任务短码才算命令，
          否则回落为提示词。
        · 与小白一致：**裸「取消」= 看列表**（不当提示词），这样用户忘带序号时
          不会白扣一次额度。
        · 「取消任务 <非序号>」按小白惯例仍走取消分支（由它给出人话用法提示），
          因为"取消任务"这三个字几乎不可能是提示词的开头。
    """
    t = _strip_noise(text)
    if not t:
        return None

    if t in ('帮助', 'help'):
        return ('help', '')

    # 裸「取消」/「我的任务」→ 列表（供用户拿到序号）
    if t in ('我的任务', '我的排队', '我的生图', '取消'):
        return ('list', '')

    # 「取消任务 01」/「取消 01」/「取消01」
    for prefix in ('取消任务', '取消'):
        if t.startswith(prefix):
            rest = t[len(prefix):].strip()
            if not rest:
                # 「取消任务」不带参数 → 当列表处理（比报用法更顺手）
                return ('list', '')
            if rest.isdigit() and len(rest) <= SEQ_MAX_LEN:
                return ('cancel', rest)
            # 任务短码（job_id 前 6~16 位十六进制）
            if re.fullmatch(r'[0-9a-fA-F]{6,16}', rest):
                return ('cancel', rest)
            # ★ 既非序号也非短码 → **不是命令**，回落为提示词
            #   （这样「取消一切杂乱背景」仍能正常出图）
            return None
    return None


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
        # ★ 消息队列 + 工作线程（2026-09-24 实测事故后加）：
        #   lark 的 WS 客户端是在 **asyncio 事件循环里同步调用** `_on_message` 的。
        #   如果在那里直接 `scheduler.submit()`（中文翻译最坏要重试 3 次、耗时几十秒），
        #   事件循环就被占住 → **心跳 ping/pong 发不出去** → 飞书按
        #   `3003 (registered) ping timeout` **把连接踢掉**，并且因为没及时 ACK
        #   **把同一条事件重投一遍**（日志里「重复事件，已忽略」就是它的脚印）。
        #   实测：一条"你好"卡了 68 秒，连接被踢。
        #   → 所以 WS 回调里只做**毫秒级**的活（去重 + 过滤 + 取文本），
        #     真正耗时的提交丢进这个队列，由工作线程慢慢做。
        self._q = queue.Queue(maxsize=MAX_QUEUE)
        self._workers = 2

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
        # ★ 工作线程：把"提交"从 WS 事件循环里挪出来（否则翻译一慢就掉线，见 __init__ 注释）
        for i in range(self._workers):
            threading.Thread(target=self._work_loop, daemon=True,
                             name=f'feishu-bot-work{i}').start()
        logger.info(f'🤖 飞书图图机器人已启动（WebSocket 监听 + 任务轮询 + {self._workers} 个工作线程）')
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
            # ★ 只入队、不在这里干活 —— 见 __init__ 里"为什么必须有工作线程"的实测事故
            self._enqueue(open_id, text)
        except Exception as e:
            logger.error(f'🤖 消息处理异常: {type(e).__name__}: {e}', exc_info=True)

    def _enqueue(self, open_id: str, text: str):
        """把耗时工作交给工作线程。队列满时**明确回绝**，绝不无声丢弃。"""
        try:
            self._q.put_nowait((open_id, text))
        except queue.Full:
            logger.warning('🤖 待处理队列已满，拒绝并回执')
            try:
                self.n.send_direct(open_id, '⚠️ 消息处理队列已满，请稍后再发一次～')
            except Exception:
                pass

    def _work_loop(self):
        while True:
            try:
                open_id, text = self._q.get()
            except Exception:
                time.sleep(0.5)
                continue
            try:
                self._handle_prompt(open_id, text)
            except Exception as e:
                logger.error(f'🤖 工作线程异常: {type(e).__name__}: {e}', exc_info=True)
            finally:
                try:
                    self._q.task_done()
                except Exception:
                    pass

    def _handle_prompt(self, open_id: str, prompt: str):
        """处理一条用户消息：**先判命令，再当提示词**。"""
        # ★★ 命令识别必须在最前面（2026-09-24 P1）：
        #    图图的主用法是"发一句话出图"，如果把「取消 01」当提示词提交，
        #    就会**真的扣一次额度并生成一张毫不相干的图** —— 用户想撤回，
        #    结果反而多花一次。这个顺序是硬要求，不是风格问题。
        cmd = _match_command(prompt)
        if cmd:
            name, arg = cmd
            try:
                if name == 'help':
                    self._cmd_help(open_id)
                elif name == 'list':
                    self._cmd_list(open_id)
                elif name == 'cancel':
                    self._cmd_cancel(open_id, arg)
                else:                      # 防御：登记了名字却忘了分支
                    logger.error(f'🤖 命令 {name!r} 已解析但没有处理分支')
                    self.n.send_direct(open_id, '内部错误：命令未实现，已记录')
            except Exception as e:
                logger.error(f'🤖 命令 {name} 执行异常: {type(e).__name__}: {e}', exc_info=True)
                try:
                    self.n.send_direct(open_id, f'❌ 命令执行失败：{str(e)[:120]}')
                except Exception:
                    pass
            return

        # 走到这里才说明"不是命令"。未知的斜杠写法给一次指引（别静默当提示词）
        if prompt.startswith('/'):
            self.n.send_direct(open_id,
                               '📝 直接发提示词即可出图，例如：一只橘猫坐在窗台上，阳光洒落\n'
                               '（发「帮助」看全部命令）')
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

    # ── 命令实现（2026-09-24 P1）──────────────────────────────────────
    def _cmd_help(self, open_id: str):
        self.n.send_direct(open_id, HELP_TEXT)

    @staticmethod
    def _fmt_job_line(idx: int, job: dict) -> str:
        """列表一行：`01 ⏳ 一只橘猫坐在窗台上（排队中 · 3ada124e）`"""
        st = job.get('status') or ''
        icon, label = STATUS_LABELS.get(st, ('❔', st or '未知'))
        prompt = re.sub(r'\s+', ' ', (job.get('prompt') or '')).strip()[:24] or '(无提示词)'
        short = (job.get('job_id') or '')[:8]
        return f'{idx:02d} {icon} {prompt}（{label} · {short}）'

    def _cmd_list(self, open_id: str):
        """列出我在途的任务（供「取消 N」定位）。**只列自己的**（SQL 层钉死 user_id）。"""
        rows = self.db.jobs_inflight_by_user(open_id, limit=LIST_LIMIT)
        if not rows:
            self.n.send_direct(open_id,
                               '📭 你当前没有排队 / 生成中的任务。\n\n'
                               '直接发一句话就能出图，例如：一只橘猫坐在窗台上，阳光洒落')
            return
        jobs = [dict(r) for r in rows]
        lines = [self._fmt_job_line(i, j) for i, j in enumerate(jobs, 1)]
        first_short = (jobs[0].get('job_id') or '')[:8]
        self.n.send_direct(
            open_id,
            '📋 我的任务（在途）：\n' + '\n'.join(lines) +
            f'\n\n✖️ 取消：发「取消 01」取消第 1 个，或「取消 {first_short}」按任务号取消。'
            '\n⚠️ 取消不退还额度（与网页端一致）。')

    def _cmd_cancel(self, open_id: str, arg: str):
        """取消自己的在途任务。`arg` 为序号或 job_id 短码。

        **权限**：`db.job_mark_deleted()` 的 SQL 钉死 `user_id=?`，
        所以"越权取消别人的任务"在**数据层就不可能发生** —— 这里不需要再鉴权，
        上层也不依赖"记得校验"（这是刻意设计，见该方法的 docstring）。
        """
        jobs = [dict(r) for r in self.db.jobs_inflight_by_user(open_id, limit=50)]
        if not jobs:
            self.n.send_direct(open_id, '📭 你当前没有可取消的任务。')
            return

        # ① 定位目标（序号优先 —— 序号是上一条「我的任务」的展示顺序，新→旧）
        target = None
        if arg.isdigit():
            idx = int(arg)
            if idx < 1 or idx > len(jobs):
                self.n.send_direct(open_id,
                                   f'⚠️ 序号 {idx} 超出范围：你当前有 {len(jobs)} 个在途任务，'
                                   f'发「我的任务」查看最新列表')
                return
            target = jobs[idx - 1]
        else:
            hits = [j for j in jobs if (j.get('job_id') or '').lower().startswith(arg.lower())]
            if not hits:
                # 措辞要覆盖「刚取消过又点一次」这个**很常见**的情形：
                # 列表已排除墓碑，所以重复取消必然走这条分支 —— 若只说"没找到"，
                # 用户会以为任务号打错了（其实他刚成功取消过）。
                self.n.send_direct(open_id,
                                   f'⚠️ 在途任务里没有以 {arg} 开头的（可能已取消或已完成）。\n'
                                   f'发「我的任务」看当前在途列表')
                return
            if len(hits) > 1:
                self.n.send_direct(open_id,
                                   f'⚠️ 任务号 {arg} 匹配到 {len(hits)} 个，请多给几位，'
                                   f'或发「我的任务」用序号取消')
                return
            target = hits[0]

        job_id = target.get('job_id') or ''
        status = target.get('status') or ''
        _, label = STATUS_LABELS.get(status, ('❔', status))

        # ② 打墓碑（幂等 / 只许删自己的 / 不退额度 —— 三条都由 db 层保证）
        try:
            marked = self.db.job_mark_deleted(job_id, open_id)
        except Exception as e:
            logger.error(f'🤖 取消任务打墓碑失败 {job_id}: {type(e).__name__}: {e}', exc_info=True)
            self.n.send_direct(open_id, f'❌ 取消失败：{str(e)[:120]}')
            return
        if not marked:
            # 幂等：已删过（可能是网页端删的）。不是错误，如实告知即可。
            self.n.send_direct(open_id, f'ℹ️ 任务 {job_id[:8]} 此前已取消，无需重复操作。')
            return

        # ③ 从调度器内存剔除（队列 / 等待池 / 去重键）。
        #    剔除失败**不回滚**墓碑：worker 与恢复入口都有兜底检查会收尾
        #    （与 `/api/delete` 同一策略 —— 宁可留个后台收尾，也不要半死状态）。
        try:
            self.scheduler.drop_job(job_id, target)
        except Exception as e:
            logger.warning(f'🤖 取消 {job_id} 时从调度器剔除失败（worker 会兜住）: {e}')

        # ④ 终止飞书侧跟踪，别让轮询线程继续盯一个已取消的任务
        try:
            self.db.feishu_track_set(job_id, phase='cancelled')
        except Exception as e:
            logger.warning(f'🤖 取消 {job_id} 后标记跟踪终态失败: {e}')

        logger.info(f'🗑️  图图用户 {open_id} 取消任务 {job_id}（原状态 {status}）')

        # ⑤ 按原状态给不同措辞 —— 尤其是 generating：用户需要知道"撤不回来，但产物会丢"
        if status == 'generating':
            tail = '⚠️ 它已在生成中，无法中途打断；后台会在跑完后丢弃产物，你不需要做什么。'
        elif status == 'waiting':
            tail = '✅ 已从「等机器恢复」池中移除，机器上线后不会再跑它。'
        else:
            tail = '✅ 已从队列移除，不会消耗 GPU。'
        self.n.send_direct(open_id,
                           f'✖️ 已取消「{label}」的任务 {job_id[:8]}\n{tail}\n'
                           f'⚠️ 额度不退还（与网页端一致）。')

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

            # ★ 立即回执（2026-09-24 实测教训）：提交里含**中文翻译**，最坏要重试 3 次、
            #   实测耗时 68 秒。这期间用户屏幕上什么都没有，只会以为"机器人坏了"。
            #   先给一条即时反馈，让他知道消息收到了、正在解析。
            self.n.send_direct(open_id, '⏳ 收到！正在解析提示词…（中文会先翻译成英文，通常十几秒）')

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

            # ★ 墓碑优先（2026-09-24）：任务已被取消/删除 → 立即终止跟踪。
            #   必须在这里判，因为**取消可能来自网页端**（`/api/delete`）——
            #   那种情况下飞书这张跟踪表完全不知情，不判就会每 5 秒空转、永不收敛
            #   （正是 P0-1 那种"永不收敛"的病：一条坏记录钉住整个轮询）。
            if job.get('deleted_at') is not None:
                if t['phase'] != 'cancelled':
                    self.db.feishu_track_set(job_id, phase='cancelled')
                    logger.info(f'🗑️  任务 {job_id} 已被取消/删除，停止跟踪')
                continue

            if st == 'done':
                self._send_result(sender, job)
                self.db.feishu_track_set(job_id, phase='done')
            elif st == 'failed':
                err = (job.get('error') or '未知错误')[:200]
                if not notified.get('failed'):
                    if self.n.send_direct(sender, f'❌ 生成失败: {err}'):
                        notified['failed'] = True
                    else:
                        logger.warning(f'🤖 {job_id} 的失败通知发送失败（无法重试：任务已终态）')
                # phase 仍要进终态 —— 否则每 5 秒重发一次，且真的失败原因早就成为噪音
                self.db.feishu_track_set(job_id, phase='failed',
                                         notified=json.dumps(notified, ensure_ascii=False))
            elif st == 'waiting':
                if not notified.get('waiting'):
                    # ⚠️ 只有**真的发出去**才置位（2026-09-24）：原写法无条件置位，
                    #    一旦这次发送失败（网络/限流），用户就**永远收不到**这条说明，
                    #    而跟踪表却认为"已经通知过了" —— 又一个"看起来做了其实没做"。
                    if self.n.send_direct(sender, '🔴 服务器暂不可达，任务已进入等待恢复队列，恢复后自动继续～'):
                        notified['waiting'] = True
                        self.db.feishu_track_set(job_id, notified=json.dumps(notified, ensure_ascii=False))
                    else:
                        logger.warning(f'🤖 {job_id} 的 waiting 通知发送失败，下一轮重试')
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

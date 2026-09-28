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
2026-09-28 v2.11.0（P1 剩余命令面 + P2 群聊）

**新增命令面**：`比例/尺寸`（/size）、`张数`（/n）、`模型`（/model）、`绑定`（/bind）、
`额度`（/quota）。**默认参数按会话持久化**（`feishu_prefs`），下一张图直接生效。

**★ 待选状态机**：`比例`（不带参数）会给出一份编号选项并登记一个"待选"，
用户回 `2` 或回 `16:9` 即被消费。key 是**三元组** `(channel, chat_id, sender_id)`
（`manager/figu_state.py` + `feishu_pending` 表）—— 不用小白的二元组，
跨会话误消费在数据层就不可能发生（设计方案 §2.2 决策 2）。

**★ P2 群聊**：放开 `chat_type` 过滤 + **必须 @图图** + **白名单**
（owner 私聊 + `FEISHU_ALLOWED_CHATS` 里的群；决策 3）。

**顺手修掉一个真缺陷（第 18 号陷阱同款）**：`feishu_tracks_inflight` 的终态集合
漏了 `cancelled` → 取消过的任务仍被算作"在途"，而 `MAX_INFLIGHT_PER_USER = 1`
→ **用户取消一次后就再也提交不了**（永远收到「你有一个任务还在生成中」），
且 GC 也不清 cancelled → 永不自愈。已收敛成单一常量 `FEISHU_TERMINAL_PHASES`。
════════════════════════════════════════════════════════════════════════════
"""
import os
import re
import sys
import json
import time
import queue
import random
import logging
import threading
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from manager import figu_state as fst
from manager.feishu_notify import (FeishuNotifier, get_app_id, get_app_secret,
                                   get_owner_open_id, get_bot_open_id, get_allowed_chats)

logger = logging.getLogger('manager.feishu_bot')

POLL_INTERVAL = 5          # 任务完成轮询间隔（秒）
MAX_INFLIGHT_GLOBAL = 200  # 全局在途上限（超出 = 拒绝提交 + 明确回执，绝不静默丢弃）
MAX_INFLIGHT_PER_USER = 1  # 每用户在途上限
TRACK_BATCH = 50           # 单轮最多处理多少条在途（防止积压把轮询线程拖住）
DEDUP_KEEP_DAYS = 7        # 去重记录保留天数
MAX_QUEUE = 200            # 待处理消息队列上限（满了明确回绝，不无声丢弃）
LIST_LIMIT = 20            # 「我的任务」最多列几条
SEQ_MAX_LEN = 3            # 序号最多几位（超过这个长度的纯数字按任务短码解释）
PENDING_TTL = fst.PENDING_TTL   # 待选状态存活秒数（300s，对齐小白）

# ── 提交前确认（2026-09-28 用户需求）─────────────────────────────────────
# 非命令文本**不再直接出图**，先回一条确认；用户回「是」才建任务（详见
# `figu_state.parse_confirm` 段注释）。默认**开** —— 这是用户明确要求的默认行为。
# 设 `FIGU_CONFIRM_SUBMIT=0` 可回退到旧行为（发一句就出图），用于应急/灰度。
# ⚠️ 显式设成空字符串（`FIGU_CONFIRM_SUBMIT=`）按「未设」处理 = 开，不是关 ——
#    「空值当关」是本仓库踩过的坑（配置文件里一行空值就把功能静默关掉）。
CONFIRM_SUBMIT = (os.environ.get('FIGU_CONFIRM_SUBMIT', '1').strip().lower()
                  not in ('0', 'false', 'no', 'off'))

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
    '• 发完我会先把这句回给你确认（带本次参数），你回「是」才开始出图\n'
    '• 回「否」就放弃；5 分钟内没回自动作废（不会白扣额度）\n'
    '• 出好后我把图片直接发给你（中文提示词会先自动翻译成英文）\n'
    '\n'
    '📐 出图参数（设置后一直生效，发命令不带参数会列出选项）\n'
    '• 发「比例」→ 选 1:1 / 16:9 / 9:16 / 4:3 / 3:4（也可直接发「比例 16:9」）\n'
    '• 发「张数」→ 一次出 1~4 张（也可直接发「张数 2」）\n'
    '• 发「模型」→ 选 klein / dev / qwen（也可直接发「模型 klein」）\n'
    '• 发「参数」→ 看当前设置\n'
    '\n'
    '📋 任务\n'
    '• 发「我的任务」或裸发「取消」→ 列出你在途的任务（带序号）\n'
    '• 发「取消 01」→ 取消列表里第 1 个\n'
    '• 发「取消 3ada124e」→ 按任务号取消（任务号见列表括号内）\n'
    '\n'
    '🔑 账户\n'
    '• 发「额度」→ 看我的套餐与本月用量\n'
    '• 发「绑定 XXXXXXXX」→ 绑定激活码，和网页端共享同一份额度\n'
    '\n'
    '👥 群里怎么用\n'
    '• 群里要 @图图 才会响应；结果只 @ 发起人，不打扰全群\n'
    '\n'
    '⚠️ 说明\n'
    '• 取消不退还额度（与网页端 flux.zhuanlu.xyz 一致）\n'
    '• 「生成中」的任务撤不回来，但我会让后台跑完后丢弃产物\n'
    '• 「等机器恢复」的任务也能取消，机器上线后不会再跑它'
)


def _at_prefix(open_id: str) -> str:
    """群里 @某人 的文本片段（飞书 text 消息用 `<at user_id="ou_xxx"></at>`）。"""
    return f'<at user_id="{open_id}"></at> ' if open_id else ''


def _params_text(prefs: dict) -> str:
    """把会话默认参数渲染成一行人类可读的摘要（命令回执与 /help 共用）。"""
    size = prefs.get('size') or '默认(竖版 768×1024)'
    n = prefs.get('n') or 1
    model = prefs.get('model') or '默认(用机器当前已加载的模型)'
    return (f'📐 当前参数\n'
            f'• 比例：{size}\n'
            f'• 张数：{n}\n'
            f'• 模型：{fst.model_label(model)}')


def _strip_noise(text: str) -> str:
    """去掉飞书 @提及 与斜杠前缀，便于统一匹配命令。"""
    t = re.sub(r'@_user_\d+', '', text or '')
    t = t.strip()
    if t.startswith('/'):
        t = t[1:].strip()
    return t


def _split_command(text: str):
    """拆成 `(是否斜杠写法, 去掉 @提及与斜杠后的正文)`。

    为什么要单独把"斜杠写法"记下来：拉丁命令名（`size` / `n` / `model` / `bind`）
    不带斜杠时与正常英文提示词**共形**（"size of the room, minimal" 以 `size` 开头）。
    所以拉丁命令名只有在**带了斜杠**时才吃参数，中文命令名不受此限。
    """
    raw = re.sub(r'@_user_\d+', '', text or '').strip()
    had_slash = raw.startswith('/')
    body = raw[1:].strip() if had_slash else raw
    return had_slash, body


# 中文前缀命令表 —— (前缀, 命令名)。**顺序敏感**：长前缀必须排在短前缀前面
# （`取消任务` 必须先于 `取消`，否则 rest 会多出「任务」两个字）。
_PREFIX_COMMANDS = (
    ('取消任务', 'cancel'), ('取消', 'cancel'),
    ('比例', 'size'), ('尺寸', 'size'), ('画幅', 'size'),
    ('张数', 'count'), ('数量', 'count'),
    ('模型', 'model'),
    ('绑定', 'bind'),
)

# 拉丁带参命令 —— **只在斜杠写法下生效**（见 _split_command 的说明）
_SLASH_ARG_COMMANDS = (
    ('size', 'size'), ('count', 'count'), ('n', 'count'),
    ('model', 'model'), ('bind', 'bind'),
)


def _parse_cmd_arg(name: str, rest: str):
    """把命令参数解析成 `(name, value)`；**解析不了返回 None（= 这条不是命令）**。

    ⚠️ 这是"不吞提示词"的唯一关口：每个分支都必须**整体匹配**才算数。
    例如 `比例 16:9 的画面` 的 rest 是 `16:9 的画面`，整体不是比例 → None →
    回落为提示词（若只取第一个 token，就会把「的画面」吃掉，用户拿到的图不是他要的）。
    """
    if name == 'cancel':
        if rest.isdigit() and len(rest) <= SEQ_MAX_LEN:
            return ('cancel', rest)
        # 任务短码（job_id 前 6~16 位十六进制）
        if re.fullmatch(r'[0-9a-fA-F]{6,16}', rest):
            return ('cancel', rest)
        return None
    if name == 'size':
        v = fst.parse_size(rest)
        return ('size', v) if v else None
    if name == 'count':
        v = fst.parse_count(rest)
        return ('count', v) if v else None
    if name == 'model':
        v = fst.parse_model_arg(rest)
        return ('model', v) if v else None
    if name == 'bind':
        v = fst.parse_code(rest)
        return ('bind', v) if v else None
    return None


def _match_command(text: str):
    """把一条文本解析成命令，返回 `(name, arg)`；**不是命令则返回 None**。

    `name` ∈ {'help', 'list', 'cancel', 'params', 'size', 'count', 'model',
              'bind', 'quota'}

    ⚠️ 判据必须**严格**，因为图图的主用法是"发一句话出图"：
      误判成命令 = 吞掉用户的提示词；漏判 = 把命令当提示词提交（扣额度出废图）。
        · 用户提示词「取消一切杂乱背景，画面干净」以"取消"二字开头 —— 这是**真实**
          的误判风险。所以「取消」后面**必须**是序号或任务短码才算命令，
          否则回落为提示词。
        · 与小白一致：**裸「取消」= 看列表**（不当提示词），这样用户忘带序号时
          不会白扣一次额度。
        · 「取消任务 <非序号>」按小白惯例仍走取消分支（由它给出人话用法提示），
          因为"取消任务"这三个字几乎不可能是提示词的开头。
        · 2026-09-28 新增的 `/size /n /model /bind` 沿用同一纪律：
          **参数解析不成功 → 整条回落为提示词**（"比例协调的画面"仍能正常出图）。
    """
    had_slash, t = _split_command(text)
    if not t:
        return None

    # ── ① 精确整串（不带参数）──
    if t in ('帮助', 'help', '说明', '使用说明'):
        return ('help', '')
    # 裸「取消」/「我的任务」→ 列表（供用户拿到序号）
    if t in ('我的任务', '我的排队', '我的生图', '取消'):
        return ('list', '')
    if t in ('额度', '配额', '用量', '我的额度', '我的配额', 'quota'):
        return ('quota', '')
    if t in ('参数', '我的参数', '当前参数', '设置'):
        return ('params', '')
    if t in ('比例', '尺寸', '画幅', 'size'):
        return ('size', '')
    if t in ('张数', '数量', 'count'):
        return ('count', '')
    if t in ('模型', 'model', '切换模型'):
        return ('model', '')
    if t in ('绑定', 'bind', '激活'):
        return ('bind', '')
    # 裸 `/n`：只在斜杠写法下认 —— 单独的拉丁字母 `n` 太容易撞提示词
    if t == 'n' and had_slash:
        return ('count', '')

    # ── ② 带参数（中文前缀，见 _PREFIX_COMMANDS）──
    for prefix, name in _PREFIX_COMMANDS:
        if t.startswith(prefix):
            rest = t[len(prefix):].strip()
            if not rest:
                # 「取消任务」不带参数 → 当列表处理（比报用法更顺手）
                return ('list', '') if name == 'cancel' else (name, '')
            return _parse_cmd_arg(name, rest)

    # ── ③ 带参数（拉丁，仅斜杠写法）──
    if had_slash:
        for key, name in _SLASH_ARG_COMMANDS:
            if t.startswith(key + ' '):
                return _parse_cmd_arg(name, t[len(key):].strip())

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
        # P2（2026-09-28）：群聊两道门用到的配置，**在建实例时读一次**
        # （`get_allowed_chats` / `get_bot_open_id` 内部会读 .env 文件，
        #   不能让它跑在"每条消息"的热路径上）
        self.allowed_chats = get_allowed_chats()
        self.bot_open_id = get_bot_open_id()
        # 提交前确认（2026-09-28）：模块级常量在建实例时快照下来，
        # 便于测试注入（`bot.confirm_enabled = False` 即可跑旧行为的回归）。
        self.confirm_enabled = CONFIRM_SUBMIT
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

        # 顺手清理（去重记录 7 天、已终态跟踪 30 天、过期待选）—— 避免表只增不减
        try:
            n1 = self.db.feishu_dedup_gc(DEDUP_KEEP_DAYS * 86400)
            n2 = self.db.feishu_track_gc(30 * 86400)
            n3 = self.db.feishu_pending_gc()
            if n1 or n2 or n3:
                logger.info(f'🤖 清理过期记录：去重 {n1} 条、已终态跟踪 {n2} 条、过期待选 {n3} 条')
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
            #   注意它**先于**群白名单/@ 过滤：被忽略的消息也要登记，
            #   否则飞书重放时会重新走一遍过滤（虽无害，但日志里会重复刷同一条忽略记录）。
            key = self.event_key(data, message)
            if key:
                if self.db.feishu_seen(key):
                    logger.info(f'🤖 重复事件，已忽略（{key}）')
                    return
            else:
                logger.warning('🤖 事件既无 event_id 也无 message_id，本次**未去重**，请检查上游')

            # ★ P2（2026-09-28）：放开群聊。两道门都必须过（设计方案 §3.2 群聊硬规则 1）：
            #   ① **白名单**（决策 3：只服务 owner 自己 + owner 自己的群）
            #   ② **必须 @图图**（不是"文本里有 @" —— 那会连 @别人 的消息一起响应）
            chat_type = getattr(message, 'chat_type', '') or ''
            is_group = chat_type == 'group'
            if chat_type not in ('p2p', 'group', ''):
                logger.info(f'🤖 忽略不支持的消息类型 chat_type={chat_type}')
                return
            chat_id = getattr(message, 'chat_id', '') or ''
            if is_group:
                if not self._group_allowed(chat_id):
                    return
                if not self._mentioned_bot(message):
                    logger.info(f'🤖 群消息未 @图图，忽略（chat={chat_id}）')
                    return
            if message.message_type != 'text':
                # P3（图生图）才接图片/文件；这里明确忽略并留痕
                logger.info(f'🤖 忽略非文本消息 type={message.message_type}')
                return
            try:
                content = json.loads(message.content or '{}')
                text = (content.get('text') or '').strip()
            except Exception:
                text = ''
            if not text:
                return
            logger.info(f'📩 图图收到 {open_id}{"(群)" if is_group else ""}: {text[:60]}')
            # ★ 只入队、不在这里干活 —— 见 __init__ 里"为什么必须有工作线程"的实测事故
            if is_group:
                self._enqueue(open_id, text, chat_id=chat_id, is_group=1)
            else:
                self._enqueue(open_id, text)
        except Exception as e:
            logger.error(f'🤖 消息处理异常: {type(e).__name__}: {e}', exc_info=True)

    def _group_allowed(self, chat_id: str) -> bool:
        """决策 3（2026-09-23 用户拍板）：只服务 owner 自己 + owner 自己的群。

        白名单**默认为空 = 一个群都不服务**（要显式配 `FEISHU_ALLOWED_CHATS`）——
        这是刻意的安全默认：宁可"配了才生效"，也不要"忘了配就对外裸奔"。
        非白名单群**连日志都只打一行**，不回复（回一句"你没权限"等于把机器人存在感暴露给陌生人）。
        """
        if not chat_id:
            return False
        if chat_id in self.allowed_chats:
            return True
        logger.info(f'🤖 群 {chat_id} 不在白名单（FEISHU_ALLOWED_CHATS），忽略')
        return False

    def _mentioned_bot(self, message) -> bool:
        """群消息是否 **@了图图本人**。

        判据是飞书事件自带的 `message.mentions`（每项有 `id.open_id`），
        与 `FEISHU_BOT_OPEN_ID` 比对 —— **不是**"文本里有没有 `@_user_`"。
        为什么不能用后者：群里 @别人 同样会产生 mention 占位符，
        那样就会**误响应不属于它的消息**（用户会觉得机器人乱插话）。

        ⚠️ 未配 `FEISHU_BOT_OPEN_ID` 时退化为"任意 @ 即响应"并打 WARNING ——
          白名单群内可接受，但**不算正确**，所以要把这条降级显式吼出来。
        """
        mentions = getattr(message, 'mentions', None) or []
        if not mentions:
            return False
        if not self.bot_open_id:
            logger.warning('🤖 未配置 FEISHU_BOT_OPEN_ID → 群聊 @ 判定退化为"任意 @ 即响应"')
            return True
        for m in mentions:
            mid = getattr(m, 'id', None)
            if mid is not None and getattr(mid, 'open_id', '') == self.bot_open_id:
                return True
        return False

    def _enqueue(self, open_id: str, text: str, chat_id: str = '', is_group: int = 0):
        """把耗时工作交给工作线程。队列满时**明确回绝**，绝不无声丢弃。"""
        try:
            self._q.put_nowait((open_id, text, chat_id, is_group))
        except queue.Full:
            logger.warning('🤖 待处理队列已满，拒绝并回执')
            self._reply(open_id, chat_id, is_group, '⚠️ 消息处理队列已满，请稍后再发一次～')

    def _work_loop(self):
        while True:
            try:
                open_id, text, chat_id, is_group = self._q.get()
            except Exception:
                time.sleep(0.5)
                continue
            try:
                self._handle_prompt(open_id, text, chat_id=chat_id, is_group=is_group)
            except Exception as e:
                logger.error(f'🤖 工作线程异常: {type(e).__name__}: {e}', exc_info=True)
            finally:
                try:
                    self._q.task_done()
                except Exception:
                    pass

    def _reply(self, open_id: str, chat_id: str, is_group: int, text: str) -> bool:
        """按**来源会话**回复：群里回群（@ 发起人），私聊回私聊。

        为什么不统一回私聊：群里发起的任务，结果丢进私聊会让人以为机器人没反应
        （同群其他人也看不到）；而且群内发起的结论本就该落在群里（设计方案 §3.2）。
        """
        try:
            if is_group and chat_id:
                return bool(self.n.send_to(chat_id, _at_prefix(open_id) + text))
            return bool(self.n.send_direct(open_id, text))
        except Exception as e:                                    # noqa: BLE001
            logger.warning(f'🤖 回执发送失败: {type(e).__name__}: {e}')
            return False

    def _session(self, open_id: str, chat_id: str) -> tuple:
        """会话三元组 `(channel, chat_id, sender_id)`（见 `manager/figu_state.py`）。"""
        return fst.session_key(chat_id, open_id)

    def _handle_prompt(self, open_id: str, prompt: str, chat_id: str = None, is_group: int = 0):
        """处理一条用户消息：**命令 → 待选状态 → 未知斜杠 → 提交前确认 → 提交**。

        ⚠️ 这个顺序是**硬要求**，每一步错位都会出事（都踩过）：
          · 命令识别晚于提交 → 「取消 01」被当提示词，**真的扣一次额度出一张废图**；
          · 待选消费早于命令   → 「取消」会被当成"选择第 N 项"而命令失效；
          · 确认晚于待选消费   → 用户回「是」会被当新提示词，又弹一次确认（永远出不了图）。
        """
        chat_id = chat_id or ''
        ses = self._session(open_id, chat_id)

        # ① 命令（最高优先级）
        cmd = _match_command(prompt)
        if cmd:
            name, arg = cmd
            # 命令一旦被识别，就说明用户**已经翻篇**了 → 清掉待选状态。
            # 这顺带把"取消待选"这件事做掉了（设计方案 §3.4 的 `/cancel` 语义），
            # 不用再单开一个命令。
            try:
                self.db.feishu_pending_clear(*ses)
            except Exception as e:                                # noqa: BLE001
                logger.warning(f'🤖 清理待选状态失败（不影响命令执行）: {e}')
            try:
                if name == 'help':
                    self._cmd_help(open_id, chat_id, is_group)
                elif name == 'list':
                    self._cmd_list(open_id, chat_id, is_group)
                elif name == 'cancel':
                    self._cmd_cancel(open_id, arg, chat_id, is_group)
                elif name == 'params':
                    self._cmd_params(open_id, chat_id, is_group)
                elif name == 'quota':
                    self._cmd_quota(open_id, chat_id, is_group)
                elif name in ('size', 'count', 'model'):
                    self._cmd_param(name, arg, open_id, chat_id, is_group)
                elif name == 'bind':
                    self._cmd_bind(open_id, arg, chat_id, is_group)
                else:                      # 防御：登记了名字却忘了分支
                    logger.error(f'🤖 命令 {name!r} 已解析但没有处理分支')
                    self._reply(open_id, chat_id, is_group, '内部错误：命令未实现，已记录')
            except Exception as e:
                logger.error(f'🤖 命令 {name} 执行异常: {type(e).__name__}: {e}', exc_info=True)
                self._reply(open_id, chat_id, is_group, f'❌ 命令执行失败：{str(e)[:120]}')
            return

        # ② 待选状态机（在命令之后、提交之前 —— 少了这一步，用户回「2」就会去出图）
        if self._try_consume_pending(ses, prompt, open_id, chat_id, is_group):
            return

        # ③ 走到这里才说明"不是命令、也不是对上一次提问的回答"。
        #    未知的斜杠写法给一次指引（别静默当提示词）
        if prompt.startswith('/'):
            self._reply(open_id, chat_id, is_group,
                        '📝 直接发提示词即可出图，例如：一只橘猫坐在窗台上，阳光洒落\n'
                        '（发「帮助」看全部命令）')
            return

        # ④ 提交前确认（2026-09-28 用户需求）：用户发来的消息**不都是生图提示词**，
        #    所以先回一条"要把这句提交为生图任务吗"，回「是」才真建任务。
        #    ★ 位置必须在**待选消费之后**：否则用户回「是」会被当成新提示词，
        #      又弹一次确认 → 永远出不了图。
        if self.confirm_enabled:
            self._start_confirm(ses, prompt, open_id, chat_id, is_group)
            return

        # ⑤ ★ P0-4：**先**判断上限再提交 —— 原来的顺序（先提交、超限就不跟踪）
        #    会让用户白扣一次额度却永远收不到图。
        with self._lock:
            if open_id in self._submitting:
                self._reply(open_id, chat_id, is_group, '⏳ 你的上一条还在处理，请稍等 1 秒再发～')
                return
            self._submitting.add(open_id)
        try:
            self._submit_locked(open_id, prompt, chat_id, is_group)
        finally:
            with self._lock:
                self._submitting.discard(open_id)

    # ── 命令实现（2026-09-24 P1 / 2026-09-28 v2.11.0 扩面）────────────
    def _cmd_help(self, open_id: str, chat_id: str = '', is_group: int = 0):
        self._reply(open_id, chat_id, is_group, HELP_TEXT)

    def _cmd_params(self, open_id: str, chat_id: str = '', is_group: int = 0):
        """「参数」：显示本会话的默认出图参数。"""
        prefs = self.db.feishu_prefs_get(*self._session(open_id, chat_id))
        self._reply(open_id, chat_id, is_group,
                    _params_text(prefs) + '\n\n（改参数：发「比例」「张数」「模型」）')

    def _cmd_quota(self, open_id: str, chat_id: str = '', is_group: int = 0):
        """「额度」：套餐 / 本月用量 / 是否绑定账户。

        注意口径：`usage_summary` 走的是 `QuotaService.effective`，
        绑定账户后按**账户聚合**（与网页端、商户中心同一口径）——
        这正是 `/bind` 能被用户自己验证的地方。
        """
        self.db.user_ensure(open_id)
        eff = self.quota.effective(open_id)
        acct = eff.get('account_id') or ''
        bits = [f'💳 {self.quota.usage_summary(open_id)}',
                f'套餐：{eff.get("plan")}',
                ('身份：owner（无限量）' if eff.get('is_owner')
                 else (f'身份：已绑定账户 {acct}（与网页端共享额度）' if acct
                       else '身份：未绑定（发「绑定 激活码」可与网页端共享额度）'))]
        self._reply(open_id, chat_id, is_group, '\n'.join(bits))

    def _cmd_param(self, name: str, arg: str, open_id: str, chat_id: str = '', is_group: int = 0):
        """参数命令（`size` / `count` / `model`）的统一入口。

        两种形态：
          · 带参数（`比例 16:9`）→ 立即写入会话默认值并回执；
          · 不带参数（`比例`）   → **登记待选状态** + 回复编号选项，
            用户下一条回 `2`（或回 `16:9`）即被消费（待选状态落 DB，重启不丢）。
        """
        ses = self._session(open_id, chat_id)
        if arg:
            self.db.feishu_prefs_set(*ses, **self._pref_kwargs(name, arg))
            self._reply(open_id, chat_id, is_group,
                        f'✅ 已设置{self._param_label(name)}为 {self._param_display(name, arg)}\n'
                        f'下一张图就按这个来。\n\n'
                        f'{_params_text(self.db.feishu_prefs_get(*ses))}')
            return
        options = self._param_options(name)
        # ★ 一个会话同时只保留一个待选（否则用户回「2」到底选了哪个？——
        #   `feishu_pending` 的 PK 含 kind 是允许并存的，收敛成单个是**调用方约定**）
        self.db.feishu_pending_clear(*ses)
        self.db.feishu_pending_set(*ses, kind=name, payload={'options': list(options)},
                                   ttl=PENDING_TTL)
        cur = self._param_display(name, self.db.feishu_prefs_get(*ses).get(
            {'size': 'size', 'count': 'n', 'model': 'model'}[name]))
        lines = '\n'.join(f'{i}. {o}' for i, o in enumerate(options, 1))
        self._reply(open_id, chat_id, is_group,
                    f'请选择{self._param_label(name)}（当前：{cur}）\n{lines}\n\n'
                    f'回序号即可，例如「2」。（也可回选项本身，如「{options[0]}」）\n'
                    f'{PENDING_TTL // 60} 分钟内有效。')

    # 参数命令的元数据 —— **只此一处**（label / prefs 字段 / 选项 / 回显）。
    # 新增参数只需要在这里加一行 + 在 figu_state 里加解析函数。
    _PARAM_SPEC = {
        'size':  ('比例', 'size',  lambda arg: fst.parse_size(arg),
                  lambda: fst.SIZE_LABELS),
        'count': ('张数', 'n',     lambda arg: fst.parse_count(arg),
                  lambda: fst.COUNT_OPTIONS),
        'model': ('模型', 'model', lambda arg: fst.parse_model_arg(arg),
                  lambda: fst.MODEL_ALIAS_LABELS),
    }

    def _param_label(self, name: str) -> str:
        return self._PARAM_SPEC[name][0]

    def _param_options(self, name: str) -> tuple:
        return tuple(self._PARAM_SPEC[name][3]())

    def _param_display(self, name: str, value) -> str:
        if name == 'count':
            return f'{value or 1} 张'
        if name == 'model':
            return fst.model_label(value) if value else '默认'
        return value or '默认'

    def _pref_kwargs(self, name: str, arg) -> dict:
        """把参数命令的值转成 `feishu_prefs_set` 的 kwargs。"""
        field = self._PARAM_SPEC[name][1]
        return {field: (int(arg) if field == 'n' else arg)}

    def _apply_param_choice(self, kind: str, value: str, ses: tuple,
                            open_id: str, chat_id: str, is_group: int):
        """把消费到的待选**值**落成会话默认值。

        值来自选项标签：size 是 `16:9`、count 是 `'2'`、model 是别名 `klein`。
        """
        if kind == 'size':
            v = fst.parse_size(value)
        elif kind == 'count':
            v = fst.parse_count(value)
        else:
            v = fst.parse_model_arg(value)
        if v is None:                       # 理论上到不了；到了说明选项与解析器漂移了
            logger.error(f'🤖 待选值无法解析: kind={kind} value={value!r}（选项与解析器可能漂移）')
            self._reply(open_id, chat_id, is_group, '⚠️ 这个选项我认不出来，请重新发一次命令')
            return
        self.db.feishu_prefs_set(*ses, **self._pref_kwargs(kind, v))
        self._reply(open_id, chat_id, is_group,
                    f'✅ 已设置{self._param_label(kind)}为 {self._param_display(kind, v)}\n'
                    f'下一张图就按这个来。')
        logger.info(f'📐 图图 {open_id} 通过待选设置了 {kind}={v}')

    def _try_consume_pending(self, ses: tuple, text: str,
                             open_id: str, chat_id: str, is_group: int) -> bool:
        """尝试用这条文本消费待选状态。返回 True = 已消费（调用方**必须**停止后续处理）。

        ★ 三道守卫（`figu_state.should_consume` + `resolve_choice`）：
          ① 空文本 / `/` 开头 → 不消费（命令优先，已在调用方更前面拦了一次，这里是双保险）
          ② 解析不出选项 → **不消费**，回落为提示词（设计方案 §3.3 守卫 2）
          ③ 超时 → DB 的 `expires_at` 已把它滤掉（查不到自然不消费）
        跨会话/跨用户隔离**不靠守卫**，而是靠 key 本身是三元组 —— 查不到别人的记录。
        """
        try:
            pend = self.db.feishu_pending_get(*ses)
        except Exception as e:                                    # noqa: BLE001
            logger.warning(f'🤖 读待选状态失败（按无待选处理）: {type(e).__name__}: {e}')
            return False
        if not pend:
            return False
        ok, reason = fst.should_consume(text)
        if not ok:
            logger.info(f'🤖 待选未消费（{reason}），继续按提示词处理')
            return False
        try:
            payload = json.loads(pend.get('payload') or '{}')
        except Exception:                                         # noqa: BLE001
            payload = {}
        # ★ 分派（2026-09-28）：确认状态（`confirm_submit`）的语义与"参数待选"完全不同 ——
        #   它**没有 options 列表**，用户回的是「是/否」而不是序号。
        #   不分派的话 `options` 为空 → `resolve_choice` 恒返 None → 永远不消费
        #   → 回「是」会被当提示词，又弹一次确认（死循环）。
        if pend['kind'] == fst.CONFIRM_KIND:
            return self._consume_confirm(pend, text, ses, open_id, chat_id, is_group)
        options = payload.get('options') or []
        idx = fst.resolve_choice(text, options)
        if idx is None:
            # ★ 非选择意图 → **不消费**（用户可能直接发了下一句提示词）
            logger.info(f'🤖 有待选 {pend["kind"]} 但 {text[:20]!r} 不是选项，按提示词处理')
            return False
        value = options[idx]
        self.db.feishu_pending_clear(*ses, pend['kind'])
        self._apply_param_choice(pend['kind'], value, ses, open_id, chat_id, is_group)
        return True

    # ── 提交前确认（2026-09-28 用户需求）─────────────────────────────────
    def _confirm_text(self, prompt: str, n: int, prefs: dict) -> str:
        """确认请求的文案。

        ★ 必须把**将要用到的参数一并显示**：用户确认的是"这一句 + 这一组参数"，
          只回显句子的话，他看不出这次会出几张、什么比例、哪个模型。
        """
        size = prefs.get('size') or '默认'
        model = fst.model_label(prefs.get('model'))
        return (f'🤔 要把这一句作为生图任务提交吗？\n'
                f'「{prompt[:150]}」\n'
                f'\n'
                f'本次参数：{n} 张 · 比例 {size} · 模型 {model}\n'
                f'回「是」开始出图 / 回「否」放弃\n'
                f'（{PENDING_TTL // 60} 分钟内有效；不回也没关系，不会扣额度）')

    def _start_confirm(self, ses: tuple, prompt: str,
                       open_id: str, chat_id: str, is_group: int) -> bool:
        """登记"待确认"并发出确认请求。返回 True = 本消息已处理（调用方必须 return）。

        ★ 参数**快照**：确认消息里显示的参数，就是提交时真正用的参数。
          为什么不"提交时重读 prefs"：那样用户中途改了「张数」就会出现
          「确认时显示 2 张、实际出了 3 张」—— 系统说的和做的不一致，是**虚假陈述**。
          快照之后语义闭合：确认的就是屏幕上那一个任务。
        """
        try:
            per_user = self.db.feishu_tracks_inflight(sender_id=open_id)
        except Exception as e:                                    # noqa: BLE001
            logger.warning(f'🤖 确认前查在途失败（按 0 处理）: {type(e).__name__}: {e}')
            per_user = 0
        if per_user >= MAX_INFLIGHT_PER_USER:
            # 提前拦：别让用户等确认完才发现"你还有任务在跑"（白等一场）
            self._reply(open_id, chat_id, is_group, '⏳ 你有一个任务还在生成中，请稍候再发～')
            return True

        prefs = self.db.feishu_prefs_get(*ses)
        n = max(1, min(fst.COUNT_MAX, int(prefs.get('n') or 1)))
        # 一个会话只保留一个待选/待确认（调用方约定，见 `feishu_pending_set` 注释）
        self.db.feishu_pending_clear(*ses)
        self.db.feishu_pending_set(
            *ses, kind=fst.CONFIRM_KIND,
            payload={'prompt': prompt, 'n': n,
                     'size': prefs.get('size'), 'model': prefs.get('model')},
            ttl=PENDING_TTL)
        self._reply(open_id, chat_id, is_group, self._confirm_text(prompt, n, prefs))
        logger.info(f'🤔 图图 {open_id} 待确认提交：{prompt[:30]!r}')
        return True

    def _consume_confirm(self, pend: dict, text: str, ses: tuple,
                         open_id: str, chat_id: str, is_group: int) -> bool:
        """消费"待确认"状态。返回 True = 已消费（调用方必须停止后续处理）。

        三条去路：
          · 回「是」类 → 用**快照参数**提交
          · 回「否」类 → 放弃，不建任务
          · 都不是     → 作废这次确认，把这条当**新提示词**让调用方继续走（用户多半改主意了）
        """
        intent = fst.parse_confirm(text)
        if intent is None:
            # ★ 既没说是、也没说否（多半直接发了新提示词）→ 作废旧的，
            #   让调用方继续处理这条文本（会重新弹一次确认）。
            self.db.feishu_pending_clear(*ses, fst.CONFIRM_KIND)
            logger.info(f'🤔 待确认未响应（{text[:20]!r}）→ 作废，按新提示词处理')
            return False
        self.db.feishu_pending_clear(*ses, fst.CONFIRM_KIND)
        if not intent:
            self._reply(open_id, chat_id, is_group, '👌 好的，这次不出了。')
            return True

        try:
            payload = json.loads(pend.get('payload') or '{}')
        except Exception:                                         # noqa: BLE001
            payload = {}
        prompt = (payload.get('prompt') or '').strip()
        if not prompt:
            self._reply(open_id, chat_id, is_group, '⚠️ 这次待确认的任务已失效，请重新发一次提示词')
            return True

        with self._lock:
            if open_id in self._submitting:
                self._reply(open_id, chat_id, is_group, '⏳ 你的上一条还在处理，请稍等 1 秒再发～')
                return True
            self._submitting.add(open_id)
        try:
            self._submit_locked(open_id, prompt, chat_id, is_group,
                                prefs_override={'n': payload.get('n'),
                                                'size': payload.get('size'),
                                                'model': payload.get('model')})
        finally:
            with self._lock:
                self._submitting.discard(open_id)
        return True

    def _cmd_bind(self, open_id: str, code: str, chat_id: str = '', is_group: int = 0):
        """`绑定 <激活码>` —— 复用 `/api/activate` 语义（决策 2，从 P5 提到 P1）。

        与网页端**同一张 accounts 表、同一份额度**：不做两套账。
        幂等：已激活过的码会把当前 open_id 并入那个账户（= 换设备）。
        """
        if not code:
            self._reply(open_id, chat_id, is_group,
                        '🔑 用法：发「绑定 XXXXXXXX」（XXXXXXXX = 激活码，8 位）\n'
                        '绑定后飞书身份与网页端**共享同一份额度**。')
            return
        self.db.user_ensure(open_id)
        existed = self.db.account_get(code) is not None
        try:
            ok = self.db.code_activate(code, open_id)
        except Exception as e:
            logger.error(f'🤖 绑定异常 {code}: {type(e).__name__}: {e}', exc_info=True)
            self._reply(open_id, chat_id, is_group, f'❌ 绑定失败：{str(e)[:120]}')
            return
        if not ok:
            # 措辞与网页端对齐（不区分"不存在/已过期/不可用"，避免探测激活码是否存在）
            self._reply(open_id, chat_id, is_group, '❌ 激活码无效、已过期或不可用')
            return
        eff = self.quota.effective(open_id)
        msg = (f'✅ 已绑定到已有账户（{code}），共享套餐：{eff.get("plan")}'
               if existed else f'✅ 激活成功，套餐：{eff.get("plan")}')
        self._reply(open_id, chat_id, is_group, f'{msg}\n\n💳 {self.quota.usage_summary(open_id)}')
        logger.info(f'🔑 图图 {open_id} 绑定激活码 {code}（{"已有账户" if existed else "新建账户"}）')

    @staticmethod
    def _fmt_job_line(idx: int, job: dict) -> str:
        """列表一行：`01 ⏳ 一只橘猫坐在窗台上（排队中 · 3ada124e）`"""
        st = job.get('status') or ''
        icon, label = STATUS_LABELS.get(st, ('❔', st or '未知'))
        prompt = re.sub(r'\s+', ' ', (job.get('prompt') or '')).strip()[:24] or '(无提示词)'
        short = (job.get('job_id') or '')[:8]
        return f'{idx:02d} {icon} {prompt}（{label} · {short}）'

    def _cmd_list(self, open_id: str, chat_id: str = '', is_group: int = 0):
        """列出我在途的任务（供「取消 N」定位）。**只列自己的**（SQL 层钉死 user_id）。"""
        rows = self.db.jobs_inflight_by_user(open_id, limit=LIST_LIMIT)
        if not rows:
            self._reply(open_id, chat_id, is_group,
                        '📭 你当前没有排队 / 生成中的任务。\n\n'
                        '直接发一句话就能出图，例如：一只橘猫坐在窗台上，阳光洒落')
            return
        jobs = [dict(r) for r in rows]
        lines = [self._fmt_job_line(i, j) for i, j in enumerate(jobs, 1)]
        first_short = (jobs[0].get('job_id') or '')[:8]
        self._reply(
            open_id, chat_id, is_group,
            '📋 我的任务（在途）：\n' + '\n'.join(lines) +
            f'\n\n✖️ 取消：发「取消 01」取消第 1 个，或「取消 {first_short}」按任务号取消。'
            '\n⚠️ 取消不退还额度（与网页端一致）。')

    def _cmd_cancel(self, open_id: str, arg: str, chat_id: str = '', is_group: int = 0):
        """取消自己的在途任务。`arg` 为序号或 job_id 短码。

        **权限**：`db.job_mark_deleted()` 的 SQL 钉死 `user_id=?`，
        所以"越权取消别人的任务"在**数据层就不可能发生** —— 这里不需要再鉴权，
        上层也不依赖"记得校验"（这是刻意设计，见该方法的 docstring）。
        """
        jobs = [dict(r) for r in self.db.jobs_inflight_by_user(open_id, limit=50)]
        if not jobs:
            self._reply(open_id, chat_id, is_group, '📭 你当前没有可取消的任务。')
            return

        # ① 定位目标（序号优先 —— 序号是上一条「我的任务」的展示顺序，新→旧）
        target = None
        if arg.isdigit():
            idx = int(arg)
            if idx < 1 or idx > len(jobs):
                self._reply(open_id, chat_id, is_group,
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
                self._reply(open_id, chat_id, is_group,
                            f'⚠️ 在途任务里没有以 {arg} 开头的（可能已取消或已完成）。\n'
                            f'发「我的任务」看当前在途列表')
                return
            if len(hits) > 1:
                self._reply(open_id, chat_id, is_group,
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
            self._reply(open_id, chat_id, is_group, f'❌ 取消失败：{str(e)[:120]}')
            return
        if not marked:
            # 幂等：已删过（可能是网页端删的）。不是错误，如实告知即可。
            self._reply(open_id, chat_id, is_group,
                        f'ℹ️ 任务 {job_id[:8]} 此前已取消，无需重复操作。')
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
        self._reply(open_id, chat_id, is_group,
                    f'✖️ 已取消「{label}」的任务 {job_id[:8]}\n{tail}\n'
                    f'⚠️ 额度不退还（与网页端一致）。')

    def _submit_locked(self, open_id: str, prompt: str, chat_id: str = '', is_group: int = 0,
                       prefs_override: dict = None):
        """真正提交（已被 `_submitting` 短锁保护，同一用户不会并发进来）。

        `prefs_override`（2026-09-28）：**确认流程传入的参数快照**。给了就用它，
        不再读库 —— 保证"确认消息里显示的参数"就是"实际提交的参数"。
        ★ 是**整体替换**而不是"只覆盖非空字段"：快照里 `size=None` 表示确认那一刻
          用户就没设比例，提交时也必须是不设；若这里改成"None 就跳过"，
          用户在中途设了比例就会出现「确认时显示默认、实际按比例出」的偏差。

        `n > 1` 走 **"一次翻译、多张候选"**：第 1 张按正常路径提交（中文翻译由调度器
        完成），随后**从库里读回它实际落地的英文提示词**，作为第 2..n 张的输入。
        为什么不各自翻译（2026-09-28）：
          · **正确性** —— 翻译会漂移（仓库既有记录：同一句中文两次译出不同英文），
            各自翻译 = n 个不同提示词 → 出的不是"同一句话的候选"，而是 n 张不相干的图；
          · **成本** —— 中文翻译是 LLM 调用、最坏 68s，n×68s 全落在工作线程上，
            会**拖住其他用户**。
        从库里读回（而不是自己再翻一次）是为了**不复制 submit() 的翻译策略** ——
        复制一份必然漂移（本仓库反复栽在"改一处要同步三处"上）。
        """
        try:
            # 每用户在途上限：**读库口径**，所以 9620 重启后依然准确
            per_user = self.db.feishu_tracks_inflight(sender_id=open_id)
            if per_user >= MAX_INFLIGHT_PER_USER:
                self._reply(open_id, chat_id, is_group, '⏳ 你有一个任务还在生成中，请稍候再发～')
                return
            inflight = self.db.feishu_tracks_inflight()
            if inflight >= MAX_INFLIGHT_GLOBAL:
                # 明确回执，且**没有提交**（不浪费额度）
                self._reply(open_id, chat_id, is_group,
                            f'⚠️ 当前在途任务较多（{inflight} 个），为免你白等，这次**没有提交**。\n'
                            f'稍后再发一次即可。')
                return

            # 会话默认参数（比例 / 张数 / 模型）—— 落库，重启后仍然生效
            prefs = self.db.feishu_prefs_get(*self._session(open_id, chat_id))
            if prefs_override is not None:
                prefs = {**prefs, **prefs_override}      # 确认流程：用快照，不读库（见 docstring）
            n = max(1, min(fst.COUNT_MAX, int(prefs.get('n') or 1)))
            width, height = fst.size_to_wh(prefs.get('size'))
            model = prefs.get('model') or None

            # ★ 立即回执（2026-09-24 实测教训）：提交里含**中文翻译**，最坏要重试 3 次、
            #   实测耗时 68 秒。这期间用户屏幕上什么都没有，只会以为"机器人坏了"。
            #   先给一条即时反馈，让他知道消息收到了、正在解析。
            hint = '⏳ 收到！正在解析提示词…（中文会先翻译成英文，通常十几秒）'
            if n > 1:
                hint += f'\n本次会出 {n} 张（发「张数 1」可改回单张）'
            self._reply(open_id, chat_id, is_group, hint)

            # 确保用户存在（飞书 open_id 即 user_id）
            self.db.user_ensure(open_id)

            job_ids, errors = [], []
            base_prompt = prompt          # 第 1 张用用户原文；第 2..n 张用第 1 张落地的英文
            for i in range(n):
                # 多图必须**显式给不同 seed**：去重键含 seed，全传 None 会被
                # "相同提示词与参数正在排队"挡住第 2 张起（本仓库 _dedup_key 的既有契约）。
                seed = None if n == 1 else random.randint(1, 2 ** 31 - 1)
                result = self.scheduler.submit(open_id, base_prompt,
                                               width=width, height=height,
                                               seed=seed, model=model)
                if 'error' in result:
                    errors.append(str(result['error']))
                    break
                jid = result['job_id']
                job_ids.append(jid)

                # ★ P0-3：先落库再回执 —— 万一进程在回执后立刻挂掉，重启也能靠这张表把图送到
                try:
                    self.db.feishu_track_add(jid, open_id, chat_id=chat_id,
                                             is_group=1 if is_group else 0,
                                             prompt=prompt, phase='queued')
                except Exception as e:
                    # 落库失败不能吞：这意味着"重启就丢结果"的风险回来了
                    logger.error(f'🤖 在途任务落库失败（重启将无法回传）: {e}')

                if i == 0 and n > 1:
                    row = self.db.job_get(jid)
                    if row is not None:
                        # 库里存的就是**实际使用**的提示词（已翻译 / 或本来英文）——
                        # 这是"不复制翻译策略"的关键：拿一手事实，不拿自己的推断。
                        base_prompt = row['prompt'] or prompt

            if not job_ids:
                self._reply(open_id, chat_id, is_group,
                            f'❌ {errors[0] if errors else "提交失败"}')
                return

            usage = self.quota.usage_summary(open_id)
            if n == 1:
                tail = (f'✅ 收到！正在排队生成「{prompt[:40]}」\n'
                        f'任务号: {job_ids[0]}\n💳 {usage}')
            else:
                got = len(job_ids)
                tail = (f'✅ 收到！正在排队生成「{prompt[:40]}」× {got}\n'
                        f'任务号: {", ".join(j[:8] for j in job_ids)}\n💳 {usage}')
                if got < n:
                    tail += f'\n⚠️ 只提交成功 {got}/{n} 张：{(errors[0] if errors else "")[:80]}'
            self._reply(open_id, chat_id, is_group, tail)
        except Exception as e:
            logger.error(f'🤖 提交异常: {type(e).__name__}: {e}', exc_info=True)
            self._reply(open_id, chat_id, is_group, f'❌ 提交失败：{str(e)[:120]}')

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
            # 回执要按**来源会话**发（P2 群聊）：群里发起 → 回群里并 @ 发起人
            chat_id = t['chat_id'] or ''
            is_group = int(t['is_group'] or 0)
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
                self._send_result(sender, job, chat_id, is_group)
                self.db.feishu_track_set(job_id, phase='done')
            elif st == 'failed':
                err = (job.get('error') or '未知错误')[:200]
                if not notified.get('failed'):
                    if self._reply(sender, chat_id, is_group, f'❌ 生成失败: {err}'):
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
                    if self._reply(sender, chat_id, is_group,
                                   '🔴 服务器暂不可达，任务已进入等待恢复队列，恢复后自动继续～'):
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

    def _send_result(self, open_id: str, job: dict, chat_id: str = '', is_group: int = 0):
        """生成完成：上传图片到飞书并回传。

        ⚠️ `job` 必须是 **dict**（P0-1 的根因就是这里传了 sqlite3.Row 又用 .get()）。

        P2：群里发起的结果回到**群里**、只 @ 发起人（不私聊、不 @ 全群）——
        群内发起的结论就该落在群里，丢私聊会让"同群的人以为机器人没反应"。
        """
        img = job.get('image_path') or ''
        if img and Path(img).exists():
            key = self.n.upload_image(img)
            if key:
                if is_group and chat_id:
                    ok = self.n.send_image_to(chat_id, key)
                else:
                    ok = self.n.send_image(open_id, key)
                if ok:
                    self._reply(open_id, chat_id, is_group, '✨ 已生成，请查收！')
                    return
        else:
            logger.error(f'🤖 任务 {job.get("job_id")} 状态 done 但图片不存在: {img!r}')
        self._reply(open_id, chat_id, is_group,
                    '❌ 生成完成但图片回传失败，可到网页 flux.zhuanlu.xyz 下载')


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

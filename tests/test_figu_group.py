#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""飞书「图图」群聊 P2 回归门（离线，不需要 GPU / 不需要真发飞书消息）。

背景（2026-09-28）：设计文档 `docs/图图-飞书完整互动-设计方案.md` 的 P2 是
「放开群聊」。决策 3（2026-09-23 用户拍板）：**只服务 owner 自己 + owner 自己的群**。

放开群聊必须过**两道门**，缺一不可（设计方案 §3.2 群聊硬规则 1）：
  ① **白名单** `FEISHU_ALLOWED_CHATS` —— **默认空 = 一个群都不服务**（刻意的安全默认：
     宁可「配了才生效」，也不要「忘了配就对外裸奔」）；
  ② **必须 @图图本人** —— 判据是飞书事件自带的 `message.mentions[].id.open_id`
     与 `FEISHU_BOT_OPEN_ID` 比对，**不是**"文本里有没有 `@`"。
     为什么不能用后者：群里 @别人 同样产生 at 占位符，那样会**误响应不属于它的消息**
     （用户会觉得机器人乱插话）。

本门除了两道门，还钉住**回程路由**（＝P2 最容易把 P0/P1 改坏的地方）：
  · 群里发起 → 结果**回群里**、只 @ 发起人（不私聊、不 @ 全群）；
  · 私聊发起 → 仍然走**私聊**（P2 不得把原有私聊路由改坏）；
  · 群里提交的任务，`feishu_tracks` 必须带上 `chat_id/is_group` ——
    否则 9620 重启后（每次改代码都要重启）结果会**回错地方**（丢私聊）。

判据尽量取「**用户可观察行为**」（提交了几次 / 消息发到哪个会话 / 有没有 @ 发起人），
而不是钉字面串 —— 免得重构一次误报一次（见 docs/PITFALLS.md 的「改门三判据」）。

用法：python tests/test_figu_group.py   （rc=0 通过）
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL, SKIP = [], [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✅' if cond else '❌'} {label}" + (f"    ({detail})" if detail else ''))


def skip(label, reason):
    """⚠️ 跳过必须**显式**且**带理由**（见 docs/PITFALLS.md #4：静默跳过 = 假绿）。"""
    SKIP.append(label)
    print(f"  ⏭️  {label}    (跳过：{reason})")


# ── 替身（离线可验的关键）──────────────────────────────────────────────
class StubNotifier:
    """记录所有外发消息与**目标会话**，不碰网络。

    与 p0 门的替身相比，这里必须多接三个 P2 方法（`send_to` / `send_image_to` /
    `at_text`）—— 缺了会直接 AttributeError 炸出来，不会静默变绿。
    """

    def __init__(self):
        self.app_id = 'stub_app'
        self.app_secret = 'stub_secret'
        self.owner_open_id = 'ou_owner'
        self.direct = []        # [(open_id, text)]        私聊文本
        self.to_chat = []       # [(chat_id, text)]        群发文本
        self.images = []        # [(open_id, image_key)]   私聊图片
        self.images_chat = []   # [(chat_id, image_key)]   群发图片
        self.uploads = []       # [path]
        self.fail_upload = False

    # —— 私聊 ——
    def send_direct(self, user_id, text):
        self.direct.append((user_id, text))
        return True

    def send_image(self, user_id, image_key):
        self.images.append((user_id, image_key))
        return True

    # —— 群聊（P2 新增）——
    def send_to(self, chat_id, text):
        self.to_chat.append((chat_id, text))
        return True

    def send_image_to(self, chat_id, image_key):
        self.images_chat.append((chat_id, image_key))
        return True

    def upload_image(self, path):
        self.uploads.append(str(path))
        return None if self.fail_upload else 'img_key_stub'

    @staticmethod
    def at_text(open_id, name=''):
        return f'<at user_id="{open_id}">{name}</at> '

    # —— 断言辅助 ——
    def all_texts(self):
        return [t for _, t in self.direct] + [t for _, t in self.to_chat]

    def text_containing(self, kw):
        return [t for t in self.all_texts() if kw in t]


class StubScheduler:
    def __init__(self):
        self.submits = []      # [(user_id, prompt, kwargs)]

    def submit(self, user_id, prompt, *a, **kw):
        self.submits.append((user_id, prompt, kw))
        return {'job_id': f'job{len(self.submits):04d}'}


class StubQuota:
    def usage_summary(self, user_id):
        return '套餐: default · 本月已用 0/10 张'


def make_event(open_id='ou_user1', text='一只橘猫', event_id='evt_1', message_id='msg_1',
               chat_type='p2p', chat_id='', message_type='text', content=None,
               mentions=None):
    """造一条飞书 `im.message.receive_v1` 事件（只带本模块用到的字段）。

    `mentions`：群聊 @ 判定用。飞书事件里每项形如
      SimpleNamespace(id=SimpleNamespace(open_id='ou_bot'), name='图图')
    """
    msg = types.SimpleNamespace(
        chat_type=chat_type, message_type=message_type, message_id=message_id,
        chat_id=chat_id, mentions=mentions or [],
        content=json.dumps(content if content is not None else {'text': text},
                           ensure_ascii=False))
    sender = types.SimpleNamespace(sender_type='user',
                                   sender_id=types.SimpleNamespace(open_id=open_id))
    hdr = types.SimpleNamespace(event_id=event_id)
    return types.SimpleNamespace(header=hdr, event=types.SimpleNamespace(sender=sender, message=msg))


def mention(open_id, name=''):
    return types.SimpleNamespace(id=types.SimpleNamespace(open_id=open_id), name=name)


def new_bot(db, sched=None, notif=None, allowed=None, bot_open_id='ou_bot'):
    """造一个 bot，并**显式**注入白名单与机器人 open_id。

    不在测试里改 `.env`：配置是从环境/文件读一次的（`__init__` 里读），
    直接覆盖实例属性等价且不污染真实配置。
    """
    import manager.feishu_bot as fb
    sched = sched or StubScheduler()
    notif = notif or StubNotifier()
    bot = fb.FeishuBot(sched, db, StubQuota(), notifier=notif)
    bot.allowed_chats = set(allowed or ())
    bot.bot_open_id = bot_open_id
    # ★ 本门只覆盖**群聊路由与两道门（白名单 / @ 判定）**，显式关掉「提交前确认」
    #   （v2.12.0 起默认开），否则每条提示词都要先走一次确认，歪曲本门断言。
    #   确认流程见 `tests/test_figu_confirm.py`。
    bot.confirm_enabled = False
    # 同步处理（不起线程、不用 sleep 猜它跑完没）——断言的是"消息处理逻辑对不对"
    bot._enqueue = lambda oid, text, chat_id='', is_group=0: bot._handle_prompt(
        oid, text, chat_id=chat_id, is_group=is_group)
    return fb, bot, sched, notif


def fresh_db(prefix):
    from manager.flux_db import FluxDB
    tmpdir = Path(tempfile.mkdtemp(prefix=prefix))
    return FluxDB(str(tmpdir / 'test.db'))


# ══════════════════════════════════════════════════════════════════════════
# [1] 白名单：解析 + 默认空
# ══════════════════════════════════════════════════════════════════════════
def t_whitelist_parsing():
    print('─' * 78)
    print('[1] 白名单解析（决策 3：默认空 = 一个群都不服务）')
    print('─' * 78)
    import manager.feishu_notify as fn

    old = os.environ.get('FEISHU_ALLOWED_CHATS')
    try:
        # ⚠️ 显式写入（`_load_env` 用 setdefault，已被赋值的不会被 .env 覆盖）
        os.environ['FEISHU_ALLOWED_CHATS'] = ''
        empty = fn.get_allowed_chats()
        check('★ 未配置时白名单为**空集合**（安全默认）', empty == set(), f'实际 {empty!r}')

        os.environ['FEISHU_ALLOWED_CHATS'] = 'oc_a, oc_b ;oc_c ,, oc_a'
        got = fn.get_allowed_chats()
        check('逗号/分号/空格混合都能解析', got == {'oc_a', 'oc_b', 'oc_c'}, f'实际 {sorted(got)}')
        check('重复项去重', len(got) == 3, f'实际 {len(got)}')
    finally:
        if old is None:
            os.environ.pop('FEISHU_ALLOWED_CHATS', None)
        else:
            os.environ['FEISHU_ALLOWED_CHATS'] = old


def t_whitelist_gate():
    print('─' * 78)
    print('[2] 白名单门：不在名单里的群一律不服务（含 owner 自己的群也要显式登记）')
    print('─' * 78)
    db = fresh_db('figu_grp_wl_')
    _, bot, sched, notif = new_bot(db, allowed=set())

    check('空名单 → 任意群都拒', bot._group_allowed('oc_any') is False)
    check('空 chat_id → 拒（防"缺字段"被当私聊放行）', bot._group_allowed('') is False)

    _, bot2, _, _ = new_bot(db, allowed={'oc_ok'})
    check('已登记的群 → 通过', bot2._group_allowed('oc_ok') is True)
    check('未登记的群 → 拒', bot2._group_allowed('oc_other') is False)

    # 端到端：非白名单群，即使 @ 了也不提交
    n0 = len(sched.submits)
    bot._on_message(make_event(open_id='ou_u', event_id='e_wl1', message_id='m_wl1',
                               chat_type='group', chat_id='oc_notlisted',
                               text='画只猫', mentions=[mention('ou_bot')]))
    check('★ 非白名单群 + @图图 → 仍不提交', len(sched.submits) == n0,
          f'实际提交 {len(sched.submits)} 次')
    check('非白名单群 → 连回执都不发（不暴露机器人存在感）', notif.all_texts() == [],
          f'实际 {notif.all_texts()!r}')


# ══════════════════════════════════════════════════════════════════════════
# [3] @ 判定：必须是"@的是图图本人"，不是"文本里有 @"
# ══════════════════════════════════════════════════════════════════════════
def t_mention_judgement():
    print('─' * 78)
    print('[3] @ 判定门：用 mentions[].id.open_id 比对，不用文本里的 "@"')
    print('─' * 78)
    db = fresh_db('figu_grp_at_')
    _, bot, _, _ = new_bot(db, allowed={'oc_ok'}, bot_open_id='ou_bot')

    check('@图图本人 → True', bot._mentioned_bot(make_event(mentions=[mention('ou_bot')]).event.message) is True)
    check('★ 只 @别人（不是图图）→ False（否则会乱插话）',
          bot._mentioned_bot(make_event(mentions=[mention('ou_other')]).event.message) is False)
    check('无 mentions → False',
          bot._mentioned_bot(make_event(mentions=[]).event.message) is False)
    check('@多人且含图图 → True',
          bot._mentioned_bot(make_event(mentions=[mention('ou_a'), mention('ou_bot')]).event.message) is True)

    # ⚠️ 降级路径：未配 FEISHU_BOT_OPEN_ID → "任意 @ 即响应"。允许，但必须显式吼出来。
    _, bot_nb, _, _ = new_bot(db, allowed={'oc_ok'}, bot_open_id='')
    check('★ 未配 bot_open_id 时退化为"任意 @ 即响应"（已知降级，非正确）',
          bot_nb._mentioned_bot(make_event(mentions=[mention('ou_anyone')]).event.message) is True)
    check('未配时"无 @ 仍不响应"（降级不放宽到"群里所有消息都答"）',
          bot_nb._mentioned_bot(make_event(mentions=[]).event.message) is False)


# ══════════════════════════════════════════════════════════════════════════
# [4] 群聊端到端：白名单 + @ → 提交，且带群路由
# ══════════════════════════════════════════════════════════════════════════
def t_group_end_to_end():
    print('─' * 78)
    print('[4] 群聊端到端：两道门都过 → 提交，且回执**回群里**并 @ 发起人')
    print('─' * 78)
    db = fresh_db('figu_grp_e2e_')
    _, bot, sched, notif = new_bot(db, allowed={'oc_ok'})

    bot._on_message(make_event(open_id='ou_alice', event_id='e_g1', message_id='m_g1',
                               chat_type='group', chat_id='oc_ok',
                               text='画一只橘猫', mentions=[mention('ou_bot')]))
    check('★ 提交成功', len(sched.submits) == 1, f'实际 {len(sched.submits)} 次')
    check('提交者 = 发起人 open_id', bool(sched.submits) and sched.submits[0][0] == 'ou_alice')

    check('★ 回执发到**群里**（不是私聊）', len(notif.to_chat) >= 1,
          f'群发 {len(notif.to_chat)} 条 / 私聊 {len(notif.direct)} 条')
    check('★ 私聊通道**没有被用**（结果不该丢进私聊）', len(notif.direct) == 0,
          f'实际私聊 {len(notif.direct)} 条')
    check('回执目标群 = 来源群', bool(notif.to_chat) and notif.to_chat[0][0] == 'oc_ok')
    check('★ 回执 @ 了发起人（不是 @ 全群）',
          bool(notif.to_chat) and '<at user_id="ou_alice">' in notif.to_chat[0][1],
          f'实际 {notif.to_chat[0][1][:60]!r}' if notif.to_chat else '无群回执')

    # ★ 群路由必须落库：9620 重启后靠它把图送回正确的群
    row = db.feishu_track_get('job0001')
    check('★ 跟踪行带 chat_id（重启后才知道回哪个群）',
          bool(row) and (row['chat_id'] if row else '') == 'oc_ok',
          f"实际 chat_id={row['chat_id']!r}" if row else '无跟踪行')
    check('★ 跟踪行带 is_group=1', bool(row) and int(row['is_group'] or 0) == 1,
          f"实际 is_group={row['is_group']!r}" if row else '无跟踪行')

    # 群里没 @ 就不该提交
    # 注意：提交路径**本来就发 2 条**（即时「正在解析」+ 排队确认，见 _submit_locked），
    # 所以这里只能断言"**没有新增**"，不能断言总数 == 1。
    n0 = len(sched.submits)
    c0 = len(notif.to_chat)
    bot._on_message(make_event(open_id='ou_bob', event_id='e_g2', message_id='m_g2',
                               chat_type='group', chat_id='oc_ok',
                               text='这只猫不错', mentions=[]))
    check('★ 白名单群内**未 @图图** → 不提交（群聊不该被围观消息刷屏）',
          len(sched.submits) == n0, f'实际 +{len(sched.submits) - n0}')
    check('未 @ 的消息也不回执', len(notif.to_chat) == c0,
          f'实际新增群发 {len(notif.to_chat) - c0} 条（总 {len(notif.to_chat)}）')


# ══════════════════════════════════════════════════════════════════════════
# [5] 私聊路由不得被 P2 改坏
# ══════════════════════════════════════════════════════════════════════════
def t_p2p_unaffected():
    print('─' * 78)
    print('[5] 私聊回归：不需要 @，回执仍走私聊（P2 不得改坏 P0/P1）')
    print('─' * 78)
    db = fresh_db('figu_grp_p2p_')
    _, bot, sched, notif = new_bot(db, allowed={'oc_ok'})

    bot._on_message(make_event(open_id='ou_carol', event_id='e_p1', message_id='m_p1',
                               chat_type='p2p', chat_id='oc_dm_carol',
                               text='画一只猫', mentions=[]))
    check('私聊无需 @ 也能提交', len(sched.submits) == 1, f'实际 {len(sched.submits)} 次')
    check('★ 私聊回执走私聊通道', len(notif.direct) >= 1, f'实际 {len(notif.direct)} 条')
    check('私聊回执不落群发通道', len(notif.to_chat) == 0, f'实际群发 {len(notif.to_chat)} 条')

    row = db.feishu_track_get('job0001')
    check('私聊跟踪行 is_group=0', bool(row) and int(row['is_group'] or 0) == 0,
          f"实际 is_group={row['is_group']!r}" if row else '无跟踪行')


# ══════════════════════════════════════════════════════════════════════════
# [6] 回程路由：_reply / _send_result
# ══════════════════════════════════════════════════════════════════════════
def t_reply_routing():
    print('─' * 78)
    print('[6] 回程路由：`_reply` 群→群(@发起人) / 私聊→私聊')
    print('─' * 78)
    db = fresh_db('figu_grp_reply_')
    _, bot, _, notif = new_bot(db)

    bot._reply('ou_dave', 'oc_g', 1, '你好')
    check('群里回 → send_to(chat_id)', notif.to_chat and notif.to_chat[-1][0] == 'oc_g')
    check('群里回 → 带 @ 发起人前缀',
          notif.to_chat and notif.to_chat[-1][1].startswith('<at user_id="ou_dave">'),
          f'实际 {notif.to_chat[-1][1][:40]!r}')
    check('群里回 → **不**走私聊', len(notif.direct) == 0, f'实际私聊 {len(notif.direct)}')

    bot._reply('ou_dave', '', 0, '你好')
    check('私聊回 → send_direct(open_id)', notif.direct and notif.direct[-1] == ('ou_dave', '你好'))
    check('私聊回 → **不**落群发', len(notif.to_chat) == 1, f'实际群发 {len(notif.to_chat)}')

    # 群回执但 chat_id 缺失（异常数据）→ 必须退化为私聊，不能静默丢消息
    n_direct = len(notif.direct)
    ok = bot._reply('ou_dave', '', 1, '兜底')
    check('★ is_group=1 但 chat_id 为空 → 退化为私聊（不静默丢消息）',
          ok is True and len(notif.direct) == n_direct + 1,
          f'实际私聊 {len(notif.direct)} 条')


def t_result_routing():
    print('─' * 78)
    print('[7] 结果路由：`_send_result` 群→send_image_to / 私聊→send_image')
    print('─' * 78)
    img = Path(tempfile.mkdtemp(prefix='figu_grp_img_')) / 'out.png'
    img.write_bytes(b'\x89PNG\r\n\x1a\n stub')

    db = fresh_db('figu_grp_res_')
    _, bot, _, notif = new_bot(db)

    job = {'job_id': 'job0001', 'image_path': str(img)}
    bot._send_result('ou_erin', job, chat_id='oc_g', is_group=1)
    check('群结果 → send_image_to(chat_id)',
          notif.images_chat and notif.images_chat[-1][0] == 'oc_g',
          f'实际 {notif.images_chat!r}')
    check('群结果 → 不走私聊图片通道', len(notif.images) == 0, f'实际 {notif.images!r}')
    check('群结果 → 回执也落群里并 @ 发起人',
          notif.to_chat and '<at user_id="ou_erin">' in notif.to_chat[-1][1],
          f'实际 {notif.to_chat[-1][1][:40]!r}' if notif.to_chat else '无')

    bot._send_result('ou_erin', job, chat_id='', is_group=0)
    check('私聊结果 → send_image(open_id)',
          notif.images and notif.images[-1][0] == 'ou_erin', f'实际 {notif.images!r}')


# ══════════════════════════════════════════════════════════════════════════
# [8] 去重先于群过滤：被忽略的群消息也要登记（防重放时重新走一遍过滤）
# ══════════════════════════════════════════════════════════════════════════
def t_dedup_before_group_filter():
    print('─' * 78)
    print('[8] 去重先于群过滤：被忽略的群消息也已登记（重放不会二次处理）')
    print('─' * 78)
    db = fresh_db('figu_grp_dedup_')
    _, bot, sched, _ = new_bot(db, allowed=set())

    ev = make_event(open_id='ou_f', event_id='e_dd', message_id='m_dd',
                    chat_type='group', chat_id='oc_unknown',
                    text='画只猫', mentions=[mention('ou_bot')])
    bot._on_message(ev)
    check('非白名单群消息被忽略', len(sched.submits) == 0)
    check('★ 被忽略的群消息也登记了去重键（重放不再处理）',
          db.feishu_seen('evt:e_dd') is True)

    n0 = len(sched.submits)
    bot._on_message(ev)                     # 同一条重放
    check('重放不产生额外提交', len(sched.submits) == n0)


# ══════════════════════════════════════════════════════════════════════════
# [9] 会话隔离：同一个人在不同会话里的参数/待选互不串（三元组口径）
# ══════════════════════════════════════════════════════════════════════════
def t_session_isolation_group_vs_p2p():
    print('─' * 78)
    print('[9] 会话隔离：同一个人在「群」与「私聊」里的参数互不影响（三元组 key）')
    print('─' * 78)
    db = fresh_db('figu_grp_sess_')
    _, bot, sched, notif = new_bot(db, allowed={'oc_ok'})

    # 私聊里设「张数 3」
    bot._handle_prompt('ou_grace', '张数 3', chat_id='oc_dm_g', is_group=0)
    prefs_dm = db.feishu_prefs_get('feishu', 'oc_dm_g', 'ou_grace')
    check('私聊里设置生效', int(prefs_dm.get('n') or 0) == 3, f"实际 n={prefs_dm.get('n')!r}")

    prefs_grp = db.feishu_prefs_get('feishu', 'oc_ok', 'ou_grace')
    check('★ 群的参数**未被**私聊设置污染（key 含 chat_id）',
          (prefs_grp.get('n') in (None, '', 0)), f"实际 n={prefs_grp.get('n')!r}")

    # 群里的设置不该影响私聊
    bot._handle_prompt('ou_grace', '张数 2', chat_id='oc_ok', is_group=1)
    prefs_dm2 = db.feishu_prefs_get('feishu', 'oc_dm_g', 'ou_grace')
    prefs_grp2 = db.feishu_prefs_get('feishu', 'oc_ok', 'ou_grace')
    check('群里设置独立生效', int(prefs_grp2.get('n') or 0) == 2, f"实际 n={prefs_grp2.get('n')!r}")
    check('★ 群里设置**未**改到私聊', int(prefs_dm2.get('n') or 0) == 3,
          f"实际 n={prefs_dm2.get('n')!r}")


def main():
    print()
    t_whitelist_parsing()
    print()
    t_whitelist_gate()
    print()
    t_mention_judgement()
    print()
    t_group_end_to_end()
    print()
    t_p2p_unaffected()
    print()
    t_reply_routing()
    print()
    t_result_routing()
    print()
    t_dedup_before_group_filter()
    print()
    t_session_isolation_group_vs_p2p()

    print()
    print('=' * 78)
    print(f'汇总：通过 {len(PASS)} / 失败 {len(FAIL)} / 跳过 {len(SKIP)}')
    if SKIP:
        print('跳过项：')
        for s in SKIP:
            print('  -', s)
    if FAIL:
        print('失败项：')
        for f in FAIL:
            print('  -', f)
    print('=' * 78)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

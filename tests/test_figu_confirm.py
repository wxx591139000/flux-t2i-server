#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""飞书「图图」提交前确认回归门（第 23 道；离线，不要 GPU / 不真发飞书消息）。

背景（2026-09-28，v2.12.0，用户需求原话）：
  「给图图发消息时并不都是生图的提示词，建生图任务前要在对话里进行确认
    是否发送"XXXX"生图任务」

原行为：凡不是命令的文本**一律直接提交出图** —— 一次误发就真扣一次额度、
出一张毫不相干的图。现在改成两步：先回一条确认（带本次参数），用户回「是」
才建任务；回「否」放弃；不回则 5 分钟自动作废。

这道门要钉住的（每条都对应一个会真花钱 / 会真出错的点）：

  1. ★★ **确认流程本身绝不提交** —— 用户没回「是」之前，jobs 表必须一行都不增。
     这是本功能存在的全部理由；实现里任何"顺手提交"都会把它废掉。
  2. ★★ **确认词必须全等** ——「好可爱的一只猫」不能等于「好」，
     「不是这只猫」不能等于「不是」。判松了就把正常提示词吞了。
  3. ★★ **顺序**：命令 → 待选 → 确认 → 提交。确认若早于"待选消费"，
     用户回「是」会被当成新提示词 → 又弹一次确认 → **永远出不了图**。
  4. ★★ **参数快照**：确认消息里显示什么，提交时就用什么。否则用户中途改了
     张数就会出现「确认时说 2 张、实际出 3 张」—— 系统说的和做的不一致。
  5. **跨会话隔离**：群里 A 的待确认，B 回「是」无效（key 是三元组）。
  6. **可回退**：关掉开关就是旧行为（发一句直接出图），应急用。

替身策略：DB / 配额 / 调度器全部用**真实实现**（临时库），只有「飞书通知器」
是替身（否则会真发消息）；LLM 翻译函数只在需要它的段落临时打桩（离线无外网）。
用法：python tests/test_figu_confirm.py     （rc=0 通过）
"""
import os
import sys
import tempfile
import time
from pathlib import Path

# 沙箱会拦 ~/.ssh/config → 必须在 import 队列之前关掉自动发现
os.environ['FLUX_SERVER_DISCOVER'] = '0'

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []

CH = 'oc_chat1'
CH2 = 'oc_chat2'
U = 'ou_me'
U2 = 'ou_bob'
CAT = '一只橘猫坐在窗台上'


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✅' if cond else '❌'} {label}" + (f"    ({detail})" if detail else ''))


# ── 替身：只替"对外发消息"这一件事 ───────────────────────────────────
class StubNotifier:
    def __init__(self):
        self.app_id = 'stub_app'
        self.app_secret = 'stub_secret'
        self.owner_open_id = ''
        self.texts = []        # [(target, text)]  target 可能是 open_id 或 chat_id
        self.images = []

    def send_direct(self, user_id, text):
        self.texts.append((user_id, text))
        return True

    def send_to(self, chat_id, text):
        self.texts.append((chat_id, text))
        return True

    def upload_image(self, path):
        return 'img_key_stub'

    def send_image(self, user_id, image_key):
        self.images.append((user_id, image_key))
        return True

    def send_image_to(self, chat_id, image_key):
        self.images.append((chat_id, image_key))
        return True

    def containing(self, kw):
        return [t for _, t in self.texts if kw in t]


def fresh(confirm=True):
    """真实 FluxDB + 真实 QuotaService + 真实 FluxQueueScheduler；只有通知器是替身。

    `confirm=True`（默认）= 保留线上默认（确认开）；`confirm=False` 用于验证回退路径。
    """
    from manager.flux_db import FluxDB
    from manager.flux_quota import QuotaService
    from manager.flux_queue import FluxQueueScheduler
    import manager.feishu_bot as fb

    d = tempfile.mkdtemp(prefix='figu-confirm-')
    db = FluxDB(os.path.join(d, 't.db'))
    quota = QuotaService(db)
    sched = FluxQueueScheduler(db, quota)
    notif = StubNotifier()
    bot = fb.FeishuBot(sched, db, quota, notifier=notif)
    bot.confirm_enabled = confirm
    # 同步处理（不起工作线程），断言才确定
    bot._enqueue = lambda oid, text, chat_id='', is_group=0: bot._handle_prompt(
        oid, text, chat_id=chat_id, is_group=is_group)
    return fb, bot, db, sched, notif


def sub(bot, notif, text, sender=U, chat_id=CH, is_group=0):
    """把一条文本喂给机器人（走真实分派路径）。返回本次新增的回执列表。"""
    n0 = len(notif.texts)
    bot._handle_prompt(sender, text, chat_id=chat_id, is_group=is_group)
    return notif.texts[n0:]


def n_jobs(db, uid=U):
    row = db._one("SELECT COUNT(*) AS c FROM jobs WHERE user_id=?", (uid,))
    return int(row['c']) if row else 0


def last_job(db, uid=U):
    return db._one("SELECT * FROM jobs WHERE user_id=? ORDER BY rowid DESC LIMIT 1", (uid,))


def prefs(db, chat_id=CH, sender=U):
    return db.feishu_prefs_get('feishu', chat_id, sender)


def pending(db, chat_id=CH, sender=U):
    return db.feishu_pending_get('feishu', chat_id, sender)


def drain(db):
    """把在途跟踪全部置终态。

    必要性：`MAX_INFLIGHT_PER_USER = 1` —— 提交一个之后，不置终态就再提交不了。
    真实场景里"图出来（done）之后自然能再提交"，这里就是模拟那一刻。
    """
    for r in db.feishu_tracks_pending(limit=1000):
        db.feishu_track_set(r['job_id'], phase='done')


def stub_translate():
    """把外网 LLM 翻译换成纯英文桩，返回还原函数（离线跑不了真翻译）。"""
    import manager.flux_queue as fq
    real = fq.translate_to_flux_prompt
    fq.translate_to_flux_prompt = lambda p: 'an orange cat sitting on a windowsill'
    return lambda: setattr(fq, 'translate_to_flux_prompt', real)


# ══════════════════════════════════════════════════════════════
# [1] ★★ 确认词判据 —— 只认全等，绝不吞提示词
# ══════════════════════════════════════════════════════════════
def t_confirm_words():
    print()
    print('─' * 78)
    print('[1] ★★ 确认词判据（全等；判松了会吞掉正常提示词）')
    print('─' * 78)
    from manager import figu_state as fst

    yes = ['是', '是的', '对', '对的', '嗯', '确认', '确定', '好', '好的', '可以',
           '提交', '出图', '开始', 'OK', 'ok', 'Ok', 'yes', 'Y']
    for t in yes:
        check(f'{t!r} → 确认', fst.parse_confirm(t) is True, f'实际 {fst.parse_confirm(t)}')

    no = ['否', '不是', '不对', '不', '不用', '不要', '算了', '放弃', '取消', 'NO', 'n']
    for t in no:
        check(f'{t!r} → 放弃', fst.parse_confirm(t) is False, f'实际 {fst.parse_confirm(t)}')

    # ★★ 这些是**正常提示词**，一条都不能被当确认/放弃
    neutral = ['好可爱的一只猫', '是不是有点糊', '不是这只猫', '是只橘猫吧', '不规则的几何图形',
               '确认一下画面里的字', '嗯…再想想', '2', '16:9', '一只猫', '', '   ']
    for t in neutral:
        check(f'{t!r} → 都不是（按提示词走）', fst.parse_confirm(t) is None,
              f'实际 {fst.parse_confirm(t)}')

    check('CONFIRM_KIND 是稳定常量', fst.CONFIRM_KIND == 'confirm_submit', fst.CONFIRM_KIND)


# ══════════════════════════════════════════════════════════════
# [2] 线上默认必须是「开」
# ══════════════════════════════════════════════════════════════
def t_default_on():
    print()
    print('─' * 78)
    print('[2] 线上默认 = 开（用户明确要求的默认行为）')
    print('─' * 78)
    import manager.feishu_bot as fb

    check('★ 模块常量 CONFIRM_SUBMIT 为 True（不设环境变量时）',
          fb.CONFIRM_SUBMIT is True, f'实际 {fb.CONFIRM_SUBMIT}')
    fb2, bot, db, sched, notif = fresh()
    check('实例属性 confirm_enabled 默认继承常量',
          bot.confirm_enabled is True, f'实际 {bot.confirm_enabled}')


# ══════════════════════════════════════════════════════════════
# [3] ★★ 主流程：发提示词 → 弹确认 → 回「是」→ 才提交
# ══════════════════════════════════════════════════════════════
def t_flow_happy():
    print()
    print('─' * 78)
    print('[3] ★★ 主流程：提示词不再直接出图，要先确认')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()
    restore = stub_translate()
    try:
        out = sub(bot, notif, CAT)
        check('★★ 只发一句提示词 → **不提交**（jobs 仍为 0）', n_jobs(db) == 0, f'实际 {n_jobs(db)}')
        check('回执是确认请求（不是"正在排队生成"）',
              any('作为生图任务提交吗' in t for _, t in out) and not any('正在排队生成' in t for _, t in out),
              f'{out[-1][1][:50] if out else "（无回执）"}')
        txt = out[-1][1] if out else ''
        check('确认文案回显**原文**（用户能核对发的是不是这句）', CAT in txt, txt[:80])
        check('确认文案带**本次参数**（否则用户不知道会出几张/什么比例）',
              '本次参数：' in txt, txt[:120])
        check('确认文案写清怎么回（是/否）', '回「是」' in txt and '回「否」' in txt)
        check('★ 待确认状态已落库（重启不丢，且 kind 正确）',
              bool(pending(db)) and pending(db)['kind'] == 'confirm_submit',
              f"实际 {pending(db)['kind'] if pending(db) else None}")

        # —— 什么都不回：仍然不能出图
        sub(bot, notif, '（这句是别的话）')      # 不是是/否 → 作废旧确认、按新提示词弹新确认
        check('★★ 回别的 → 旧确认作废 + 新确认（全程 jobs 仍为 0）',
              n_jobs(db) == 0 and pending(db) is not None, f'jobs={n_jobs(db)}')

        # —— 回「是」才真提交
        out = sub(bot, notif, '是')
        check('★★ 回「是」→ 真的提交了 1 个任务', n_jobs(db) == 1, f'实际 {n_jobs(db)}')
        check('提交后用**原始中文**入库（可追溯）',
              last_job(db)['original_prompt'] == '（这句是别的话）',
              f"实际 {last_job(db)['original_prompt']!r}")
        check('回执告知已排队', bool(notif.containing('正在排队生成')))
        check('★ 提交后待确认被清掉（不会二次触发）', pending(db) is None,
              f'实际 {pending(db)}')
        # 再回一次「是」→ 没有待确认，会被当**新提示词**再弹一次确认（不是重复提交）
        n_before = n_jobs(db)
        sub(bot, notif, '是')
        check('★ 再回一次「是」→ 不重复提交（会当成新提示词重新确认）',
              n_jobs(db) == n_before, f'jobs {n_before}→{n_jobs(db)}')
    finally:
        restore()


# ══════════════════════════════════════════════════════════════
# [4] 回「否」= 放弃；且**绝不**动额度
# ══════════════════════════════════════════════════════════════
def t_decline():
    print()
    print('─' * 78)
    print('[4] 回「否」→ 放弃（不建任务、不扣额度）')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()
    sub(bot, notif, CAT)
    before_quota = bot.quota.usage_summary(U)
    out = sub(bot, notif, '否')
    check('★ 回「否」→ 没有建任务', n_jobs(db) == 0, f'实际 {n_jobs(db)}')
    check('回执确认已放弃', any('不出了' in t for _, t in out), f'{out[-1][1] if out else ""}')
    check('待确认已清', pending(db) is None, f'实际 {pending(db)}')
    check('额度口径未变（放弃不消耗）', bot.quota.usage_summary(U) == before_quota,
          f'{before_quota} → {bot.quota.usage_summary(U)}')


# ══════════════════════════════════════════════════════════════
# [5] 发新提示词 → 旧确认作废（不会出现"两个待确认"）
# ══════════════════════════════════════════════════════════════
def t_new_prompt_discards():
    print()
    print('─' * 78)
    print('[5] 用户改主意直接发新提示词 → 旧的作废，只留最新一条')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()
    restore = stub_translate()
    try:
        sub(bot, notif, '第一句：一只橘猫')
        sub(bot, notif, '第二句：一只柴犬')
        p = pending(db)
        check('★ 只保留一条待确认（调用方约定：一个会话同时一个）', p is not None)
        check('★ 待确认里存的是**最新**那句（不是第一句）',
              '柴犬' in (p['payload'] if p else ''), (p['payload'] if p else '')[:80])
        last = [t for _, t in notif.texts if '作为生图任务提交吗' in t][-1]
        check('确认文案回显最新那句', '柴犬' in last, last[:60])

        drain(db)
        sub(bot, notif, '是')
        row = last_job(db)
        check('★★ 回「是」提交的是**最新**那句（旧句不会被误提交）',
              row is not None and '柴犬' in (row['original_prompt'] or ''),
              f"实际 {row['original_prompt']!r}" if row else '无任务')
    finally:
        restore()


# ══════════════════════════════════════════════════════════════
# [6] ★★ 参数快照：确认时显示什么，提交就用什么
# ══════════════════════════════════════════════════════════════
def t_snapshot_params():
    print()
    print('─' * 78)
    print('[6] ★★ 参数快照（确认显示 3 张 → 实际必须 3 张，不许中途漂移）')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()
    restore = stub_translate()
    try:
        sub(bot, notif, '张数 3')
        sub(bot, notif, '比例 16:9')
        out = sub(bot, notif, CAT)
        txt = out[-1][1] if out else ''
        check('确认文案显示 3 张 + 比例 16:9',
              '3 张' in txt and '16:9' in txt, txt[:140])

        # ★ 绕过命令，直接改库（模拟"确认期间参数被改了"）——
        #   走命令的话会清待确认（见 [7]），测不到快照本身。
        db.feishu_prefs_set('feishu', CH, U, n=1, size='1:1')
        check('已把库里参数改成 1 张 / 1:1（但用户确认的是 3 张 / 16:9）',
              prefs(db)['n'] == 1 and prefs(db)['size'] == '1:1', f"prefs={prefs(db)}")

        sub(bot, notif, '是')
        rows = db._all("SELECT * FROM jobs WHERE user_id=? ORDER BY rowid ASC", (U,))
        check('★★ 实际提交 **3 张**（用快照，而不是库里的 1 张）', len(rows) == 3, f'实际 {len(rows)}')
        check('★★ 实际用 16:9 = 1280×720（用快照，而不是库里的 1:1）',
              all(r['width'] == 1280 and r['height'] == 720 for r in rows),
              f"实际 {[(r['width'], r['height']) for r in rows]}")
    finally:
        restore()


# ══════════════════════════════════════════════════════════════
# [7] ★★ 顺序：命令 > 待选 > 确认
# ══════════════════════════════════════════════════════════════
def t_order_commands_first():
    print()
    print('─' * 78)
    print('[7] ★★ 顺序硬要求：命令不被确认拦；确认不早于待选消费')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()

    out = sub(bot, notif, '比例 16:9')
    check('① 带参命令 → 直接设参数，**不弹确认**',
          not any('作为生图任务提交吗' in t for _, t in out) and prefs(db).get('size') == '16:9',
          f"size={prefs(db).get('size')}")
    check('① 命令不进 jobs（命令没被当提示词）', n_jobs(db) == 0, f'实际 {n_jobs(db)}')

    out = sub(bot, notif, '张数')
    check('② 不带参命令 → 弹**选项列表**（不是确认）',
          any('请选择张数' in t for _, t in out) and not any('作为生图任务提交吗' in t for _, t in out),
          f'{(out[-1][1] if out else "")[:50]}')
    check('② 待选 kind 是 count', (pending(db) or {}).get('kind') == 'count',
          f"实际 {(pending(db) or {}).get('kind')}")

    sub(bot, notif, '2')
    check('③ 回「2」→ 被**待选**消费（不是被确认吃掉）', prefs(db).get('n') == 2,
          f"n={prefs(db).get('n')}")
    check('③ 消费后待选清空', pending(db) is None, f'实际 {pending(db)}')

    # ★ 关键：有待选时发提示词 → 提示词处理完会弹确认（不是被待选吞掉）
    sub(bot, notif, '张数')
    out = sub(bot, notif, CAT)
    check('★★ 有"参数待选"时发提示词 → 待选作废 + 弹**确认**（提示词不被吞）',
          any('作为生图任务提交吗' in t for _, t in out), f'{(out[-1][1] if out else "")[:50]}')

    # ★ 反方向：有待确认时发命令 → 待确认作废 + 命令照常执行
    check('此刻确实有待确认', pending(db) is not None)
    out = sub(bot, notif, '帮助')
    check('★★ 有待确认时发命令 → 命令照常执行（不被当成"是/否"）',
          any('图图 · 生图助手' in t for _, t in out), f'{(out[-1][1] if out else "")[:40]}')
    check('命令执行后待确认被清（用户已翻篇）', pending(db) is None, f'实际 {pending(db)}')


# ══════════════════════════════════════════════════════════════
# [8] ★★ 跨会话隔离（三元组 key）
# ══════════════════════════════════════════════════════════════
def t_isolation():
    print()
    print('─' * 78)
    print('[8] ★★ 跨会话隔离：别人的「是」不能替 A 确认')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()
    restore = stub_translate()
    try:
        sub(bot, notif, CAT, sender=U, chat_id=CH, is_group=1)
        check('A 的待确认落在 (chat1, A)', pending(db, CH, U) is not None)

        sub(bot, notif, '是', sender=U2, chat_id=CH, is_group=1)
        check('★★ 群里 **B 回「是」** → 不消费 A 的待确认，A 的仍在那儿',
              pending(db, CH, U) is not None and n_jobs(db, U) == 0,
              f'jobs(A)={n_jobs(db, U)}')
        check('B 的「是」被当成 B 自己的提示词 → 给 B 弹了确认',
              pending(db, CH, U2) is not None, f'B 待确认={pending(db, CH, U2) is not None}')

        sub(bot, notif, '是', sender=U, chat_id=CH2)
        check('★★ **同一个人在别的会话** 回「是」 → 也不消费',
              pending(db, CH, U) is not None and n_jobs(db, U) == 0,
              f'jobs(A)={n_jobs(db, U)}')

        sub(bot, notif, '是', sender=U, chat_id=CH)
        check('★★ 本人在**原会话**回「是」 → 正常提交',
              n_jobs(db, U) == 1, f'实际 {n_jobs(db, U)}')
    finally:
        restore()


# ══════════════════════════════════════════════════════════════
# [9] 过期作废（TTL）
# ══════════════════════════════════════════════════════════════
def t_expired():
    print()
    print('─' * 78)
    print('[9] 待确认超时（5 分钟）→ 回「是」不再提交')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()
    sub(bot, notif, CAT)
    p = pending(db, CH, U)
    check('待确认已登记', p is not None)
    # 把 expires_at 推到过去（等价于 5 分钟后）
    db._exec('UPDATE feishu_pending SET expires_at=? WHERE channel=? AND chat_id=? AND sender_id=?',
             (int(time.time()) - 1, 'feishu', CH, U))
    check('★ 已过期的待确认查不出来（DB 侧按 expires_at 过滤）', pending(db, CH, U) is None)
    out = sub(bot, notif, '是')
    check('★★ 过期后回「是」→ 不提交（会被当新提示词再确认）', n_jobs(db) == 0, f'实际 {n_jobs(db)}')
    check('过期后回「是」也不会静默丢弃（仍给回执）', len(out) >= 1, f'{len(out)} 条')


# ══════════════════════════════════════════════════════════════
# [10] 在途上限：确认前就拦（不让用户白等）
# ══════════════════════════════════════════════════════════════
def t_inflight_guard():
    print()
    print('─' * 78)
    print('[10] 在途超限 → 发提示词时就被拦（不发确认、不白等）')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()
    restore = stub_translate()
    try:
        # 造一个在途：直接落库 + 队列提交
        r = sched.submit(U, 'an orange cat', width=1024, height=1024)
        db.feishu_track_add(r['job_id'], U, chat_id=CH, is_group=0, prompt=CAT, phase='queued')
        check('已存在 1 个在途（= 每用户上限）',
              db.feishu_tracks_inflight(sender_id=U) == 1,
              f'实际 {db.feishu_tracks_inflight(sender_id=U)}')

        n_before = n_jobs(db)
        out = sub(bot, notif, '再出一只柴犬')
        check('★★ 超限时**不发确认**（直接回"还在生成中"）',
              not any('作为生图任务提交吗' in t for _, t in out),
              f'{(out[-1][1] if out else "")[:50]}')
        check('超限时也没有提交', n_jobs(db) == n_before, f'{n_before}→{n_jobs(db)}')
        check('超限时不留待确认（避免用户回「是」后才发现超限）', pending(db) is None,
              f'实际 {pending(db)}')
    finally:
        restore()


# ══════════════════════════════════════════════════════════════
# [11] 回退开关（应急）
# ══════════════════════════════════════════════════════════════
def t_switch_off():
    print()
    print('─' * 78)
    print('[11] 关掉开关 → 旧行为（发一句直接出图）')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh(confirm=False)
    restore = stub_translate()
    try:
        sub(bot, notif, CAT)
        check('★ confirm_enabled=False → 直接提交（旧行为回归保护）',
              n_jobs(db) == 1, f'实际 {n_jobs(db)}')
        check('不弹确认', not any('作为生图任务提交吗' in t for _, t in notif.texts),
              f'{notif.containing("作为生图任务提交吗")}')
    finally:
        restore()


# ══════════════════════════════════════════════════════════════
# [12] 群聊：确认请求发到群里并 @ 发起人
# ══════════════════════════════════════════════════════════════
def t_group_route():
    print()
    print('─' * 78)
    print('[12] 群聊：确认请求落在**群**里且 @ 发起人（不是私聊）')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()
    bot.allowed_chats = {CH}
    bot.bot_open_id = 'ou_bot'
    out = sub(bot, notif, CAT, sender=U, chat_id=CH, is_group=1)
    check('确认请求发到**群**（而不是私聊）',
          bool(out) and out[-1][0] == CH, f'实际发往 {out[-1][0] if out else "（无）"}')
    check('确认请求 @ 了发起人（群里知道在问谁）',
          bool(out) and '<at user_id="ou_me">' in out[-1][1], f'{(out[-1][1] if out else "")[:60]}')


def main():
    t_confirm_words()
    t_default_on()
    t_flow_happy()
    t_decline()
    t_new_prompt_discards()
    t_snapshot_params()
    t_order_commands_first()
    t_isolation()
    t_expired()
    t_inflight_guard()
    t_switch_off()
    t_group_route()

    print()
    print('=' * 78)
    print(f'汇总：通过 {len(PASS)}，失败 {len(FAIL)}')
    if FAIL:
        for f in FAIL:
            print(f'  ❌ {f}')
        return 1
    print('✅ 全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())

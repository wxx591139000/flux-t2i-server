#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""飞书「图图」参数状态机回归门（第 21 道；离线，不要 GPU / 不真发飞书消息）。

背景（2026-09-28，v2.11.0）：P1 剩余的图图命令面 ——
`比例`/`尺寸`（/size）、`张数`（/n）、`模型`（/model）、`绑定`（/bind）、`额度`（/quota），
以及**待选状态机**（发「比例」不带参数 → 列编号选项 → 用户回「2」→ 生效）。

为什么这道门必须存在（每一条都对应一个会真花钱 / 会真出错的点）：

  1. ★★ **命令识别必须早于提示词提交**。图图的主用法是"发一句话出图"，
     若把「比例 16:9」当提示词提交 → **真扣额度出一张废图**。
  2. ★★ **不能吞掉提示词**。「比例协调的画面」「模型感很强的一张照片」
     「张数很多的画面」都是完全可能的中文提示词；判据必须**整体匹配**才算命令。
  3. ★★ **待选状态的 key 必须是三元组 (channel, chat_id, sender_id)**。
     小白用二元组 (channel, sender_id)，后来只能靠"跨会话校验 + 冷却"打补丁
     （transcribe-bot `orchestrator.py:380-383/:470`）。这里直接断言
     「同一个人在别的会话回 2」「别人在同一个会话回 2」**都不能消费**。
  4. **参数必须真的生效** —— 断言 jobs 表里落地的 width/height/model/seed，
     而不是只断言"偏好存下来了"（存了不用 = 装饰）。
  5. **多图只翻译一次** —— n×LLM 调用既拖住工作线程，又因翻译漂移让"候选"
     变成几张不相干的图。

替身策略：**DB / 配额 / 调度器全部用真实实现**（临时库），只有「飞书通知器」是替身
（否则会真发消息）。与 `test_feishu_bot_cancel.py`、`test_delete_job.py` 同一取舍。
唯一额外打桩的是 **LLM 翻译函数**（外网依赖，离线跑不了），且只在需要它的那一段
临时替换、离开即还原。

用法：python tests/test_figu_params.py     （rc=0 通过）
"""
import json
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

CH = 'oc_chat1'        # 默认会话的 chat_id
CH2 = 'oc_chat2'
U = 'ou_me'


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
        self.images = []       # [(target, image_key)]

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

    def last(self):
        return self.texts[-1][1] if self.texts else ''

    def containing(self, kw):
        return [t for _, t in self.texts if kw in t]


def fresh():
    """真实 FluxDB + 真实 QuotaService + 真实 FluxQueueScheduler；只有通知器是替身。"""
    from manager.flux_db import FluxDB
    from manager.flux_quota import QuotaService
    from manager.flux_queue import FluxQueueScheduler
    import manager.feishu_bot as fb

    d = tempfile.mkdtemp(prefix='figu-params-')
    db = FluxDB(os.path.join(d, 't.db'))
    quota = QuotaService(db)
    sched = FluxQueueScheduler(db, quota)
    notif = StubNotifier()
    bot = fb.FeishuBot(sched, db, quota, notifier=notif)
    # ★ 本门只覆盖**命令面与参数落库**，所以显式关掉「提交前确认」（v2.12.0 起默认开）。
    #   不关的话每条提示词都会先弹一次确认，命令面的断言要处处跟着改成两步。
    #   确认流程由专门的门 `tests/test_figu_confirm.py` 覆盖 —— 那道门里也钉住了
    #   「线上默认是开」这个事实（断言 `fb.CONFIRM_SUBMIT is True`）。
    bot.confirm_enabled = False
    # 同步处理（不起工作线程），断言才确定
    bot._enqueue = lambda oid, text, chat_id='', is_group=0: bot._handle_prompt(
        oid, text, chat_id=chat_id, is_group=is_group)
    return fb, bot, db, sched, notif


def sub(bot, notif, text, sender=U, chat_id=CH, is_group=0):
    """把一条文本喂给机器人（走真实分派路径）。返回本次新增的回执。"""
    n0 = len(notif.texts)
    bot._handle_prompt(sender, text, chat_id=chat_id, is_group=is_group)
    return notif.texts[n0:]


def qsize(sched):
    return sched._pq.qsize()


def drain(db):
    """把在途跟踪全部置终态。

    必要性：`MAX_INFLIGHT_PER_USER = 1`（每用户只允许 1 个在途），
    所以同一个用户连续提交两次，第二次会被**正确地**挡住。
    真实场景里"图出来（done）之后自然能再提交"，这里就是模拟那一刻。
    """
    for r in db.feishu_tracks_pending(limit=1000):
        db.feishu_track_set(r['job_id'], phase='done')


def last_job(db, uid=U):
    return db._one("SELECT * FROM jobs WHERE user_id=? ORDER BY rowid DESC LIMIT 1", (uid,))


def prefs(db, chat_id=CH, sender=U):
    return db.feishu_prefs_get('feishu', chat_id, sender)


def pending(db, chat_id=CH, sender=U):
    return db.feishu_pending_get('feishu', chat_id, sender)


# ══════════════════════════════════════════════════════════════
# [1] ★★ 命令识别 —— 既不吞提示词，也不让命令被当提示词
# ══════════════════════════════════════════════════════════════
def t_command_parsing():
    print()
    print('─' * 78)
    print('[1] 命令识别（安全红线：错一个方向就白扣额度 / 吞掉提示词）')
    print('─' * 78)
    import manager.feishu_bot as fb

    must_be_cmd = [
        ('帮助', 'help'), ('/help', 'help'),
        ('我的任务', 'list'), ('取消', 'list'),
        ('额度', 'quota'), ('/quota', 'quota'),
        ('参数', 'params'),
        ('比例', 'size'), ('尺寸', 'size'), ('画幅', 'size'), ('/size', 'size'),
        ('张数', 'count'), ('数量', 'count'), ('/n', 'count'), ('/count', 'count'),
        ('模型', 'model'), ('/model', 'model'),
        ('绑定', 'bind'), ('/bind', 'bind'),
    ]
    for text, name in must_be_cmd:
        got = fb._match_command(text)
        check(f'{text!r} → {name}', bool(got) and got[0] == name, f'实际 {got}')

    with_arg = [
        ('比例 16:9', ('size', '16:9')), ('/size 9:16', ('size', '9:16')),
        ('尺寸 4:3', ('size', '4:3')), ('比例 3:4', ('size', '3:4')),
        ('张数 2', ('count', '2')), ('数量 4', ('count', '4')), ('/n 3', ('count', '3')),
        ('模型 klein', ('model', 'FLUX.2-klein-4B')),
        ('模型 dev', ('model', 'FLUX.1-dev')), ('/model qwen', ('model', 'Qwen-Image-2.1')),
        ('绑定 abcdefgh', ('bind', 'ABCDEFGH')),
        # 取消那套不能被新命令破坏
        ('取消 01', ('cancel', '01')), ('取消 3ada124e', ('cancel', '3ada124e')),
        ('@_user_1 取消 01', ('cancel', '01')),
    ]
    for text, want in with_arg:
        got = fb._match_command(text)
        check(f'{text!r} → {want}', got == want, f'实际 {got}')

    # ★★ 最关键的一组：这些**必须**回落成提示词（吞掉 = 用户拿不到图还白扣额度）
    must_be_prompt = [
        # 取消那套（v2.10.0 就有的红线）
        '取消一切杂乱背景，画面干净',
        '取消背景的杂物，只留主体',
        '取消abc',
        # v2.11.0 新增命令带来的新风险面
        '比例协调的画面',
        '尺寸要标准的产品图',
        '张数很多的画面',
        '数量很多的产品',
        '模型感很强的一张照片',
        '绑定关系要清晰',
        '额度不够怎么办',
        '参数很复杂的机械结构',
        '比例 16:9 的画面',          # ★ 参数必须整体匹配，不能只取第一个 token
        '模型 klein 风格',
        'size of the room, minimal',  # 拉丁命令名不带斜杠 → 不吃参数
        'nature 2 只鹿',
        '一只橘猫坐在窗台上，阳光洒落',
        '2', '16:9',
    ]
    for text in must_be_prompt:
        got = fb._match_command(text)
        check(f'★★ {text!r} 不被当命令（回落为提示词）', got is None, f'实际 {got} ← 会被误吞')

    # 括号边界：序号最多 3 位、短码 6~16 位十六进制（与 v2.10.0 一致）
    check('「取消 0001」超 3 位 → 不是命令', fb._match_command('取消 0001') is None)
    check('「张数 5」超范围 → 回落为提示词', fb._match_command('张数 5') is None)
    check('「比例 2:1」不在选项里 → 回落为提示词', fb._match_command('比例 2:1') is None)


# ══════════════════════════════════════════════════════════════
# [2] 参数持久化 + **真的生效**（断言 jobs 表，不断言文案）
# ══════════════════════════════════════════════════════════════
def t_params_effective():
    print()
    print('─' * 78)
    print('[2] ★★ 参数不只是"存着看"：断言它真的落到了 jobs 表')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()

    sub(bot, notif, '比例 16:9')
    check('「比例 16:9」写入会话默认值', prefs(db).get('size') == '16:9', f'prefs={prefs(db)}')
    check('回执确认已设置', bool(notif.containing('已设置比例')), notif.last()[:50])

    # 用**英文**提示词提交：不经过 LLM，断言才确定（中文路径另在 [6] 里用打桩验证）
    sub(bot, notif, 'a red apple on a wooden table')
    row = last_job(db)
    check('★★ 下一张图按 1280×720 提交（比例真的生效了）',
          row is not None and row['width'] == 1280 and row['height'] == 720,
          f"width={row['width'] if row else None} height={row['height'] if row else None}")

    # 改比例 → 再提交 → 尺寸跟着变（防"只在第一次生效"这类一次性实现）
    drain(db)
    sub(bot, notif, '比例 9:16')
    sub(bot, notif, 'a blue cup on a desk')
    row = last_job(db)
    check('★★ 改比例后新图跟着变（720×1280）',
          row is not None and row['width'] == 720 and row['height'] == 1280,
          f"width={row['width'] if row else None}")

    # 重置为默认
    drain(db)
    sub(bot, notif, '比例 1:1')
    sub(bot, notif, 'a yellow hat')
    row = last_job(db)
    check('比例切到 1:1 → 1024×1024',
          row is not None and row['width'] == 1024 and row['height'] == 1024,
          f"width={row['width'] if row else None}")

    # 张数默认 1（不设时不能莫名其妙出 4 张）
    check('未设置张数时默认 1 张', (prefs(db).get('n') or 1) == 1)

    # 「参数」命令回显
    sub(bot, notif, '参数')
    txt = notif.last()
    check('「参数」回显当前设置', '1:1' in txt and '张数' in txt, txt[:60])


# ══════════════════════════════════════════════════════════════
# [3] ★★ 待选状态机：无参数命令 → 登记待选 → 用回复消费
# ══════════════════════════════════════════════════════════════
def t_pending_state_machine():
    print()
    print('─' * 78)
    print('[3] ★★ 待选状态机（发「比例」→ 列选项 → 回「2」→ 生效）')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()

    n0 = qsize(sched)
    sub(bot, notif, '比例')
    p = pending(db)
    check('「比例」无参数 → 登记待选状态（落 DB，重启不丢）',
          bool(p) and p['kind'] == 'size', f'实际 {p}')
    txt = notif.last()
    check('回执列出编号选项', '1. 1:1' in txt and '3. 9:16' in txt, txt[:70].replace('\n', ' | '))
    check('回执提示有效时长', '分钟' in txt)
    check('登记待选本身不产生提交（不能顺手出图）', qsize(sched) == n0, f'队列 {n0} → {qsize(sched)}')

    sub(bot, notif, '2')
    check('★★ 回「2」→ 比例设为 16:9', prefs(db).get('size') == '16:9', f'prefs={prefs(db)}')
    check('消费后待选被清空（不会二次消费）', pending(db) is None)
    check('★★ 消费待选**不产生提交**（回 2 不能去出一张"2"的图）',
          qsize(sched) == n0, f'队列 {n0} → {qsize(sched)}')

    # 回选项标签本身也认
    sub(bot, notif, '张数')
    check('「张数」登记 count 待选', (pending(db) or {}).get('kind') == 'count')
    sub(bot, notif, '3')
    check('回「3」→ 张数设为 3', prefs(db).get('n') == 3, f'prefs={prefs(db)}')

    sub(bot, notif, '模型')
    check('「模型」登记 model 待选', (pending(db) or {}).get('kind') == 'model')
    sub(bot, notif, 'qwen')
    check('★ 回**选项标签**（qwen）也能消费',
          prefs(db).get('model') == 'Qwen-Image-2.1', f'prefs={prefs(db)}')

    # 同一会话只保留一个待选（否则回「2」到底选了哪个？）
    sub(bot, notif, '比例')
    sub(bot, notif, '张数')
    check('★ 新待选覆盖旧待选（同一会话不并存，避免歧义）',
          (pending(db) or {}).get('kind') == 'count')


# ══════════════════════════════════════════════════════════════
# [4] ★★ 跨会话隔离 —— 三元组 key 的核心价值
# ══════════════════════════════════════════════════════════════
def t_cross_session_isolation():
    print()
    print('─' * 78)
    print('[4] ★★ 跨会话隔离（比小白强的地方：不是靠补丁，是靠 key 本身）')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()

    sub(bot, notif, '张数', sender=U, chat_id=CH)
    check('前置：A 在会话 1 有待选', pending(db, CH, U) is not None)

    # ① 同一个人在**另一个会话**回 2
    drain(db)
    sub(bot, notif, '2', sender=U, chat_id=CH2)
    check('★★ 同一个人在别的会话回「2」→ 不消费会话 1 的待选',
          pending(db, CH, U) is not None)
    check('★ 也没有把参数设置到会话 2', not prefs(db, CH2, U).get('n'),
          f'会话2 prefs={prefs(db, CH2, U)}')

    # ② 另一个人在**同一个会话**回 2
    drain(db)
    sub(bot, notif, '2', sender='ou_other', chat_id=CH)
    check('★★ 别人在同一个会话回「2」→ 不消费 A 的待选（三元组隔离）',
          pending(db, CH, U) is not None)
    check('★ 也没有给别人设置参数', not prefs(db, CH, 'ou_other').get('n'))

    # ③ 本人回 → 正常消费
    sub(bot, notif, '2', sender=U, chat_id=CH)
    check('本人回「2」→ 正常消费', prefs(db, CH, U).get('n') == 2, f'prefs={prefs(db, CH, U)}')


# ══════════════════════════════════════════════════════════════
# [5] 守卫：命令优先 / 非选项不消费 / 超时不消费
# ══════════════════════════════════════════════════════════════
def t_guards():
    print()
    print('─' * 78)
    print('[5] 守卫（每一条都是小白踩过并写进 PITFALLS 的）')
    print('─' * 78)
    import manager.feishu_bot as fb
    from manager import figu_state as fst

    fb_, bot, db, sched, notif = fresh()

    # ① 未知斜杠写法：命令优先放行，不消费待选，也不当提示词
    sub(bot, notif, '比例')
    check('前置：待选已登记', pending(db) is not None)
    n0 = qsize(sched)
    sub(bot, notif, '/whatever')
    check('★ 未知斜杠写法不消费待选（命令优先放行）', pending(db) is not None)
    check('★ 也没有被当提示词提交', qsize(sched) == n0, f'队列 {n0} → {qsize(sched)}')
    check('未知斜杠给出用法指引', bool(notif.containing('直接发提示词')))

    # ② 已登记的命令会清掉待选（= 设计方案里的 /cancel 语义，不用再单开命令）
    sub(bot, notif, '帮助')
    check('★ 命令会清掉待选（等价 /cancel 语义）', pending(db) is None)

    # ③ 非选项 → 不消费，回落为提示词（真的提交）
    sub(bot, notif, '比例')
    n1 = qsize(sched)
    sub(bot, notif, 'a minimalist white sneaker')
    check('★★ 待选中收到正常提示词 → 不消费，照常提交',
          qsize(sched) == n1 + 1, f'队列 {n1} → {qsize(sched)}')
    check('★ 待选仍在（没被提示词吃掉）', pending(db) is not None)
    check('★ 也没有把参数设成提示词', not prefs(db).get('size'), f'prefs={prefs(db)}')

    # ④ 序号越界同理：不算"选择"
    drain(db)
    n2 = qsize(sched)
    sub(bot, notif, '9')
    check('★ 序号越界（9）→ 不消费，按提示词处理',
          pending(db) is not None and qsize(sched) == n2 + 1)

    # ⑤ 超时（ttl=0 注入）：DB 层就查不到，自然不消费
    fb2, bot2, db2, sched2, notif2 = fresh()
    db2.feishu_pending_set('feishu', CH, U, 'size', {'options': list(fst.SIZE_LABELS)}, ttl=0)
    check('前置：ttl=0 的待选立刻过期（查不到）', pending(db2) is None)
    sub(bot2, notif2, '2')
    check('★ 过期待选不会被消费（回落为提示词）', not prefs(db2, CH, U).get('size'))

    # ⑥ 纯函数守卫直接钉（这几条不依赖任何 IO，最适合单元化）
    check('空文本不消费', fst.should_consume('') == (False, 'empty'))
    check('斜杠开头不消费', fst.should_consume('/n 2') == (False, 'slash'))
    check('上传后冷却期内不消费（P3 接图片后真正生效）',
          fst.should_consume('2', now=1004.0, uploaded_at=1000.0) == (False, 'cooldown'))
    check('冷却期外可以消费', fst.should_consume('2', now=1006.0, uploaded_at=1000.0) == (True, ''))
    check('三元组 key：缺 chat_id 时用 open_id 兜底，不会退化成二元组',
          fst.session_key('', 'ou_x') != fst.session_key('', 'ou_y'))
    check('★ 同一 (chat, sender) 的 key 稳定（默认 channel = feishu）',
          fst.session_key(CH, U) == ('feishu', CH, U))


# ══════════════════════════════════════════════════════════════
# [6] ★★ 多图：一次翻译、n 张候选
# ══════════════════════════════════════════════════════════════
def t_multi_image():
    print()
    print('─' * 78)
    print('[6] ★★ /n 多图（一次翻译多张候选 —— 各自翻译会让"候选"变成不相干的图）')
    print('─' * 78)
    import manager.flux_queue as fq

    fb, bot, db, sched, notif = fresh()
    calls = []
    real = fq.translate_to_flux_prompt
    # 只打桩"外网 LLM 翻译"这一件事（离线跑不了）；返回**纯英文**，
    # 否则第 2..n 张会因为 base 里含中文而被再翻译一次，测的就不是实现了。
    fq.translate_to_flux_prompt = lambda p: (calls.append(p),
                                            'an orange cat sitting on a windowsill')[1]
    try:
        sub(bot, notif, '张数 3')
        check('「张数 3」写入默认值', prefs(db).get('n') == 3, f'prefs={prefs(db)}')

        sub(bot, notif, '一只橘猫坐在窗台上')
        rows = db._all("SELECT * FROM jobs WHERE user_id=? ORDER BY rowid ASC", (U,))
        check('一句提示词提交 3 个任务', len(rows) == 3, f'实际 {len(rows)}')
        check('★★ 中文只翻译 1 次（n×LLM 会拖住工作线程，也让候选不是同一句）',
              len(calls) == 1, f'实际翻译 {len(calls)} 次')
        check('★★ 第 2..n 张复用第 1 张**落库**的英文提示词',
              len({r['prompt'] for r in rows}) == 1,
              f"实际 {[r['prompt'][:24] for r in rows]}")
        check('任务 1 记下用户原始中文（可追溯）',
              rows[0]['original_prompt'] == '一只橘猫坐在窗台上')
        seeds = {r['seed'] for r in rows}
        check('★★ n 张的 seed 各不相同（去重键含 seed，全传 None 会被自己挡住）',
              len(seeds) == 3 and None not in seeds, f'seeds={seeds}')
        check('回执告知会出几张', bool(notif.containing('本次会出 3 张')),
              notif.containing('本次会出')[0][:40] if notif.containing('本次会出') else '')
        check('回执汇总任务号', bool(notif.containing('任务号')))
    finally:
        fq.translate_to_flux_prompt = real

    # 张数改回 1 → 只 1 张，且 seed 交回服务端（None = 服务端随机并回填）
    drain(db)
    sub(bot, notif, '张数 1')
    sub(bot, notif, 'a green bottle')
    row = last_job(db)
    check('张数改回 1 → 只提交 1 张且 seed 为 None（交回服务端）',
          row is not None and row['seed'] is None, f"seed={row['seed'] if row else None}")


# ══════════════════════════════════════════════════════════════
# [7] 模型选择
# ══════════════════════════════════════════════════════════════
def t_model():
    print()
    print('─' * 78)
    print('[7] 模型选择（别名 → model id，且必须真的透传）')
    print('─' * 78)
    import manager.feishu_bot as fb
    from manager import figu_state as fst

    fb_, bot, db, sched, notif = fresh()

    sub(bot, notif, '模型 klein')
    check('「模型 klein」→ 存的是 **model id** 而不是别名',
          prefs(db).get('model') == 'FLUX.2-klein-4B', f'prefs={prefs(db)}')
    sub(bot, notif, 'a wooden chair')
    row = last_job(db)
    check('★★ model 真的透传到 jobs.model（选机靠它：dev/klein/qwen 在不同机器上）',
          row is not None and row['model'] == 'FLUX.2-klein-4B',
          f"model={row['model'] if row else None}")
    check('回执用别名回显（便于用户复述）', bool(notif.containing('klein')))

    drain(db)
    sub(bot, notif, '模型 FLUX.1-dev')
    check('★ 也接受完整 model id', prefs(db).get('model') == 'FLUX.1-dev', f'prefs={prefs(db)}')

    # 别名 → id 的映射只此一处（figu_state）
    check('三个别名都有映射', set(fst.MODEL_ALIASES) == {'klein', 'dev', 'qwen'})
    check('认不出的模型名不是命令（回落为提示词）', fb._match_command('模型 乱七八糟') is None)
    check('认不出时偏好未被改动', prefs(db).get('model') == 'FLUX.1-dev')


# ══════════════════════════════════════════════════════════════
# [8] 绑定激活码 + 额度
# ══════════════════════════════════════════════════════════════
def t_bind_and_quota():
    print()
    print('─' * 78)
    print('[8] ★★ /bind 绑定（决策 2：飞书身份与网页账户**共享同一份额度**，不做两套账）')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()
    from manager.flux_quota import current_ym

    code = db.code_generate('pro', n=1)[0]
    check('前置：生成一个未使用的激活码', db.account_get(code) is None)

    sub(bot, notif, f'绑定 {code}', sender='ou_bind', chat_id=CH)
    check('★★ 绑定成功并建了账户', db.account_get(code) is not None)
    check('★★ open_id 被并入该账户（不是两套账）',
          db.account_id_of('ou_bind') == code, f"account_id={db.account_id_of('ou_bind')}")
    check('★★ 套餐按激活码生效（pro）', db.get_user('ou_bind')['plan'] == 'pro')
    check('回执照实说"激活成功"', bool(notif.containing('激活成功')), notif.last()[:40])
    check('回执带额度（用户能自己验证）', bool(notif.containing('本月已用')))

    # 第二台"设备"（另一个 open_id）绑同一个码 → 并入，不新建
    sub(bot, notif, f'绑定 {code}', sender='ou_bind2', chat_id=CH)
    check('★ 同一个码再绑 → 并入已有账户（不是新建）',
          db.account_id_of('ou_bind2') == code and bool(notif.containing('已有账户')))

    # 额度真的共享：一个 open_id 用了 2 张 → 账户口径也能看到
    ym = current_ym()
    db.usage_add('ou_bind', ym, 2)
    check('★★ 额度按账户聚合（两个 open_id 共享同一份用量）',
          db.account_usage(code, ym) == 2, f'account_usage={db.account_usage(code, ym)}')

    # 无效码：明确失败，不留半截状态
    sub(bot, notif, '绑定 ZZZZZZZZ', sender='ou_bind3', chat_id=CH)
    check('★★ 无效激活码 → 明确失败（不建账户、不静默）',
          db.account_get('ZZZZZZZZ') is None and bool(notif.containing('无效')),
          notif.last()[:40])
    check('失败时用户仍是未绑定状态', db.account_id_of('ou_bind3') == '')

    # 裸「绑定」给用法而不是报错
    sub(bot, notif, '绑定', sender='ou_bind3', chat_id=CH)
    check('裸「绑定」给用法而不是报错', bool(notif.containing('用法')), notif.last()[:40])

    # 「额度」命令
    sub(bot, notif, '额度', sender='ou_bind', chat_id=CH)
    t = notif.last().replace('\n', ' | ')
    check('「额度」显示套餐与绑定状态（owner/已绑定/未绑定三态）',
          'pro' in t and '已绑定账户' in t, t[:80])
    sub(bot, notif, '额度', sender='ou_unbound', chat_id=CH)
    check('未绑定用户被明确提示"未绑定"', '未绑定' in notif.last(), notif.last()[:60].replace('\n', ' | '))


# ══════════════════════════════════════════════════════════════
# [9] ★★ 真缺陷回归：取消过的任务不能再算"在途"
# ══════════════════════════════════════════════════════════════
def t_cancelled_not_inflight():
    print()
    print('─' * 78)
    print('[9] ★★ 真缺陷回归：cancelled 必须算终态（第 18 号陷阱）')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()
    from manager.flux_db import FEISHU_TERMINAL_PHASES

    check('终态集合是单一事实源且含 cancelled',
          set(FEISHU_TERMINAL_PHASES) == {'done', 'failed', 'cancelled'})

    def n_jobs():
        return len(db._all("SELECT job_id FROM jobs WHERE user_id=?", (U,)))

    sub(bot, notif, 'a green bottle')
    row = last_job(db)
    jid = row['job_id'] if row else ''
    check('前置：提交成功且被跟踪', bool(jid) and db.feishu_track_get(jid) is not None)

    sub(bot, notif, f'取消 {jid[:8]}')
    check('取消成功（墓碑已打）', db.job_get(jid)['deleted_at'] is not None)
    check('跟踪 phase → cancelled', db.feishu_track_get(jid)['phase'] == 'cancelled')
    check('★★ 取消过的任务**不再计入在途**（漏了这条 = 用户取消一次就再也提交不了）',
          db.feishu_tracks_inflight(sender_id=U) == 0,
          f'inflight={db.feishu_tracks_inflight(sender_id=U)}')

    n0 = n_jobs()
    sub(bot, notif, 'a silver spoon')
    check('★★ 取消之后仍能提交新任务（v2.10.0 的真缺陷）',
          n_jobs() == n0 + 1, f'{n0} → {n_jobs()}')
    check('新任务没被误判成重复提交（drop_job 释放了去重键）',
          not bool(notif.containing('重复提交')))

    check('★ GC 能清理 cancelled 行（原先只清 done/failed → 表只增不减）',
          db.feishu_track_gc(keep_seconds=-1) >= 1)


def main():
    t_command_parsing()
    t_params_effective()
    t_pending_state_machine()
    t_cross_session_isolation()
    t_guards()
    t_multi_image()
    t_model()
    t_bind_and_quota()
    t_cancelled_not_inflight()

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


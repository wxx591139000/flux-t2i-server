#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""飞书「图图」取消任务回归门（第 20 道；离线，不要 GPU / 不真发飞书消息）。

背景（2026-09-24）：用户问「像小白一样有取消任务的能力吗」——
查下来底层**早就有了**（`/api/delete` + `queue.drop_job` + 墓碑机制，2026-09-21
为网页端写的），缺的只是**图图这一层的命令面**。本门把这一层钉住。

为什么这道门必须存在（两条都是"会真花钱 / 会真出错"的）：
  1. ★★ **命令识别必须早于提示词提交**。图图的主用法是"发一句话出图"，
     要是把「取消 01」当提示词提交，就**真的扣一次额度、出一张毫不相干的图** ——
     用户想撤回反而多花一次。所以这里断言的是「提交次数」，不是文案。
  2. ★★ **不能吞掉以"取消"开头的正常提示词**。用户完全可能发
     「取消一切杂乱背景，画面干净」。判据必须严格到"取消 + 序号/短码"才算命令。

替身策略：**DB 与调度器用真实实现**（临时库），只有「飞书通知器」是替身
（否则会真发消息）。这样断言的才是上生产的代码，而不是自证的替身 ——
与 `test_delete_job.py` 同一取舍。

用法：python tests/test_feishu_bot_cancel.py     （rc=0 通过）
"""
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

# 沙箱会拦 ~/.ssh/config → 必须在 import 队列之前关掉自动发现
os.environ['FLUX_SERVER_DISCOVER'] = '0'

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✅' if cond else '❌'} {label}" + (f"    ({detail})" if detail else ''))


# ── 替身（只替"对外发消息"这一件事）────────────────────────────────────
class StubNotifier:
    def __init__(self):
        self.app_id = 'stub_app'
        self.app_secret = 'stub_secret'
        self.owner_open_id = ''
        self.texts = []

    def send_direct(self, user_id, text):
        self.texts.append((user_id, text))
        return True

    def upload_image(self, path):
        return 'img_key_stub'

    def send_image(self, user_id, image_key):
        return True

    def last(self):
        return self.texts[-1][1] if self.texts else ''

    def containing(self, kw):
        return [t for _, t in self.texts if kw in t]


class StubQuota:
    def usage_summary(self, user_id):
        return '套餐: default · 本月已用 0/10 张'

    def precheck(self, user_id):
        return (True, None)

    def consume(self, *a, **k):
        return True


def fresh():
    """真实 FluxDB + 真实 FluxQueueScheduler；只有通知器是替身。"""
    from manager.flux_db import FluxDB
    from manager.flux_queue import FluxQueueScheduler
    import manager.feishu_bot as fb

    d = tempfile.mkdtemp(prefix='feishu-cancel-')
    db = FluxDB(os.path.join(d, 't.db'))
    quota = StubQuota()
    sched = FluxQueueScheduler(db, quota)
    notif = StubNotifier()
    bot = fb.FeishuBot(sched, db, quota, notifier=notif)
    # 同步处理（不起工作线程），断言才确定
    bot._enqueue = lambda oid, text: bot._handle_prompt(oid, text)
    return fb, bot, db, sched, notif


def put_job(db, jid, uid, status, prompt='一只橘猫坐在窗台上', error=None):
    db._exec('INSERT INTO jobs(job_id,user_id,prompt,status,error,created_at) VALUES(?,?,?,?,?,?)',
             (jid, uid, prompt, status, error, int(time.time())))


def sub_bot(fb, bot, db, sched, notif, text, sender='ou_me'):
    """把一条文本喂给机器人（走真实分派路径）。"""
    n_before = notif.texts.__len__()
    bot._handle_prompt(sender, text)
    return notif.texts[n_before:]


# ══════════════════════════════════════════════════════════════
# [1] ★★ 命令识别 —— 既不吞提示词，也不让命令被当提示词
# ══════════════════════════════════════════════════════════════
def t_command_parsing():
    print()
    print('─' * 78)
    print('[1] 命令识别（安全红线：错一个方向就白扣额度）')
    print('─' * 78)
    import manager.feishu_bot as fb

    must_be_cmd = [
        ('帮助', 'help'), ('/help', 'help'),
        ('我的任务', 'list'), ('我的排队', 'list'), ('取消', 'list'),
    ]
    for text, name in must_be_cmd:
        got = fb._match_command(text)
        check(f'{text!r} 解析为命令 {name}', bool(got) and got[0] == name, f'实际 {got}')

    cases = [
        ('取消 01', '01'), ('取消01', '01'), ('取消任务 01', '01'),
        ('取消 3ada124e', '3ada124e'), ('/取消 01', '01'),
        ('@_user_1 取消 01', '01'),
    ]
    for text, arg in cases:
        got = fb._match_command(text)
        check(f'{text!r} → cancel({arg})', bool(got) and got == ('cancel', arg), f'实际 {got}')

    # ★★ 最关键的一组：这些**必须**回落成提示词
    must_be_prompt = [
        '取消一切杂乱背景，画面干净',
        '取消背景的杂物，只留主体',
        '取消abc',
        '一只橘猫坐在窗台上，阳光洒落',
    ]
    for text in must_be_prompt:
        got = fb._match_command(text)
        check(f'★★ {text!r} 不被当命令（回落为提示词）', got is None, f'实际 {got} ← 会被误吞')


# ══════════════════════════════════════════════════════════════
# [2] ★★ 命令早于提交 —— 用「提交次数」判，不用文案
# ══════════════════════════════════════════════════════════════
def t_command_never_submits():
    print()
    print('─' * 78)
    print('[2] ★★ 命令绝不产生提交（用户想撤回，不能反而多花一次）')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()
    put_job(db, 'jA1', 'ou_me', 'queued')

    def qsize():
        return sched._pq.qsize()

    n0 = qsize()
    sub_bot(fb, bot, db, sched, notif, '我的任务')
    check('「我的任务」没有提交任何任务', qsize() == n0, f'队列 {n0} → {qsize()}')

    sub_bot(fb, bot, db, sched, notif, '取消 01')
    check('「取消 01」没有提交任何任务', qsize() == n0, f'队列 {n0} → {qsize()}')

    sub_bot(fb, bot, db, sched, notif, '帮助')
    check('「帮助」没有提交任何任务', qsize() == n0, f'队列 {n0} → {qsize()}')
    check('「帮助」回了命令清单', bool(notif.containing('取消 01')), '回执里没有取消用法')

    # ★ 反面：以"取消"开头的提示词**必须**被提交
    n1 = qsize()
    sub_bot(fb, bot, db, sched, notif, '取消一切杂乱背景，画面干净')
    check('★★ 以「取消」开头的提示词仍会被提交（没被命令吞掉）',
          qsize() == n1 + 1, f'队列 {n1} → {qsize()}')


# ══════════════════════════════════════════════════════════════
# [3] 列表：只列自己的 + 覆盖三种在途状态
# ══════════════════════════════════════════════════════════════
def t_list():
    print()
    print('─' * 78)
    print('[3] 「我的任务」列表')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()
    put_job(db, 'aaaa1111', 'ou_me', 'queued')
    put_job(db, 'bbbb2222', 'ou_me', 'generating')
    put_job(db, 'cccc3333', 'ou_me', 'waiting')
    put_job(db, 'dddd4444', 'ou_me', 'done')          # 终态 → 不该出现
    put_job(db, 'eeee5555', 'ou_other', 'queued')     # 别人的 → 不该出现

    sub_bot(fb, bot, db, sched, notif, '我的任务')
    txt = notif.last()
    check('列出 queued 任务', 'aaaa1111'[:8] in txt)
    check('列出 generating 任务', 'bbbb2222'[:8] in txt)
    check('列出 waiting 任务（最需要能取消的一个）', 'cccc3333'[:8] in txt)
    check('★ 不列已完成任务', 'dddd4444'[:8] not in txt)
    check('★ 不列别人的任务', 'eeee5555'[:8] not in txt)

    # 撤销墓碑后不该再出现
    db.job_mark_deleted('aaaa1111', 'ou_me')
    sub_bot(fb, bot, db, sched, notif, '我的任务')
    check('★ 已取消的任务不再出现在列表里', 'aaaa1111'[:8] not in notif.last())

    # 干净用户
    sub_bot(fb, bot, db, sched, notif, '我的任务', sender='ou_nobody')
    check('没有任务时给出空态提示（不报错）', '没有排队' in notif.last(), notif.last()[:40])


# ══════════════════════════════════════════════════════════════
# [4] ★★ 权限：取消只作用于自己的任务
# ══════════════════════════════════════════════════════════════
def t_permission():
    print()
    print('─' * 78)
    print('[4] ★★ 权限：只能取消自己的')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()
    put_job(db, 'fc0ffee1', 'ou_other', 'queued')
    put_job(db, 'aaaa1111', 'ou_me', 'queued')

    # 用**别人的任务短码**去取消
    sub_bot(fb, bot, db, sched, notif, '取消 fc0ffee1')
    row = db.job_get('fc0ffee1')
    check('★★ 用别人的任务号取消 → 对方任务未被标记墓碑',
          row is not None and row['deleted_at'] is None,
          f"deleted_at={row['deleted_at'] if row else '行没了'}")
    check('回执说明"在途里没有"（不泄漏"这个 id 存在"）',
          '在途任务里没有' in notif.last(), notif.last()[:60])

    # 自己的仍然可以取消
    sub_bot(fb, bot, db, sched, notif, '取消 aaaa1111')
    check('自己的任务取消成功',
          db.job_get('aaaa1111')['deleted_at'] is not None)


# ══════════════════════════════════════════════════════════════
# [5] 取消的副作用：墓碑 + 调度器剔除 + 跟踪终态 + 不退款
# ══════════════════════════════════════════════════════════════
def t_cancel_effects():
    print()
    print('─' * 78)
    print('[5] 取消的副作用（每一条都是"看起来做了其实没做"的高发区）')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()

    # (a) queued：从队列剔除
    put_job(db, 'aaaa1111', 'ou_me', 'queued')
    sched._pq.put((0, 1, 'aaaa1111'))
    sched._waiting.add('aaaa1111')
    db.feishu_track_add('aaaa1111', 'ou_me', prompt='x', phase='queued')

    sub_bot(fb, bot, db, sched, notif, '取消 01')
    check('★ 墓碑已打', db.job_get('aaaa1111')['deleted_at'] is not None)
    left = []
    while not sched._pq.empty():
        left.append(sched._pq.get_nowait()[2])
    check('★★ 已从优先级队列剔除（不烧 GPU）', 'aaaa1111' not in left, f'剩余 {left}')
    check('★ 已从等待池剔除（防幽灵复活）', 'aaaa1111' not in sched._waiting)
    check('★ 飞书跟踪已终止（轮询不再盯它）',
          'aaaa1111' not in [r['job_id'] for r in db.feishu_tracks_pending()])
    check('回执含「已取消」', '已取消' in notif.last(), notif.last()[:40])

    # (b) ★ 配额不变（不退）—— 与网页端 /api/delete 同一口径
    from manager.flux_quota import current_ym
    ym = current_ym()
    db.usage_add('ou_me', ym, 3)
    n_usage = db.usage_get('ou_me', ym)
    put_job(db, 'bbbb2222', 'ou_me', 'queued')
    sub_bot(fb, bot, db, sched, notif, '取消 bbbb2222')
    check('★★ 取消**不退还额度**（否则等于开了"删了重传刷额度"的口子）',
          db.usage_get('ou_me', ym) == n_usage == 3,
          f'{n_usage} → {db.usage_get("ou_me", ym)}')

    # (c) 幂等：再取消一次。
    #     ⚠️ 这里**不会**走到 `_cmd_cancel` 里 `if not marked` 那个分支 ——
    #     因为列表本身已排除墓碑，第二次查找就找不到命中了。
    #     所以用户实际看到的是"在途任务里没有…（可能已取消）"，
    #     而不是"此前已取消"。措辞必须覆盖这个情形，否则用户以为任务号打错了。
    put_job(db, 'cccc3333', 'ou_me', 'queued')
    sub_bot(fb, bot, db, sched, notif, '取消 cccc3333')
    check('第一次取消成功', '已取消' in notif.last(), notif.last()[:40])
    sub_bot(fb, bot, db, sched, notif, '取消 cccc3333')
    second = notif.last()
    check('★ 重复取消是幂等的：不报错，且说明它已不在在途',
          ('❌' not in second) and ('没有' in second), second[:70])
    check('重复取消未把任务"复活"（墓碑仍在）',
          db.job_get('cccc3333')['deleted_at'] is not None)


# ══════════════════════════════════════════════════════════════
# [6] 按状态给不同措辞（用户需要知道"撤得回来吗"）
# ══════════════════════════════════════════════════════════════
def t_status_wording():
    print()
    print('─' * 78)
    print('[6] 按状态给措辞（generating 撤不回来，必须说清）')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()

    put_job(db, '9e0a0001', 'ou_me', 'generating')
    sub_bot(fb, bot, db, sched, notif, '取消 9e0a0001')
    t_gen = notif.last()
    check('generating 的取消回执说明"无法中途打断"',
          '无法中途打断' in t_gen or '生成中' in t_gen, t_gen[:60])
    check('generating 的取消回执说明"产物会被丢弃"',
          '丢弃产物' in t_gen, t_gen[:60])

    put_job(db, '7a170001', 'ou_me', 'waiting', error='[SERVER_DOWN] x')
    sub_bot(fb, bot, db, sched, notif, '取消 7a170001')
    t_wait = notif.last()
    check('waiting 的取消回执说明"机器上线后不会再跑它"',
          '不会再跑' in t_wait, t_wait[:60])

    put_job(db, '0e0e0001', 'ou_me', 'queued')
    sub_bot(fb, bot, db, sched, notif, '取消 0e0e0001')
    t_q = notif.last()
    check('queued 的取消回执说明"不会消耗 GPU"', '不会消耗 GPU' in t_q, t_q[:60])
    check('所有取消回执都提示"额度不退还"（口径透明）',
          all('不退还' in t for t in (t_gen, t_wait, t_q)))


# ══════════════════════════════════════════════════════════════
# [7] 序号与短码的边界
# ══════════════════════════════════════════════════════════════
def t_lookup_edges():
    print()
    print('─' * 78)
    print('[7] 定位边界：越界 / 找不到 / 多匹配')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()

    # 越界
    put_job(db, 'only0001', 'ou_me', 'queued')
    sub_bot(fb, bot, db, sched, notif, '取消 99')
    check('★ 序号越界 → 报范围 + 提示查看列表（不静默失败）',
          '超出范围' in notif.last() and '我的任务' in notif.last(), notif.last()[:60])
    check('越界时未误伤任何任务',
          db.job_get('only0001')['deleted_at'] is None)

    # 0 号、负数写法
    sub_bot(fb, bot, db, sched, notif, '取消 0')
    check('序号 0 → 报越界（1 开始）', '超出范围' in notif.last(), notif.last()[:50])

    # 找不到
    sub_bot(fb, bot, db, sched, notif, '取消 deadbeef')
    check('短码找不到 → 明确回执（措辞含"在途里没有"）',
          '在途任务里没有' in notif.last(), notif.last()[:60])

    # 多匹配（两个任务短码前缀相同）
    put_job(db, 'abab0001', 'ou_me', 'queued')
    put_job(db, 'abab0002', 'ou_me', 'queued')
    sub_bot(fb, bot, db, sched, notif, '取消 abab00')
    check('★ 短码多匹配 → 要求更长的前缀（不猜、不误删）',
          '匹配到' in notif.last(), notif.last()[:60])
    check('多匹配时两个任务都未被标记',
          db.job_get('abab0001')['deleted_at'] is None
          and db.job_get('abab0002')['deleted_at'] is None)

    # 用更长的前缀就能精确定位
    sub_bot(fb, bot, db, sched, notif, '取消 abab0002')
    check('长前缀可精确定位并取消',
          db.job_get('abab0002')['deleted_at'] is not None
          and db.job_get('abab0001')['deleted_at'] is None)

    # 无可取消任务时（全被取消完）
    fb2, bot2, db2, sched2, notif2 = fresh()
    sub_bot(fb2, bot2, db2, sched2, notif2, '取消 01')
    check('没有可取消任务 → 空态提示（不报"不存在"这种吓人话）',
          '没有可取消' in notif2.last(), notif2.last()[:50])


# ══════════════════════════════════════════════════════════════
# [8] ★★ 跨渠道取消：网页端删了，飞书轮询必须收敛
# ══════════════════════════════════════════════════════════════
def t_cross_channel_cancel():
    print()
    print('─' * 78)
    print('[8] ★★ 跨渠道：网页端删除后，飞书轮询必须停止盯它')
    print('─' * 78)
    fb, bot, db, sched, notif = fresh()

    # 通过飞书提交（留下跟踪记录），然后**在网页端**（直接改库）删掉它
    put_job(db, 'cross001', 'ou_me', 'queued')
    db.feishu_track_add('cross001', 'ou_me', prompt='一只橘猫', phase='queued')
    pend = [r['job_id'] for r in db.feishu_tracks_pending()]
    check('前置：该任务确实在被跟踪', 'cross001' in pend, f'实际 {pend}')

    db.job_mark_deleted('cross001', 'ou_me')     # ← 模拟网页端 /api/delete
    bot._poll_once()

    pend2 = [r['job_id'] for r in db.feishu_tracks_pending()]
    check('★★ 轮询发现墓碑 → 终止跟踪（否则每 5 秒空转、永不收敛）',
          'cross001' not in pend2, f'实际仍在跟踪 {pend2}')
    row = db._one("SELECT phase FROM feishu_tracks WHERE job_id='cross001'")
    check('跟踪 phase 落到 cancelled 终态',
          row is not None and row['phase'] == 'cancelled',
          f"phase={row['phase'] if row else '(无)'}")


def main():
    t_command_parsing()
    t_command_never_submits()
    t_list()
    t_permission()
    t_cancel_effects()
    t_status_wording()
    t_lookup_edges()
    t_cross_channel_cancel()

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

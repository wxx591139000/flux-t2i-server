#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""飞书「图图」机器人 P0 回归门（离线，不需要 GPU / 不需要真发飞书消息）。

背景（2026-09-23）：设计文档 `docs/图图-飞书完整互动-设计方案.md` §6 的 P0 是
「修 3 个会烧钱/会丢用户结果的缺陷」。动手时又查出**第 4 个、而且最严重**：

  P0-1 ★ `_send_result` 用 `job.get('image_path')`，而 `job` 是 `sqlite3.Row`
        —— Row 没有 `.get()`（实测 AttributeError）。异常被轮询循环的宽 except 吞掉，
        于是**图片永远发不出去**，且该任务永远留在跟踪表里每 5 秒重试一次、永不收敛。
        → 也就是说图图的**核心功能一直是坏的**。
  P0-2 无事件去重 → 飞书重放一次 = 重复出图 + 重复扣额度
  P0-3 在途任务只在内存 → 9620 重启（每次改代码都要重启）后结果**永远回不来**
  P0-4 超出在途上限时只"不跟踪" → 任务照跑、额度照扣，用户**收不到图也不知原因**

本门把这些钉住。判据尽量取「用户可观察行为」（收到了什么消息 / 提交了几次），
而不是钉字面串 —— 免得重构一次误报一次（见 docs/PITFALLS.md 的改门三判据）。

用法：python tests/test_feishu_bot_p0.py   （rc=0 通过）
"""
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(label, cond, detail=''):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✅' if cond else '❌'} {label}" + (f"    ({detail})" if detail else ''))


# ── 替身（离线可验的关键）──────────────────────────────────────────────
class StubNotifier:
    """记录所有外发消息，不碰网络。"""

    def __init__(self):
        self.app_id = 'stub_app'
        self.app_secret = 'stub_secret'
        self.owner_open_id = 'ou_owner'
        self.texts = []      # [(open_id, text)]
        self.images = []     # [(open_id, image_key)]
        self.uploads = []    # [path]
        self.fail_upload = False

    def send_direct(self, user_id, text):
        self.texts.append((user_id, text))
        return True

    def upload_image(self, path):
        self.uploads.append(str(path))
        return None if self.fail_upload else 'img_key_stub'

    def send_image(self, user_id, image_key):
        self.images.append((user_id, image_key))
        return True

    def text_containing(self, kw):
        return [t for _, t in self.texts if kw in t]


class StubScheduler:
    def __init__(self):
        self.submits = []      # [(user_id, prompt)]
        self.next_id = 0

    def submit(self, user_id, prompt, *a, **kw):
        self.submits.append((user_id, prompt))
        self.next_id += 1
        return {'job_id': f'job{self.next_id:04d}'}


class StubQuota:
    def usage_summary(self, user_id):
        return '套餐: default · 本月已用 0/10 张'


def make_event(open_id='ou_user1', text='一只橘猫', event_id='evt_1', message_id='msg_1',
               chat_type='p2p', message_type='text', content=None):
    """造一条飞书 im.message.receive_v1 事件（只带本模块用到的字段）。"""
    msg = types.SimpleNamespace(
        chat_type=chat_type, message_type=message_type, message_id=message_id,
        content=json.dumps(content if content is not None else {'text': text},
                           ensure_ascii=False))
    sender = types.SimpleNamespace(sender_type='user',
                                   sender_id=types.SimpleNamespace(open_id=open_id))
    hdr = types.SimpleNamespace(event_id=event_id)
    return types.SimpleNamespace(header=hdr, event=types.SimpleNamespace(sender=sender, message=msg))


def new_bot(db, sched=None, notif=None, sync=True):
    """造一个 bot。

    `sync=True`（默认）把 `_enqueue` 换成**同步处理** —— 绝大多数用例关心的是
    「消息处理逻辑对不对」，同步执行才确定（不用起线程 + sleep 猜它跑完没）。
    「WS 回调不阻塞」这条必须验真实路径，所以那一条用 `sync=False`。
    """
    import manager.feishu_bot as fb
    sched = sched or StubScheduler()
    notif = notif or StubNotifier()
    bot = fb.FeishuBot(sched, db, StubQuota(), notifier=notif)
    if sync:
        bot._enqueue = lambda oid, text: bot._handle_prompt(oid, text)
    return fb, bot, sched, notif


def safe_poll(bot):
    """像 `_poll_loop` 那样跑一轮轮询（吞掉异常并显式打印）。

    ⚠️ 为什么门里也要吞异常：真实运行时轮询线程的宽 except **就是**把异常吞掉的
    （P0-1 能藏那么久正是因为它只打一行「任务轮询异常」）。
    所以本门要按「**用户最终有没有收到消息**」判，而不是让异常直接炸穿测试 ——
    那样只会得到一个崩溃，看不出"用户收不到图"这个真正的症状。
    """
    try:
        bot._poll_once()
        return None
    except Exception as e:
        print(f'    ⚠️ 轮询抛异常（真实环境会被 _poll_loop 吞掉）: {type(e).__name__}: {e}')
        return e


def main():
    tmpdir = Path(tempfile.mkdtemp(prefix='figu_p0_'))
    # 数据库：显式传路径，避免污染真实 data/flux_service.db
    from manager.flux_db import FluxDB
    db = FluxDB(str(tmpdir / 'test.db'))

    print('─' * 78)
    print('P0-2 事件去重：重放不重复出图（否则重复扣额度）')
    print('─' * 78)
    fb, bot, sched, notif = new_bot(db)

    ev = make_event(event_id='evt_replay', message_id='msg_replay', text='重放测试')
    bot._on_message(ev)
    bot._on_message(ev)                      # 同一条事件重放
    check('同一事件重放 2 次，只提交 1 次', len(sched.submits) == 1, f'实际 {len(sched.submits)} 次')
    check('同一事件重放 2 次，只回 1 条确认', len(notif.text_containing('✅ 收到')) == 1,
          f"实际 {len(notif.text_containing('✅ 收到'))} 条")

    # ⚠️ 下面两个用例**必须换用户**：同一用户的第二条消息会被"每用户在途上限"
    #    正确拦住（见 P0-4 段），那会把"去重"的验证串成"限流"的验证。
    bot._on_message(make_event(open_id='ou_user2', event_id='', message_id='msg_other', text='另一条'))
    check('不同事件不被误杀（用 message_id 兜底）', len(sched.submits) == 2,
          f'实际 {len(sched.submits)} 次')

    k = 'evt:atomic_probe'
    first = db.feishu_seen(k)
    second = db.feishu_seen(k)
    check('feishu_seen 原子判重：首次 False、再次 True', (first is False and second is True),
          f'first={first} second={second}')

    # 拿不到任何 id 时必须"明确记录未去重"，不能假装去过重
    ev_nokey = make_event(open_id='ou_user3', event_id='', message_id='', text='无 id 事件')
    n_before = len(sched.submits)
    bot._on_message(ev_nokey)
    check('无 event_id/message_id 时仍能处理（不静默丢弃）', len(sched.submits) == n_before + 1,
          f'实际 +{len(sched.submits) - n_before}')

    print()
    print('─' * 78)
    print('P0-4 在途上限：超出必须明确回执，且不浪费额度')
    print('─' * 78)
    fb2, bot2, sched2, notif2 = new_bot(db)
    old_global = fb2.MAX_INFLIGHT_GLOBAL
    fb2.MAX_INFLIGHT_GLOBAL = 0              # 人为压到 0：任何提交都算超限
    bot2._on_message(make_event(open_id='ou_g', event_id='evt_g1', message_id='m_g1', text='会超限'))
    fb2.MAX_INFLIGHT_GLOBAL = old_global
    check('超全局上限时不提交（不扣额度）', len(sched2.submits) == 0,
          f'实际提交 {len(sched2.submits)} 次')
    check('超全局上限时必须回执（不静默）', len(notif2.text_containing('没有提交')) == 1)

    # 每用户上限：读库口径 → 重启后依然有效
    fb3, bot3, sched3, notif3 = new_bot(db)
    uid = 'ou_busy'
    db.user_ensure(uid)
    db.feishu_track_add('job_busy_1', uid, prompt='在途', phase='queued')
    bot3._on_message(make_event(open_id=uid, event_id='evt_busy2', message_id='m_busy2', text='再来一张'))
    check('该用户已有在途 → 不再提交', len(sched3.submits) == 0)
    check('该用户已有在途 → 明确回执', len(notif3.text_containing('还在生成中')) == 1)
    check('每用户在途口径来自 DB（重启后仍准）', db.feishu_tracks_inflight(sender_id=uid) == 1)

    print()
    print('─' * 78)
    print('P0-1 / P0-3 图片回传 + 重启恢复（本门最重要）')
    print('─' * 78)
    # 造一个真实存在的"图片"文件：_send_result 会 Path(img).exists() 检查
    img = tmpdir / 'out.png'
    img.write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 64)

    uid2 = 'ou_recv'
    db.user_ensure(uid2)
    db.feishu_track_add('job_recv_1', uid2, prompt='要一张图', phase='queued')
    # 直接改 jobs 表造一个 done 任务（不经过调度器，保持离线）
    with db._lock:
        db._conn.execute(
            'INSERT OR REPLACE INTO jobs(job_id,user_id,prompt,status,image_path,created_at)'
            ' VALUES(?,?,?,?,?,?)',
            ('job_recv_1', uid2, '要一张图', 'done', str(img), int(time.time())))
        db._conn.commit()

    # ★ 模拟"9620 重启"：全新 bot 实例，内存里没有任何历史
    fb4, bot4, sched4, notif4 = new_bot(db)
    check('重启后的新实例能从 DB 查到在途任务', len(db.feishu_tracks_pending()) >= 1,
          f"pending={len(db.feishu_tracks_pending())}")
    safe_poll(bot4)
    check('★ 重启后仍能把图片回传给用户（P0-1+P0-3）', len(notif4.images) == 1,
          f'实际发图 {len(notif4.images)} 条')
    check('回传用的确实是该任务的文件', notif4.uploads and notif4.uploads[-1] == str(img),
          f'uploads={notif4.uploads}')
    check('回传后跟踪表置为终态 done', (db.feishu_track_get('job_recv_1'))['phase'] == 'done')

    # 终态任务不再被重复处理（否则每 5 秒重发一次图）
    n_img = len(notif4.images)
    safe_poll(bot4)
    check('已终态任务不会被重复回传', len(notif4.images) == n_img, f'实际 {len(notif4.images)}')

    # 失败：只通知一次；重启后不重发
    db.feishu_track_add('job_fail_1', uid2, prompt='会失败', phase='queued')
    with db._lock:
        db._conn.execute(
            'INSERT OR REPLACE INTO jobs(job_id,user_id,prompt,status,error,created_at)'
            ' VALUES(?,?,?,?,?,?)',
            ('job_fail_1', uid2, '会失败', 'failed', '上游 OOM', int(time.time())))
        db._conn.commit()
    safe_poll(bot4)
    c1 = len(notif4.text_containing('生成失败'))
    fb5, bot5, sched5, notif5 = new_bot(db)      # 再"重启"一次
    safe_poll(bot5)
    check('失败只通知一次（重启后不重发）', c1 == 1 and len(notif5.text_containing('生成失败')) == 0,
          f'首次 {c1} 条 / 重启后 {len(notif5.text_containing("生成失败"))} 条')
    check('失败任务置为终态 failed', (db.feishu_track_get('job_fail_1'))['phase'] == 'failed')

    # waiting：只通知一次
    db.feishu_track_add('job_wait_1', uid2, prompt='等待恢复', phase='queued')
    with db._lock:
        db._conn.execute(
            'INSERT OR REPLACE INTO jobs(job_id,user_id,prompt,status,created_at) VALUES(?,?,?,?,?)',
            ('job_wait_1', uid2, '等待恢复', 'waiting', int(time.time())))
        db._conn.commit()
    safe_poll(bot5)
    w1 = len(notif5.text_containing('服务器暂不可达'))
    safe_poll(bot5)
    check('waiting 只通知一次', w1 == 1 and len(notif5.text_containing('服务器暂不可达')) == 1,
          f'实际 {len(notif5.text_containing("服务器暂不可达"))} 条')

    # done 但图片文件不存在 → 必须明确告知，不能静默
    db.feishu_track_add('job_noimg_1', uid2, prompt='图丢了', phase='queued')
    with db._lock:
        db._conn.execute(
            'INSERT OR REPLACE INTO jobs(job_id,user_id,prompt,status,image_path,created_at)'
            ' VALUES(?,?,?,?,?,?)',
            ('job_noimg_1', uid2, '图丢了', 'done', str(tmpdir / 'nope.png'), int(time.time())))
        db._conn.commit()
    safe_poll(bot5)
    check('done 但图片不存在 → 明确回执（不静默）', len(notif5.text_containing('图片回传失败')) == 1)

    print()
    print('─' * 78)
    print('P0-5 2026-09-24 实测事故：WS 回调阻塞 → 飞书踢连接；通知未判发送结果')
    print('─' * 78)

    # ── 5a. WS 回调绝不能阻塞（lark 在 asyncio 事件循环里同步调用 _on_message）──
    class SlowScheduler(StubScheduler):
        def submit(self, user_id, prompt, *a, **kw):
            time.sleep(2.5)                      # 模拟中文翻译最坏耗时
            return super().submit(user_id, prompt, *a, **kw)

    fb7, bot7, sched7, notif7 = new_bot(db, sched=SlowScheduler(), sync=False)
    import threading as _th
    for _ in range(bot7._workers):               # start() 里会起；测试里手工起
        _th.Thread(target=bot7._work_loop, daemon=True).start()

    t0 = time.time()
    bot7._on_message(make_event(open_id='ou_fast', event_id='evt_fast',
                                message_id='m_fast', text='慢翻译测试'))
    dt = time.time() - t0
    check('WS 回调不阻塞事件循环（提交慢也不拖住它）', dt < 1.0, f'{dt * 1000:.0f} ms')
    for _ in range(80):
        if sched7.submits:
            break
        time.sleep(0.1)
    check('入队的消息最终由工作线程处理', len(sched7.submits) == 1, f'实际 {len(sched7.submits)}')
    check('提交前先发即时回执（用户不会以为机器人坏了）',
          len(notif7.text_containing('正在解析提示词')) == 1,
          f"实际 {len(notif7.text_containing('正在解析提示词'))} 条")

    # ── 5b. 通知只在**真的发出去**之后才置位 ──
    class GateNotifier(StubNotifier):
        def __init__(self):
            super().__init__()
            self.allow = False

        def send_direct(self, uid, text):
            if not self.allow:
                return False                     # 模拟发送失败（限流/网络）
            return super().send_direct(uid, text)

    uid3 = 'ou_notify'
    db.user_ensure(uid3)
    db.feishu_track_add('job_notify_1', uid3, prompt='等待中', phase='queued')
    with db._lock:
        db._conn.execute(
            'INSERT OR REPLACE INTO jobs(job_id,user_id,prompt,status,created_at) VALUES(?,?,?,?,?)',
            ('job_notify_1', uid3, '等待中', 'waiting', int(time.time())))
        db._conn.commit()

    gn = GateNotifier()
    fb8, bot8, _, _ = new_bot(db, notif=gn)
    safe_poll(bot8)
    n1 = json.loads(db.feishu_track_get('job_notify_1')['notified'] or '{}').get('waiting')
    check('发送失败时**不**置位 notified（否则用户永远收不到）', n1 is not True, f'notified.waiting={n1}')

    gn.allow = True
    safe_poll(bot8)
    n2 = json.loads(db.feishu_track_get('job_notify_1')['notified'] or '{}').get('waiting')
    check('发送成功后置位 notified，且下一轮不再重发', n2 is True and len(gn.text_containing('暂不可达')) == 1,
          f'notified.waiting={n2} 文本={len(gn.text_containing("暂不可达"))} 条')

    print()
    print('─' * 78)
    print('其他：非文本/群聊消息被忽略，且不产生副作用')
    print('─' * 78)
    fb6, bot6, sched6, notif6 = new_bot(db)
    bot6._on_message(make_event(event_id='evt_img', message_id='m_img',
                                message_type='image', content={'image_key': 'k'}))
    bot6._on_message(make_event(event_id='evt_grp', message_id='m_grp', chat_type='group'))
    check('图片/群聊消息不提交任务（P0 范围外，P2/P3 再接）', len(sched6.submits) == 0,
          f'实际 {len(sched6.submits)}')
    check('被忽略的消息也已登记去重（重放不会二次处理）',
          db.feishu_seen('evt:evt_img') is True and db.feishu_seen('evt:evt_grp') is True)

    print()
    print('=' * 78)
    print(f'汇总：通过 {len(PASS)} / 失败 {len(FAIL)}')
    if FAIL:
        print('失败项：')
        for f in FAIL:
            print('  -', f)
    print('=' * 78)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

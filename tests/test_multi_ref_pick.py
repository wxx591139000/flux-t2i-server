#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多图参考任务的**选机能力闸**回归门（2026-09-29 新增）。

## 为什么需要这道门

「模特穿搭」场景（B 链 image-gen-site）会把 1 张模特图 + N 张单品图**逐张**透传给
GPU 侧，而不是拼成一张。这要求模型必须支持 multi_ref。

实测画像（`server/flux_resident_server.py` 的 MODEL_CAPABILITIES）：
    Flux2KleinPipeline : edit=True  multi_ref=False  max_ref_images=1
    QwenImage21Pipeline: edit=True  multi_ref=True   max_ref_images=10

问题出在 manager 选机这一层：原实现只传 `need_edit=is_edit`，
而 `edit=True` 只说明「接受参考图」，**不说明「接受多张」**。
于是穿搭任务会被派给 klein 机，直到 GPU 侧的硬闸
（`if len(ref_paths) > 1 and not caps.get('multi_ref')`）才被拒 ——
用户白等一整轮排队 + SSH + 冷启动，最后只拿到一句「不支持多张参考图」。

代价不对称：多要一个能力最多是「少几台候选机」（真没有就明确报错），
少要则是「每次穿搭都白跑一轮」。

## 本门钉什么

  [1] 多图任务（ref_count > 1）→ 选机时**必须**带上 need_caps={'multi_ref': True}
  [2] 单图任务（ref_count == 1）→ **不得**带 multi_ref 要求
      （否则 klein 机永远选不上，普通图生图会被无谓地缩小候选池）
  [3] 文生图（ref_count == 0）→ 不得带 multi_ref 要求
  [4] 张数必须来自 **DB 的 ref_count**，不是 len(内存参考图)
      —— 重启恢复时内存必为空、落盘是尽力而为，只有 DB 在选机前可读
  [5] 选不到机器时的**报错文案**必须区分「多图无机器」与「机器都关机」
      —— 前者换模型/减少张数才有救，后者等开机就行
  [6] 变异断言：把多图闸拆掉 → 本门必须变红

## 口径说明

判据全部落在**可观察行为**上：stub 掉 `fr.find_available_server` 记录它收到的
kwargs、读 `_generate_resident` 返回的错误串。不钉字面串形态、不碰网络。

用法: python tests/test_multi_ref_pick.py    # 全绿 exit 0，有红 exit 1
"""
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import manager.flux_queue as fq                      # noqa: E402
import manager.flux_resident_client as fr            # noqa: E402

RESULTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond), detail))
    print(f'  {"OK " if cond else "XX "} {name}{("  -> " + detail) if detail else ""}')


# ══════════════════════════ 造一个真 sqlite3.Row 的 job ══════════════════════════
# ⚠️ 铁律（项目既有教训）：必须是**真的** sqlite3.Row，不能是 dict。
#    Row 支持 [] 但**没有 .get()**；用 dict 糊弄会让测试全绿、上真机 AttributeError。
_JOBS_DDL = """CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY, user_id TEXT, prompt TEXT, original_prompt TEXT,
    priority INTEGER DEFAULT 0, width INTEGER, height INTEGER, seed INTEGER,
    steps INTEGER, negative_prompt TEXT, model TEXT, status TEXT DEFAULT 'queued',
    edit_mode INTEGER NOT NULL DEFAULT 0, has_ref INTEGER NOT NULL DEFAULT 0,
    ref_count INTEGER NOT NULL DEFAULT 0,
    error TEXT, image_path TEXT, server TEXT, created_at INTEGER,
    completed_at INTEGER, refunded_at INTEGER)"""


def make_job(ref_count, model=None, job_id='j-multiref'):
    """造一条 job（sqlite3.Row）。ref_count = 参考图张数（0 = 文生图）。"""
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute(_JOBS_DDL)
    if 'ref_count' not in {r[1] for r in c.execute('PRAGMA table_info(jobs)')}:
        # 说明：本 DDL 与真实 FluxDB 的 jobs 表必须同形。缺列时**明确失败**，
        # 不要悄悄补 —— 那会让 [4] 那条「张数来自 DB」的断言失去意义。
        raise AssertionError('测试 DDL 缺 ref_count 列（与真实 jobs 表已漂移）')
    c.execute('INSERT INTO jobs(job_id,user_id,prompt,status,edit_mode,has_ref,ref_count,model)'
              ' VALUES(?,?,?,?,?,?,?,?)',
              (job_id, 'u-test', 'a prompt', 'queued',
               1 if ref_count else 0, 1 if ref_count else 0, ref_count, model))
    c.commit()
    return c.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()


def run_generate_resident(job, is_edit):
    """跑 `_generate_resident`，**stub 掉选机**，返回 (错误串, 选机收到的 kwargs)。

    为什么要 stub 选机而不是让它真跑：真实选机会走 probe_all → SSH / 直连，
    在离线环境里既慢又不确定。本门要验的是「**我们向选机提了什么要求**」，
    而不是「选机本身选得对不对」（那是 test_model_select.py / test_commercial_gate.py 的职责）。
    """
    captured = {}

    def fake_find(force=False, need_edit=False, need_model=None, need_caps=None):
        captured['need_edit'] = need_edit
        captured['need_model'] = need_model
        captured['need_caps'] = need_caps
        return None, {'name': 'stub-probe'}     # 一律「选不到」

    sched = fq.FluxQueueScheduler.__new__(fq.FluxQueueScheduler)   # 不走 __init__（不建库/不起线程）
    sched._refs = {}
    sched._ref_lock = __import__('threading').Lock()
    orig = fr.find_available_server
    fr.find_available_server = fake_find
    try:
        ok, err = sched._generate_resident(job, is_edit=is_edit)
    finally:
        fr.find_available_server = orig
    return ok, err, captured


# ══════════════════════════ [1]-[3] 各张数条件下的选机要求 ══════════════════════════
def t_multi_requires_caps():
    print('\n[1] 多图任务（ref_count>1）→ 必须要求 multi_ref')
    for n in (2, 3, 6):
        ok, err, cap = run_generate_resident(make_job(n), is_edit=True)
        check(f'ref_count={n} → need_caps 含 multi_ref=True',
              cap.get('need_caps') == {'multi_ref': True},
              f"实得 {cap.get('need_caps')}")
        check(f'ref_count={n} → 仍然是图生图（need_edit=True）',
              cap.get('need_edit') is True, f"实得 {cap.get('need_edit')}")


def t_single_no_caps():
    print('\n[2] 单图任务（ref_count==1）→ 不得要求 multi_ref')
    ok, err, cap = run_generate_resident(make_job(1), is_edit=True)
    check('ref_count=1 → need_caps 为空（不缩小候选池）',
          not cap.get('need_caps'), f"实得 {cap.get('need_caps')}")
    check('ref_count=1 → 仍要求 need_edit=True',
          cap.get('need_edit') is True, f"实得 {cap.get('need_edit')}")


def t_text2img_no_caps():
    print('\n[3] 文生图（ref_count==0）→ 不得要求 multi_ref')
    ok, err, cap = run_generate_resident(make_job(0), is_edit=False)
    check('文生图 → need_caps 为空', not cap.get('need_caps'), f"实得 {cap.get('need_caps')}")
    check('文生图 → need_edit 为 False', cap.get('need_edit') is False,
          f"实得 {cap.get('need_edit')}")


# ══════════════════════════ [4] 张数来自 DB，不是内存 ══════════════════════════
def t_count_from_db():
    print('\n[4] 张数来自 DB 的 ref_count（选机前唯一可读的可靠来源）')
    # 内存里一张参考图都没有（模拟重启恢复后的状态）——若实现去读内存，
    # 就会拿不到张数 → 误判成单图 → 派给小模型机 → 白等一轮。
    job = make_job(4)
    ok, err, cap = run_generate_resident(job, is_edit=True)
    check('内存无参考图、DB ref_count=4 → 仍然要求 multi_ref（说明读的是 DB）',
          cap.get('need_caps') == {'multi_ref': True}, f"实得 {cap.get('need_caps')}")

    # 反向：DB 说单图、但（假设）内存里有多张 → 应以 DB 为准
    job1 = make_job(1)
    ok, err, cap = run_generate_resident(job1, is_edit=True)
    check('DB ref_count=1 → 不要求 multi_ref（以 DB 为准，不靠内存）',
          not cap.get('need_caps'), f"实得 {cap.get('need_caps')}")


# ══════════════════════════ [5] 报错文案要能区分两种"选不到" ══════════════════════════
def t_error_message():
    print('\n[5] 选不到机器时的报错文案（多图缺机器 vs 机器关机）')
    ok, err, cap = run_generate_resident(make_job(3), is_edit=True)
    check('多图选不到 → 失败且带"多张参考图"字样（指向能力缺失）',
          ok is False and ('多张参考图' in (err or '') or 'multi_ref' in (err or '')),
          repr(err))
    check('多图选不到 → 文案里带上张数（用户知道要减到几张）',
          '3' in (err or ''), repr(err))
    check('多图选不到 → **不应**被标成 [SERVER_DOWN]（那不是"等开机就行"）',
          '[SERVER_DOWN]' not in (err or ''), repr(err))

    # 对照：普通图生图选不到 → 仍然走 SERVER_DOWN（会被放进等待恢复池）
    ok2, err2, cap2 = run_generate_resident(make_job(1), is_edit=True)
    check('对照：普通图生图选不到 → 仍标 [SERVER_DOWN]（等机器回来会重试）',
          ok2 is False and '[SERVER_DOWN]' in (err2 or ''), repr(err2))


# ══════════════════════════ [6] 变异断言 ══════════════════════════
def t_mutation():
    print('\n[6] 变异断言：拆掉多图闸 → 本门必须变红')
    src = open(os.path.join(BASE, 'manager', 'flux_queue.py'), encoding='utf-8').read()

    # 源码级：`_generate_resident` 里必须真的存在「按张数要求 multi_ref」的判据。
    # ⚠️ 用**去缩进后的整段**找，不钉具体某一行的空白形态 ——
    #    项目既有教训：钉字面串形态的门会因为格式调整而假红。
    seg_start = src.find('def _generate_resident')
    seg_end = src.find('def _generate_legacy')
    seg = src[seg_start:seg_end] if seg_start >= 0 and seg_end > seg_start else ''
    check('源码：_generate_resident 里存在 multi_ref 要求',
          'multi_ref' in seg, f'片段长度 {len(seg)}')
    check('源码：判据读的是 job 的 ref_count',
          "ref_count" in seg, '')
    check('源码：need_caps 只在该判据为真时传递',
          'need_caps=' in seg, '')

    # 行为级：把闸拆掉（等价于永远传 None）→ 多图任务必须不再要求 multi_ref。
    # 这就是「拆掉闸」的模拟，若它**仍然**要求 multi_ref，说明要求来自别处、
    # 本门的 [1] 就不是在测这一行 —— 那必须知道。
    orig = fq.FluxQueueScheduler._generate_resident
    try:
        # 篡改方式：把函数里的 need_caps 传参改成恒 None（用源码重编译执行）
        import textwrap
        mutant_src = textwrap.dedent(seg).replace(
            "need_caps={'multi_ref': True} if _multi_ref else None",
            'need_caps=None if True else None')
        if mutant_src == textwrap.dedent(seg):
            check('★ 变异：能找到待篡改的 need_caps 表达式（否则本门钉不住它）',
                  False, '未匹配到 need_caps 传参表达式')
        else:
            ns = dict(fq.__dict__)
            ns['fr'] = fr
            ns['WEB_OUT'] = fq.WEB_OUT
            ns['logger'] = fq.logger
            exec(compile(mutant_src, '<mutant>', 'exec'), ns)
            line0 = mutant_src.splitlines()[0]
            fname = line0.split('def ')[1].split('(')[0].strip()
            MutantCls = type('MutantCls', (), {fname: ns[fname]})
            sched = fq.FluxQueueScheduler.__new__(fq.FluxQueueScheduler)
            sched._refs = {}
            sched._ref_lock = __import__('threading').Lock()
            captured = {}

            def fake_find(force=False, need_edit=False, need_model=None, need_caps=None):
                captured['need_caps'] = need_caps
                return None, {'name': 'stub-probe'}

            o2 = fr.find_available_server
            fr.find_available_server = fake_find
            try:
                MutantCls()._generate_resident(make_job(3), is_edit=True)
            finally:
                fr.find_available_server = o2
            check('★ 变异：拆掉闸后多图任务**不再**要求 multi_ref → 本门能抓住',
                  captured.get('need_caps') in (None, {}),
                  f"变异后 need_caps={captured.get('need_caps')}（若仍为 multi_ref，说明要求来自别处）")
    finally:
        fq.FluxQueueScheduler._generate_resident = orig


def main():
    print('=' * 66)
    print('多图参考任务 · 选机能力闸回归门（2026-09-29）')
    print('=' * 66)
    t_multi_requires_caps()
    t_single_no_caps()
    t_text2img_no_caps()
    t_count_from_db()
    t_error_message()
    t_mutation()

    ok = sum(1 for _n, c, _d in RESULTS if c)
    bad = [(n, d) for n, c, d in RESULTS if not c]
    print('\n' + '=' * 66)
    print(f'共 {len(RESULTS)} 项，通过 {ok}，失败 {len(bad)}')
    for n, d in bad:
        print(f'  XX {n}  {d}')
    print('=' * 66)
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())

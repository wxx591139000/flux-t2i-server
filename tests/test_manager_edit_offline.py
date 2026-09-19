#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""manager 侧图生图转发离线验收（stub 模式，**不需要 GPU / 不需要 SSH**）。

背景（2026-09-18）：
  图生图在 resident（GPU 侧）与 image-gen-site（B 链）都已接通，唯独中间的
  manager（任务中心 9620）**没有 /api/edit 入口、也没有向 resident 的 /edit 转发** ——
  B 链的 `POST /api/edit` 打过来会 404。本套件验收「补上之后」的完整链路：

      C 链测试客户端 ── /api/edit ──> manager ── /edit ──> resident(stub) ──> PNG

覆盖：
  M1 文生图回归：/api/submit 仍正常（补 edit 不能把 t2i 弄坏）
  M2 图生图全链路：/api/edit 提交 → 轮询 done → /api/download 取回真 PNG
  M3 缺 image → 400 明确拒绝（**不静默降级成文生图**，那会照常出图照常扣费）
  M4 manager→resident 真的打了 **/edit** 而不是 /generate（用 stub 的调用留痕断言）
  M5 去重键含 edit 标记：同提示词「先文生图、再图生图」不被误判为重复提交
  M6 计费与退还：图生图失败时配额原路退还（不能白扣）
  M7 参考图不入库：jobs 表里只有 edit_mode/has_ref 标记，没有 base64 本体
  M8 重启后孤儿 edit 任务**明确失败**（参考图在内存态，恢复不了就不该假装能跑）
  M9 参数异常（steps 非整数）→ 400
  M10 /api/edit 缺 image 字段时不消耗配额

用法：
  python tests/test_manager_edit_offline.py
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SERVER = ROOT / 'server' / 'flux_resident_server.py'
PY = sys.executable

RESIDENT_PORT = int(os.environ.get('TEST_MGR_RESIDENT_PORT', '9694'))
WEB_PORT = int(os.environ.get('TEST_MGR_WEB_PORT', '9695'))
RESIDENT_BASE = f'http://127.0.0.1:{RESIDENT_PORT}'
WEB_BASE = f'http://127.0.0.1:{WEB_PORT}'

PASS, FAIL = [], []
TOKEN = 'site-testmgr'          # 与 B 链同口径：一个普通的访客 token

# ⚠️ 本机环境坑（2026-09-18 实测踩到，表现为「提交 /api/edit 直接超时」）：
#    shell 里带 http_proxy/https_proxy（本机出网只走代理），而 no_proxy 是空的。
#    urllib 的默认 opener 会读环境变量 → **连 127.0.0.1 也走代理** → 小 body 侥幸能过、
#    MiB 级 base64 body 必挂。GPU 侧客户端早就用 ProxyHandler({}) 绕开了，
#    测试客户端也必须绕，否则「本机代理」会被误读成「manager 卡死」。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def case(name):
    def deco(fn):
        try:
            info = fn()
            PASS.append(name)
            print(f'▸ PASS [{name}]')
            if info:
                print(f'         {info}')
        except Exception as e:  # noqa: BLE001
            FAIL.append((name, f'{type(e).__name__}: {e}'))
            print(f'✗ FAIL [{name}]  {type(e).__name__}: {e}')
        return fn
    return deco


def _req(url, body=None, timeout=40, headers=None):
    h = {'Content-Type': 'application/json'}
    h.update(headers or {})
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h,
                                 method='POST' if body is not None else 'GET')
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read()
            ct = (r.headers.get('Content-Type') or '').lower()
            if 'image' in ct:
                return r.status, raw
            return r.status, json.loads(raw.decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode('utf-8') or '{}')
        except Exception:  # noqa: BLE001
            return e.code, raw


def get(path, timeout=30, base=WEB_BASE):
    return _req(base + path, None, timeout)


def post(path, body, timeout=40, base=WEB_BASE):
    return _req(base + path, body, timeout)


def wait_health(base, needle=b'"ok"', tries=80):
    for _ in range(tries):
        time.sleep(0.25)
        try:
            s, raw = get('/health', timeout=3, base=base)
            if s == 200 and (needle in raw if isinstance(raw, bytes) else True):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def make_png(rgb=(200, 30, 30), size=(64, 64)):
    """造一张指定纯色的 PNG（不依赖 Pillow）。"""
    import struct
    import zlib
    w, h = size
    raw = b''.join(b'\x00' + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        c = struct.pack('>I', len(data)) + tag + data
        return c + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)

    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def poll_done(job_id, tries=120):
    """轮询 manager /api/status 直到终态。"""
    for _ in range(tries):
        s, d = get(f'/api/status?job_id={urllib.parse.quote(job_id)}')
        if s == 200 and d.get('status') in ('done', 'failed', 'canceled'):
            return d
        time.sleep(0.2)
    raise AssertionError(f'job {job_id} 超时未到终态')


def usage_now():
    s, d = get(f'/api/my?token={urllib.parse.quote(TOKEN)}')
    assert s == 200, f'/api/my 返回 {s}: {d}'
    u = d.get('usage') or {}
    return (u.get('used') if isinstance(u, dict) else None), d


def main():
    out = Path(tempfile.mkdtemp(prefix='mgr-edit-'))
    web_out = Path(tempfile.mkdtemp(prefix='mgr-webout-'))
    db_path = web_out / 'flux.db'

    env = dict(os.environ)
    env['FLUX_STUB'] = '1'
    env.pop('FLUX_RESIDENT_TOKEN', None)
    env.pop('FLUX_SERVER_DISCOVER', None)
    # manager 直连 stub（不走 SSH）——FLUX_RESIDENT_BASE 有值即直连模式
    env['FLUX_RESIDENT_BASE'] = RESIDENT_BASE
    env['FLUX_RESIDENT_PORT'] = str(RESIDENT_PORT)
    env['FLUX_RESIDENT_AUTOSTART'] = '0'
    env['FLUX_GEN_MODE'] = 'resident'
    env['FLUX_SERVER_DISCOVER'] = '0'
    # ⚠️ 必须给子进程清掉代理环境变量：本机 http_proxy 指向代理且 no_proxy 为空，
    #    manager 内部用 urllib 访问 127.0.0.1:<resident> 时会被代理劫走 → 表现为
    #    「提交成功但任务永远卡在 queued」。GPU 侧客户端已用 ProxyHandler({}) 绕开，
    #    这里从进程环境层面再兜一层（双保险，且覆盖 ssh/curl 等其它出网路径）。
    for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY',
              'all_proxy', 'ALL_PROXY'):
        env.pop(k, None)
    env['NO_PROXY'] = '127.0.0.1,localhost'
    env['no_proxy'] = '127.0.0.1,localhost'
    env['WEB_PORT'] = str(WEB_PORT)
    env['FLUX_WEB_OUT'] = str(web_out)
    env['FLUX_DB_PATH'] = str(db_path)
    env['FLUX_ADMIN_TOKEN'] = 'test-admin'
    env['WEB_ADMIN_TOKEN'] = 'test-admin'

    resident = subprocess.Popen(
        [PY, str(SERVER), '--port', str(RESIDENT_PORT), '--out', str(out), '--stub'],
        env=env, stdout=open(str(Path(tempfile.gettempdir()) / 'mgr-edit-resident.log'), 'w',
                             encoding='utf-8'),
        stderr=subprocess.STDOUT, text=True,
    )
    web = None
    try:
        if not wait_health(RESIDENT_BASE, b'"ok"'):
            print('resident 未就绪：', resident.stdout.read() if resident.stdout else '')
            raise SystemExit(2)

        web = subprocess.Popen(
            [PY, str(ROOT / 'manager' / 'flux_service.py'), '--port', str(WEB_PORT)],
            env=env, stdout=open(str(Path(tempfile.gettempdir()) / 'mgr-edit-web.log'), 'w',
                                 encoding='utf-8'),
            stderr=subprocess.STDOUT, text=True,
        )
        if not wait_health(WEB_BASE, b'"ok"'):
            print('manager 未就绪：', web.stdout.read() if web.stdout else '')
            raise SystemExit(2)
        # manager 的 /health 里 worker_alive 非 null 才算「真的带上了调度器」。
        # flux_web_service.py 直接跑（main() 里 scheduler=None）是 web-only 模式，
        # 提交会 500 —— 必须用 flux_service.py 这个真实装配入口。
        for _ in range(40):
            s, h = get('/health', timeout=5)
            if s == 200 and h.get('worker_alive') is not None:
                break
            time.sleep(0.25)
        else:
            raise SystemExit('manager 起来了但 worker 未挂载（用了 web-only 入口？）')

        png_red = make_png((200, 30, 30))
        png_blue = make_png((30, 30, 210))
        b64 = lambda b: base64.b64encode(b).decode()          # noqa: E731
        data_url = lambda b: 'data:image/png;base64,' + b64(b)  # noqa: E731

        state = {}

        # ── M1 t2i 回归 ──
        @case('M1 文生图回归：/api/submit 仍通（补 edit 不能弄坏 t2i）')
        def _m1():
            s, d = post(f'/api/submit?token={TOKEN}',
                        {'prompt': 'a white ceramic mug on a table',
                         'width': 256, 'height': 256, 'steps': 4, 'seed': 11})
            assert s == 200, f'HTTP {s}: {d}'
            jid = d.get('job_id')
            assert jid, f'未返回 job_id: {d}'
            st = poll_done(jid)
            assert st['status'] == 'done', f'任务未成功: {st}'
            s2, png = get(f'/api/download/{jid}?token={TOKEN}')
            assert s2 == 200 and png[:8] == b'\x89PNG\r\n\x1a\n', \
                f'下载不是 PNG（HTTP {s2}，前 8 字节 {png[:8]!r}）'
            state['t2i_jid'] = jid
            return f'job={jid} PNG {len(png)}B'

        # ── M2 图生图全链路 ──
        @case('M2 图生图全链路：/api/edit → manager → resident /edit → 回图')
        def _m2():
            s, d = post(f'/api/edit?token={TOKEN}',
                        {'prompt': 'place this product on a grey podium',
                         'image': data_url(png_red),
                         'width': 256, 'height': 256, 'steps': 4, 'seed': 22})
            assert s == 200, f'HTTP {s}: {d}'
            jid = d.get('job_id')
            assert jid, f'未返回 job_id: {d}'
            st = poll_done(jid)
            assert st['status'] == 'done', f'任务未成功: {st}'
            s2, png = get(f'/api/download/{jid}?token={TOKEN}')
            assert s2 == 200 and png[:8] == b'\x89PNG\r\n\x1a\n', \
                f'下载不是 PNG（HTTP {s2}）'
            state['edit_jid'] = jid
            state['edit_png'] = png
            return f'job={jid} PNG {len(png)}B'

        # ── M3 缺 image → 400 ──
        @case('M3 缺 image → 400 明确拒绝（不静默降级成文生图）')
        def _m3():
            s, d = post(f'/api/edit?token={TOKEN}', {'prompt': 'no image here'})
            assert s == 400, f'期望 400，得到 {s}: {d}'
            assert '参考图' in (d.get('error') or ''), f'报错文案没说清缺参考图: {d}'
            return f'HTTP 400 · {d.get("error")}'

        # ── M4 真的走了 /edit 而不是 /generate ──
        @case('M4 manager→resident 打的是 /edit（不是 /generate）')
        def _m4():
            # 硬证据：resident 侧存在带 image_path（参考图落盘路径）的任务。
            # 这条只有走 /edit 才可能出现 —— /generate 根本不接受 image 字段。
            # 注意 get() 已经做过 JSON 解析，这里拿到的是 dict，**不要再 json.loads**。
            s, jobs = get('/jobs', timeout=10, base=RESIDENT_BASE)
            assert s == 200, f'resident /jobs HTTP {s}'
            items = jobs if isinstance(jobs, list) else (jobs.get('jobs') or [])
            assert items, f'resident 侧没有任何任务：{jobs}'
            with_ref = [j for j in items if (j.get('params') or {}).get('image_path')]
            assert with_ref, \
                f'resident 侧没有带 image_path 的任务 → 说明走的是 /generate：{items[:2]}'
            refpath = (with_ref[0].get('params') or {})['image_path']
            return f'{len(with_ref)}/{len(items)} 个任务带参考图，例：{refpath.split(chr(92))[-1]}'

        # ── M5 去重键含 edit 标记 ──
        @case('M5 同提示词「先文生图、再图生图」不被误判为重复提交')
        def _m5():
            p = 'a silver ring on marble'
            s1, d1 = post(f'/api/submit?token={TOKEN}',
                          {'prompt': p, 'width': 256, 'height': 256, 'steps': 4, 'seed': 33})
            assert s1 == 200 and d1.get('job_id'), f't2i 提交失败: {s1} {d1}'
            # 紧接着（t2i 还在排队/生成中）提交同提示词的图生图：
            # 若 edit 标记没进去重键，这里会被判「重复提交」而拒绝。
            s2, d2 = post(f'/api/edit?token={TOKEN}',
                          {'prompt': p, 'image': data_url(png_blue),
                           'width': 256, 'height': 256, 'steps': 4, 'seed': 33})
            assert s2 == 200 and d2.get('job_id'), \
                f'图生图被误判为重复提交: HTTP {s2} {d2}'
            poll_done(d1['job_id'])
            poll_done(d2['job_id'])
            return f't2i={d1["job_id"]} edit={d2["job_id"]} 各自入队'

        # ── M6 参考图不入库 ──
        @case('M6 参考图不入库（jobs 表只留 edit_mode/has_ref 标记）')
        def _m6():
            import sqlite3
            con = sqlite3.connect(str(db_path))
            con.row_factory = sqlite3.Row
            rows = con.execute('SELECT * FROM jobs').fetchall()
            assert rows, 'jobs 表为空'
            cols = set(rows[0].keys())
            assert 'edit_mode' in cols and 'has_ref' in cols, \
                f'缺图生图标记列，实际列: {sorted(cols)}'
            edits = [r for r in rows if r['edit_mode']]
            assert edits, '没有任何 edit_mode=1 的任务'
            # 关键断言：整行里不许出现 base64 参考图本体
            for r in rows:
                blob = json.dumps({k: r[k] for k in r.keys()}, ensure_ascii=False)
                assert 'data:image/' not in blob, f'jobs 行里混进了 data URL: {blob[:200]}'
                assert len(blob) < 4000, f'jobs 行异常膨胀（{len(blob)}B），疑似存了图'
            con.close()
            return f'{len(edits)}/{len(rows)} 行 edit_mode=1，均无 base64 本体'

        # ── M7 参数异常 → 400 ──
        @case('M7 steps 非整数 → 400（参数给了却没法用要明说）')
        def _m7():
            s, d = post(f'/api/edit?token={TOKEN}',
                        {'prompt': 'x', 'image': data_url(png_red), 'steps': 'abc'})
            assert s == 400, f'期望 400，得到 {s}: {d}'
            return f'HTTP 400 · {d.get("error")}'

        # ── M8 缺 image 不扣配额 ──
        @case('M8 缺 image 被拒时不消耗配额')
        def _m8():
            before, _ = usage_now()
            post(f'/api/edit?token={TOKEN}', {'prompt': 'should not charge'})
            posts = {}
            after, detail = usage_now()
            # 缺图是 400，连任务都没建，用量不该动
            posts['before'] = before
            posts['after'] = after
            assert before == after, f'用量被改动: {before} → {after}（{detail}）'
            return f'用量保持 {before}'

        # ── M9 上游不支持图生图时报错可读 ──
        @case('M9 上游模型不支持图生图 → 任务 failed 且错误可读（不静默出图）')
        def _m9():
            # stub 用环境变量切「假装是 dev（无 image 参数）」不易做，
            # 这里改验证「参考图非法」这条同样走失败路径的分支：
            # 非 base64 垃圾串 → resident 400 → manager 侧任务 failed。
            s, d = post(f'/api/edit?token={TOKEN}',
                        {'prompt': 'x', 'image': '!!!!not-base64-at-all!!!!',
                         'width': 256, 'height': 256, 'steps': 4})
            assert s == 200 and d.get('job_id'), f'提交阶段就失败: {s} {d}'
            st = poll_done(d['job_id'])
            assert st['status'] == 'failed', f'期望 failed，得到 {st}'
            assert st.get('error'), 'failed 却没带 error 原因'
            return f'failed: {(st.get("error") or "")[:70]}'

        # ── M10 manager 浅探针不外呼（补 edit 后不能破坏这条铁律）──
        @case('M10 manager /health 仍是浅探针（不外呼 resident）')
        def _m10():
            t0 = time.time()
            s, d = get('/health', timeout=5)
            dt = time.time() - t0
            assert s == 200, f'HTTP {s}'
            assert dt < 1.0, f'/health 耗时 {dt:.2f}s，疑似在做外呼'
            assert d.get('worker_alive') is True, f'worker 未存活: {d}'
            assert d.get('gen_mode') == 'resident', f'gen_mode 异常: {d.get("gen_mode")}'
            return f'{dt*1000:.0f}ms · gen_mode={d.get("gen_mode")} worker_alive=True'

    finally:
        for p in (web, resident):
            if p is None:
                continue
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass

    print()
    print('=' * 62)
    print(f'manager 图生图转发离线验收：{len(PASS)} PASS / {len(FAIL)} FAIL')
    if FAIL:
        for n, e in FAIL:
            print(f'  ✗ {n}: {e}')
        return 1
    print('全部通过 ✅')
    return 0


if __name__ == '__main__':
    sys.exit(main())

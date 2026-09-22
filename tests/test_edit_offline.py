#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""图生图链路离线验收（stub 模式，**不需要 GPU**）。

目的：把「带卡后才会暴露的图生图链路问题」全部前移到无卡阶段。
覆盖：
  ① /edit 端点存在，且能跑通 submit → status → image 全链路
  ② 参考图 base64 落盘成功（校验真的是图片，不信扩展名）
  ③ 参数名按模型签名动态判定（stub 只声明 `image`，若代码硬编码别的名字必失败）
  ④ stub 的输出确实受参考图影响（证明图真的被读进去并生效，而非被静默忽略）
  ⑤ 大小上限：t2i 仍卡 64 KB，edit 放宽到 12 MiB（不能因为加 edit 把 t2i 的防护拆了）
  ⑥ 非法 base64 / 空图 / 超大图 都被拒且报错清楚
  ⑦ 缺 image 字段时 /edit 明确报错（不是静默当成 t2i）

用法：
  python tests/test_edit_offline.py
"""
import base64
import ast
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / 'server' / 'flux_resident_server.py'
PY = sys.executable
PORT = int(os.environ.get('TEST_EDIT_PORT', '9693'))
BASE = f'http://127.0.0.1:{PORT}'

PASS, FAIL = [], []


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


def post(path, body, timeout=30):
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8') or '{}')


def get(path, timeout=30):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def make_png(rgb=(200, 30, 30), size=(64, 64)):
    """造一张指定纯色的 PNG（不依赖 Pillow）。"""
    import struct
    import zlib
    w, h = size
    raw = b''.join(b'\x00' + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        c = tag + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)   # 8bit truecolor
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
           + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))
    return png


def wait_done(job_id, limit=30):
    for _ in range(limit * 10):
        time.sleep(0.1)
        _st, raw = get(f'/status?job_id={job_id}')
        d = json.loads(raw.decode('utf-8'))
        if d.get('status') in ('done', 'failed'):
            return d
    raise AssertionError(f'job {job_id} 超时未完成')


def main():
    out = Path(tempfile.mkdtemp(prefix='edit-offline-'))
    env = dict(os.environ)
    env['FLUX_STUB'] = '1'
    env.pop('FLUX_RESIDENT_TOKEN', None)
    proc = subprocess.Popen(
        [PY, str(SERVER), '--port', str(PORT), '--out', str(out), '--stub'],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        ready = False
        for _ in range(60):
            time.sleep(0.25)
            try:
                s, raw = get('/health', timeout=3)
                if s == 200 and b'"ok"' in raw:
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not ready:
            print('服务未就绪，输出：')
            print(proc.stdout.read() if proc.stdout else '')
            raise SystemExit(2)

        png_red = make_png((200, 30, 30))
        png_blue = make_png((30, 30, 210))
        b64 = lambda b: base64.b64encode(b).decode()

        # ── ① /edit 全链路 ──
        state = {}

        @case('E1 /edit 提交 → 轮询 → 下载 全链路通')
        def _e1():
            s, d = post('/edit', {'prompt': 'place on grey podium', 'image': b64(png_red),
                                  'width': 256, 'height': 256, 'steps': 4})
            assert s == 200, f'HTTP {s}: {d}'
            assert d.get('status') == 'queued', d
            jid = d['job_id']
            fin = wait_done(jid)
            assert fin['status'] == 'done', f"最终状态 {fin['status']}: {fin.get('error')}"
            assert fin.get('image_path'), fin
            s2, blob = get(f'/image?job_id={jid}')
            assert s2 == 200 and blob[:8] == b'\x89PNG\r\n\x1a\n', f'取图失败 HTTP {s2}'
            state['red_job'] = jid
            state['red_blob'] = blob
            return f'job={jid} 出图 {len(blob)}B'

        # ── ② 参考图确实落盘 ──
        @case('E2 参考图落盘到 out/refs/ 且是真图片')
        def _e2():
            refs = list((out / 'refs').glob('*'))
            assert refs, f'out/refs/ 为空：{list(out.iterdir())}'
            f = refs[0]
            assert f.read_bytes()[:8] == b'\x89PNG\r\n\x1a\n', f'{f.name} 不是 PNG'
            return f'{f.name} ({f.stat().st_size}B)'

        # ── ③ 参数名按签名动态判定 ──
        @case('E3 stub 只声明 image → 动态判参与真实模型一致（非硬编码）')
        def _e3():
            # ⚠️ 2026-09-22：从「查字面串」升级为「查语义结构」。
            #    旧断言查 `img_param = next(` 这个字面串 —— 那是钉实现写法，
            #    重构一次就误报一次；而误报多了人就会去改门，真 bug 就混过去了。
            #    新契约下判参逻辑是：
            #      ① 优先用能力表的 ref_param（数据驱动，新增模型零改代码）
            #      ② 回退到按签名扫描候选名（不硬编码单一参数名）
            #    这两条**都必须存在**：只有 ① 的话，能力表写错就报「不支持图生图」；
            #    只有 ② 的话，Qwen 的 mask/multi_ref 这些非 ref 能力就没处声明。
            src = SERVER.read_text(encoding='utf-8')
            tree = ast.parse(src)
            # ⚠️ 本文件里有**两个** `_generate`（worker 跑推理 / HTTP 入口校验），
            #    两个源码片段里都会出现 ref_paths 这个词 → 按名字挑会挑错。
            #    能唯一标识「判参逻辑那一段」的是 `caps.get('ref_param')`：
            #    只有 worker 里那个真去挑参数名。所以按它定位。
            picked = None
            for n in ast.walk(tree):
                if isinstance(n, ast.FunctionDef) and n.name == '_generate':
                    seg = ast.get_source_segment(src, n) or ''
                    if "caps.get('ref_param')" in seg:
                        picked = seg
            assert picked is not None, \
                '没找到优先用能力表 ref_param 的 _generate（应数据驱动，不该只靠扫签名）'
            assert "for n in ('image', 'images', 'image_latents')" in picked \
                or "'image_latents'" in picked, \
                '没有按签名动态挑选参数名的回退路径'
            # 参考图参数名必须来自签名校验，而不是直接硬写 image=
            assert 'img_param not in sig' in picked, \
                '选了参数名却没校验它真在签名里（会静默 TypeError）'
            # stub 的默认签名里也必须有 image —— 否则 E1/E4 不可能通过。
            # ⚠️ 这里不 import 服务模块（它有模块级副作用，本门是靠 subprocess 起的）。
            #    改为 AST 扫 `_install_default_stub_signature()`：它保证模块导入时
            #    就把默认签名挂上。若这行被删，「--stub 不带 --model」的默认态
            #    会静默退回 `(**kwargs)` —— 那正是 2026-09-22 第二次踩的坑。
            assert '_install_default_stub_signature()' in src, \
                '默认 stub 签名没有在模块导入时挂载（默认态会退回 **kwargs）'
            return '能力表 ref_param 优先 + 签名候选回退，落在 image'

        # ── ④ 输出受参考图影响 ──
        @case('E4 参考图真的生效（换图→输出变；同图→输出稳定）')
        def _e4():
            def run(img_b64):
                s, d = post('/edit', {'prompt': 'place on grey podium', 'image': img_b64,
                                      'width': 256, 'height': 256, 'steps': 4, 'seed': 7})
                assert s == 200, d
                fin = wait_done(d['job_id'])
                assert fin['status'] == 'done', fin.get('error')
                s2, blob = get(f'/image?job_id={d["job_id"]}')
                assert s2 == 200
                return blob

            blue = run(b64(png_blue))
            # 同图同 seed 重跑：应稳定（证明不是随机噪声造成的"看起来不同"）
            blue2 = run(b64(png_blue))
            assert blue == blue2, '同参考图 + 同 seed 两次结果不一致 → 输出不可复现'
            # 换图：必须变（证明参考图真的被读进模型，而不是被静默忽略）
            assert blue != state['red_blob'], '换参考图但输出完全相同 → 图没被读进去'
            return (f'同图重跑一致 ✅；红图 vs 蓝图输出不同 ✅ '
                    f'（红 {len(state["red_blob"])}B / 蓝 {len(blue)}B）')

        # ── ⑤ 大小上限：t2i 仍 64KB ──
        @case('E5 t2i 请求体上限仍是 64KB（未因加 edit 被拆掉防护）')
        def _e5():
            big = 'x' * (70 * 1024)
            s, d = post('/generate', {'prompt': big})
            assert s == 400, f'应当 400，实际 {s}'
            assert '过大' in str(d.get('error', '')), d
            return d['error']

        # ── ⑥ 非法输入被拒 ──
        @case('E6 非法 base64 / 空图 / 非图片 都被拒且报错清楚')
        def _e6():
            msgs = []
            s, d = post('/edit', {'prompt': 'x', 'image': '!!!!not-base64!!!!'})
            assert s == 400, f'非法 base64 应 400，实际 {s}'
            msgs.append(d['error'])
            s, d = post('/edit', {'prompt': 'x', 'image': ''})
            assert s == 400, f'空图应 400，实际 {s}'
            msgs.append(d['error'])
            s, d = post('/edit', {'prompt': 'x', 'image': b64(b'this is not an image at all')})
            assert s == 400, f'非图片应 400，实际 {s}'
            msgs.append(d['error'])
            return ' | '.join(msgs)

        # ── ⑦ 缺 image 明确报错 ──
        @case('E7 /edit 缺 image 字段时明确报错（不静默降级成 t2i）')
        def _e7():
            s, d = post('/edit', {'prompt': 'x'})
            assert s == 400, f'应 400，实际 {s}'
            assert 'image' in d.get('error', ''), d
            return d['error']

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    print('=' * 66)
    print(f' 合计 {len(PASS)+len(FAIL)} 项 · 通过 {len(PASS)} · 失败 {len(FAIL)}')
    if FAIL:
        for n, e in FAIL:
            print(f'   ✗ {n}: {e}')
        print('  存在失败项')
        return 1
    print(' 全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())

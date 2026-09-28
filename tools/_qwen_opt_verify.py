#!/usr/bin/env python3
"""验证 2026-09-28 的 Qwen 优化在真机上确实生效（端到端，非离线断言）。

验四条
  V1  /edit **不传尺寸** → 输出跟随参考图宽高比（官方 ratio_follow）+ 灰带≈0
  V2  /edit **显式传尺寸** → 尊重用户（仍为 1024×1024）
  V3  /generate transparent=true → 套官方 RGBA 模板 + 输出真透明（alpha.min()==0）
  V4  /edit 的 mask 入参仍被拒（回归：优化不该破坏既有硬闸）

判据说明
  · 灰带用「逐行是否为中灰(127,127,127)」判定，与实现解耦
  · 透明用 alpha.min()==0（**不用** mode=='RGBA' —— 那是容器型断言，会假绿）
"""
import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = 'http://127.0.0.1:9630'
REF = '/root/autodl-tmp/flux-t2i/ob-refs'
OUT = '/root/autodl-tmp/flux-t2i/_qwen_opt_verify.json'
GRAY = (127, 127, 127)
RES = []


def ok(name, cond, detail=''):
    RES.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ''), flush=True)


def enc(name, max_side=1024):
    from PIL import Image
    raw = open(os.path.join(REF, name), 'rb').read()
    im = Image.open(io.BytesIO(raw)).convert('RGB')
    w, h = im.size
    s = max_side / max(w, h)
    if s < 1:
        im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=85)
    return base64.b64encode(buf.getvalue()).decode(), im.size


def req(method, path, payload=None):
    r = urllib.request.Request(API + path,
                               data=json.dumps(payload).encode() if payload is not None else None,
                               method=method, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b'{}')
        except Exception:
            return e.code, {}


def wait(job, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(5)
        _, d = req('GET', '/jobs')
        for j in (d.get('jobs') or []):
            if j.get('job_id') == job and j.get('status') in ('done', 'failed'):
                return j, round(time.time() - t0, 1)
    return {}, round(time.time() - t0, 1)


def analyze(path):
    """返回 (W, H, mode, a_min, a0_pct, 灰带上下像素)。"""
    from PIL import Image
    import numpy as np
    im = Image.open(path)
    W, H = im.size
    a = np.array(im.convert('RGBA'))[:, :, 3]
    rgb = np.array(im.convert('RGB'))
    top = 0
    while top < H and tuple(rgb[top, W // 2]) == GRAY and rgb[top].std() < 4:
        top += 1
    bot = 0
    while bot < H and tuple(rgb[H - 1 - bot, W // 2]) == GRAY and rgb[H - 1 - bot].std() < 4:
        bot += 1
    return W, H, im.mode, int(a.min()), round((a == 0).mean() * 100, 2), top, bot


def fetch(job_id, dst):
    """经 resident 的 /download 把产物取回本地（若没这个端点则直接从磁盘拷）。"""
    import shutil
    src = f'/root/autodl-tmp/flux-t2i/resident_out/{job_id}.png'
    shutil.copy(src, dst)
    return dst


print('══ V1. /edit 不传尺寸 → 跟随参考图宽高比 + 灰带≈0 ══', flush=True)
b, size = enc('img-018.png')                       # 1024×681（3:2）
st, d = req('POST', '/edit', {'prompt': '在图中白色涂抹的区域添加一名潜水员',
                             'image': b, 'steps': 20, 'true_cfg_scale': 4.0, 'seed': 42})
print('  提交:', st, d, flush=True)
job = d.get('job_id')
j, rt = wait(job)
print('  完成:', j.get('status'), f'{rt}s', flush=True)
if j.get('status') == 'done':
    W, H, mode, amin, a0, top, bot = analyze(fetch(job, '/tmp/_v1.png'))
    ratio_ref, ratio_out = size[0] / size[1], W / H
    ok('V1a 输出跟随参考图宽高比（非方形）',
       abs(ratio_out - ratio_ref) / ratio_ref < 0.05,
       f'参考 {size} 比例 {ratio_ref:.3f} → 输出 {W}×{H} 比例 {ratio_out:.3f}')
    ok('V1b 输出不是方形', W != H, f'{W}×{H}')
    ok('V1c 灰带 ≈ 0（修复生效）', top + bot <= max(4, int(H * 0.01)),
       f'灰带 top={top} bot={bot}（基线方形画布时 3:2 参考图灰带 171/172）')
else:
    ok('V1 执行成功', False, f"status={j.get('status')} err={str(j.get('error'))[:150]}")
print(f'  → 对照：修复前 1024×1024 + 灰带 171/171', flush=True)

print('\n══ V2. /edit 显式传尺寸 → 尊重用户 ══', flush=True)
st, d = req('POST', '/edit', {'prompt': '在图中白色涂抹的区域添加一名潜水员',
                             'image': b, 'width': 1024, 'height': 1024,
                             'steps': 20, 'true_cfg_scale': 4.0, 'seed': 42})
job = d.get('job_id')
j, rt = wait(job)
if j.get('status') == 'done':
    W, H, *_ = analyze(fetch(job, '/tmp/_v2.png'))
    ok('V2 显式 1024×1024 被尊重', (W, H) == (1024, 1024), f'实际 {W}×{H}')
else:
    ok('V2 执行成功', False, f"status={j.get('status')}")

print('\n══ V3. /generate transparent=true → 官方模板 + 真透明 ══', flush=True)
st, d = req('POST', '/generate', {'prompt': '一只可爱的卡通龙贴纸',
                                 'transparent': True, 'steps': 20, 'true_cfg_scale': 1.0,
                                 'seed': 42, 'width': 1024, 'height': 1024})
print('  提交:', st, d, flush=True)
job = d.get('job_id')
j, rt = wait(job)
if j.get('status') == 'done':
    W, H, mode, amin, a0, top, bot = analyze(fetch(job, '/tmp/_v3.png'))
    ok('V3a 输出真透明（alpha.min()==0）', amin == 0, f'mode={mode} a_min={amin} a0={a0}%')
    ok('V3b 透明占比合理（>1%）', a0 > 1.0, f'a0={a0}%')
    ok('V3c 任务记录里有 transparent 留痕',
       bool((j.get('params') or {}).get('transparent')), f"params.transparent={j.get('params')}")
else:
    ok('V3 执行成功', False, f"status={j.get('status')} err={str(j.get('error'))[:150]}")

print('\n══ V4. 回归：mask 入参仍被拒 ══', flush=True)
st, d = req('POST', '/edit', {'prompt': '改背景', 'image': b, 'mask': b,
                             'steps': 20, 'true_cfg_scale': 1.0})
ok('V4 mask 仍返回 400 且文案引导合成', st == 400 and 'mask' in json.dumps(d, ensure_ascii=False),
   f'HTTP {st} {str(d)[:120]}')

n_pass = sum(1 for _, p, _ in RES if p)
n_fail = len(RES) - n_pass
print('\n' + '=' * 62)
print(f'  Qwen 优化真机验证：{n_pass} PASS / {n_fail} FAIL')
for n, p, d in RES:
    if not p:
        print(f'    - {n}  {d}')
print('=' * 62)
json.dump(RES, open(OUT, 'w'), ensure_ascii=False, indent=2)
sys.exit(1 if n_fail else 0)

#!/usr/bin/env python3
"""Qwen-Image「图片参考 / 编辑」用例批量执行（一次性工具）。

为什么单独写：用例有 6+ 条、每条要 POST 后轮询到 done/failed，
手工敲 curl 既慢又容易漏判据。这个脚本把「提交 → 轮询 → 记录」串起来，
结果落 JSON，产物留在 resident_out/ 供下载做机器断言。

★ 关键：参考图先做**前端式压缩**（长边 ≤1024、JPEG q85），两个目的
  ① 让「10 张参考」不至于爆掉 MAX_EDIT_BYTES=12MB
  ② 顺带验证方案里「前端压缩」这一步是必要且可行的
  掩码类小图（<200KB）**保持 PNG 原样**，避免 JPEG 破坏纯色掩码。

用法：/root/autodl-tmp/envs/qwen/bin/python _qwen_ref_tests.py [caseid ...]
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
RESULT = '/root/autodl-tmp/flux-t2i/_qwen_ref_results.json'
MAX_SIDE = 1024
JPEG_Q = 85
SMALL_KEEP = 200 * 1024          # 小于此体积的图原样保留（掩码/小图）


def prep(name):
    """读参考图 → 压缩 → 返回 (base64, 说明)。模拟前端的 PreparedRef。"""
    p = os.path.join(REF, name)
    raw = open(p, 'rb').read()
    if len(raw) <= SMALL_KEEP:
        return base64.b64encode(raw).decode(), f'{name} {len(raw)}B kept-as-is'
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    im = im.convert('RGB')
    w, h = im.size
    scale = MAX_SIDE / max(w, h)
    if scale < 1:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=JPEG_Q)
    b = buf.getvalue()
    return base64.b64encode(b).decode(), f'{name} {len(raw)}B -> {im.size} {len(b)}B jpeg'


def req(method, path, payload=None, timeout=90):
    url = API + path
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b'{}')
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body or b'{}')
        except Exception:
            return e.code, {'raw': body[:300].decode('utf-8', 'replace')}


def wait(job_id, timeout=1800):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        time.sleep(5)
        st, d = req('GET', '/jobs')
        for j in (d.get('jobs') or []):
            if j.get('job_id') == job_id:
                last = j
                if j.get('status') in ('done', 'failed'):
                    return j
    return last or {'job_id': job_id, 'status': 'timeout'}


# (case, 端点, prompt, 参考图列表, w, h, steps, cfg)
CASES = [
    ('B1',   '/edit',     '给这匹马戴一顶草帽', ['img-020.png'], 1024, 1024, 20, 4.0),
    ('B2C3', '/edit',     '生成一张坐在马背上的牛仔人物，保持马的姿态与原图一致',
                          ['img-020.png', 'img-021.png'], 1024, 1024, 20, 4.0),
    ('B4',   '/edit',     '把这十张参考图中的元素整合成一张协调的画面',
                          ['img-020.png', 'img-016.png', 'img-018.png', 'img-011.png',
                           'img-009.png', 'img-021.png', 'img-020.png', 'img-016.png',
                           'img-018.png', 'img-011.png'], 1024, 1024, 20, 4.0),
    ('B4a',  '/edit',     '参考这三张图生成一张协调的画面',
                          ['img-020.png', 'img-016.png', 'img-018.png'], 1024, 1024, 20, 4.0),
    ('B4b',  '/edit',     '参考这五张图生成一张协调的画面',
                          ['img-020.png', 'img-016.png', 'img-018.png', 'img-011.png',
                           'img-009.png'], 1024, 1024, 20, 4.0),
    ('B4c',  '/edit',     '参考这八张图生成一张协调的画面',
                          ['img-020.png', 'img-016.png', 'img-018.png', 'img-011.png',
                           'img-009.png', 'img-020.png', 'img-016.png', 'img-018.png'],
                          1024, 1024, 20, 4.0),
    ('C1',   '/edit',     '按图中标记编辑：去掉蓝色圆圈内的金属手表，把红色圆圈内的头发改成黑色，把绿色圆圈区域替换成灰色短袖亚麻睡衣',
                          ['img-016.png'], 1024, 1024, 20, 4.0),
    ('C2',   '/edit',     '在图中白色涂抹的区域添加一名潜水员',
                          ['img-018.png'], 1024, 1024, 20, 4.0),
    ('A2',   '/generate', 'a red apple', [], 256, 256, 20, 4.0),
    ('A3',   '/generate', 'a red apple', [], 1000, 1000, 20, 4.0),
    ('G1a',  '/generate', 'a red apple on a wooden table', [], 1024, 1024, 20, 1.0),
    ('G1b',  '/generate', 'a red apple on a wooden table', [], 1024, 1024, 20, 4.0),
    ('D1',   '/generate', 'a red apple on a pure white background, transparent PNG with alpha channel',
                          [], 1024, 1024, 20, 4.0),
    ('D2',   '/edit',     '提取画面主体，输出透明背景的图层',
                          ['img-011.png'], 1024, 1024, 20, 4.0),
    ('E1',   '/edit',     '把图中的文字 BLOOM 改成 Qwen-Image，其余保持不变',
                          ['img-009.png'], 1024, 1024, 20, 4.0),
]


def main():
    only = set(sys.argv[1:])
    results = []
    for cid, ep, prompt, imgs, w, h, steps, cfg in CASES:
        if only and cid not in only:
            continue
        payload = {'prompt': prompt, 'width': w, 'height': h, 'steps': steps,
                   'true_cfg_scale': cfg, 'seed': 42}
        notes = []
        if imgs:
            encs = []
            for n in imgs:
                b, note = prep(n)
                encs.append(b)
                notes.append(note)
            # 1 张发 image（兼容老 worker），>1 张发 images
            payload['image' if len(encs) == 1 else 'images'] = encs[0] if len(encs) == 1 else encs
        body_size = len(json.dumps(payload))
        rec = {'case': cid, 'endpoint': ep, 'n_refs': len(imgs),
               'req_bytes': body_size, 'prep': notes}
        t0 = time.time()
        st, d = req('POST', ep, payload)
        rec['http'] = st
        rec['submit'] = d
        rec['submit_sec'] = round(time.time() - t0, 1)
        print(f"[{cid}] POST {ep} n_refs={len(imgs)} {body_size}B -> HTTP {st} {d}", flush=True)
        if st == 200 and d.get('job_id'):
            j = wait(d['job_id'])
            rec.update({'job_status': j.get('status'), 'runtime': j.get('runtime'),
                        'image_path': j.get('image_path'), 'seed': j.get('seed'),
                        'error': (j.get('error') or '')[:400]})
            print(f"[{cid}] -> {j.get('status')} runtime={j.get('runtime')} "
                  f"path={j.get('image_path')} err={(j.get('error') or '')[:120]}", flush=True)
        results.append(rec)
    json.dump(results, open(RESULT, 'w'), ensure_ascii=False, indent=2)
    print('WROTE', RESULT, flush=True)


if __name__ == '__main__':
    main()

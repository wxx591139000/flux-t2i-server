#!/usr/bin/env python3
"""Qwen-Image-2.1「参考图张数 vs 显存峰值」探针 —— 回答「32G 够不够 / 要不要 48G」。

方法
  每条用例提交前起一个 nvidia-smi 采样线程（0.5s 一次），任务结束取峰值。
  这样得到的是**真实峰值**，不是从 OOM 报错反推。

判据
  · 峰值显存（MiB）—— 硬件事实
  · 是否 OOM（任务 failed 且 error 含 OutOfMemory）
  · 单张增量 = (peak_n - peak_1) / (n - 1)，用于外推 48G 的安全张数

用法（远端）
  /root/autodl-tmp/envs/qwen/bin/python _qwen_vram_probe.py 1 2 3 4 5
"""
import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

API = 'http://127.0.0.1:9630'
REF = '/root/autodl-tmp/flux-t2i/ob-refs'
OUT = '/root/autodl-tmp/flux-t2i/_qwen_vram_probe.json'
MAX_SIDE, JPEG_Q, SMALL_KEEP = 1024, 85, 200 * 1024

# 与 C2 同一条编辑指令、同一批来源，只改张数 → 变量唯一
# 远端 ob-refs 实有 6 张；超过 6 张时循环取用（内容重复不影响显存标定）
SRC = ['img-020.png', 'img-016.png', 'img-018.png',
       'img-011.png', 'img-009.png', 'img-021.png']
PROMPT = '参考这N张图生成一张协调的画面'


def prep(name):
    p = os.path.join(REF, name)
    raw = open(p, 'rb').read()
    if len(raw) <= SMALL_KEEP:
        return base64.b64encode(raw).decode(), f'{name} {len(raw)}B kept'
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert('RGB')
    w, h = im.size
    s = MAX_SIDE / max(w, h)
    if s < 1:
        im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=JPEG_Q)
    return base64.b64encode(buf.getvalue()).decode(), f'{name}->{im.size} {len(buf.getvalue())}B'


def req(method, path, payload=None, timeout=120):
    r = urllib.request.Request(API + path,
                               data=json.dumps(payload).encode() if payload is not None else None,
                               method=method, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b'{}')
        except Exception:
            return e.code, {}


class Sampler(threading.Thread):
    """0.5s 采样 nvidia-smi，记录本进程的峰值显存。"""
    def __init__(self):
        super().__init__(daemon=True)
        self.stop = False
        self.samples = []

    def run(self):
        while not self.stop:
            try:
                o = subprocess.run(
                    ['nvidia-smi', '--query-gpu=memory.used,memory.free',
                     '--format=csv,noheader,nounits'],
                    capture_output=True, text=True, timeout=5).stdout.strip()
                used, free = (int(x) for x in o.split(','))
                self.samples.append((used, free))
            except Exception:
                pass
            time.sleep(0.5)


def run_case(n):
    encs, notes = [], []
    for i in range(n):
        name = SRC[i % len(SRC)]
        b, note = prep(name)
        encs.append(b)
        notes.append(note)
    payload = {'prompt': PROMPT.replace('N', str(n)), 'width': 1024, 'height': 1024,
               'steps': 20, 'true_cfg_scale': 4.0, 'seed': 42}
    payload['image' if n == 1 else 'images'] = encs[0] if n == 1 else encs
    size = len(json.dumps(payload))

    smp = Sampler()
    smp.start()
    t0 = time.time()
    st, d = req('POST', '/edit', payload)
    job = d.get('job_id')
    final = {}
    if job:
        while time.time() - t0 < 1200:
            time.sleep(5)
            _, jl = req('GET', '/jobs')
            for j in (jl.get('jobs') or []):
                if j.get('job_id') == job and j.get('status') in ('done', 'failed'):
                    final = j
                    break
            if final:
                break
    rt = round(time.time() - t0, 1)
    time.sleep(2)                       # 让最后几帧显存回落前再采一次
    smp.stop = True
    smp.join(timeout=3)
    peak = max((u for u, _ in smp.samples), default=0)
    lo = min((f for _, f in smp.samples), default=0)
    err = (final.get('error') or '')
    rec = {'n_refs': n, 'req_bytes': size, 'http': st, 'status': final.get('status'),
           'oom': 'OutOfMemory' in err, 'runtime': rt,
           'peak_used_mib': peak, 'min_free_mib': lo,
           'idle_used_mib': smp.samples[0][0] if smp.samples else None,
           'error': err[:220], 'prep': notes}
    print(f"[{n:>2} 张] {rec['status']:<7} oom={rec['oom']!s:<5} peak={peak} MiB "
          f"min_free={lo} MiB  {rt}s  req={size}B", flush=True)
    return rec


if __name__ == '__main__':
    ns = [int(x) for x in sys.argv[1:]] or [1, 2, 3, 4, 5]
    recs = []
    for n in ns:
        recs.append(run_case(n))
        json.dump(recs, open(OUT, 'w'), ensure_ascii=False, indent=2)

    ok = [r for r in recs if r['status'] == 'done']
    bad = [r for r in recs if r['oom']]
    print('\n=== 汇总 ===')
    for r in recs:
        print(f"  {r['n_refs']:>2} 张: peak={r['peak_used_mib']} MiB "
              f"min_free={r['min_free_mib']} MiB  {'OK' if r['status'] == 'done' else r['status']}")
    if len(ok) >= 2:
        ok.sort(key=lambda r: r['n_refs'])
        slope = ((ok[-1]['peak_used_mib'] - ok[0]['peak_used_mib'])
                 / (ok[-1]['n_refs'] - ok[0]['n_refs']))
        print(f"\n单张参考图显存增量 ≈ {slope:.0f} MiB/张"
              f"（由 {ok[0]['n_refs']} 张 {ok[0]['peak_used_mib']} MiB → "
              f"{ok[-1]['n_refs']} 张 {ok[-1]['peak_used_mib']} MiB）")
        for cap in (32760, 49140):
            head = ok[0]['peak_used_mib'] - slope * ok[0]['n_refs']
            n = int((cap * 0.94 - head) / slope)   # 留 6% 余量给碎片与峰值抖动
            print(f"  外推 {cap} MiB 卡：安全张数 ≈ {n} 张（留 6% 余量）")
    print('OOM 条数:', len(bad))

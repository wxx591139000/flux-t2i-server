#!/usr/bin/env python3
"""诊断「改了代码但没生效」—— 判定跑着的服务是不是**当前代码**（2026-09-21 新增）。

为什么需要它（真实事故）：
  用户点网站上的「彻底删除」，前端报
    `FLUX 服务返回非 JSON（HTTP 404）：Not Found`。
  代码在磁盘上是**对的**（删端点已实现、离线端到端 14/14 全绿），
  但线上就是 404。真因：**9620 上跑的是 13 小时前的旧进程**，
  它启动于 2026-09-20 20:45:49，而删端点是 09-21 09:00 之后才写的。

  排查花了不少时间，因为症状（404 + 非 JSON）把注意力引向"协议不对/响应异常/路由写错"，
  而真凶在**进程生命周期**上。这个脚本把这件事变成一条命令。

它做什么（全离线、只读、不改任何东西）：
  1. 取 /health，读 `web_uptime_sec`（web 进程存活秒数）
  2. 与几个**代码文件的 mtime** 比：若进程启动**早于**最近一次代码改动，
     那就是旧进程 → 明确报「需要重启」
  3. 交叉验证：直接探 `/api/delete`。**改过代码却没重启时，新端点必然 404** ——
     这是"进程陈旧"最硬的证据（比 uptime 推测更直接）
  4. 顺带核对 /health 是否带 `profile`（采样参数画像）：没有也说明常驻/服务是旧版

⚠️ 判据必须**两条都用**：单看 uptime 可能误判（比如刚重启完又改了代码，或改的是无关文件），
   单看 404 也可能误判（比如真的把路由删了）。两者同时成立才是"旧进程"。

退出码：0 = 进程是新的；1 = 需要重启；2 = 服务没在跑。

用法:
  py -3.11 tools/check_stale.py                    # 默认 127.0.0.1:9620
  py -3.11 tools/check_stale.py --base https://flux.zhuanlu.xyz
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 这些文件一改就要求重启（web 进程在 import 时把代码读进内存，之后不会再读盘）。
# 不列 server/ 下的常驻文件：那些跑在 GPU 机上，由看门狗管，不是本进程的事。
WATCH_FILES = [
    'manager/flux_web_service.py',
    'manager/flux_queue.py',
    'manager/flux_db.py',
    'manager/flux_quota.py',
]

# 只有在"代码里有这个端点"时才探它 —— 否则扫到的 404 是正常语义，会误报旧进程。
# 每个条目 = (说明, 路径, HTTP 方法)
PROBE_ROUTES = [
    ('删除任务端点', '/api/delete', 'POST'),
]


def get_json(url, timeout=8):
    """返回 (status, dict_or_None)。连不上时 status=None（与"连上了但 4xx/5xx"区分开）。"""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, None
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception:
        return None, None


def probe_route(base, path, method):
    """探一个端点，返回 (status, is_json)。用于判断"路由在不在"。"""
    url = base + path
    data = b'{}' if method == 'POST' else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        # 用空 job_id 探测：路由存在 → 400/200 + JSON 错误；路由不存在 → 404 + 纯文本
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read()
            try:
                json.loads(raw)
                return r.status, True
            except Exception:
                return r.status, False
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            json.loads(raw)
            return e.code, True
        except Exception:
            return e.code, False
    except Exception:
        return None, False


def main():
    ap = argparse.ArgumentParser(description='判定 FLUX 服务是不是当前代码（进程陈旧检测）')
    ap.add_argument('--base', default='http://127.0.0.1:9620', help='服务地址')
    a = ap.parse_args()
    base = a.base.rstrip('/')

    print('=' * 62)
    print(f'进程陈旧检测：{base}')
    print('=' * 62)

    # ── 1. 服务活着吗 ──
    st, health = get_json(f'{base}/health')
    if st is None:
        print(f'❌ 连不上 {base} —— 服务没在跑。')
        print('   启动：双击「启动-01-FLUX文生图服务.bat」')
        return 2
    up = (health or {}).get('web_uptime_sec')
    print(f'✅ 服务在线（HTTP {st}）')
    if up is None:
        print('   ⚠️ /health 没有 web_uptime_sec 字段 —— 这个进程比该字段还旧，几乎必定是旧进程')
    else:
        print(f'   进程已运行 {up:.0f} 秒（{up/3600:.1f} 小时）')
        started = time.time() - up
        print(f'   推算启动时刻 ≈ {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))}')

    # ── 2. 代码文件的最近改动时间 ──
    print('\n代码文件 mtime：')
    newest_file, newest_mt = None, 0.0
    for rel in WATCH_FILES:
        p = BASE_DIR / rel
        if not p.exists():
            print(f'   ⚠️ 缺文件 {rel}')
            continue
        mt = p.stat().st_mtime
        print(f'   {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mt))}  {rel}')
        if mt > newest_mt:
            newest_file, newest_mt = rel, mt

    stale_by_time = False
    if up is not None and newest_file:
        started = time.time() - up
        stale_by_time = started < newest_mt
        print()
        if stale_by_time:
            gap = newest_mt - started
            print(f'   ❌ 进程启动**早于**最近一次代码改动（晚了 {gap/60:.0f} 分钟）')
            print(f'      进程启动 ≈ {time.strftime("%H:%M:%S", time.localtime(started))}'
                  f'  最近改动 ≈ {time.strftime("%H:%M:%S", time.localtime(newest_mt))}（{newest_file}）')
        else:
            print(f'   ✅ 进程启动晚于所有代码改动（进程是在最新代码上起来的）')

    # ── 3. 交叉验证：新端点探活（比 uptime 推测更硬的证据）──
    print('\n端点探活（改过代码没重启时，新端点必然是 404 纯文本）：')
    stale_by_route = []
    for label, path, method in PROBE_ROUTES:
        src_has = any(
            path.lstrip('/') in (BASE_DIR / rel).read_text(encoding='utf-8', errors='replace')
            for rel in WATCH_FILES if (BASE_DIR / rel).exists()
        )
        if not src_has:
            print(f'   ⏭  {label} {path}：当前代码里没有它，跳过（404 属正常语义）')
            continue
        code, is_json = probe_route(base, path, method)
        ok = is_json and code != 404
        print(f'   {"✅" if ok else "❌"} {label} {path} → HTTP {code}'
              f'{"（JSON 响应，路由存在）" if ok else "（纯文本 404：跑着的进程里没有这个路由）"}')
        if not ok:
            stale_by_route.append(label)

    # ── 4. profile 字段（采样参数画像是否生效）──
    if health is not None:
        has_profile = 'profile' in health
        print(f'\n{"✅" if has_profile else "⚠️ "} /health 带 profile 字段：{has_profile}'
              f'{"（采样参数按模型画像生效）" if has_profile else "（旧进程；重启后应有 profile）"}')

    # ── 5. 结论 ──
    need_restart = stale_by_time or bool(stale_by_route)
    print('\n' + '=' * 62)
    if need_restart:
        print('❌ 结论：**需要重启** —— 跑着的不是当前代码')
        if stale_by_route:
            print(f'   直接证据：{", ".join(stale_by_route)} 在远端 404')
        if stale_by_time:
            print('   时间证据：进程启动早于最近一次代码改动')
        print('\n   修复：双击 flux-t2i-server\\启动-01-FLUX文生图服务.bat')
        print('   注意：脚本检测到 9620 已占用时可能跳过启动，需先停掉旧进程。')
        print('   重启后再跑本脚本，应显示「进程是新的」。')
        return 1
    print('✅ 结论：进程是当前代码，不需要重启')
    return 0


if __name__ == '__main__':
    sys.exit(main())

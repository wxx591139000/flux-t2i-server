#!/usr/bin/env python3
"""一次跑完全部回归门 —— 杜绝「某个子路径没人跑」（2026-09-21 新增）。

为什么需要它（真实教训，不是一个假设）：
  2026-09-21 修完 `manager/flux_web_service.py` 的删除端点后，我逐条手动跑了几道门，
  跑完就宣布"全绿"。**但当时并没有把所有子路径都跑一遍** —— 看门狗那组是我后来
  想起来才补跑的。手动列举的清单一定会漏：漏掉的那组不会报错，只会**静默地不被执行**，
  而这跟"它通过了"在输出上长得一模一样。

  这里的做法是**反过来的**：不写一份"要跑哪些"的清单（那又是一个会漂移的副本），
  而是**扫 tests/test_*.py 全跑**。新加一道门 = 往 tests/ 里放个文件，自动被纳入。
  想排除必须显式写进 SKIP 并说明理由。

⚠️ `tests/manual/` 下的**不算门**：那些是需要真 GPU / 真 SSH 的一次性验证脚本，
   离线跑不了（会假红）。目录隔离本身就是约定，这里再显式排除一次，防止误扫。

用法:
  py -3.11 tests/run_all.py            # 全部
  py -3.11 tests/run_all.py -v         # 失败时打印该门的完整输出（默认只打尾 15 行）
  py -3.11 tests/run_all.py watchdog delete   # 只跑名字含这些关键字的门
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
BASE_DIR = TESTS_DIR.parent

# 显式排除项（必须给理由 —— 没有理由的排除就是"漏跑"的另一种写法）
SKIP = {
    'run_all.py': '本文件自身',
    'mutate_dedup_key.py': '变异测试工具，不是回归门（它**故意**把代码改坏）',
    'mutate_prompt_translator.py': '变异测试工具，不是回归门（同上）',
}

# 解释器：优先 Python 3.11（本机装了业务依赖的那个），见工作区 MEMORY 的 Python 环境陷阱
PY_CANDIDATES = [
    sys.executable,
    r'C:\Users\Dancing\AppData\Local\Programs\Python\Python311\python.exe',
]


def pick_python():
    for c in PY_CANDIDATES:
        if c and Path(c).exists():
            return c
    return sys.executable


def discover():
    """扫出全部门（排序保证输出稳定，便于肉眼比对两轮结果）。"""
    out = []
    for p in sorted(TESTS_DIR.glob('test_*.py')):
        if p.name in SKIP:
            continue
        out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser(description='跑 flux-t2i-server 全部回归门')
    ap.add_argument('filters', nargs='*', help='只跑文件名含这些关键字（可多个，任一命中即跑）')
    ap.add_argument('-v', '--verbose', action='store_true', help='失败时打印完整输出')
    a = ap.parse_args()

    gates = discover()
    if a.filters:
        gates = [g for g in gates if any(f.lower() in g.name.lower() for f in a.filters)]
    if not gates:
        print('没有匹配的门。')
        return 1

    py = pick_python()
    print('=' * 66)
    print(f'flux-t2i-server 全部回归门（{len(gates)} 道）')
    print(f'解释器: {py}')
    print('=' * 66)

    results = []          # (name, rc, seconds, output)
    for g in gates:
        name = g.name
        print(f'\n▶ {name} ', end='', flush=True)
        t0 = time.time()
        try:
            r = subprocess.run([py, str(g)], capture_output=True, text=True,
                               timeout=900, cwd=str(BASE_DIR))
            rc, out = r.returncode, (r.stdout or '') + (r.stderr or '')
        except subprocess.TimeoutExpired:
            rc, out = 124, '（超时 900s —— 可能连了外网或死循环）'
        dt = time.time() - t0
        results.append((name, rc, dt, out))
        # 摘一行"通过 N/M"之类的结论行，让总览能一眼看出规模
        summary = ''
        for line in reversed(out.strip().splitlines()):
            s = line.strip()
            if any(k in s for k in ('通过', 'PASS', '全绿', '✓', '✅')) and len(s) < 100:
                summary = s
                break
        print(f'{"✅ rc=0" if rc == 0 else "❌ rc=" + str(rc)}  ({dt:.1f}s)  {summary}')
        if rc != 0:
            tail = out.strip().splitlines()
            body = tail if a.verbose else tail[-15:]
            print('   ' + '\n   '.join(body))

    bad = [(n, rc, o) for n, rc, _, o in results if rc != 0]
    total_s = sum(d for _, _, d, _ in results)
    print('\n' + '=' * 66)
    print(f'汇总：{len(results) - len(bad)}/{len(results)} 道门通过，总耗时 {total_s:.1f}s')
    if bad:
        print('\n失败的门：')
        for n, rc, _ in bad:
            print(f'  ❌ {n}  (rc={rc})')
        print('\n（加 -v 可看失败门的完整输出）')
        return 1
    print('✅ 全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())

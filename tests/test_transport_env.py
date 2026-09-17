#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
传输层 & 常驻档位 —— 回归闸门（完全离线：不需要 GPU、不需要网络、不碰真队列）

为什么会有这个文件
──────────────────
2026-09-17 一天之内爆了两个互相独立的坑，症状都是「任务卡住 / 图出不来」，
但根因完全不同，且**都源于"默认值 / 失败路径选错了方向"**：

  A. ssh/scp 靠 `bash -lc` 执行（flux_server_manager.run），而 run() 原先把 bash
     当成"一定在 PATH 里"。Git for Windows 默认只把 <Git>\\cmd 放进 PATH（里面只有
     git.exe），bash.exe 在 <Git>\\bin —— 不在 PATH。
       · 从 Git Bash 里起的服务      → 找得到 bash → 一切正常
       · 从资源管理器双击 .bat 起的服务 → 找不到 bash → FileNotFoundError 被
         `except Exception` 吞成 (False, '...') → 每台机器 ~0.07s 就"不可达"
         → 三台全被判「SSH 不通（可能关机）」→ waiting 池永久卡死、健康监控误报。
     教训：**「本机缺工具」绝不能退化成「远程机器不可达」**。

  B. 拉起远端常驻服务时透传 FLUX_OFFLOAD，旧代码默认值是 `none`（全程显存）。
     32G 卡（RTX 4080 SUPER 32760 MiB）装 fp16 权重约 31.2 GiB + 推理激活值
     → 必然 "CUDA out of memory, total capacity 31.48 GiB"，每张图都失败。
     教训：**默认值要选「一定能跑」，不是「最快」**；想追速度的机器自己声明。

这两条都是"知识型约束"，光改一次代码不能防止下次被改回去 —— 所以固化成断言，
改动 flux_server_manager.py / flux_resident_client.py 后跑一遍，几秒钟出结果。

跑法
────
    <python> tests\\test_transport_env.py
    全部通过 → stdout 每行 `▸ PASS ...`，exit 0
    任一失败 → 打印失败详情，exit 1

    Windows 上建议显式调用与线上一致的解释器：
    C:\\Users\\Dancing\\AppData\\Local\\Programs\\Python\\Python311\\python.exe tests\\test_transport_env.py
"""
import os
import sys
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = sys.executable

PRELUDE = (
    'import os, sys\n'
    f'sys.path.insert(0, r"{BASE}")\n'
)

CASES = []


def case(name, title):
    def deco(fn):
        CASES.append((name, title, fn))
        return fn
    return deco


# ── A 组：bash 定位与失败可观测性 ────────────────────────────────────────

@case('A1', 'PATH 里没有 bash 时（模拟双击 .bat 的环境）仍能定位到 bash')
def _a1():
    return r'''
import shutil
from pathlib import Path
g = shutil.which('git')
assert g, '用例前提不成立：当前 PATH 里找不到 git'
gitroot = Path(g).resolve().parent.parent
# 复刻真实双击场景：PATH 里有 <Git>\cmd（只有 git.exe）与 System32，但没有 <Git>\bin
os.environ['PATH'] = r'C:\Windows\System32' + os.pathsep + str(gitroot / 'cmd')
assert shutil.which('bash') is None, '用例前提不成立：构造后的 PATH 里仍有 bash'
from manager import flux_server_manager as fsm
b = fsm.find_bash()
assert b, '受限 PATH 下 find_bash() 返回 None —— 又退化成"服务器不可达"了'
assert Path(b).is_file(), f'定位到的 bash 不存在: {b}'
print('PASS|', '定位到', b)
'''


@case('A2', '真的没有 bash 时，run() 明确报 NO_BASH 而不是静默失败')
def _a2():
    return r'''
from manager import flux_server_manager as fsm
fsm.find_bash = lambda: None          # 模拟"本机确实没有 bash"
ok, msg = fsm.run('echo hello')
assert ok is False, '无 bash 时 run() 竟然报成功'
assert 'NO_BASH' in msg, f'错误信息没有点明缺 bash，运维会被引向"服务器关机": {msg!r}'
print('PASS|', msg[:70])
'''


@case('A3', 'run() 失败时带回 stderr（旧版只取 stdout，ssh 的报错全丢）')
def _a3():
    return r'''
from manager import flux_server_manager as fsm
ok, msg = fsm.run('echo boom-stderr >&2; exit 3')
assert ok is False, '非零退出码被判成功'
assert 'boom-stderr' in msg, f'失败原因丢失（stderr 没带回来）: {msg!r}'
print('PASS|', msg[:70])
'''


@case('A4', 'probe_full 失败时保留真实原因，不一律退化成"可能关机"')
def _a4():
    return r'''
from manager import flux_server_manager as fsm
fsm.run = lambda cmd, timeout=30: (False, 'ssh: connect to host x port 22: Connection timed out')
p = fsm.probe_full({'name': 't', 'alias': 't',
                    'remote_base': '/x', 'remote_model': '/x'}, force=True)
assert 'Connection timed out' in (p['error'] or ''), \
    f'真实原因被吞掉了: {p["error"]!r}'
print('PASS|', p['error'][:70])
'''


# ── B 组：常驻服务档位（offload）────────────────────────────────────────

@case('B1', '未声明 offload 的机器，拉起时常驻档位是安全值而非 none')
def _b1():
    return r'''
from manager import flux_resident_client as fr
cap = {}


def fake_run(cmd, timeout=30):
    cap.setdefault('cmd', cmd)
    return True, 'ok'


fr.fsm.run = fake_run
fr.upload_scripts = lambda s: (True, 'ok')
fr.probe = lambda s, force=False: {'resident': True, 'reachable': True,
                                   'gpu_ok': True, 'model_ok': True}
# 故意不带 offload 字段：走"安全默认"分支
srv = {'name': 't', 'alias': 't', 'remote_base': '/x', 'remote_model': '/x'}
r = fr.ensure_resident(srv, wait_ready=False)
cmd = cap.get('cmd', '')
assert cmd, f'没走到拉起分支（返回 {r}）'
assert 'FLUX_OFFLOAD=none' not in cmd, \
    f'32G 卡会 OOM 的档位又回来了: {cmd[:200]}'
assert 'FLUX_OFFLOAD=model' in cmd, f'没透传安全档位: {cmd[:200]}'
print('PASS|', 'FLUX_OFFLOAD=model')
'''


@case('B2', '显式 FLUX_OFFLOAD 环境变量优先级最高（可临时追速度）')
def _b2():
    return r'''
os.environ['FLUX_OFFLOAD'] = 'sequential'
from manager import flux_resident_client as fr
cap = {}
fr.fsm.run = lambda cmd, timeout=30: (cap.setdefault('cmd', cmd), (True, 'ok'))[1]
fr.upload_scripts = lambda s: (True, 'ok')
fr.probe = lambda s, force=False: {'resident': True, 'reachable': True,
                                   'gpu_ok': True, 'model_ok': True}
srv = {'name': 't', 'alias': 't', 'remote_base': '/x', 'remote_model': '/x',
       'offload': 'model'}
fr.ensure_resident(srv, wait_ready=False)
cmd = cap.get('cmd', '')
assert 'FLUX_OFFLOAD=sequential' in cmd, f'环境变量没覆盖机器默认: {cmd[:200]}'
print('PASS|', 'env 覆盖生效')
'''


@case('B3', '每台默认机都显式声明了安全档位（防新增机器时漏写）')
def _b3():
    return r'''
from manager import flux_server_manager as fsm
bad = [s['name'] for s in fsm._DEFAULT_SERVERS
       if (s.get('offload') or '').strip() in ('', 'none')]
assert not bad, (
    f'这些机器没声明可用档位（32G 卡用 none 会 OOM）: {bad}；'
    f'若确有大显存机器想跑 none，请在该条目上显式写 offload="none" 并说明显存大小')
print('PASS|', [(s['name'], s.get('offload')) for s in fsm._DEFAULT_SERVERS])
'''


@case('B4', '常驻服务 in offload 报错时，等待逻辑要抛失败而不是死等')
def _b4():
    return r'''
from manager import flux_resident_client as fr


class FakeT:
    def __init__(self): self.n = 0

    def get_json(self, path, timeout=None):
        self.n += 1
        return {'model_loaded': False,
                'model_error': 'OutOfMemoryError: CUDA out of memory'}


fr._transport = lambda s: FakeT()
try:
    fr.wait_model_loaded({'name': 't', 'alias': 't'}, timeout=30, interval=0)
except fr.TransportError as e:
    assert 'OutOfMemory' in str(e), str(e)
    print('PASS|', 'OOM 立即失败，不干等 30s')
else:
    raise AssertionError('模型加载报错时竟然没抛异常（会死等到超时）')
'''


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    print('=' * 66)
    print(' 传输层 & 常驻档位 回归闸门')
    print(f' 仓库: {BASE}')
    print(f' 解释器: {PY}')
    print('=' * 66)

    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env.pop('FLUX_SERVER_DISCOVER', None)

    failed = []
    for name, title, fn in CASES:
        code = PRELUDE + fn()
        p = subprocess.run([PY, '-c', code], capture_output=True, text=True,
                           encoding='utf-8', errors='replace',
                           env=env, cwd=str(BASE))
        out = (p.stdout or '')
        err = (p.stderr or '')

        def clean(text):
            # 被测模块会往 stdout/stderr 打日志（logging.StreamHandler 默认 stderr），
            # 还有环境自带的 RequestsDependencyWarning —— 都别当成用例输出。
            return [ln.rstrip() for ln in text.splitlines()
                    if 'RequestsDependencyWarning' not in ln
                    and 'warnings.warn' not in ln
                    and not ln.startswith('  warnings.warn')]

        # 用哨兵匹配，不要求 'PASS' 在首行（日志可能排在前面）
        sentinel = [ln for ln in clean(out) if ln.startswith('PASS|')]
        if p.returncode == 0 and sentinel:
            print(f'▸ PASS [{name}] {title}')
            print(f'         {sentinel[-1][5:].strip()[:100]}')
        else:
            print(f'▸ FAIL [{name}] {title}')
            tail = clean(err) or clean(out)
            for ln in tail[-8:]:
                print(f'         {ln}')
            failed.append(name)
        print()

    print('=' * 66)
    print(f' 合计 {len(CASES)} 项 · 通过 {len(CASES) - len(failed)} · 失败 {len(failed)}')
    if failed:
        print(' 失败项: ' + ', '.join(failed))
        print('=' * 66)
        return 1
    print(' 全部通过')
    print('=' * 66)
    return 0


if __name__ == '__main__':
    sys.exit(main())

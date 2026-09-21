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

  C. 图生图（/edit）的 body 含参考图 base64，旧写法把它**内联进 ssh 命令行**
     （`echo <b64> | base64 -d | curl ...`）。Windows 的 CreateProcess 命令行
     上限约 32K → `FileNotFoundError: [WinError 206] 文件名或扩展名太长`。
     报的是 FileNotFoundError，看着像"文件丢了"，实际是"参数太长"；
     上层把它归类成 SERVER_DOWN 一直重试 → 站点任务永远卡在「生成中，已等待」
     （2026-09-20 站点任务 ceff1b2c 卡死的真因）。
     修法：body 走 **ssh 的 stdin**（curl `--data-binary @-` 从 stdin 读）。
     教训：**大 payload 永远不要进 argv**。

这三条都是"知识型约束"，光改一次代码不能防止下次被改回去 —— 所以固化成断言，
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
    f'BASE = r"{BASE}"\n'
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


# ── C 组：图生图大 payload 走 stdin，不进 argv ──────────────────────────

@case('C1', '2MB 参考图提交时，ssh 命令行保持很短（WinError 206 防回归）')
def _c1():
    return r'''
from manager import flux_resident_client as fr
cap = {}


def fake_run(cmd, timeout=30, stdin_data=None):
    cap['cmd'] = cmd
    cap['stdin'] = stdin_data
    return True, '{"status":"done","job_id":"x"}'


fr.fsm.run = fake_run
big = 'A' * (2 * 1024 * 1024)          # 2MB base64，远超 Windows 32K 上限
t = fr.SshCurlTransport({'name': 't', 'alias': 't'})
t.post_json('/edit', {'prompt': 'x', 'image': big}, timeout=60)
cmd = cap['cmd']
assert len(cmd) < 4000, (
    f'命令行 {len(cmd)} 字符 —— 大 payload 又回到 argv 了，'
    f'Windows 上必然 WinError 206（图生图任务会永久卡在「生成中」）')
assert 'A' * 200 not in cmd, 'body 出现在命令行里'
print('PASS|', f'命令行仅 {len(cmd)} 字符，与 body 大小无关')
'''


@case('C2', 'body 确实走 stdin_data 传给 ssh（curl 用 --data-binary @- 读它）')
def _c2():
    return r'''
import json
from manager import flux_resident_client as fr
cap = {}
fr.fsm.run = lambda cmd, timeout=30, stdin_data=None: (
    cap.setdefault('cmd', cmd), cap.setdefault('stdin', stdin_data),
    (True, '{"status":"done","job_id":"x"}'))[2]
big = 'B' * (300 * 1024)
t = fr.SshCurlTransport({'name': 't', 'alias': 't'})
t.post_json('/edit', {'prompt': '中文提示词', 'image': big}, timeout=60)
assert cap.get('stdin'), 'stdin 是空的 —— body 没走 stdin'
assert big in cap['stdin'], 'body 内容不在 stdin 里'
assert '中文提示词' in cap['stdin'], '中文被转义/丢失'
assert '@-' in cap['cmd'], f'curl 不从 stdin 读 body 了: {cap["cmd"][:200]}'
print('PASS|', f'stdin {len(cap["stdin"])} 字符，含中文与完整 body')
'''


@case('C3', 'fsm.run 真的把 stdin_data 喂给命令（端到端走一次 bash）')
def _c3():
    return r'''
from manager import flux_server_manager as fsm
ok, out = fsm.run('cat', stdin_data='hello-stdin-payload')
assert ok, f'run() 失败: {out}'
assert 'hello-stdin-payload' in out, f'stdin 没喂进去: {out!r}'
# 不传 stdin 时也不该卡住（显式关掉 stdin，不继承父进程）
ok2, out2 = fsm.run('echo no-stdin-ok')
assert ok2 and 'no-stdin-ok' in out2, f'不传 stdin 时异常: {out2!r}'
print('PASS|', 'stdin 透传正常，且缺省时立即关闭不空等')
'''


@case('C4', '源码里不再有「base64 内联进命令行」的旧写法')
def _c4():
    # ⚠️ 必须剥掉 docstring 再查：修 bug 时留下的注释/文档里**会引用旧写法**
    #    （`echo <b64> | base64 -d | curl`），不剥就会误报，跟 watchdog 那个
    #    「判据不含旧链路关键字」的假红是同一类坑。
    return r'''
import ast
from pathlib import Path


def code_only(path):
    tree = ast.parse(Path(path).read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        body = getattr(node, 'body', None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                body[0] = ast.Pass()
    return ast.unparse(tree)


code = code_only(Path(BASE) / 'manager' / 'flux_resident_client.py')
assert 'base64 -d' not in code, '_curl_cmd 又把 body 内联成 echo <b64> | base64 -d 了'
assert 'body_b64' not in code, '内联 payload 的参数回来了'
fsm_code = code_only(Path(BASE) / 'manager' / 'flux_server_manager.py')
assert 'input=stdin_data' in fsm_code or 'stdin_data' in fsm_code, \
    'fsm.run 不再把 stdin_data 交给 subprocess'
print('PASS|', '无内联 payload；fsm.run 已接 stdin')
'''


# ── D 组：子进程不得弹控制台窗口（2026-09-21 用户报"一直有 2 个 bash.exe 不时弹窗"）──
#
# 现象与根因：flux_service.py 是**无控制台**进程（.bat 用 -WindowStyle Hidden，
# 我这边用 DETACHED_PROCESS 拉）。此时 Windows 给它的子进程**新建一个可见控制台**。
# fsm.run() 用 subprocess.run([bash, '-lc', cmd]) 且**没有 creationflags**，
# 而 _dispatch 的 interval=120s → 每轮探活弹一个黑窗，表现为"不时弹窗"。
# 实测证据：Get-CimInstance 抓到 bash.exe 的 PPID == flux_service 的 PID。
#
# ⚠️ 这条断言必须钉在**所有** subprocess 调用上，不只是 run()：
#   新写一处 subprocess 就会重新开始弹窗，而"偶尔弹个黑窗"没人会当成 bug 报上来。

@case('D1', '存在 CREATE_NO_WINDOW 常量且仅 Windows 生效（Linux 上为 0）')
def _d1():
    return r'''
from manager import flux_server_manager as fsm
assert hasattr(fsm, 'CREATE_NO_WINDOW'), '缺少 CREATE_NO_WINDOW 常量'
if os.name == 'nt':
    assert fsm.CREATE_NO_WINDOW == 0x08000000, \
        f'CREATE_NO_WINDOW 值不对: {fsm.CREATE_NO_WINDOW:#x}（应为 0x8000000）'
else:
    assert fsm.CREATE_NO_WINDOW == 0, '非 Windows 平台应为 0（该常量无意义）'
assert hasattr(fsm, 'SUBPROC_FLAGS'), '缺少统一的 SUBPROC_FLAGS'
assert fsm.SUBPROC_FLAGS == fsm.CREATE_NO_WINDOW, 'SUBPROC_FLAGS 与常量不一致'
print('PASS|', f'CREATE_NO_WINDOW={fsm.CREATE_NO_WINDOW:#x} SUBPROC_FLAGS={fsm.SUBPROC_FLAGS:#x}')
'''


@case('D2', 'run() 真的把 creationflags 传给了 subprocess（不是只定义常量不用）')
def _d2():
    return r'''
import ast
from pathlib import Path

src = Path(BASE, 'manager', 'flux_server_manager.py').read_text(encoding='utf-8')
tree = ast.parse(src)

# 找到所有 subprocess.run(...) 调用
calls = [n for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Attribute)
         and n.func.attr in ('run', 'Popen', 'call', 'check_output', 'check_call')
         and isinstance(n.func.value, ast.Name)
         and n.func.value.id == 'subprocess']
assert calls, '文件里没有任何 subprocess 调用？断言定位失败，请更新本用例'

missing = []
for c in calls:
    kw = {k.arg for k in c.keywords}
    if 'creationflags' not in kw:
        missing.append(getattr(c, 'lineno', '?'))
assert not missing, (
    f'有 {len(missing)} 处 subprocess 调用没传 creationflags（会在无控制台进程里弹窗）：'
    f' 行 {missing}。请加 creationflags=SUBPROC_FLAGS'
)
print('PASS|', f'{len(calls)} 处 subprocess 调用全部带 creationflags')
'''


@case('D3', '变异断言：拿掉 creationflags 必须报红（证明 D2 真的有约束力）')
def _d3():
    return r'''
import ast
from pathlib import Path

src = Path(BASE, 'manager', 'flux_server_manager.py').read_text(encoding='utf-8')
# 模拟"下一个人新写 subprocess 时忘了加" —— 删掉 creationflags 关键字再跑同一套检查
mutated = src.replace('creationflags=SUBPROC_FLAGS', '')
assert mutated != src, '变异没生效：源码里找不到 creationflags=SUBPROC_FLAGS（常量名改过了？）'

tree = ast.parse(mutated)
calls = [n for n in ast.walk(tree)
         if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Attribute)
         and n.func.attr in ('run', 'Popen', 'call', 'check_output', 'check_call')
         and isinstance(n.func.value, ast.Name)
         and n.func.value.id == 'subprocess']
bad = [getattr(c, 'lineno', '?') for c in calls if 'creationflags' not in {k.arg for k in c.keywords}]
assert bad, '变异版竟然还全带 creationflags —— 说明 D2 的检查逻辑写空了'
print('PASS|', f'变异版正确报红（{len(bad)} 处缺失，行 {bad}）')
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

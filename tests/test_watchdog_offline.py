#!/usr/bin/env python3
"""回归门：VPS 看门狗必须守的是「常驻链路」，且机器清单只有一个来源

起因（2026-09-20）：用户问「以前是用 VPS 服务器来自动检测 flux 服务器存活并拉起服务吧」。
查下来 watchdog/ 早在 commit 9c0cadd 就写好了，但：
  1) **从来没部署过** —— VPS 上 /opt/flux-watchdog 不存在、服务 inactive。
     原因：部署散在 README 的手工命令里，改完代码没人会想起来跑。
  2) v1 预热的是**旧链路**（gen_flux.py / start_gen.sh / screen fluxgen / out/），
     而 A 链 2026-09-16 起已改成常驻服务（9630 /health）。部署上去只会拉起一个
     没人用的旧进程 → 网站照样转圈，且**没有任何报错**（静默失败）。
  3) 机器清单硬编码在脚本的 TARGETS 里 → 加机器要改三处，漏一处就是机器永远不被预热。

本门守住六条不变量（全离线：不连 SSH、不要 GPU）
  1. 就绪判据指向常驻服务（flux_resident_server.py / start_resident.sh / model_loaded / 9630）
  2. 判据里**不出现**旧链路关键字（gen_flux.py / start_gen.sh / fluxgen）
  3. 看门狗从 targets.conf 读机器，不硬编码 TARGETS
  4. gen_targets 生成的每行 6 段 host:port:user:workdir:model:offload，且跳过 enabled=false
  5. deploy_vps 会带上常驻服务镜像文件（裸机能自补件）+ 用 restart 而非仅 enable --now
  6. selftest_ready.sh 正反路径都在（无卡/无模型/常驻未起 → 1；常驻已加载 → 0）

用法: python tests/test_watchdog_offline.py    # 全绿 exit 0，有红 exit 1
"""
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, 'manager'))
sys.path.insert(0, os.path.join(BASE, 'watchdog'))

WATCHDOG = os.path.join(BASE, 'watchdog')
READY = os.path.join(WATCHDOG, 'flux_server_ready.sh')
WATCH = os.path.join(WATCHDOG, 'flux_watchdog.sh')
SELFTEST = os.path.join(WATCHDOG, 'selftest_ready.sh')
DEPLOY = os.path.join(WATCHDOG, 'deploy_vps.py')
GEN = os.path.join(WATCHDOG, 'gen_targets.py')
AUTH = os.path.join(WATCHDOG, 'authorize_key.py')

RESULTS = []


def check(name, ok, extra=''):
    RESULTS.append((name, bool(ok), extra))
    print(f'  {"✅" if ok else "❌"} {name}' + (f' — {extra}' if extra else ''))


def read(p):
    with open(p, encoding='utf-8') as f:
        return f.read()


def code_only(src: str) -> str:
    """去掉纯注释行再断言。

    注释里**必须**保留"v1 预热的是旧链路 gen_flux.py…"这类说明（否则后人不知道为什么改），
    所以断言只能针对真正的代码 —— 不然注释一写就误报红灯，门会被人当成噪音关掉。
    """
    return '\n'.join(l for l in src.splitlines() if not l.strip().startswith('#'))


def t_resident_chain():
    print('\n[1] 就绪判据指向常驻链路（不是旧链路）')
    for p in (READY, WATCH, SELFTEST, DEPLOY, GEN, AUTH):
        check(f'文件存在 {os.path.basename(p)}', os.path.isfile(p))
    src = read(READY)
    for kw in ('flux_resident_server.py', 'start_resident.sh', 'model_loaded', '9630'):
        check(f'判据含常驻关键字 {kw}', kw in src)
    # 无卡模式：nvidia-smi 存在、exit 0、输出 0 字节（2026-09-20 flux4 实测）
    check('带卡判定有独立函数 f_gpu（不靠退出码）', 'f_gpu()' in src)
    check('f_gpu 要求 nvidia-smi 输出非空', '--query-gpu=name' in src and '-n "$name"' in src)
    check('无卡模式早退（不白等 400s 拖住巡检）',
          '无卡模式' in src and code_only(src).count('exit 1') >= 2)
    legacy = [kw for kw in ('gen_flux.py', 'start_gen.sh', 'fluxgen') if kw in code_only(src)]
    check('判据代码里不含旧链路关键字（注释里的说明不算）', not legacy, f'残留: {legacy}')

    # 变异断言：检测器本身必须有效（否则这条门恒真，等于没守）
    v1_src = 'screen -dmS fluxgen bash start_gen.sh; python gen_flux.py --prompt x'
    hit = [kw for kw in ('gen_flux.py', 'start_gen.sh', 'fluxgen') if kw in code_only(v1_src)]
    check('变异断言：把 v1 旧链路代码喂进来必须报红', len(hit) == 3, f'命中 {hit}')


def t_no_hardcoded_targets():
    print('\n[2] 机器清单只有一个来源（targets.conf，由 servers.json 生成）')
    src = read(WATCH)
    check('从 targets.conf 读机器', 'targets.conf' in src and 'CONF=' in src)
    check('不硬编码 TARGETS 数组', 'TARGETS=(' not in src)
    # --check 成功和失败都会打印一行，判空永远不成立 → 看门狗再也不预热（静默失效）
    check('按**退出码**分支而不是判输出为空', 'rc=$?' in src and 'if [ $rc -eq 0 ]' in src)
    check('无卡模式跳过预热（不空转、不刷日志）', 'NOGPU' in src and '无卡模式' in src)
    gsrc = read(GEN)
    check('gen_targets 源是 servers.json', 'servers.json' in gsrc or 'load_registry' in gsrc)
    check('gen_targets 会跳过 enabled=false', 'enabled' in gsrc)


def t_gen_lines():
    print('\n[3] gen_targets 输出格式')
    import gen_targets
    lines, skipped = gen_targets.build_lines()
    check('至少产出 1 台', len(lines) >= 1, f'{len(lines)} 台')
    ok_fmt = all(len(l.split(':')) == 6 for l in lines)
    check('每行 6 段 host:port:user:workdir:model:offload', ok_fmt, lines[0] if lines else '')
    ok_port = all(l.split(':')[1].isdigit() for l in lines)
    check('port 段是数字', ok_port)
    # 被释放的机器（enabled:false）不得出现 —— 否则每轮轮询都白等 SSH 超时
    import manager.flux_server_manager as fsm
    disabled = [s for s in fsm._DEFAULT_SERVERS if s.get('enabled') is False] \
        if hasattr(fsm, '_DEFAULT_SERVERS') else []
    hosts = [l.split(':')[0] for l in lines]
    leaked = [s['host'] for s in disabled if s.get('host') in hosts]
    check('enabled=false 的机器不进清单', not leaked, f'泄漏: {leaked}')


def t_deploy():
    print('\n[4] deploy_vps 部署完整性')
    src = code_only(read(DEPLOY))
    check('带常驻服务镜像（裸机可自补件）', 'flux_resident_server.py' in src and 'start_resident.sh' in src)
    check('用 restart 而非仅 enable --now（改完代码要真生效）',
          'systemctl restart' in src and 'enable --now' not in src)
    check('部署后跑判据自测', 'selftest_ready.sh' in src)


def t_selftest():
    print('\n[5] selftest_ready.sh 覆盖正反路径')
    src = read(SELFTEST)
    check('有假 nvidia-smi 桩件', 'nvidia-smi' in src)
    check('有假常驻服务（model_loaded:true）', 'model_loaded' in src and 'HTTPServer' in src)
    check('正向用例期望 0 / 负向用例期望 1', 'chk "常驻已加载模型" 0' in src and 'chk "常驻未起" 1' in src)
    check('覆盖无卡模式「空输出但 exit 0」桩件',
          "$SB/silent" in src and 'exit 0' in src)
    check('沙箱建在 /tmp，不碰真实工作目录', '/tmp/flux-ready-selftest' in src)


def t_syntax():
    print('\n[6] shell 语法（bash -n）')
    bash = shutil.which('bash')
    if not bash:
        check('bash 可用', False, '本机 PATH 里没有 bash —— 未能验证（不跳过、据实标红）')
        return
    for p in (READY, WATCH, SELFTEST):
        r = subprocess.run([bash, '-n', p], capture_output=True, text=True)
        check(f'bash -n {os.path.basename(p)}', r.returncode == 0, (r.stderr or '').strip()[:160])


def main():
    print('=' * 60)
    print('VPS 看门狗 回归门（常驻链路 / 单一清单 / 可部署）')
    print('=' * 60)
    t_resident_chain()
    t_no_hardcoded_targets()
    t_gen_lines()
    t_deploy()
    t_selftest()
    t_syntax()
    bad = [n for n, ok, _ in RESULTS if not ok]
    print('\n' + '=' * 60)
    print(f'共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}')
    if bad:
        for n in bad:
            print(f'  ❌ {n}')
        return 1
    print('✅ 全绿')
    return 0


if __name__ == '__main__':
    sys.exit(main())

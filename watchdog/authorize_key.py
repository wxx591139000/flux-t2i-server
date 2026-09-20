#!/usr/bin/env python3
"""把 VPS 看门狗的公钥装到各 GPU 机，并验证 VPS 能免密连上。

为什么要有它（2026-09-20）：
  看门狗在 VPS 上跑，它用的是**自己的** key（/opt/flux-watchdog/id_flux_watchdog），
  不是本机的 key。新克隆的实例只认本机 key → 看门狗连不上 → 该机永远不会被预热，
  表现就是「服务器明明开着，网站却一直转圈」。所以每次克隆新实例后必须跑一次本脚本。

做什么：
  1. 从 VPS 取回看门狗公钥（没有就顺手生成）
  2. 用**本机** key 直连每台 GPU 机，把公钥幂等地追加进 ~/.ssh/authorized_keys
  3. `--check`：站在 VPS 上用看门狗 key 逐台试连，报告 通/不通

用法：
  python watchdog/authorize_key.py              # 装机（幂等，重复跑无害）
  python watchdog/authorize_key.py --check      # 只验证 VPS→各机 是否免密可达
  python watchdog/authorize_key.py --vps root@1.2.3.4
"""
import argparse
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / 'manager'))

import manager.flux_server_manager as fsm          # noqa: E402

KEY_PATH = '/opt/flux-watchdog/id_flux_watchdog'
MARK = 'flux-watchdog'          # 追加行尾标记，便于识别与幂等去重


def sh(cmd, timeout=120):
    # ⚠️ errors='replace'：远端/本机可能吐 GBK 字节（Windows 侧中文横幅、AutoDL 登录提示），
    #    严格 utf-8 解码会直接抛 UnicodeDecodeError 把整条命令判成失败（实测踩到）。
    r = subprocess.run(cmd, shell=True, capture_output=True,
                       encoding='utf-8', errors='replace', timeout=timeout)
    return r.returncode == 0, (r.stdout or '') + (r.stderr or '')


def fetch_pubkey(vps, key=KEY_PATH):
    ok, out = sh(f'ssh {vps} "[ -f {key}.pub ] || ssh-keygen -t ed25519 -N \'\' -f {key} >/dev/null; '
                 f'cat {key}.pub"')
    if not ok:
        return None, f'取公钥失败: {out.strip()[:200]}'
    pub = [l for l in out.splitlines() if l.startswith('ssh-ed25519')]
    if not pub:
        return None, f'VPS 上没拿到公钥，原始输出: {out.strip()[:200]}'
    return pub[-1].strip(), ''


def main():
    ap = argparse.ArgumentParser(description='把 VPS 看门狗公钥装到各 GPU 机')
    ap.add_argument('--vps', default='vps-aliyun')
    ap.add_argument('--check', action='store_true', help='只验证 VPS→各机 免密可达')
    ap.add_argument('--strict', action='store_true',
                    help='离线也算失败（默认不算：机器关机时连不上是**预期状态**，'
                         '不该让整条部署命令失败）')
    a = ap.parse_args()

    servers = [s for s in fsm.FLUX_SERVERS if s.get('host')]
    if not servers:
        print('❌ servers.json 里没有带 host 的机器（VPS 无法直连）')
        return 1

    if a.check:
        ok_all = True
        for s in servers:
            tgt = f'-i {KEY_PATH} -o BatchMode=yes -o ConnectTimeout=8 ' \
                  f'-o StrictHostKeyChecking=accept-new ' \
                  f'-o UserKnownHostsFile=/opt/flux-watchdog/known_hosts ' \
                  f'-p {s["port"]} {s["user"]}@{s["host"]}'
            # ⚠️ 内层远程命令只能用单引号：本机 Windows 的 subprocess shell=True 走 cmd.exe，
            #    嵌套 \" 会被 cmd 的引号规则吃掉（实测远端报 head: invalid trailing option -- \）。
            #    nvidia-smi 在**无卡模式**下输出为空但 rc=0（flux4 实测），所以带一个
            #    「无卡」兜底文案，别让人误判成机器有问题。
            #    ⚠️ 内层**不能出现双引号**：cmd 的引号是「遇 " 就切换」而不是配对，
            #    内层一有 " 就把外层的引号拆开，cmd 随后把剩余片段当路径解析 →
            #    报 GBK 的「系统找不到指定的路径」（实测踩到，极具迷惑性）。
            inner = "echo ok; nvidia-smi -L 2>/dev/null | head -1; " \
                    "nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null " \
                    "| grep -q . && echo GPU_OK || echo NOGPU"
            ok, out = sh(f"""ssh {a.vps} "ssh {tgt} '{inner}'\"""", 60)
            print(f'{"✅" if ok else "❌"} {s["name"]:6s} {s["host"]}:{s["port"]} '
                  f'{out.strip().splitlines()[0] if ok else out.strip()[:120]}')
            ok_all = ok_all and ok
        return 0 if ok_all else 1

    pub, err = fetch_pubkey(a.vps)
    if not pub:
        print(f'❌ {err}')
        return 1
    print(f'🔑 看门狗公钥: {pub[:40]}…（{len(pub)} 字节）')

    # 「连不上」是预期状态（机器关机），不是本命令的失败 —— 否则每次有一台机器关机
    # 就会让 onboard/deploy 整条链路中止，逼着人手动跳过。真失败（权限/路径错）才报错。
    OFFLINE_MARK = ('Connection refused', 'Connection timed out', 'timed out',
                    'No route to host', 'Network is unreachable')
    n_ok = n_off = n_err = 0
    for s in servers:
        tgt = fsm.ssh_target(s)
        # 幂等：先 grep 掉带标记的旧行，再追加；chmod 保证文件权限正确（权限不对 sshd 会忽略）
        cmd = (f'ssh -o ConnectTimeout=10 {tgt} '
               f'"mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && '
               f'grep -v \'{MARK}\' ~/.ssh/authorized_keys > ~/.ssh/ak.tmp 2>/dev/null; '
               f'echo \'{pub} {MARK}\' >> ~/.ssh/ak.tmp && mv ~/.ssh/ak.tmp ~/.ssh/authorized_keys && '
               f'chmod 600 ~/.ssh/authorized_keys && echo INSTALLED"')
        ok, out = sh(cmd, 60)
        if ok:
            n_ok += 1
            print(f'✅ {s["name"]:6s} {s["host"]}:{s["port"]} 已装机')
        elif any(m in out for m in OFFLINE_MARK):
            n_off += 1
            print(f'💤 {s["name"]:6s} {s["host"]}:{s["port"]} 离线（关机/释放）—— 开机后重跑本命令')
        else:
            n_err += 1
            print(f'❌ {s["name"]:6s} {s["host"]}:{s["port"]} {out.strip()[:160]}')

    print(f'\n装机 {n_ok} 台 / 离线 {n_off} 台 / 失败 {n_err} 台')
    print('验证：python watchdog/authorize_key.py --check')
    if n_err:
        return 1
    if a.strict and n_off:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

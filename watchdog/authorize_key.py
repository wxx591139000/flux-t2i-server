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
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
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
            ok, out = sh(f'ssh {a.vps} "ssh {tgt} \\"echo ok; nvidia-smi -L | head -1\\""', 60)
            print(f'{"✅" if ok else "❌"} {s["name"]:6s} {s["host"]}:{s["port"]} '
                  f'{out.strip().splitlines()[0] if ok else out.strip()[:120]}')
            ok_all = ok_all and ok
        return 0 if ok_all else 1

    pub, err = fetch_pubkey(a.vps)
    if not pub:
        print(f'❌ {err}')
        return 1
    print(f'🔑 看门狗公钥: {pub[:40]}…（{len(pub)} 字节）')

    ok_all = True
    for s in servers:
        tgt = fsm.ssh_target(s)
        # 幂等：先 grep 掉带标记的旧行，再追加；chmod 保证文件权限正确（权限不对 sshd 会忽略）
        cmd = (f'ssh -o ConnectTimeout=10 {tgt} '
               f'"mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && '
               f'grep -v \'{MARK}\' ~/.ssh/authorized_keys > ~/.ssh/ak.tmp 2>/dev/null; '
               f'echo \'{pub} {MARK}\' >> ~/.ssh/ak.tmp && mv ~/.ssh/ak.tmp ~/.ssh/authorized_keys && '
               f'chmod 600 ~/.ssh/authorized_keys && echo INSTALLED"')
        ok, out = sh(cmd, 60)
        print(f'{"✅" if ok else "❌"} {s["name"]:6s} {s["host"]}:{s["port"]} '
              f'{out.strip()[:160]}')
        ok_all = ok_all and ok

    print('\n验证：python watchdog/authorize_key.py --check')
    return 0 if ok_all else 1


if __name__ == '__main__':
    sys.exit(main())

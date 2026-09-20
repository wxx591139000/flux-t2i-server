#!/usr/bin/env python3
"""一条命令把看门狗部署到 VPS（生成清单 → 传文件 → 装 systemd → 启服务 → 打印状态）。

为什么要有它（2026-09-20）：
  watchdog/ 里的脚本早在 commit 9c0cadd 就写好了，但**从来没真正部署过** —— VPS 上
  `ls /opt/flux-watchdog` 是空的、服务 inactive。原因是部署散在 README 的 5 段手工命令里，
  人在别的机器上干别的活时根本不会想起来跑。现在压成一条命令，改完代码随手一跑就同步。

做什么：
  1. 从 manager/servers.json 生成 targets.conf（机器清单唯一来源，不手抄）
  2. 传到 VPS：targets.conf / flux_watchdog.sh / flux_server_ready.sh / flux-watchdog.service
     + mirror/（常驻服务两个文件，给裸机补件用）
  3. 远端：落盘到 /opt/flux-watchdog、chmod +x、装 systemd unit、daemon-reload、enable + restart
  4. 回读 systemctl status + 最近日志，肉眼可验收

用法：
  python watchdog/deploy_vps.py                     # 部署（默认别名 vps-aliyun）
  python watchdog/deploy_vps.py --vps root@1.2.3.4  # 指定 VPS
  python watchdog/deploy_vps.py --dry-run           # 只打印将执行的命令，不动远端
  python watchdog/deploy_vps.py --status            # 只看远端状态，不传文件
"""
import argparse
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / 'manager'))
sys.path.insert(0, str(Path(__file__).parent))

import gen_targets                                   # noqa: E402

WATCHDOG_DIR = Path(__file__).parent
REMOTE_DIR = '/opt/flux-watchdog'
REMOTE_TMP = '/tmp/flux-watchdog-deploy'
SERVICE_NAME = 'flux-watchdog'
# 常驻服务所需文件（本地 server/ → VPS mirror/，机器缺件时由看门狗补传）
MIRROR_FILES = [
    (BASE_DIR / 'server' / 'flux_resident_server.py', 'flux_resident_server.py'),
    (BASE_DIR / 'server' / 'start_resident.sh', 'start_resident.sh'),
]


def run(cmd, timeout=120, dry=False):
    print(('[dry] ' if dry else '$ ') + cmd)
    if dry:
        return True, ''
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout or '') + (r.stderr or '')
    if r.returncode != 0:
        print(f'   ❌ 退出码 {r.returncode}: {out.strip()[:400]}')
    return r.returncode == 0, out


def main():
    ap = argparse.ArgumentParser(description='部署 FLUX 看门狗到 VPS')
    ap.add_argument('--vps', default='vps-aliyun', help='VPS ssh 别名或 user@host（默认 vps-aliyun）')
    ap.add_argument('--remote-dir', default=REMOTE_DIR)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--status', action='store_true', help='只看远端运行状态')
    ap.add_argument('--authorize', action='store_true',
                    help='部署后顺手把看门狗公钥装到各 GPU 机（只对当前开机的机器生效；'
                         '新克隆实例开机后要再跑一次 watchdog/authorize_key.py）')
    a = ap.parse_args()
    vps, rd, dry = a.vps, a.remote_dir, a.dry_run

    if a.status:
        ok, out = run(f"""ssh {vps} "systemctl status {SERVICE_NAME} --no-pager | head -12; """
                      f"""echo '--- 最近日志 ---'; """
                      f"""journalctl -u {SERVICE_NAME} -n 10 --no-pager\"""", 60, dry)
        if not dry:
            print(out)
        return 0 if ok else 1

    # ── 1. 生成 targets.conf ──
    lines, skipped = gen_targets.build_lines()
    if not lines:
        print('❌ 没有可用机器：servers.json 里都要有 host/port 且未 enabled:false')
        for n, why in skipped:
            print(f'   - {n}: {why}')
        return 1
    conf = WATCHDOG_DIR / 'targets.conf'
    conf.write_text(
        '# 由 watchdog/gen_targets.py 从 manager/servers.json 生成 —— **不要手改**\n'
        '# 格式: host:port:user:workdir:model:offload\n' + '\n'.join(lines) + '\n',
        encoding='utf-8')
    print(f'✅ targets.conf：{len(lines)} 台')
    for n, why in skipped:
        print(f'   ⏭  跳过 {n}: {why}')

    # ── 2. 传文件 ──
    payload = [
        (conf, 'targets.conf'),
        (WATCHDOG_DIR / 'flux_watchdog.sh', 'flux_watchdog.sh'),
        (WATCHDOG_DIR / 'flux_server_ready.sh', 'flux_server_ready.sh'),
        (WATCHDOG_DIR / 'flux-watchdog.service', 'flux-watchdog.service'),
        # 自测脚本：判据改坏了能零成本发现（不需要开机花钱）
        (WATCHDOG_DIR / 'selftest_ready.sh', 'selftest_ready.sh'),
    ] + MIRROR_FILES
    for local, _ in payload:
        if not local.exists():
            print(f'❌ 本地缺文件 {local}')
            return 1
    ok, _ = run(f'ssh {vps} "mkdir -p {REMOTE_TMP}/mirror"', 60, dry)
    if not ok:
        return 1
    for local, name in payload:
        sub = 'mirror/' if (local, name) in MIRROR_FILES else ''
        ok, _ = run(f'scp "{local.as_posix()}" {vps}:{REMOTE_TMP}/{sub}{name}', 120, dry)
        if not ok:
            return 1

    # ── 3. 远端安装 ──
    install = (
        f'mkdir -p {rd}/mirror && '
        f'cp {REMOTE_TMP}/targets.conf {REMOTE_TMP}/flux_watchdog.sh {REMOTE_TMP}/flux_server_ready.sh '
        f'{REMOTE_TMP}/selftest_ready.sh {rd}/ && '
        f'cp {REMOTE_TMP}/mirror/* {rd}/mirror/ && '
        f'cp {REMOTE_TMP}/flux-watchdog.service /etc/systemd/system/{SERVICE_NAME}.service && '
        f'chmod +x {rd}/*.sh && '
        # ⚠️ 这里用 -N '' 而不是 -N ""：本机是 Windows，subprocess shell=True 走 cmd.exe，
        #    cmd 的双引号配对规则和 bash 不同，"" 会被吃掉导致远端收到不成对的引号
        #    （实测报 bash: -c: line 1: unexpected EOF while looking for matching `"'）。
        f'[ -f {rd}/id_flux_watchdog ] || ssh-keygen -t ed25519 -N \'\' -f {rd}/id_flux_watchdog && '
        # ⚠️ 必须 restart 不能只 enable --now：服务已在跑的话它不会重新读脚本，
        #    改完代码重新部署后，跑着的还是旧逻辑（表现为"我明明改了怎么没生效"）。
        f'systemctl daemon-reload && systemctl enable {SERVICE_NAME} && '
        f'systemctl restart {SERVICE_NAME} && sleep 3 && '
        f'systemctl is-active {SERVICE_NAME} && systemctl is-enabled {SERVICE_NAME}'
    )
    ok, out = run(f'ssh {vps} "{install}"', 180, dry)
    if not ok:
        return 1
    if not dry:
        print(out.strip())

    # ── 4. 回读验收 ──
    ok, out = run(f"""ssh {vps} "systemctl status {SERVICE_NAME} --no-pager | head -12; """
                  f"""echo '--- 最近日志 ---'; """
                  f"""journalctl -u {SERVICE_NAME} -n 10 --no-pager\"""", 60, dry)
    if not dry:
        print(out)
    # ── 5. 判据自测（零成本：不用开机就能证明就绪判据没写错）──
    ok2, out2 = run(f'ssh {vps} "bash {rd}/selftest_ready.sh"', 120, dry)
    if not dry:
        print(out2)
    rc = 0 if (ok and ok2) else 1

    # ── 6. 可选：装公钥（subprocess 起新进程，authorize_key 有自己的 argv）──
    if a.authorize:
        cmd = f'"{sys.executable}" "{WATCHDOG_DIR / "authorize_key.py"}" --vps {vps}'
        print('$ ' + cmd)
        if not dry:
            r = subprocess.run(cmd, shell=True)
            rc = rc or r.returncode
    else:
        print('\n⚠️ 还没装公钥：新克隆实例开机后跑一次 → python watchdog/authorize_key.py'
              '\n   （看门狗用的是自己的 key，不是本机 key；不装的话它连不上任何 GPU 机）')
    return rc


if __name__ == '__main__':
    sys.exit(main())

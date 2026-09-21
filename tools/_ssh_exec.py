"""远端命令执行器：把整条远端 shell 脚本 base64 编码后传输再解码执行。

**为什么必须这样**：Windows 客户端（OpenSSH for Windows）把 ssh 的参数
按空格重新拼成一条远端命令字符串时，会剥掉一层引号 →
`ssh host 'grep -n "def foo" file'` 到远端变成 `grep -n def foo file`，
报 `grep: foo: No such file or directory`（2026-09-21 实测）。

base64 后整条命令只含 `[A-Za-z0-9+/=]`，**不含空格与引号**，
任何客户端都无法把它拆坏。这是跨 Windows → Linux 传复杂命令的最稳做法。

用法（作为模块）:
    from tools._ssh_exec import sh   # 或直接复制此函数
    ok, out = sh(srv, 'grep -n "def foo" /path/file.py; echo done')
"""
import base64
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import manager.flux_server_manager as fsm  # noqa: E402


def sh(srv: dict, script: str, timeout: int = 120):
    """在远端执行任意 shell 脚本（多行、含引号都安全）。返回 (ok, out)。"""
    b64 = base64.b64encode(script.encode('utf-8')).decode('ascii')
    tgt = fsm.ssh_target(srv)
    # 远端侧：base64 -d 还原脚本 → sh 执行。整条外发命令无空格无引号。
    remote = f"echo {b64} | base64 -d | sh"
    return fsm.run(f'ssh {tgt} "{remote}"', timeout=timeout)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--server', default='flux5')
    ap.add_argument('script', help='要执行的远端 shell 脚本')
    args = ap.parse_args()
    srv = [s for s in fsm.load_registry() if s.get('name') == args.server][0]
    ok, out = sh(srv, args.script)
    print(f'ok={ok}')
    print(out)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())

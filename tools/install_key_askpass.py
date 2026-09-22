"""用 SSH_ASKPASS 机制把本机公钥装到新实例（无人值守，不需要 sshpass）。

为什么要这个脚本（工具链现状）：
  本机**没有 sshpass / plink**（实测 which 为空），而 Bash 工具是非交互的，
  ssh 的密码提示会直接饿死（表现为 hang 到超时，不是报错）。
  解法是把密码交给 SSH_ASKPASS：SSH 在无 tty 时会去调它取密码。

⚠️ 两个必须同时满足的条件（少一个就静默退回「无 tty 直接失败」）：
  1. `setsid` 让 ssh 认为「没有终端」（否则 ssh 会用 /dev/tty 而不是 ASKPASS）
  2. `SSH_ASKPASS_REQUIRE=force`（OpenSSH ≥8.4 才有；强制走 ASKPASS 而不是要求 DISPLAY）
  本机 OpenSSH 版本够新（实测通过）。
"""
import os
import pathlib
import subprocess
import sys
import tempfile

HOST = os.environ.get('QHOST', 'connect.weste.seetacloud.com')
PORT = os.environ.get('QPORT', '13889')
USER = os.environ.get('QUSER', 'root')
PASSWORD = os.environ.get('QPASS', '')

PUB = pathlib.Path.home() / '.ssh' / 'id_rsa_musetalk.pub'

if not PASSWORD:
    print('[X] 没给密码：请设 QPASS 环境变量')
    sys.exit(2)

pub = PUB.read_text(encoding='utf-8').strip()
print(f'[i] 目标 {USER}@{HOST}:{PORT}')
print(f'[i] 公钥 {PUB} → {pub[:40]}...')

# 写一个临时 askpass；用完即删（密码不落盘到项目目录）
# ⚠️ Windows 上 OpenSSH 是**直接执行** SSH_ASKPASS 指向的文件，
#    不像 POSIX 那样用 sh 解释 → 写 .sh 会在 Windows 上「不是可执行文件」。
#    所以按平台选后缀与内容：POSIX 用 shebang sh，Windows 用 .cmd。
if os.name == 'nt':
    fd, ap = tempfile.mkstemp(prefix='askpass-', suffix='.cmd')
    with os.fdopen(fd, 'w', encoding='ascii', newline='\r\n') as f:
        f.write('@echo off\r\n')
        # 不能带换行（askpass 的 stdout 会被当密码，尾随 CRLF 会破坏它）
        f.write(f'set /p={PASSWORD}<nul\r\n')
        f.write(f'echo {PASSWORD}\r\n')
else:
    fd, ap = tempfile.mkstemp(prefix='askpass-', suffix='.sh')
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
        f.write('#!/bin/sh\n')
        f.write(f'printf %s "{PASSWORD}"\n')
os.chmod(ap, 0o700)

env = dict(os.environ)
env['SSH_ASKPASS'] = ap
env['SSH_ASKPASS_REQUIRE'] = 'force'
env['DISPLAY'] = ':0'                      # 老版本 ssh 需要它才认 ASKPASS
env.pop('SSH_AUTH_SOCK', None)

remote = (f'mkdir -p ~/.ssh && chmod 700 ~/.ssh && '
          f'grep -qF {pub!r} ~/.ssh/authorized_keys 2>/dev/null || '
          f'echo {pub!r} >> ~/.ssh/authorized_keys; '
          f'chmod 600 ~/.ssh/authorized_keys; echo INSTALLED_OK')


def run(cmd, timeout=45):
    # ⚠️ Windows 没有 os.setsid（本机实测 AttributeError）。
    #    Windows OpenSSH 走 SSH_ASKPASS 的条件与 POSIX 不同：
    #    它不要求「无控制终端」，只要 SSH_ASKPASS 存在且
    #    SSH_ASKPASS_REQUIRE=force 就优先用它。
    #    所以这里不能再抄 POSIX 的 setsid 套路 —— 会直接 AttributeError。
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True,
                           timeout=timeout, **kwargs)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except subprocess.TimeoutExpired:
        return 124, 'TIMEOUT'


try:
    rc, out = run(['ssh', '-o', 'StrictHostKeyChecking=accept-new',
                   '-o', 'NumberOfPasswordPrompts=1', '-o', 'ConnectTimeout=15',
                   '-o', 'PubkeyAuthentication=no',
                   '-p', str(PORT), f'{USER}@{HOST}', remote])
    print(f'[装 key] rc={rc}')
    print(out.strip()[-500:])
    if 'INSTALLED_OK' not in out:
        print('[X] 公钥安装失败')
        sys.exit(1)

    # 复验：这次只用密钥（禁掉密码），必须免密通过
    rc2, out2 = run(['ssh', '-o', 'StrictHostKeyChecking=accept-new',
                     '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                     '-i', str(PUB.with_suffix('')),
                     '-p', str(PORT), f'{USER}@{HOST}', 'echo KEY_OK; hostname'])
    print(f'[验 密] rc={rc2}')
    print(out2.strip()[-300:])
    sys.exit(0 if 'KEY_OK' in out2 else 1)
finally:
    try:
        os.unlink(ap)
    except OSError:
        pass

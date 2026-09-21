#!/usr/bin/env python3
"""从 manager/servers.json 生成看门狗的 targets.conf —— 机器清单只有**一个**来源。

为什么要有它（2026-09-20）：
  看门狗 v1 把机器硬编码在 `flux_watchdog.sh` 的 TARGETS 里。于是加一台机器要改三处
  （servers.json + ~/.ssh/config + 看门狗 TARGETS）—— 这正是用户明确反感的人肉一致性
  负担，漏一处就表现为「网站一直转圈」且极难定位。
  现在：servers.json 是唯一事实来源，看门狗的 targets.conf 由本脚本生成。

生成格式（每行）：host:port:user:workdir:model:offload

用法：
  python watchdog/gen_targets.py                    # 写到 watchdog/targets.conf
  python watchdog/gen_targets.py --print            # 只打印，不落盘
  python watchdog/gen_targets.py --out /tmp/t.conf
  python watchdog/gen_targets.py --upload vps-aliyun:/opt/flux-watchdog/
                                                    # 直接 scp 到 VPS（用本机 ssh 别名）
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / 'manager'))

import manager.flux_server_manager as fsm          # noqa: E402

DEFAULT_OUT = Path(__file__).parent / 'targets.conf'


def build_lines(registry_path=None) -> tuple:
    """返回 (lines, skipped)。skipped = 因缺 host / 已停用而跳过的机器及原因。"""
    servers = fsm.load_registry(registry_path) if registry_path else fsm.FLUX_SERVERS
    lines, skipped = [], []
    for s in servers:
        name = s.get('name')
        if not s.get('host'):
            skipped.append((name, '没写 host（只有 alias，VPS 无法直连）'))
            continue
        if s.get('enabled') is False:
            skipped.append((name, 'enabled=false（已释放/停用）'))
            continue
        lines.append(':'.join([
            str(s['host']),
            str(s.get('port') or 22),
            s.get('user') or 'root',
            s.get('remote_base') or '/root/autodl-tmp/flux-t2i',
            s.get('remote_model') or '/root/autodl-tmp/models/FLUX.1-dev',
            s.get('offload') or 'model',
            # 第 7 段 = name（2026-09-21 加）。看门狗用它在 active.json 里回报
            # 「哪台开机」，manager 再用 name/alias 匹配回候选机条目。
            # 没有它就只能回报 host —— 而 host 会随实例重建变化，name 才是稳定标识。
            s.get('name') or '',
        ]))
    return lines, skipped


def main():
    ap = argparse.ArgumentParser(description='servers.json → 看门狗 targets.conf')
    ap.add_argument('--out', default=str(DEFAULT_OUT))
    ap.add_argument('--registry', default=None, help='指定 servers.json 路径（默认走注册表）')
    ap.add_argument('--print', action='store_true', help='只打印不落盘')
    ap.add_argument('--upload', default=None, help='scp 到 VPS，如 vps-aliyun:/opt/flux-watchdog/')
    a = ap.parse_args()

    lines, skipped = build_lines(a.registry)
    if not lines:
        print('❌ 没有可用机器：请检查 manager/servers.json（都要有 host/port，且未 enabled:false）')
        for n, why in skipped:
            print(f'   - {n}: {why}')
        return 1

    body = '# 由 watchdog/gen_targets.py 从 manager/servers.json 生成 —— **不要手改**\n' \
           '# 格式: host:port:user:workdir:model:offload\n' + '\n'.join(lines) + '\n'

    if a.print:
        print(body, end='')
    else:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        # ⚠️ 必须显式写 LF（2026-09-21 实测踩到）：本机是 Windows，`write_text` 的默认
        #    newline 会把每行结尾写成 CRLF。看门狗跑在 Linux 上，`read` 会把末尾的 `\r`
        #    留在**最后一个字段**里 → targets.conf 第 7 段 name 变成 `flux1\r`，
        #    而 manager 用 `'flux1'` 去匹配 → **永远不中** → active 收敛静默失效
        #    （症状：机器明明开着，候选集却是空的，且全程无报错）。
        #    这是「Windows 生成 / Linux 消费」的经典坑，必须在这里断根。
        out.write_bytes(body.encode('utf-8'))
        print(f'✅ 已写入 {out}（{len(lines)} 台）')
        for n, why in skipped:
            print(f'   ⏭  跳过 {n}: {why}')

    if a.upload:
        # 走本机 ssh 别名（~/.ssh/config 里配好的 vps-aliyun）
        local = a.out if not a.print else str(DEFAULT_OUT)
        if a.print and not Path(local).exists():
            print('❌ --print + --upload 组合下没有文件可传（先落盘再传）')
            return 1
        r = subprocess.run(['scp', local, a.upload], capture_output=True, text=True)
        print(('✅ 已上传 ' + a.upload) if r.returncode == 0
              else f'❌ 上传失败: {(r.stderr or r.stdout).strip()[:200]}')
        return r.returncode
    return 0


if __name__ == '__main__':
    sys.exit(main())

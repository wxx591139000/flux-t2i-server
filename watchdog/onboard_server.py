#!/usr/bin/env python3
"""克隆 / 新增一台 GPU 机后的**一条命令**接入：登记 → 同步看门狗清单 → 装公钥 → 可选验收。

为什么要有它（2026-09-20）：
  用户克隆实例到新服务器后要手工做四件事：改 servers.json、跑 deploy_vps.py、
  跑 authorize_key.py、跑 acceptance_gpu.py。漏一件就是「服务器开着、网站一直转圈」，
  而且四件分散在三个文件里，隔几天再来根本记不全。现在压成一条命令。

做什么（按顺序，任一步失败就停并报清楚）：
  1. 登记进 manager/servers.json（唯一事实来源；同名条目会被更新，不是重复追加）
  2. 可选 --retire <name>：把被替换掉的旧机器标 enabled:false（保留留痕，不再探活）
  3. 调 deploy_vps.py：重新生成 targets.conf → 传 VPS → 重启看门狗（带 --authorize 顺手装公钥）
  4. 可选 --accept：跑 manager/acceptance_gpu.py 做端到端验收（真的出图）

用法：
  # 克隆了一台 klein 机（能图生图）
  python watchdog/onboard_server.py --name flux5 --host connect.westc.seetacloud.com --port 31234 \
      --model klein --retire flux4

  # 克隆了一台 dev 机（只能文生图）
  python watchdog/onboard_server.py --name flux6 --host connect.westd.seetacloud.com --port 45678 \
      --model dev

  # 登记完直接端到端验收（会真的出图，要几分钟）
  python watchdog/onboard_server.py --name flux5 --host ... --port ... --model klein --accept

  python watchdog/onboard_server.py --name flux7 --host ... --port ... --model klein --dry-run
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

REGISTRY = BASE_DIR / 'manager' / 'servers.json'
WATCHDOG = Path(__file__).parent

# 模型预设：klein（能图生图）/ dev（只能文生图）
PRESETS = {
    'klein': {
        'remote_model': '/root/klein-models/FLUX.2-klein-4B',
        'offload': 'none',
        'supports_edit': True,
        'why': 'klein 4B 约 15 GiB，32G 卡可 offload=none；pipeline 接受 image → 能图生图',
    },
    'dev': {
        'remote_model': '/root/autodl-tmp/models/FLUX.1-dev',
        'offload': 'model',
        'supports_edit': False,
        'why': 'FLUX.1-dev 权重 ~31 GiB，32G 卡必须 offload=model；FluxPipeline 无 image 参数 → 不能图生图',
    },
}
PY = 'py -3.11'


def run(cmd, timeout=600):
    print('\n$ ' + cmd)
    r = subprocess.run(cmd, shell=True, cwd=str(BASE_DIR), timeout=timeout)
    if r.returncode != 0:
        print(f'   ❌ 退出码 {r.returncode} —— 后续步骤已中止，先解决这一步')
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description='一条命令接入新 GPU 机')
    ap.add_argument('--name', required=True, help='机器名，写进 jobs.server，排查时看得到是哪台出的图')
    ap.add_argument('--host', required=True, help='AutoDL SSH 命令里的 host')
    ap.add_argument('--port', required=True, type=int, help='AutoDL SSH 命令里的 port')
    ap.add_argument('--user', default='root')
    ap.add_argument('--model', choices=sorted(PRESETS), default='klein',
                    help='klein=能图生图（默认）/ dev=只能文生图')
    ap.add_argument('--model-path', default=None, help='覆盖预设的模型路径')
    ap.add_argument('--workdir', default='/root/autodl-tmp/flux-t2i')
    ap.add_argument('--identity-file', default='~/.ssh/id_rsa_musetalk')
    ap.add_argument('--retire', default=None, help='把这台旧机器标 enabled:false（被替换掉的）')
    ap.add_argument('--note', default='', help='备注（从哪克隆的、什么卡）')
    ap.add_argument('--no-deploy', action='store_true', help='只登记，不同步到 VPS')
    ap.add_argument('--accept', action='store_true', help='登记后跑端到端验收（真出图）')
    ap.add_argument('--dry-run', action='store_true', help='只打印将要写入的条目，不改文件')
    a = ap.parse_args()

    preset = PRESETS[a.model]
    entry = {
        'name': a.name,
        'host': a.host,
        'port': a.port,
        'user': a.user,
        'identity_file': a.identity_file,
        'remote_base': a.workdir,
        'remote_model': a.model_path or preset['remote_model'],
        'offload': preset['offload'],
        'supports_edit': preset['supports_edit'],
        'note': (a.note or f"由 watchdog/onboard_server.py 登记").strip()
                + f"｜模型预设 {a.model}：{preset['why']}",
    }

    print('=' * 62)
    print(f'接入新 GPU 机：{a.name}  {a.host}:{a.port}  模型={a.model}')
    print('=' * 62)
    print(json.dumps(entry, ensure_ascii=False, indent=2))

    data = json.loads(REGISTRY.read_text(encoding='utf-8'))
    servers = data.get('servers', [])

    if a.retire:
        hit = [s for s in servers if s.get('name') == a.retire]
        if not hit:
            print(f'\n❌ --retire {a.retire} 在注册表里找不到')
            return 1
        hit[0]['enabled'] = False
        hit[0]['note'] = (hit[0].get('note', '') +
                          f'｜⚠️ {a.retire} 已被 {a.name} 取代，停用留痕，不再探活').strip('｜')
        print(f'\n♻️  旧机 {a.retire} 已标 enabled=false（留痕，不参与探活）')

    old = [s for s in servers if s.get('name') == a.name]
    if old:
        print(f'\n♻️  同名条目 {a.name} 已存在 → 原地更新（不重复追加）')
        old[0].update(entry)
    else:
        servers.append(entry)
    data['servers'] = servers

    if a.dry_run:
        print('\n[dry-run] 未写入 servers.json，也未同步 VPS')
        return 0

    REGISTRY.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'\n✅ 已写入 {REGISTRY}')

    # 立刻用代码验证一遍：注册表能被解析且新机器在生效候选里
    importlib = __import__('importlib')
    importlib.reload(fsm)
    fsm.REGISTRY_PATH = REGISTRY
    active = fsm.load_registry()
    if a.name not in [s['name'] for s in active]:
        print(f'❌ 写完读回来却找不到 {a.name}（enabled 被置 false 了？）')
        return 1
    print(f'✅ 回读校验通过，当前生效候选：{[s["name"] for s in active]}')

    if a.no_deploy:
        print('\n（--no-deploy：跳过 VPS 同步）')
        return 0

    if not run(f'{PY} watchdog/deploy_vps.py --authorize'):
        return 1
    print('\n✅ 看门狗清单已同步，公钥已尝试装机（机器必须在线才装得上）')

    if a.accept:
        if not run(f'{PY} manager/acceptance_gpu.py --server {a.name}'):
            return 1
        print(f'\n✅ {a.name} 端到端验收通过')
    else:
        print(f'\n下一步（可选但要跑）：{PY} manager/acceptance_gpu.py --server {a.name}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""上游代码上机验收：确认 GPU 机跑的是本地当前版本，且画像表实跑正确。

**这个脚本解决的痛点**（2026-09-21 全部踩过）：
  1. 上传后 `upload_scripts()` 返回 ok=True，**但文件可能没落盘**
     （实测：上传后校验是新版，再查变回旧版，mtime 却是新的）。
     → 必须**两次**独立复验哈希，不能只信上传返回值。
  2. Windows 客户端会剥掉 ssh 远端命令的引号 → 命令被拆坏，
     报错却像"文件里没有这个东西"。→ 全部走 base64（见 `_ssh_exec.py`）。
  3. `importlib exec_module` 不注册 `sys.modules` → 假 AttributeError。
     → 探针里先注册再执行。

用法:
    py -3.11 tools/verify_deploy.py --server flux5
    py -3.11 tools/verify_deploy.py --server flux5 --skip-run   # 只比哈希

退出码: 0 全部通过 / 1 有失败项
"""
import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import manager.flux_server_manager as fsm  # noqa: E402

REMOTE_PY = '/root/miniconda3/envs/flux/bin/python'
REMOTE_BASE = '/root/autodl-tmp/flux-t2i'
FILES = [
    ('server/flux_resident_server.py', 'flux_resident_server.py'),
    ('server/start_resident.sh', 'start_resident.sh'),
]

# 远端实跑探针：导入模块并调 model_profile（纯 CPU，不加载模型）
PROBE = r'''
import importlib.util, json, sys
P = 'REMOTE_BASE/flux_resident_server.py'
spec = importlib.util.spec_from_file_location('frs', P)
m = importlib.util.module_from_spec(spec)
sys.modules['frs'] = m          # ★ 先注册再执行
spec.loader.exec_module(m)
print('HAS=' + json.dumps(sorted(
    n for n in ('model_profile', 'MODEL_PROFILES', 'FALLBACK_PROFILE') if hasattr(m, n))))
out = {}
for cls in ('Flux2KleinPipeline', 'FluxPipeline', 'SomethingUnknown'):
    d, mx, g, note = m.model_profile(cls)
    out[cls] = {'steps': d, 'max_steps': mx, 'guidance': g}
out['_keys'] = sorted(m.MODEL_PROFILES.keys())
print('RESULT=' + json.dumps(out, ensure_ascii=False))
'''


def md5_local(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def sh(srv, script, timeout=180):
    """base64 传输整条脚本再执行（规避 Windows 剥引号）。"""
    b64 = base64.b64encode(script.encode('utf-8')).decode('ascii')
    return fsm.run(f'ssh {fsm.ssh_target(srv)} "echo {b64} | base64 -d | sh"',
                   timeout=timeout)


def remote_md5s(srv) -> dict:
    remote = ' '.join(f'{REMOTE_BASE}/{r}' for _, r in FILES)
    ok, out = sh(srv, f'md5sum {remote}')
    d = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 32:
            d[Path(parts[1]).name] = parts[0]
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--server', default='flux5')
    ap.add_argument('--skip-run', action='store_true', help='跳过画像实跑（只比哈希）')
    args = ap.parse_args()

    srv = [s for s in fsm.load_registry() if s.get('name') == args.server]
    if not srv:
        print(f'注册表里没有 {args.server}')
        return 1
    srv = srv[0]
    fails = []

    # ── 第 1 关：哈希比对（查两次，防"写入未落盘"）──
    print('══ 第 1 关：本地 vs 远端 哈希 ══')
    for label in ('第一次', '第二次'):
        rmd = remote_md5s(srv)
        for local_rel, remote_name in FILES:
            lmd = md5_local(BASE / local_rel)
            r = rmd.get(remote_name)
            status = 'PASS' if r == lmd else 'FAIL'
            print(f'  [{status}] {label} {remote_name}: {r} (本地 {lmd})')
            if r != lmd and label == '第二次':
                fails.append(f'{remote_name} 哈希不一致（本地 {lmd} / 远端 {r}）')
        if label == '第一次':
            print('  --- 再查一次，确认已落盘 ---')

    # ── 第 2 关：远端实跑画像表 ──
    if not args.skip_run:
        print('\n══ 第 2 关：远端实跑 model_profile() ══')
        probe = PROBE.replace('REMOTE_BASE', REMOTE_BASE)
        b64 = base64.b64encode(probe.encode('utf-8')).decode('ascii')
        p = f'{REMOTE_PY} /tmp/_verify_probe.py'
        ok, out = fsm.run(
            f'ssh {fsm.ssh_target(srv)} '
            f'"echo {b64} | base64 -d > /tmp/_verify_probe.py && {p}"', timeout=180)
        res = [l for l in out.splitlines() if l.startswith('RESULT=')]
        if not res:
            print(out)
            fails.append('远端探针无输出（导入失败？）')
        else:
            data = json.loads(res[0][len('RESULT='):])
            k = data.get('Flux2KleinPipeline', {})
            d = data.get('FluxPipeline', {})
            u = data.get('SomethingUnknown', {})
            checks = [
                ('klein 画像', (k.get('steps'), k.get('max_steps'), k.get('guidance')), (4, 8, 1.0)),
                ('dev 画像', (d.get('steps'), d.get('max_steps'), d.get('guidance')), (25, 100, 3.5)),
                ('未知退回默认', (u.get('steps'), u.get('guidance')), (25, 3.5)),
                ('画像表键名', tuple(data.get('_keys') or []), ('Flux2KleinPipeline', 'FluxPipeline')),
            ]
            for name, got, want in checks:
                st = 'PASS' if got == want else 'FAIL'
                print(f'  [{st}] {name}: {got}' + ('' if got == want else f' 期望 {want}'))
                if got != want:
                    fails.append(f'{name} 实跑结果不对: {got} != {want}')

    print()
    if fails:
        for f in fails:
            print(f'❌ {f}')
        return 1
    print('✅ 上机验收全部通过 —— GPU 机跑的是本地当前版本，画像表实跑正确')
    return 0


if __name__ == '__main__':
    sys.exit(main())

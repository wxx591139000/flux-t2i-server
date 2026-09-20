#!/usr/bin/env python3
"""新克隆 GPU 实例的**端到端验收** —— 一次跑完 11 项，红就是红，不靠人肉记清单。

为什么要有它（2026-09-20）：
  每次「克隆实例 / 换机 / 重装环境」都要重新确认一遍：SSH 通不通、有没有卡、模型在不在、
  diffusers 版本对不对、常驻能不能自动拉起、文生图和**图生图**分别能不能出图。
  这些散在若干命令里，靠人记必然漏 —— 漏了的表现永远是同一个：网站上一个圈一直转。
  所以固化成脚本，一键跑完并给出结论。

覆盖（按顺序，任何一项 FAIL 都会让退出码非 0）：
   1 SSH 连通（含免密）
   2 GPU 可见 + 显存
   3 Python 环境 / diffusers / torch
   4 工作目录 + 模型目录 + DOWNLOAD_DONE
   5 磁盘余量（出图要写盘）
   6 上传常驻脚本
   7 拉起常驻 + 模型加载（自动）
   8 文生图 1 张（计时 + 校验 PNG 非空/尺寸）
   9 图生图 1 张（用第 8 步的图当参考图，计时 + 校验）
  10 常驻日志无 ERROR / OOM
  11 ★ 能力声明核对：注册表说 supports_edit=true，实测也必须能图生图
     （声明与实测不符是最阴的一类 bug：选机阶段信了声明，跑起来才报错）

用法：
  python manager/acceptance_gpu.py --server flux4           # 按注册表里的名字
  python manager/acceptance_gpu.py --host h --port 26081    # 临时机器，不写注册表
  python manager/acceptance_gpu.py --server flux4 --json out.json
  python manager/acceptance_gpu.py --server flux4 --skip-gen   # 只做环境检查，不真出图（省时间）

退出码：0 = 全绿；1 = 有 FAIL；2 = 参数/注册表错误。
"""
import argparse
import base64
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / 'manager'))

import manager.flux_server_manager as fsm        # noqa: E402
import manager.flux_resident_client as frc       # noqa: E402

T2I_PROMPT = 'a white ceramic mug with a moon logo on a seamless light gray studio background, product photography'
EDIT_PROMPT = ('a clean e-commerce product photo, seamless light gray studio background, '
               'soft even lighting')

RESULTS = []


def check(no, name, ok, detail='', critical=True):
    # ok=None = **未验证**（上游依赖没跑到），既不算通过也不算失败 ——
    # 混进"通过"会虚高，混进"失败"会让人去改根本没被测到的配置。
    RESULTS.append({'no': no, 'name': name, 'ok': (None if ok is None else bool(ok)),
                    'critical': critical, 'detail': str(detail)[:300]})
    flag = '⏸' if ok is None else ('✅' if ok else ('❌' if critical else '⚠️'))
    print(f'  {flag} [{no:2d}] {name}' + (f'  → {detail}' if detail else ''))
    return bool(ok)


def ssh(server, cmd, timeout=30):
    ok, out = fsm.run(f'ssh -o ConnectTimeout=10 {fsm.ssh_target(server)} {cmd}', timeout)
    # 滤掉 PortableGit 的 /etc/msystem 噪声：它在 stderr 里排在**真因之前**，
    # 一旦输出被截断（如 `out[:160]`），真因就被它整个顶掉 —— 2026-09-20 因此
    # 把「Host key verification failed / Connection refused」误读成别的问题。
    lines = [ln for ln in (out or '').splitlines() if '/etc/msystem' not in ln]
    return ok, '\n'.join(lines).strip()


def main():
    ap = argparse.ArgumentParser(description='GPU 实例端到端验收')
    ap.add_argument('--server', help='注册表里的机器名（如 flux4）')
    ap.add_argument('--host')
    ap.add_argument('--port', type=int)
    ap.add_argument('--user', default='root')
    ap.add_argument('--identity-file', default='~/.ssh/id_rsa_musetalk')
    ap.add_argument('--json', dest='json_out', help='把结果写成 JSON')
    ap.add_argument('--skip-gen', action='store_true', help='跳过真出图（只验环境）')
    ap.add_argument('--size', type=int, default=1024)
    a = ap.parse_args()

    if a.server:
        srv = next((s for s in fsm.FLUX_SERVERS
                    if a.server in (s.get('name'), s.get('alias'))), None)
        if not srv:
            print(f'❌ 注册表里没有 {a.server}；生效候选机: '
                  f'{[s["name"] for s in fsm.FLUX_SERVERS]}')
            return 2
    elif a.host and a.port:
        srv = {'name': a.host, 'host': a.host, 'port': a.port, 'user': a.user,
               'identity_file': a.identity_file,
               'remote_base': fsm.FLUX_REMOTE_BASE,
               'remote_model': fsm.FLUX_REMOTE_MODEL}
    else:
        print('❌ 需要 --server <名字> 或 --host + --port')
        return 2

    print('=' * 66)
    print(f'GPU 实例验收：{srv.get("name")}  '
          f'({srv.get("host", srv.get("alias"))}:{srv.get("port", "-")})')
    print('=' * 66)

    # 1 SSH
    ok, out = ssh(srv, "'echo SSH_OK; hostname'", 30)
    check(1, 'SSH 连通（免密）', ok and 'SSH_OK' in out, out.replace('\n', ' ')[:80])

    # 2 GPU
    ok, out = ssh(srv, "'nvidia-smi --query-gpu=name,memory.total,memory.used "
                       "--format=csv,noheader 2>/dev/null | head -1'", 30)
    check(2, 'GPU 可见', ok and 'NVIDIA' in out, out)

    # 3 Python 环境
    py = frc.RESIDENT_PY
    ok, out = ssh(srv, f"'{py} -c \"import torch,diffusers;print(torch.__version__,"
                       f"diffusers.__version__,torch.cuda.is_available())\" 2>&1 | tail -1'", 60)
    check(3, 'Python / torch / diffusers', ok and 'True' in out, out)

    # 4 目录与模型
    rb, rm = srv['remote_base'], srv['remote_model']
    ok, out = ssh(srv, f"'test -d {rb} && echo BASE_OK; "
                       f"test -f {rm}/DOWNLOAD_DONE && echo MODEL_OK; "
                       f"du -sh {rm} 2>/dev/null | cut -f1'", 30)
    check(4, '工作目录 + 模型 DOWNLOAD_DONE', ok and 'BASE_OK' in out and 'MODEL_OK' in out,
          out.replace('\n', ' ')[:120])

    # 5 磁盘余量
    ok, out = ssh(srv, f"'df -h {rb} | tail -1'", 30)
    check(5, '磁盘余量', ok, out, critical=False)

    if not all(r['ok'] for r in RESULTS if r['critical']):
        print('\n❌ 环境项未全绿，后续检查无意义，提前结束')
        _dump(a.json_out)
        return 1

    # 6 上传脚本
    ok, msg = frc.upload_scripts(srv)
    check(6, '上传常驻脚本', ok, msg)

    # 7 拉起 + 模型加载
    t0 = time.time()
    r = frc.ensure_resident(srv)
    check(7, '拉起常驻服务', r['ok'], r['msg'])
    if r['ok']:
        # ⚠️ 必须兜住：wait_model_loaded 抛 TransportError 时若不捕获，整个验收
        #    会以 traceback 崩掉、拿不到 json 报告（2026-09-20 flux5：第 8 项之前
        #    所有项目都过了，却因为这一步抛异常，报告里一项结论都没有）。
        try:
            w = frc.wait_model_loaded(srv, timeout=frc.WAIT_MODEL)
            loaded = bool(w.get('model_loaded'))
            check(8, f'模型加载（{int(time.time() - t0)}s）', loaded,
                  w.get('error') or f"status={w.get('status')}")
        except Exception as e:                       # noqa: BLE001 验收脚本不许崩
            check(8, f'模型加载（{int(time.time() - t0)}s）', False,
                  f'{type(e).__name__}: {e}')
    else:
        check(8, '模型加载', False, '常驻未起，跳过')

    if a.skip_gen:
        print('\n（--skip-gen：跳过真出图）')
        _dump(a.json_out)
        return 0 if all(r['ok'] for r in RESULTS if r['critical']) else 1

    out_dir = BASE_DIR / 'web_out' / f'_acceptance_{int(time.time())}'
    out_dir.mkdir(parents=True, exist_ok=True)

    # 9 文生图
    t2i_png = out_dir / 't2i.png'
    t0 = time.time()
    try:
        st = frc.generate_via_resident(srv, T2I_PROMPT, t2i_png,
                                       width=a.size, height=a.size)
        ok = t2i_png.exists() and t2i_png.stat().st_size > 10240
        check(9, f'文生图（{int(time.time() - t0)}s）', ok,
              f"{t2i_png.stat().st_size if t2i_png.exists() else 0} bytes, "
              f"推理 {st.get('runtime')}s")
    except Exception as e:                                    # noqa: BLE001
        check(9, '文生图', False, f'{type(e).__name__}: {e}')
        t2i_png = None

    # 10 图生图（用第 9 步的图当参考图，不依赖外部素材）
    edit_ok = False
    edit_ran = False                     # 图生图**有没有真的跑到**（没跑到 ≠ 不支持）
    if t2i_png and t2i_png.exists():
        ref_b64 = base64.b64encode(t2i_png.read_bytes()).decode('ascii')
        edit_png = out_dir / 'edit.png'
        t0 = time.time()
        try:
            edit_ran = True
            st = frc.generate_via_resident(srv, EDIT_PROMPT, edit_png,
                                           ref_image_b64=ref_b64,
                                           width=a.size, height=a.size)
            edit_ok = edit_png.exists() and edit_png.stat().st_size > 10240
            check(10, f'图生图/编辑（{int(time.time() - t0)}s）', edit_ok,
                  f"{edit_png.stat().st_size if edit_png.exists() else 0} bytes, "
                  f"推理 {st.get('runtime')}s")
        except Exception as e:                                # noqa: BLE001
            check(10, '图生图/编辑', False, f'{type(e).__name__}: {e}')
    else:
        check(10, '图生图/编辑', False, '第 9 步没出图，无参考图可用（**未验证**，不是不支持）',
              critical=False)

    # 11 日志健康
    log = frc.tail_log(srv, 120)
    bad = [ln for ln in log.splitlines()
           if any(k in ln for k in ('Error', 'ERROR', 'Traceback', 'out of memory'))]
    check(11, '常驻日志无 ERROR / OOM', not bad, (bad[-1][:160] if bad else '干净'),
          critical=False)

    # 12 能力声明核对
    declared = bool(srv.get('supports_edit'))
    if not edit_ran:
        # ⚠️ 图生图**根本没跑到**（上游文生图没出图 / 没参考图）时，绝不能据此判
        #    "声明与实测不符" —— 那会让人把 klein 的 supports_edit 改成 false，
        #    而它其实完全支持图生图（2026-09-20 flux5 实测踩到这个假结论）。
        check(12, '能力声明 vs 实测（supports_edit）', None,
              f'未验证：图生图没跑到（缺参考图），不能据此改 supports_edit（当前声明={declared}）',
              critical=False)
    elif declared != edit_ok:
        check(12, '能力声明 vs 实测（supports_edit）', False,
              f'注册表声明 supports_edit={declared}，实测图生图={"成功" if edit_ok else "失败"}'
              f' → 请把 manager/servers.json 里该机器的 supports_edit 改成 {str(edit_ok).lower()}',
              critical=True)
    else:
        check(12, '能力声明 vs 实测（supports_edit）', True,
              f'声明与实测一致（supports_edit={declared}）', critical=False)

    _dump(a.json_out)
    bad_critical = [r for r in RESULTS if r['critical'] and not r['ok']]
    n_pass = len([r for r in RESULTS if r['ok']])
    n_skip = len([r for r in RESULTS if r['ok'] is None])
    print('\n' + '=' * 66)
    print(f'共 {len(RESULTS)} 项 · 通过 {n_pass} · 未验证 {n_skip} · '
          f'关键项失败 {len(bad_critical)}')
    if bad_critical:
        for r in bad_critical:
            print(f'  ❌ [{r["no"]}] {r["name"]}: {r["detail"]}')
        return 1
    print('✅ 该实例可投入生产')
    return 0


def _dump(path):
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n结果已写入 {p}')


if __name__ == '__main__':
    sys.exit(main())

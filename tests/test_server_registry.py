#!/usr/bin/env python3
"""回归门：机器注册表 / 连接目标 / 编辑能力选机 / 等待判死口径 / 参考图落盘

起因（2026-09-20 一次真实事故，全部都是同一个现象：网站上一直转圈）
  1) 候选机只从 ~/.ssh/config 匹配 autodl-flux* 自动发现。新克隆的 GPU 机没写进
     那个仓库外的文件 → manager 永远看不见它 → 任务进 waiting 池死等 8 分钟。
     → 修法：注册表搬进仓库 manager/servers.json，且支持 host/port/user **直连**。
  2) 图生图要 pipeline 的 __call__ 接受 `image`，而 FLUX.1-dev 的 FluxPipeline
     没有这个参数 → edit 任务落在 dev 机上必然报错。
     → 修法：注册表声明 supports_edit，选机阶段按能力过滤。
  3) waiting 任务按「重试 3 次」判死，而机器开机要 1~3 分钟 → 用户刚点开机，
     任务已被判死并退款，机器白开。
     → 修法：改成按**等待总时长**（WAIT_MAX_SEC）判死。
  4) 编辑任务的参考图只在内存 → manager 一重启 / FIFO 淘汰，编辑任务必失败。
     → 修法：同时落盘 web_out/<job_id>/ref.png，用时磁盘兜底。

本门守住七条不变量（全离线：不连 SSH、不要 GPU、不碰数据库）
  1. 注册表能被解析，enabled=false 的记录被跳过（释放的机器不再白等 SSH 超时）
  2. 直连条目解析出 -p/-i（ssh）与 -P/-i（scp）；无 host 的条目回退 alias
  3. 生效候选机 = 注册表 + 代码内默认机的并集，不重复
  4. need_edit=True 时只在 supports_edit 的机器里挑（dev 机常驻在跑也不选它）
  5. 没有任何机器声明 supports_edit 时不崩、不过滤（向后兼容老部署）
  6. flux_queue 里 waiting 按等待时长判死，且 edit 任务按能力选机（源码级）
  7. 参考图双写：_put_ref 落盘 / _get_ref 磁盘兜底 / _drop_ref 终态清理（源码级）

用法: python tests/test_server_registry.py    # 全绿 exit 0，有红 exit 1
"""
import ast
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, 'manager'))

import manager.flux_server_manager as fsm          # noqa: E402
import manager.flux_resident_client as frc        # noqa: E402

REGISTRY = os.path.join(BASE, 'manager', 'servers.json')
QUEUE_SRC = os.path.join(BASE, 'manager', 'flux_queue.py')

RESULTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond), detail))
    print(f'  {"✅" if cond else "❌"} {name}{("  → " + detail) if detail else ""}')


def t_registry_enabled():
    print('\n[1] 注册表：解析 + enabled 过滤')
    raw = fsm.load_registry.__wrapped__ if hasattr(fsm.load_registry, '__wrapped__') else fsm.load_registry
    all_items = raw(REGISTRY) if raw is not fsm.load_registry else _load_raw()
    check('注册表可解析且非空', len(all_items) > 0, f'{len(all_items)} 条')
    names_all = [s['name'] for s in all_items]
    check('含 flux4（已释放的机器仍留痕）', 'flux4' in names_all, str(names_all))
    active = [s for s in fsm.FLUX_SERVERS]
    names_active = [s['name'] for s in active]
    check('enabled=false 的 flux4 不参与探活', 'flux4' not in names_active, str(names_active))
    check('生效候选机不重复', len(names_active) == len(set(names_active)), str(names_active))
    check('每台生效机器都有 remote_base / remote_model',
          all(s.get('remote_base') and s.get('remote_model') for s in active))


def _load_raw():
    import json
    with open(REGISTRY, encoding='utf-8') as f:
        data = json.load(f)
    return data.get('servers', [])


def t_targets():
    print('\n[2] 连接目标：ssh -p / scp -P / alias 回退')
    s = {'name': 'x', 'host': 'h.example.com', 'port': 26081, 'user': 'root',
         'identity_file': '~/.ssh/id_rsa_musetalk'}
    ssh_t = fsm.ssh_target(s)
    scp_t = fsm.scp_target(s)
    check('ssh 用小写 -p', ' -p 26081 ' in f' {ssh_t} ' or ssh_t.endswith(' -p 26081'), ssh_t)
    check('scp 用大写 -P', ' -P 26081' in scp_t, scp_t)
    check('两者都带上私钥 -i', '-i ~/.ssh/id_rsa_musetalk' in ssh_t and '-i ~/.ssh/id_rsa_musetalk' in scp_t)
    check('两者都是 user@host', ssh_t.endswith('root@h.example.com') and scp_t.endswith('root@h.example.com'))
    check('scp 不残留小写 -p（语义是保留时间戳）', ' -p ' not in scp_t, scp_t)
    a = {'name': 'y', 'alias': 'autodl-flux'}
    check('无 host 时回退 alias', fsm.ssh_target(a) == 'autodl-flux' and fsm.scp_target(a) == 'autodl-flux')
    p = {'name': 'z', 'host': 'h2', 'port': 22, 'user': 'ubuntu'}
    check('无 identity_file 时不带 -i', '-i' not in fsm.ssh_target(p), fsm.ssh_target(p))


def _cand(name, supports_edit, **probe):
    p = {'name': name, 'reachable': False, 'gpu_ok': False, 'model_ok': False,
         'resident': False, 'model_loaded': False, 'error': ''}
    p.update(probe)
    return ({'name': name, 'supports_edit': supports_edit}, p)


def t_edit_capability():
    print('\n[3] 编辑能力：need_edit 只在 supports_edit 的机器里挑')
    dev = _cand('dev-machine', False, reachable=True, gpu_ok=True, model_ok=True,
                resident=True, model_loaded=True)     # 常驻在跑·模型已加载（最高级）
    klein = _cand('klein-machine', True, reachable=True, gpu_ok=True, model_ok=True)
    s, p = frc._pick_from([dev, klein], need_edit=True)
    check('选 klein 而不是"常驻在跑"的 dev 机', s and s['name'] == 'klein-machine',
          s['name'] if s else 'None')
    s2, _ = frc._pick_from([dev, klein], need_edit=False)
    check('need_edit=False 仍按分级选（dev 优先）', s2 and s2['name'] == 'dev-machine',
          s2['name'] if s2 else 'None')
    s3, p3 = frc._pick_from([_cand('only-dev', False, reachable=True, gpu_ok=True,
                                   model_ok=True, resident=True, model_loaded=True)],
                            need_edit=True)
    check('无 capable 机器时不过滤（向后兼容，不崩）', s3 is not None and s3['name'] == 'only-dev')
    s4, _ = frc._pick_from([], need_edit=True)
    check('空候选返回 None', s4 is None)


def t_source_level():
    print('\n[4] 源码级：waiting 判死 / 能力选机 / 参考图落盘')
    src = open(QUEUE_SRC, encoding='utf-8').read()
    check('WAIT_MAX_SEC 已定义', 'WAIT_MAX_SEC' in src)
    check('waiting 判死用等待时长而非重试次数',
          'waited > WAIT_MAX_SEC' in src and 'retry >= MAX_RETRY' not in src)
    check('edit 任务按能力选机', 'find_available_server(need_edit=is_edit)' in src)
    check('参考图落盘 _ref_dir', 'def _ref_dir(self, job_id: str)' in src)
    check('参考图磁盘兜底 _get_ref', 'def _get_ref(self, job_id: str)' in src)
    check('参考图终态清理 _drop_ref', 'def _drop_ref(self, job_id: str)' in src)
    check('done/failed 两处都调用 _drop_ref', src.count('self._drop_ref(job_id)') >= 2,
          f'{src.count("self._drop_ref(job_id)")} 处')

    tree = ast.parse(src)
    has_persist = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == 'write_bytes' and 'ref_dir' in ast.dump(node.func.value):
                has_persist = True
    check('_put_ref 确实写盘（AST 断言）', has_persist)
    check('WAIT_MAX_SEC 默认 4 小时', 'str(4 * 3600)' in src or '14400' in src)


def t_mutation():
    """变异断言：把过滤条件拿掉，门必须变红（证明门真的在起作用，不是恒真）。"""
    print('\n[5] 变异断言：拆掉能力过滤，选机必须退化')
    dev = _cand('dev-machine', False, reachable=True, gpu_ok=True, model_ok=True,
                resident=True, model_loaded=True)
    klein = _cand('klein-machine', True, reachable=True, gpu_ok=True, model_ok=True)

    def broken_pick(cands, need_edit=False):
        """变异版：忽略 need_edit（等价于改回旧行为）"""
        for s, p in cands:
            if p['resident'] and p['model_loaded']:
                return s, p
        for s, p in cands:
            if p['reachable'] and p['gpu_ok'] and p['model_ok']:
                return s, p
        return None, None

    good = frc._pick_from([dev, klein], need_edit=True)[0]['name']
    bad = broken_pick([dev, klein], need_edit=True)[0]['name']
    check('正常实现选 klein，变异版选 dev（门能抓住）',
          good == 'klein-machine' and bad == 'dev-machine', f'正常={good} 变异={bad}')


def main():
    print('=' * 60)
    print('服务器注册表 / 选机 / 等待策略 回归门')
    print('=' * 60)
    t_registry_enabled()
    t_targets()
    t_edit_capability()
    t_source_level()
    t_mutation()
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

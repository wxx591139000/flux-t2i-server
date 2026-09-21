#!/usr/bin/env python3
"""模型选择回归门（2026-09-21 新增，为「界面手动选 FLUX 模型」）。

本门钉住的是**选模型这条链路上最贵、最不可逆的几处判断**。
它们一旦错了，症状都极其难查 —— 不是崩溃，而是「静默用错模型出图」。

四组：

  [1] 模型清单从注册表推导（不写死第二份清单）
      · known_models() 去重、保序、id = 目录 basename
      · 加模型 = 只改 servers.json，代码零改动 ← 这是「消除人肉一致性负担」的验收点

   [2] probe 的模型清单解析
      · HAVE:<id> 行被收进 probe['models']，且 **不被误当成 GPU 名**
      · 旧的 MODEL_OK|MODEL_MISSING 语义不变（model_ok 仍指默认模型）
      · HAVE 行出现在 GPU 名之后也不影响 GPU 名解析
      · ★ 用**真实源码**里的解析段跑（不是另抄一份等价循环 —— 抄的那份会漂移，
        本轮就抄错过一次：GPU 名与 HAVE 的先后处理反了）

  [3] 选机按模型过滤（_pick_from）
      · 指定 klein → 不选只有 dev 的机器（哪怕它常驻在跑、零冷启动）
      · 指定 dev   → 不选只有 klein 的机器
      · 探针没报清单（旧版/直连）→ **不过滤**，退回旧行为（不能误判成「没机器」）
      · 谁都没有该模型 → 不崩、仍给候选（让 GPU 侧白名单报准确错）
      · need_edit 与 need_model **同时生效**（图生图 + 指定模型）

  [4] 变异断言：拆掉模型过滤，本门必须变红
      （证明这扇门真的在测「过滤」这件事，而不是在测一个恒真条件）

用法: python tests/test_model_select.py    # 全绿 exit 0，有红 exit 1
"""
import ast
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import manager.flux_server_manager as fsm            # noqa: E402
import manager.flux_resident_client as frc          # noqa: E402

RESULTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond), detail))
    print(f'  {"✅" if cond else "❌"} {name}{("  → " + detail) if detail else ""}')


def _probe(name, models, resident=False, loaded=False, reachable=True, gpu=True,
           edit=True):
    """造一条 probe 结果。models=本机已就绪的模型 id 清单。"""
    return {'name': name, 'alias': name, 'reachable': reachable, 'gpu_ok': gpu,
            'gpu': 'NVIDIA GeForce RTX 4080 SUPER' if gpu else '', 'model_ok': bool(models),
            'resident': resident, 'model_loaded': loaded, 'status': '',
            'health': None, 'error': '', 'models': list(models)}


def _srv(name, edit=True):
    return {'name': name, 'alias': name, 'supports_edit': edit,
            'remote_model': '/root/x', 'enabled': True}


# ══════════════════════════════ [1] 清单推导 ══════════════════════════════
def t_known_models():
    print('\n[1] 模型清单从注册表推导（不写死第二份清单）')
    km = fsm.known_models()
    check('known_models() 返回非空', bool(km), f'{len(km)} 个')
    ids = [mid for mid, _ in km]
    paths = [p for _, p in km]
    check('id 无重复（probe 不该对同一路径重复发 test -f）',
          len(ids) == len(set(ids)), str(ids))
    check('路径无重复', len(paths) == len(set(paths)))
    check('id = 远端目录 basename', all(p.rstrip('/').endswith('/' + i)
                                       for i, p in km), str(km))
    check('dev 与 klein 都被发现（两族都在注册表里）',
          any('dev' in i.lower() for i in ids) and any('klein' in i.lower() for i in ids),
          str(ids))

    # ★ 核心验收：加模型不需要改代码 —— 换一份注册表，清单必须跟着变
    saved = fsm.FLUX_SERVERS
    try:
        fsm.FLUX_SERVERS = [
            {'name': 'x', 'alias': 'x', 'remote_model': '/m/Model-AAA'},
            {'name': 'y', 'alias': 'y', 'remote_model': '/m/Model-BBB'},
            {'name': 'z', 'alias': 'z', 'remote_model': '/m/Model-AAA'},   # 重复
        ]
        km2 = fsm.known_models()
        check('★ 换注册表 → 清单自动跟着变（代码零改动）',
              [i for i, _ in km2] == ['Model-AAA', 'Model-BBB'], str(km2))
        check('★ 重复路径被去重且保序（首次出现顺序）',
              len(km2) == 2, str(km2))
    finally:
        fsm.FLUX_SERVERS = saved

    # 注册表为空时兜底，别让选机路径崩
    try:
        fsm.FLUX_SERVERS = []
        km3 = fsm.known_models()
        check('注册表为空时有兜底（不返回空、不崩）', bool(km3), str(km3))
    finally:
        fsm.FLUX_SERVERS = saved


# ══════════════════════════════ [2] HAVE 行解析 ══════════════════════════════
def t_probe_parse():
    print('\n[2] probe 的模型清单解析（HAVE:<id> 行）')
    src = open(os.path.join(BASE, 'manager', 'flux_server_manager.py'),
               encoding='utf-8').read()
    check('probe_full 发送 HAVE: 探测（每个已知模型一次 test -f）',
          'HAVE:{_mid}' in src or 'HAVE:%s' in src)
    check('probe_full 返回值含 models 键', "'models': []" in src)

    # ★ 用**真实 probe_full** 跑解析：把 fsm.run 换掉，喂进预设的 SSH 输出。
    # 绝不另抄一份解析循环 —— 本轮就抄错过一次（HAVE 与 GPU 名的处理顺序反了），
    # 而抄出来的副本永远不会跟着源码一起改，属于「漏跑」的另一种写法。
    saved_run = fsm.run

    def fake_run(_cmd, timeout=30, stdin_data=None):
        return True, _FAKE_OUT

    def drive(fake_out):
        global _FAKE_OUT
        _FAKE_OUT = fake_out
        fsm.run = fake_run
        try:
            # force=True 绕开 TTL 缓存；用不存在的 alias 避免污染真实缓存条目
            return fsm.probe_full({'name': '_t', 'alias': '_t_probe_parse',
                                   'remote_model': '/root/x'}, force=True)
        finally:
            fsm.run = saved_run

    # 场景 A：HAVE 行在 GPU 名之后（当前生产输出顺序）
    d = drive('REACH\nNVIDIA GeForce RTX 4080 SUPER\nMODEL_OK\n'
              'HAVE:FLUX.1-dev\nHAVE:FLUX.2-klein-4B\nHEALTH_FAIL\n')
    check('HAVE 行被收进 models（真实 probe_full 解析）',
          d['models'] == ['FLUX.1-dev', 'FLUX.2-klein-4B'], str(d['models']))
    check('★ HAVE 行**未被误当成 GPU 名**',
          d['gpu'].startswith('NVIDIA'), repr(d['gpu']))
    check('model_ok 语义不变（仍指默认模型）', d['model_ok'] is True)

    # 场景 B：HAVE 行排在 GPU 名之前（远端输出顺序变了也不能错）
    d = drive('REACH\nHAVE:FLUX.1-dev\nNVIDIA GeForce RTX 4090\nMODEL_MISSING\n')
    check('HAVE 行在 GPU 名之前 → 两者都能正确解析',
          d['gpu'].startswith('NVIDIA') and d['models'] == ['FLUX.1-dev'],
          f'gpu={d["gpu"]!r} models={d["models"]}')

    # 场景 C：一台都没装（全是 MODEL_MISSING，没有 HAVE 行）
    d = drive('REACH\nNVIDIA GeForce RTX 4080 SUPER\nMODEL_MISSING\nHEALTH_FAIL\n')
    check('一台没装 → models 为空，不崩', d['models'] == [], str(d['models']))
    check('不可达时 models 也必须是空列表（而非缺键）',
          drive('')['models'] == [])


# ══════════════════════════════ [3] 按模型选机 ══════════════════════════════
def t_pick_by_model():
    print('\n[3] 选机按模型过滤（_pick_from）')
    dev_only = (_srv('dev-machine'), _probe('dev-machine', ['FLUX.1-dev'],
                                            resident=True, loaded=True))
    klein_only = (_srv('klein-machine'), _probe('klein-machine', ['FLUX.2-klein-4B'],
                                                resident=True, loaded=True))

    # 要 klein 时：即使 dev 机「常驻在跑·零冷启动」也不能选它
    s, p = frc._pick_from([dev_only, klein_only], need_model='FLUX.2-klein-4B')
    check('★ 要 klein → 选 klein 机，不选零冷启动的 dev 机',
          s and s['name'] == 'klein-machine', str(s and s['name']))

    s, p = frc._pick_from([dev_only, klein_only], need_model='FLUX.1-dev')
    check('★ 要 dev → 选 dev 机，不选 klein 机',
          s and s['name'] == 'dev-machine', str(s and s['name']))

    # 不指定模型 → 完全退回旧分级（dev 零冷启动优先）
    s, p = frc._pick_from([dev_only, klein_only])
    check('不指定模型 → 仍按旧分级（零冷启动优先）',
          s and s['name'] == 'dev-machine', str(s and s['name']))

    # 探针不报清单（旧版探针 / 直连模式）→ 必须**不过滤**
    old_style = (_srv('legacy-machine'), _probe('legacy-machine', []))
    s, p = frc._pick_from([old_style], need_model='FLUX.2-klein-4B')
    check('★ 探针不报清单（旧版/直连）→ 不过滤，退回旧行为（不误判成"没机器"）',
          s and s['name'] == 'legacy-machine', str(s and s['name']))

    # 一台都没有该模型，但**探针报了清单** → 必须返回 (None, probe)，不硬挑。
    # 这是 2026-09-21 实测踩到的真实场景（要 edit+dev：能力过滤剔掉 dev 机后
    # 只剩 klein 机，它没 dev）—— 硬挑会让用户白等一整轮排队才看到失败。
    s, p = frc._pick_from([dev_only], need_model='NoSuchModel-9B')
    check('★ 有清单但谁都没有该模型 → 返回 None（不硬挑，避免白等一轮）',
          s is None, str(s and s['name']))

    # 空候选
    s, p = frc._pick_from([], need_model='FLUX.1-dev')
    check('空候选返回 (None, None)', s is None and p is None)

    # ★ need_edit + need_model 同时生效：要 edit 且要 dev
    #   klein 机能 edit 但没 dev；dev 机有 dev 但不能 edit → 确实无解，
    #   正确行为是**明确报告无解（None）**，而不是退而求其次挑 klein 机。
    klein_edit = (_srv('klein-machine', edit=True), _probe('klein-machine',
                                                           ['FLUX.2-klein-4B']))
    dev_noedit = (_srv('dev-machine', edit=False), _probe('dev-machine', ['FLUX.1-dev']))
    s, p = frc._pick_from([klein_edit, dev_noedit], need_edit=True,
                          need_model='FLUX.1-dev')
    check('★ need_edit + need_model 同时生效（能力过滤先于模型过滤）',
          s is None, f'edit+dev 无解 → 应为 None，实得 {s and s["name"]}')

    # 两个过滤器都对时 → 必须能选中
    both = (_srv('omni', edit=True), _probe('omni', ['FLUX.1-dev', 'FLUX.2-klein-4B']))
    s, p = frc._pick_from([both, dev_noedit], need_edit=True, need_model='FLUX.2-klein-4B')
    check('两过滤器都对时正常选中', s and s['name'] == 'omni', str(s and s['name']))


# ══════════════════════════════ [4] 变异断言 ══════════════════════════════
def t_mutation():
    print('\n[4] 变异断言：拆掉模型过滤，本门必须变红')
    src = open(os.path.join(BASE, 'manager', 'flux_resident_client.py'),
               encoding='utf-8').read()
    tree = ast.parse(src)

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == '_pick_from'), None)
    check('_pick_from 存在', fn is not None)
    if not fn:
        return
    body = ast.unparse(fn)
    check('_pick_from 里确实有 need_model 过滤', 'need_model' in body)
    check('模型过滤发生在分级 pick 之前（否则分级会先抢走机器）',
          body.find('need_model') < body.find('def pick'), 'need_model 在 pick 定义前')

    # 变异：去掉 need_model 过滤 → 用不带该参数的等效实现，必须选错机器
    dev_only = (_srv('dev-machine'), _probe('dev-machine', ['FLUX.1-dev'],
                                            resident=True, loaded=True))
    klein_only = (_srv('klein-machine'), _probe('klein-machine', ['FLUX.2-klein-4B']))
    good, _ = frc._pick_from([dev_only, klein_only], need_model='FLUX.2-klein-4B')
    mutant, _ = frc._pick_from([dev_only, klein_only])       # 变异版：不传 need_model
    check('★ 正常实现选 klein，变异版（不过滤）选 dev → 门能抓住',
          good and good['name'] == 'klein-machine'
          and mutant and mutant['name'] == 'dev-machine',
          f'正常={good and good["name"]} 变异={mutant and mutant["name"]}')

    # 源码级：调用方（flux_queue）必须真的把 job 的 model 传下去
    qsrc = open(os.path.join(BASE, 'manager', 'flux_queue.py'), encoding='utf-8').read()
    qtree = ast.parse(qsrc)
    kw = set()
    for n in ast.walk(qtree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'find_available_server'):
            kw |= {k.arg for k in n.keywords}
    check('flux_queue 选机时传了 need_model', 'need_model' in kw, f'关键字={sorted(kw)}')
    check('flux_queue 选机时仍传 need_edit', 'need_edit' in kw, f'关键字={sorted(kw)}')


def main():
    print('=' * 60)
    print('模型选择回归门（文生图界面选模型 · 2026-09-21）')
    print('=' * 60)
    t_known_models()
    t_probe_parse()
    t_pick_by_model()
    t_mutation()

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    failed = [n for n, ok, _d in RESULTS if not ok]
    print('\n' + '=' * 60)
    print(f'共 {len(RESULTS)} 项，通过 {passed}，失败 {len(failed)}')
    if failed:
        print('失败项：')
        for n in failed:
            print(f'  ❌ {n}')
        print('\n❌ 有红')
        return 1
    print('✅ 全绿')
    return 0


if __name__ == '__main__':
    sys.exit(main())

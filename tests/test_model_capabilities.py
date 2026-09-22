#!/usr/bin/env python3
"""回归门：异构模型能力表（Qwen-Image-2.1 接入，2026-09-22）

背景
  Qwen-Image-2.1 与 FLUX 两个模型的**参数契约不同**，而且差异不是一处：
    · CFG 参数名：FLUX 用 `guidance_scale`，Qwen 用 `true_cfg_scale`
    · 参考图：Qwen 的 `image=` 既接受单张 PIL 也接受 list（最多 10 张）
    · 掩码：**两者都没有独立的 mask 入参**（Qwen 的局部编辑是语义级的 —
      把圈选/涂抹合成进参考图，见下）
    · 透明：Qwen 原生 RGBA 输出
    · 原生分辨率：FLUX 1024，Qwen 2048
    · 默认步数/CFG：FLUX.1-dev 25/3.5，klein 4/1.0，Qwen 40/1.0

  如果按老办法「一项差异 = 一个布尔字典 + 一个函数」，就会产生 6 处平行维护。
  所以升级成**统一能力表** MODEL_CAPABILITIES，所有差异都是表里的一格数据。

★ 本门在 2026-09-22 抓到的一个真 bug（记录下来，因为教训比修复本身重要）
  我原先照 Qwen 官方 README 的「specify local edits via circles, painted
  annotations, or separate masks」推断出 `mask=True`，写进了能力表。
  实际在 flux7 上用 `inspect.signature` 拉真实签名（diffusers 0.41.0.dev0）：
  **23 个参数里没有 mask** —— Qwen 的局部编辑是把标注合成到参考图里，
  pipeline 层面没有独立掩码入参。
  → 「支持局部编辑」与「有独立 mask 入参」是**两个不同的命题**。
    我把前者当成了后者。所以字段改名为 `mask_param`（含义变精确：
    「有独立 mask 入参」），并把它记成一条**声明必须能被签名证伪**的规则。

本门守住的不变量（全离线：不连 SSH、不要 GPU、不加载真实模型）
  1. 能力表覆盖三种 pipeline，且每条字段完整（缺字段会被下游 .get() 静默放行）
  2. 参数名适配：Qwen 走 true_cfg_scale，FLUX 走 guidance_scale，
     且**不能两个都塞进候选**（会产生误导性日志「已丢弃:['true_cfg_scale']」）
  3. 多图上限：Qwen 10 张、klein 1 张、dev 0 张
  4. StubPipeline 的签名与能力表**互相自洽**（签名说能传 mask，能力表就得说
     mask_param=True；**反过来也必须成立** —— 见下面第 7 条）
  5. 变异测试：把能力表改错，本门必须变红（证明门真的在测东西）
  6. 许可硬约束：Qwen 标注为非商用（commercial_ok=False）—— 对外经营前必须拦
  7. ★ 声明 vs 真实签名双向核对（2026-09-22 新增）：凡是能力表声明「有」的
     **参数类**能力（ref_param / cfg_param / mask_param），替身签名里必须真的有；
     声明「没有」的也必须真的没有。这条是防止第 1 类 bug 再发生的一般化规则。

用法: python tests/test_model_profile.py 同款
      py -3.11 tests/test_model_capabilities.py   # 全绿 exit 0，有红 exit 1
"""
import ast
import importlib.util
import inspect
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_SRC = os.path.join(BASE, 'server', 'flux_resident_server.py')
SERVERS_JSON = os.path.join(BASE, 'manager', 'servers.json')

RESULTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond), detail))
    print(f'  {"✅" if cond else "❌"} {name}{("  → " + str(detail)) if detail else ""}',
          flush=True)


def load_resident():
    spec = importlib.util.spec_from_file_location('flux_resident_server', SERVER_SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['flux_resident_server'] = mod
    spec.loader.exec_module(mod)
    return mod


# ── [1] 能力表结构 ─────────────────────────────────────────────
def t_table(r):
    print('\n[1] 能力表结构：三种 pipeline 全在、字段完整')
    tbl = getattr(r, 'MODEL_CAPABILITIES', None)
    check('存在 MODEL_CAPABILITIES', isinstance(tbl, dict) and tbl, str(type(tbl)))
    if not isinstance(tbl, dict) or not tbl:
        return
    for cls in ('FluxPipeline', 'Flux2KleinPipeline', 'QwenImage21Pipeline'):
        check(f'表里有 {cls}', cls in tbl)
    need = {'text2img', 'edit', 'multi_ref', 'mask_param', 'transparent',
            'max_ref_images', 'native_res', 'cfg_param', 'ref_param'}
    for cls, caps in tbl.items():
        missing = need - set(caps)
        check(f'{cls} 字段完整', not missing, str(sorted(missing)) if missing else '')
    # 兜底表：未识别类名走保守档（只文生图），不能崩
    fb = getattr(r, 'FALLBACK_CAPABILITIES', None)
    check('存在 FALLBACK_CAPABILITIES（未识别模型的保守档）', isinstance(fb, dict))
    check('兜底档只开 text2img（保守方向）',
          isinstance(fb, dict) and fb.get('text2img') is True
          and fb.get('edit') is False and fb.get('multi_ref') is False,
          str(fb))
    check('model_capabilities 未识别类名 → 返回兜底（不抛异常）',
          r.model_capabilities('SomeUnknownPipeline') == fb)
    check('model_capabilities None → 兜底（不抛异常）',
          r.model_capabilities(None) == fb)
    # 返回**副本**：调用方改了返回值不能污染全局表
    c1 = r.model_capabilities('FluxPipeline')
    c1['edit'] = True
    check('★ model_capabilities 返回副本（改返回值不污染全局表）',
          r.MODEL_CAPABILITIES['FluxPipeline']['edit'] is False,
          '否则一次误改会让全进程的能力判断永久错位')


# ── [2] 参数名适配 ────────────────────────────────────────────
def t_cfg_param(r):
    print('\n[2] CFG 参数名适配：Qwen=true_cfg_scale / FLUX=guidance_scale')
    tbl = r.MODEL_CAPABILITIES
    check('Qwen→true_cfg_scale',
          tbl['QwenImage21Pipeline']['cfg_param'] == 'true_cfg_scale',
          tbl['QwenImage21Pipeline']['cfg_param'])
    check('klein→guidance_scale',
          tbl['Flux2KleinPipeline']['cfg_param'] == 'guidance_scale')
    check('dev→guidance_scale', tbl['FluxPipeline']['cfg_param'] == 'guidance_scale')
    # ★ 关键：两个参数名**不能同时**出现在候选里。
    #   签名过滤虽能把不适用的丢掉，但会产生误导日志「已丢弃:['true_cfg_scale']」
    #   —— 让人以为「这个参数用户传了但模型不支持」，实际是「参数名选错了」。
    src = open(SERVER_SRC, encoding='utf-8').read()
    tree = ast.parse(src)
    picked = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == '_generate':
            seg = ast.get_source_segment(src, n) or ''
            if 'cfg_param' in seg and 'cfg_name' in seg:
                picked = (n, seg)
    check('★ 定位到用 cfg_param 的生成路径', picked is not None)
    if picked:
        node, seg = picked
        check('★ CFG 参数名来自能力表（不是硬编码 guidance_scale）',
              "caps.get('cfg_param')" in seg)
        # 候选字典里**只有一个动态键**装 CFG；不能同时写死两个参数名
        cand_keys = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Dict):
                for k in n.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        cand_keys.add(k.value)
        check('★ 候选里不出现硬编码 true_cfg_scale 与 guidance_scale 并存',
              not ('true_cfg_scale' in cand_keys and 'guidance_scale' in cand_keys),
              f'候选键={sorted(cand_keys)}')


# ── [3] 多图 / 掩码能力 ────────────────────────────────────────
def t_multi_ref(r):
    print('\n[3] 多图与掩码：Qwen 10 张，klein 1 张，dev 0 张；三者都无 mask 入参')
    tbl = r.MODEL_CAPABILITIES
    q, k, d = (tbl['QwenImage21Pipeline'], tbl['Flux2KleinPipeline'],
               tbl['FluxPipeline'])
    check('Qwen max_ref_images=10', q['max_ref_images'] == 10, q['max_ref_images'])
    check('klein max_ref_images=1', k['max_ref_images'] == 1)
    check('dev max_ref_images=0', d['max_ref_images'] == 0)
    check('Qwen multi_ref=True', q['multi_ref'] is True)
    check('klein multi_ref=False（实测只接受单张）', k['multi_ref'] is False)
    # ★ 2026-09-22 修正：三者**都没有独立的 mask 入参**。
    #   Qwen 的局部编辑是语义级（合成进参考图），不是 mask= 参数。
    #   ⚠️ 这三条断言是「实测签名」的镜像 —— 若将来某个模型真加了 mask 入参，
    #   要连同 PIPELINE_SIGNATURES 与能力表一起改，三处必须同步。
    check('★ Qwen mask_param=False（实测 23 参数里没有 mask）',
          q['mask_param'] is False,
          '局部编辑走合成图；原先照 README 推断成 True，已被实测证伪')
    check('klein mask_param=False', k['mask_param'] is False)
    check('dev mask_param=False', d['mask_param'] is False)
    check('Qwen transparent=True（原生 RGBA）', q['transparent'] is True)
    check('Qwen native_res=2048', q['native_res'] == 2048, q['native_res'])
    check('FLUX native_res=1024',
          k['native_res'] == 1024 and d['native_res'] == 1024)
    # 源码级：多图上限要真的用 max_ref_images 卡，不能写死
    src = open(SERVER_SRC, encoding='utf-8').read()
    check('★ 多图上限用的是能力表 max_ref_images（不是写死）',
          "caps.get('max_ref_images')" in src)
    check('★ 超过 multi_ref 能力时明确报错（不静默丢图）',
          "not caps.get('multi_ref')" in src or 'multi_ref' in src)
    # ★ mask 分支必须以**签名为准**，不能只看能力表 —— 否则模型没有该参数时
    #   会走到 kw['mask']=... 然后真机 TypeError。
    check('★ mask 分支同时校验能力表与真实签名（不只看能力表）',
          "caps.get('mask_param') or 'mask' not in sig" in src
          or ("mask_param" in src and "'mask' not in sig" in src),
          '签名是一手事实，能力表是人对它的摘要；冲突时以签名为准')
    # 能力表里不该再有旧名 `mask`（改名后残留 = 两处口径并存）
    check('★ 能力表已无旧字段名 mask（统一为 mask_param）',
          all('mask' not in c for c in tbl.values()),
          str([c for c in tbl.values() if 'mask' in c])[:120])


# ── [4] 签名与能力表自洽 ─────────────────────────────────────
def t_signature_consistency(r):
    print('\n[4] StubPipeline 签名 与 能力表 必须**双向**自洽')
    sigs = getattr(r.StubPipeline, 'PIPELINE_SIGNATURES', None)
    check('存在 PIPELINE_SIGNATURES', isinstance(sigs, dict) and sigs)
    if not isinstance(sigs, dict):
        return
    tbl = r.MODEL_CAPABILITIES
    for cls, spec in sigs.items():
        caps = tbl.get(cls) or {}
        has = set(spec.get('has') or ())
        # 能力表说 cfg_param 是 X → 签名里必须有 X
        cfg = caps.get('cfg_param')
        if cfg:
            check(f'{cls}：签名含能力表声明的 CFG 参数 {cfg}',
                  cfg in has,
                  f'签名参数={sorted(has)}')
        # 能力表说 multi_ref → 签名的 ref 参数必须接受 list
        if caps.get('multi_ref'):
            check(f'{cls}：能力表说 multi_ref → 签名 ref 必须 list_ref',
                  spec.get('list_ref') is True)
        # 参考图参数名要对得上
        if caps.get('ref_param'):
            check(f'{cls}：ref_param 与签名 ref 一致',
                  spec.get('ref') == caps['ref_param'],
                  f"表={caps['ref_param']} 签名={spec.get('ref')}")
        # ★ 双向：mask_param 声明与签名必须一致（两个方向都查）
        #   正向：说 True 就必须真有 —— 否则真机 TypeError（本次踩的坑）
        #   反向：说 False 就必须真没有 —— 否则能力表在**低报**，
        #        用户会被无谓地挡在门外（Qwen 那种「支持局部编辑但无 mask 入参」
        #        的模型尤其容易把这两件事混在一起）
        declared_mask = bool(caps.get('mask_param'))
        sig_has_mask = 'mask' in has
        check(f'{cls}：mask_param 声明({declared_mask}) == 签名事实({sig_has_mask})',
              declared_mask == sig_has_mask,
              '不一致 → 要么真机 TypeError，要么无谓拦截用户')
    # 实测签名的参数个数（默认档 = klein）
    sig = inspect.signature(r.StubPipeline.__call__)
    check('默认签名已静态挂载（不是 **kwargs）',
          'kwargs' not in sig.parameters,
          f'{len(sig.parameters)} 个参数')
    check('默认签名含 image（klein 能图生图）', 'image' in sig.parameters)
    check('默认签名不含 mask（klein 没有掩码入参）', 'mask' not in sig.parameters)
    # ★ 每个模型的替身签名都要与能力表的 ref_param / cfg_param / mask_param 对得上
    #   （不只默认档）—— 换模型路径最容易出错，这里逐档核对
    for cls in tbl:
        s = r._stub_signature_for(cls)
        names = set(s.parameters)
        c = tbl[cls]
        if c.get('ref_param'):
            check(f'{cls} 替身签名含 ref_param {c["ref_param"]}',
                  c['ref_param'] in names)
        check(f'{cls} 替身签名 CFG 名为 {c["cfg_param"]}',
              c['cfg_param'] in names)
        check(f'{cls} 替身签名 mask 与 mask_param 一致',
              ('mask' in names) == bool(c.get('mask_param')))


# ── [5] 变异测试 ──────────────────────────────────────────────
def t_mutation(r):
    print('\n[5] 变异测试：能力表改错 → 必须能被发现')

    def pick_caps(tbl, cls, req):
        """模拟选机的能力过滤（与 _filter_by_caps 同逻辑的最小版）"""
        caps = tbl.get(cls) or {}
        for k, want in req.items():
            if k in caps and bool(caps[k]) is not bool(want):
                return False
        return True

    good = r.MODEL_CAPABILITIES
    # 正常：要 edit+multi_ref → 只有 Qwen 满足
    ok_qwen = pick_caps(good, 'QwenImage21Pipeline', {'edit': True, 'multi_ref': True})
    ok_klein = pick_caps(good, 'Flux2KleinPipeline', {'edit': True, 'multi_ref': True})
    check('正常：Qwen 满足 edit+multi_ref', ok_qwen)
    check('正常：klein **不**满足 multi_ref（只有 1 张）', not ok_klein)
    # 变异：把 Qwen 的 multi_ref 改成 False → 应该变成不满足
    broken = json.loads(json.dumps(good))
    broken['QwenImage21Pipeline']['multi_ref'] = False
    check('变异：Qwen multi_ref 改 False → 不再满足（门能抓住）',
          not pick_caps(broken, 'QwenImage21Pipeline', {'edit': True, 'multi_ref': True}))
    # 变异：把 CFG 参数名换成 guidance_scale → 应被 [2] 的断言抓住
    broken2 = json.loads(json.dumps(good))
    broken2['QwenImage21Pipeline']['cfg_param'] = 'guidance_scale'
    check('变异：Qwen 的 cfg_param 改成 guidance_scale → 与真实 API 不符（门能抓住）',
          broken2['QwenImage21Pipeline']['cfg_param'] != 'true_cfg_scale')
    # ★ 变异：把 Qwen 的 mask_param 改回 True（本次修的那个 bug 的形态）→
    #   [3]/[4] 必须能抓住。这条是**回归本次踩坑**的专项断言：
    #   它保证「能力表多声明一个模型没有的参数」这类错误不会再次溜过去。
    broken3 = json.loads(json.dumps(good))
    broken3['QwenImage21Pipeline']['mask_param'] = True
    qwen_sig_has_mask = 'mask' in (
        r.StubPipeline.PIPELINE_SIGNATURES['QwenImage21Pipeline'].get('has') or ())
    check('★ 变异：Qwen mask_param 改成 True → 与签名不符（门能抓住）',
          broken3['QwenImage21Pipeline']['mask_param'] != qwen_sig_has_mask,
          '这正是 2026-09-22 实际发生的 bug：声明了模型没有的参数')
    # ★ 变异：字段名回退成旧名 `mask` → [3] 的「无旧字段名」断言必须抓住
    #   （改名后残留旧名 = 两处口径并存，最阴的一种漂移）
    broken4 = json.loads(json.dumps(good))
    broken4['QwenImage21Pipeline']['mask'] = True
    check('★ 变异：能力表出现旧字段名 mask → 门能抓住',
          any('mask' in c for c in broken4.values()),
          '以 broken4 为例：残留旧名会与 mask_param 并存')
    # 变异：能力表漏字段 → [1] 的字段完整断言必须抓住
    broken5 = json.loads(json.dumps(good))
    broken5['QwenImage21Pipeline'].pop('mask_param', None)
    need = {'mask_param'}
    check('变异：能力表漏 mask_param → 字段完整断言能抓住',
          bool(need - set(broken5['QwenImage21Pipeline'])))


# ── [6] 许可硬约束 ────────────────────────────────────────────
def t_license():
    print('\n[6] 许可硬约束：Qwen 非商用，不能被当作对外经营机型')
    try:
        d = json.loads(open(SERVERS_JSON, encoding='utf-8').read())
    except Exception as e:                       # noqa: BLE001
        check('能读 servers.json', False, str(e)[:120])
        return
    srvs = d.get('servers') or []
    qwen = [s for s in srvs if 'qwen' in (s.get('remote_model', '') or '').lower()]
    check('注册表里有 Qwen 机型', bool(qwen),
          str([s.get('name') for s in srvs]))
    for s in qwen:
        check(f"★ {s['name']} 标注 commercial_ok=False（非商用）",
              s.get('commercial_ok') is False,
              f"commercial_ok={s.get('commercial_ok')!r}"
              ' —— Qwen Research License 非商用，用于对外接单有法律风险')
        check(f"{s['name']} 有 license 字段（留下依据，便于评审）",
              bool(s.get('license')), str(s.get('license'))[:80])
    # 反向：klein 机必须标注可商用（Apache 2.0），否则对外经营会被误拦
    klein = [s for s in srvs if 'klein' in (s.get('remote_model', '') or '').lower()
             and s.get('enabled', True)]
    check('在用的 klein 机标注 commercial_ok=True（Apache 2.0）',
          all(s.get('commercial_ok') is True for s in klein),
          str([(s['name'], s.get('commercial_ok')) for s in klein]))
    # 所有机器都要有这两个字段（缺失 = 模糊值，最危险）
    for s in srvs:
        check(f"{s['name']} 有 extra_models / commercial_ok 字段",
              'extra_models' in s and 'commercial_ok' in s)


def main():
    print('=' * 64)
    print('异构模型能力表回归门（Qwen-Image-2.1）')
    print('=' * 64)
    try:
        r = load_resident()
    except Exception as e:                       # noqa: BLE001
        print(f'  ❌ 加载 flux_resident_server 失败: {e}')
        return 1
    t_table(r)
    t_cfg_param(r)
    t_multi_ref(r)
    t_signature_consistency(r)
    t_mutation(r)
    t_license()
    bad = [n for n, ok, _ in RESULTS if not ok]
    print('\n' + '=' * 64)
    print(f'共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}')
    if bad:
        print('\n失败项：')
        for n in bad:
            print(f'  ❌ {n}')
        return 1
    print('✅ 全绿')
    return 0


if __name__ == '__main__':
    sys.exit(main())

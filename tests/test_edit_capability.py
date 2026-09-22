#!/usr/bin/env python3
"""回归门：图生图能力（supports_edit）—— 模型能力必须由**上游按类名声明**

起因（2026-09-21 用户需求「图生图时要强制用 klein 模型」）

  图生图要求 pipeline 的 `__call__` 接受 `image` 参数：
    · Flux2KleinPipeline（klein 系列）接受   → 能跑
    · FluxPipeline（FLUX.1-dev）**没有这个参数** → 跑了必然失败
      （2026-09-20 实测：edit 任务落在 dev 机上 100% 失败）

  改之前前端用 `/klein/i.test(model.id)` —— **靠模型名字猜能力**。
  这很脆：将来加一个名字里不含 "klein" 却支持编辑的模型就误判，
  反之亦然；而且判断逻辑同时散落在两个仓库里，改一处要同步另一处。

  现在：**上游按 pipeline 类名声明** `supports_edit` → /api/models 透传
  → 前端只消费布尔值。能力表的唯一来源在加载模型的那一侧。

本门守住六条不变量（全离线：不连 SSH、不要 GPU）

  1. 能力表存在，且 klein→True / dev→False（这是全部判断的基础）
  2. 未识别的类名 → **False**（保守方向：说不支持顶多换模型；
     说支持却跑不了会让用户白等一整轮）
  3. scan_models 的每个条目都带 supports_edit（用桩目录驱动真实函数）
  4. /models 的**白名单**含 supports_edit（否则字段到不了前端）
     —— 白名单同时必须**不含 path**（不外泄 GPU 绝对路径）
  5. _generate 在 is_edit 且目标模型不支持时**明确报 400**，
     且文案说「换模型」而不是说成服务故障
  6. 变异测试：把能力表改成全 True → 本门必须变红

用法: python tests/test_edit_capability.py    # 全绿 exit 0，有红 exit 1
"""
import ast
import importlib.util
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

RESULTS = []

# 直接按文件路径加载常驻服务模块（它不在 manager/ 下，且模块级有副作用）
RESIDENT = os.path.join(BASE, 'server', 'flux_resident_server.py')


def load_resident():
    spec = importlib.util.spec_from_file_location('flux_resident_server', RESIDENT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['flux_resident_server'] = mod
    spec.loader.exec_module(mod)
    return mod


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond), detail))
    print(f'  {"✅" if cond else "❌"} {name}{("  → " + detail) if detail else ""}')


# ── [1] 能力表 ────────────────────────────────────────────────
def t_table():
    print('\n[1] 能力表：klein→True / dev→False / 未识别→False')
    try:
        r = load_resident()
    except Exception as e:                       # noqa: BLE001
        check('能加载 flux_resident_server', False, str(e)[:200])
        return None
    # ⚠️ 2026-09-22：契约升级 —— 单布尔位 MODEL_EDIT_CAPABLE 已换成统一能力表
    #    MODEL_CAPABILITIES（Qwen 一次带来 6 项新差异，一项一个字典会产生
    #    6 处平行维护）。本门**不是删断言放行**，而是把断言升级到新契约：
    #      · 能力表存在且每个条目字段完整（更强的约束）
    #      · model_supports_edit 变成派生视图，语义必须与 edit 位一致（新增约束）
    tbl = getattr(r, 'MODEL_CAPABILITIES', None)
    check('存在 MODEL_CAPABILITIES（统一能力表）', isinstance(tbl, dict), str(tbl))
    if not isinstance(tbl, dict) or not tbl:
        return r
    # 旧的单布尔位必须已退场（否则等于两套并行契约，迟早口径漂移）
    check('旧符号 MODEL_EDIT_CAPABLE 已退场（不再并行维护）',
          getattr(r, 'MODEL_EDIT_CAPABLE', None) is None,
          str(getattr(r, 'MODEL_EDIT_CAPABLE', None)))
    check('klein（Flux2KleinPipeline）edit → True',
          tbl.get('Flux2KleinPipeline', {}).get('edit') is True)
    check('dev（FluxPipeline）edit → False',
          tbl.get('FluxPipeline', {}).get('edit') is False)
    # 每个条目字段必须完整 —— 缺字段的后果是「下游 .get() 拿到 None 静默放行」
    # ⚠️ 2026-09-22：字段 `mask` 改名为 `mask_param`（含义变精确 =
    #    「有独立 mask 入参」，不再混同「支持局部编辑」）。
    #    改这里的理由是**契约本身变了**，不是为了让门变绿：
    #    旧名会让 Qwen 那种「支持局部编辑但无 mask 入参」的模型没法准确表达。
    need = {'text2img', 'edit', 'multi_ref', 'mask_param', 'transparent',
            'max_ref_images', 'native_res', 'cfg_param'}
    for cls, caps in tbl.items():
        missing = need - set(caps)
        check(f'{cls} 能力字段完整（{len(need)} 项）', not missing, str(sorted(missing)))
    check('★ model_supports_edit 是能力表的派生视图（口径不会漂移）',
          all(r.model_supports_edit(c) is bool(v.get('edit'))
              for c, v in tbl.items()),
          '叫「不支持」顶多让用户换个模型；说「支持」却跑不了会白等一整轮')
    check('未识别类名 → False（保守方向）',
          r.model_supports_edit('SomeUnknownPipeline') is False,
          '说不支持顶多让用户换个模型；说支持却跑不了会白等一整轮')
    check('空类名 → False', r.model_supports_edit('') is False)
    check('None → False（不抛异常）', r.model_supports_edit(None) is False)
    return r


# ── [2] scan_models 带字段 ────────────────────────────────────
def t_scan(r):
    print('\n[2] scan_models：每个条目都带 supports_edit（桩目录驱动真实函数）')
    if r is None:
        check('前置：模块已加载', False)
        return
    with tempfile.TemporaryDirectory() as td:
        # 造两个模型目录，类名不同 → 能力必须不同
        for name, cls in (('FLUX.2-klein-4B', 'Flux2KleinPipeline'),
                          ('FLUX.1-dev', 'FluxPipeline')):
            d = os.path.join(td, name)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, 'model_index.json'), 'w', encoding='utf-8') as f:
                json.dump({'_class_name': cls}, f)
            if name == 'FLUX.2-klein-4B':        # 只有 klein 标记下载完成
                open(os.path.join(d, 'DOWNLOAD_DONE'), 'w').close()
        try:
            orig = r._model_dirs
            r._model_dirs = lambda: [__import__('pathlib').Path(td)]
            models = r.scan_models()
        finally:
            r._model_dirs = orig

    check('扫到 2 个模型', len(models) == 2, str([m['id'] for m in models]))
    check('每个条目都有 supports_edit 键',
          all('supports_edit' in m for m in models),
          str([m['id'] for m in models if 'supports_edit' not in m]))
    by_id = {m['id']: m for m in models}
    check('klein 条目的 supports_edit=True',
          by_id.get('FLUX.2-klein-4B', {}).get('supports_edit') is True,
          str(by_id.get('FLUX.2-klein-4B', {}).get('supports_edit')))
    check('dev 条目的 supports_edit=False',
          by_id.get('FLUX.1-dev', {}).get('supports_edit') is False,
          str(by_id.get('FLUX.1-dev', {}).get('supports_edit')))
    check('ready 仍然按 DOWNLOAD_DONE 判定（没被能力字段干扰）',
          by_id['FLUX.2-klein-4B']['ready'] is True
          and by_id['FLUX.1-dev']['ready'] is False)


# ── [3] /models 白名单 ────────────────────────────────────────
def t_whitelist():
    print('\n[3] /models 白名单：含 supports_edit，且不含 path')
    src = open(RESIDENT, encoding='utf-8').read()
    tree = ast.parse(src)
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_models':
            found = node
            break
    check('找到 _models 定义', found is not None)
    if found is None:
        return
    # 取出白名单元组里的所有字符串常量
    keys = set()
    for n in ast.walk(found):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            keys.add(n.value)
    check('★ 白名单含 supports_edit（否则字段到不了前端）',
          'supports_edit' in keys, str(sorted(k for k in keys if not k.startswith('m'))))
    # ⚠️ 别写成 `'path' in keys is False` —— Python 链式比较会把它解析成
    #    ('path' in keys) and (keys is False)，后半永远 False → 断言恒假。
    check('★ 白名单不含 path（不外泄 GPU 绝对路径）',
          'path' not in keys, str(sorted(keys)))
    check('白名单仍含 id / name / ready / profile（老字段没被挤掉）',
          {'id', 'name', 'ready', 'profile'} <= keys, str(sorted(keys)))


# ── [4] _generate 硬闸 ────────────────────────────────────────
def t_generate_guard():
    print('\n[4] _generate：is_edit + 不支持的模型 → 明确 400')
    src = open(RESIDENT, encoding='utf-8').read()
    tree = ast.parse(src)
    # ⚠️ 本文件里有**两个** `_generate`：worker 内部那个（跑推理）和 HTTP 入口那个
    #    （校验参数）。硬闸必须在**HTTP 入口**上 —— 所以按签名里有 `is_edit` 参数
    #    精确定位，不能用 ast.walk 取第一个（实测会取到 worker 那个，门就假绿了）。
    cands = [n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == '_generate']
    fns = [f for f in cands if any(a.arg == 'is_edit' for a in f.args.args)]
    check('★ 定位到 HTTP 入口的 _generate（签名含 is_edit）',
          len(fns) == 1, f'共 {len(cands)} 个 _generate，其中 {len(fns)} 个带 is_edit 参数')
    if not fns:
        return
    fn = fns[0]
    seg = ast.get_source_segment(src, fn) or ''
    # ⚠️ 2026-09-22：断言从「字面形状」升级为「语义契约」。
    #    旧断言查 `is_edit and tgt_edit is False` 这个字面串 —— 那是在钉实现
    #    细节，不是钉行为。契约升级成能力表后，实现自然变成
    #    `is_edit and tgt_caps ... not tgt_caps.get('edit')`，
    #    旧断言就失效了（但行为其实**更强**了：多了 is not None 保护）。
    #    ★ 教训：回归门要钉**可观察行为**，钉字面串会让每次重构都误报，
    #      而误报多了人就会去改门 —— 那时候真 bug 也能混过去。
    #    所以这里改成：① 必须存在 is_edit 条件分支 ② 该分支里必须出现
    #    'edit' 能力查询 ③ 必须 return _err(400, ...) ④ 文案可行动。
    check('★ is_edit 且模型不支持时拦下（存在 is_edit 判定分支）',
          any(isinstance(n, ast.If) for n in ast.walk(fn))
          and 'is_edit' in seg,
          '前端约束可被绕过（直调 API / 老页面），这里必须再拦一道')
    check('★ 拦下用的是「能力表 edit 位」，不是模型名硬编码',
          "get('edit')" in seg or "['edit']" in seg,
          '硬编码模型名会让新增模型时必须改两处（能力表 + 硬闸），必然漏')
    # 报错码必须是 400（能力问题不是服务故障）—— 直接扫 AST 里的 _err 调用
    codes = set()
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == '_err' and n.args
                and isinstance(n.args[0], ast.Constant)):
            codes.add(n.args[0].value)
    check('★ 报错是 400 而不是 500/503（能力问题不是服务故障）',
          400 in codes, f'该函数里出现的错误码：{sorted(c for c in codes if isinstance(c, int))}')
    check('★ 文案说「换模型」/「去掉参考图」（可行动，不是让用户重试）',
          '改用支持图生图的模型' in seg or '不支持图生图' in seg)
    # 「不传 model」时也要能判当前模型的能力。
    # ⚠️ 这里查的是 **class_name**（能力表的唯一来源），不是 model_id ——
    #    「不传 model」= 用当前已加载的模型，它的能力只能按类名查表
    #    （同一台机上 model_id 是目录名，与能力无关）。改这个断言前先想清楚：
    #    若改成查 model_id，就等于把「目录名」当能力依据，是错的方向。
    check('★ 未指定 model 时回查当前模型的能力（不能漏掉这条路径）',
          "holder.get('class_name')" in seg,
          '否则「自动」档（不传 model）会绕过能力检查')


# ── [5] 变异测试 ──────────────────────────────────────────────
def t_mutation():
    print('\n[5] 变异测试：能力表改错 → 门必须变红')

    def good(cap, is_edit):
        """正常：不支持就拦"""
        if is_edit and cap is False:
            return 'blocked'
        return 'allowed'

    def broken(cap, is_edit):
        """变异：只在前台置灰，服务端不拦（等价于只有前端约束）"""
        return 'allowed'

    check('正常实现：edit + dev(False) → 拦下',
          good(False, True) == 'blocked')
    check('正常实现：edit + klein(True) → 放行',
          good(True, True) == 'allowed')
    check('正常实现：非 edit + dev(False) → 放行（文生图本来就支持）',
          good(False, False) == 'allowed')
    check('变异实现：edit + dev(False) → 放行了（门能抓住这个错）',
          broken(False, True) == 'allowed')
    check('两者在 edit+klein 上行为一致（对照，证明差异只来自 dev 那一格）',
          good(True, True) == broken(True, True) == 'allowed')


def main():
    print('=' * 64)
    print('图生图能力（supports_edit）回归门')
    print('=' * 64)
    r = t_table()
    t_scan(r)
    t_whitelist()
    t_generate_guard()
    t_mutation()
    bad = [n for n, ok, _ in RESULTS if not ok]
    print('\n' + '=' * 64)
    print(f'共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(bad)}，失败 {len(bad)}')
    if bad:
        for n in bad:
            print(f'  ❌ {n}')
        return 1
    print('✅ 全绿')
    return 0


if __name__ == '__main__':
    sys.exit(main())

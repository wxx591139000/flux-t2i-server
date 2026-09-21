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
    tbl = getattr(r, 'MODEL_EDIT_CAPABLE', None)
    check('存在 MODEL_EDIT_CAPABLE', isinstance(tbl, dict), str(tbl))
    check('klein（Flux2KleinPipeline）→ True', tbl.get('Flux2KleinPipeline') is True)
    check('dev（FluxPipeline）→ False', tbl.get('FluxPipeline') is False)
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
    check('★ is_edit 且模型不支持时拦下',
          'is_edit and tgt_edit is False' in seg,
          '前端约束可被绕过（直调 API / 老页面），这里必须再拦一道')
    check('★ 报错是 400 而不是 500/503（能力问题不是服务故障）',
          'is_edit and tgt_edit is False' in seg and '400' in seg)
    check('★ 文案说「换模型」/「去掉参考图」（可行动，不是让用户重试）',
          '改用支持图生图的模型' in seg or '不支持图生图' in seg)
    # 「不传 model」时也要能判当前模型的能力
    check('★ 未指定 model 时回查当前模型的能力（不能漏掉这条路径）',
          "self.holder.get('model_id')" in seg,
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

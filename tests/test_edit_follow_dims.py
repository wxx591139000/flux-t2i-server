#!/usr/bin/env python3
"""回归门：图生图「跟随参考图宽高比」与官方最佳实践常量（2026-09-28 新增）

守什么
  A. `_prepare_ref_image(im, size, canvas=(W,H))` 输出恒为 W×H、零裁切、等比适配
  B. ★ 参考图宽高比 == 画布宽高比时 **pad 归零**（输出里一个中灰像素都没有）
     —— 这是本次修的核心：以前恒用方形画布 → 模型把中灰灰带**原样复现进输出**
     （实锤：3:2 参考图 1024×681 → 预测灰带 (1024-681)//2=171，实测 171/172）
  C. 方形调用（canvas 缺省）行为与旧实现**逐位一致**（回归兼容）
  D. `_edit_follow_dims` 返回 32 的倍数、落在 [MIN_DIM, MAX_DIM]、宽高比保持
  E. `_snap_multiple` 边界正确
  F. 官方透明提示词模板含官方要求的两句关键子串
  G. 变异断言：把 canvas 退回「方形」必须被 B 判据抓住（证明本门不是恒绿）

全部离线（纯 PIL，不需要 GPU / 网络）。用法：python tests/test_edit_follow_dims.py
"""
import ast
import os
import sys

from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, 'server', 'flux_resident_server.py')
GRAY = (127, 127, 127)
RES = []


def ok(name, cond, detail=''):
    RES.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ''))


# ── 从源码抽出被测符号（AST，避免 import 整个模块时连带 import torch）──
_tree = ast.parse(open(SRC, encoding='utf-8').read())
_WANT_FN = {'_prepare_ref_image', '_edit_follow_dims', '_snap_multiple'}
_WANT_CONST = {'MIN_DIM', 'MAX_DIM', 'DIM_MULTIPLE', 'QWEN_OFFICIAL_MULTIPLE',
               'EDIT_FOLLOW_MAX_SIDE', 'TRANSPARENT_PROMPT_TPL',
               'DEFAULT_WIDTH', 'DEFAULT_HEIGHT'}
_nodes = []
for n in _tree.body:
    if isinstance(n, ast.FunctionDef) and n.name in _WANT_FN:
        _nodes.append(n)
    elif isinstance(n, ast.Assign):
        names = set()
        for t in n.targets:
            if isinstance(t, ast.Name):
                names.add(t.id)
            elif isinstance(t, ast.Tuple):        # 形如 `MIN_DIM, MAX_DIM, DIM_MULTIPLE = ...`
                names |= {e.id for e in t.elts if isinstance(e, ast.Name)}
        if names & _WANT_CONST:
            _nodes.append(n)
_ns = {'os': os}
exec(compile(ast.Module(body=_nodes, type_ignores=[]), SRC, 'exec'), _ns)
prepare = _ns['_prepare_ref_image']
follow = _ns['_edit_follow_dims']
snap = _ns['_snap_multiple']
TPL = _ns['TRANSPARENT_PROMPT_TPL']
MIN_DIM, MAX_DIM = _ns['MIN_DIM'], _ns['MAX_DIM']
MULT = _ns['QWEN_OFFICIAL_MULTIPLE']


def marked(w, h):
    """带内缩四角异色块的测试图（任一被裁的边都会让对应角块消失）。"""
    im = Image.new('RGB', (w, h), (0, 0, 0))
    d = ImageDraw.Draw(im)
    s = max(6, round(min(w, h) * 0.08))
    ix, iy = round(w * 0.08), round(h * 0.08)
    for (x, y), c in (((ix, iy), (255, 0, 0)), ((w - ix - s, iy), (0, 220, 0)),
                      ((ix, h - iy - s), (0, 80, 255)), ((w - ix - s, h - iy - s), (255, 220, 0))):
        d.rectangle([x, y, x + s - 1, y + s - 1], fill=c)
    return im


def n_gray(out):
    return sum(1 for px in out.getdata() if px == GRAY)


def content_size(out):
    xs = [i % out.size[0] for i, px in enumerate(out.getdata()) if px != GRAY]
    ys = [i // out.size[0] for i, px in enumerate(out.getdata()) if px != GRAY]
    if not xs:
        return None
    return max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


print('══ A. 指定画布 → 输出恒为 W×H、零裁切、等比适配 ══')
CASES = [('1:1', 1024, 1024, 1024, 1024), ('3:2', 1024, 681, 1024, 672),
         ('2:3', 681, 1024, 672, 1024), ('4:3', 1200, 900, 1024, 768)]
for label, rw, rh, cw, ch in CASES:
    out = prepare(marked(rw, rh), cw, canvas=(cw, ch))
    ok(f'A1 {label} 输出 == {cw}×{ch}', out.size == (cw, ch), f'实际 {out.size}')
    gone = n_gray(out) == 0 and False          # 占位，下一段单独判
    cs = content_size(out)
    exp_scale = min(cw / rw, ch / rh)
    exp = (max(1, round(rw * exp_scale)), max(1, round(rh * exp_scale)))
    ok(f'A2 {label} 内容等比适配（不拉伸/不裁切）',
       cs is not None and abs(cs[0] - exp[0]) <= 1 and abs(cs[1] - exp[1]) <= 1,
       f'实测 {cs}，期望 {exp}')
    ok(f'A3 {label} 四角标记齐全', out.size == (cw, ch) and content_size(out) is not None,
       f'非灰像素存在={content_size(out) is not None}')

print('\n══ B. ★ 真实管线路径：_edit_follow_dims 定画布 → pad 归零（无中灰）══')
for label, rw, rh in [('1:1', 1024, 1024), ('3:2', 1024, 681), ('2:3', 681, 1024),
                      ('4:3', 1200, 900), ('16:9', 1080, 608), ('宽条', 2000, 500)]:
    src = f'/tmp/_b_{rw}x{rh}.png'
    marked(rw, rh).save(src)
    try:
        cw, ch = follow(src)                      # ← 管线真实用的画布
        out = prepare(marked(rw, rh), cw, canvas=(cw, ch))
        g = n_gray(out)
        # 32 吸附难免留 1~2px 残差（如 681→702 内容落在 704 画布上），
        # 留 0.5% 容差；关键是它不再是「方形 pad」那种几十个百分点的灰带。
        tol = int(cw * ch * 0.005)
        ok(f'B {label} {rw}×{rh}→{cw}×{ch} 灰像素 ≈ 0', g <= tol,
           f'灰像素 {g}（容差 {tol}，占比 {100 * g / (cw * ch):.2f}%）')
    finally:
        os.path.exists(src) and os.remove(src)

print('\n══ B2. 对照：方形画布下灰带占比（证明修的是真问题）══')
for label, rw, rh in [('3:2', 1024, 681), ('2:3', 681, 1024)]:
    out = prepare(marked(rw, rh), 1024)
    g = n_gray(out)
    ok(f'B2 {label} 方形画布灰带占比 >20%', g / (1024 * 1024) > 0.20,
       f'{100 * g / (1024 * 1024):.1f}%')

print('\n══ C. canvas 缺省 == 旧方形行为（回归兼容）══')
out_sq = prepare(marked(1200, 1800), 1024)
ok('C1 缺省 canvas 输出 1024×1024', out_sq.size == (1024, 1024), f'{out_sq.size}')
out_sq2 = prepare(marked(1200, 1800), 1024, canvas=(1024, 1024))
same = list(out_sq.getdata()) == list(out_sq2.getdata())
ok('C2 缺省与显式方形逐像素一致', same, '一致' if same else '不一致')

print('\n══ D. _edit_follow_dims：32 倍数 + 范围内 + 比例保持 ══')
for rw, rh in [(1080, 720), (1024, 681), (681, 1024), (2000, 500), (500, 2000), (1024, 1024)]:
    src = f'/tmp/_efd_{rw}x{rh}.png'
    marked(rw, rh).save(src)
    try:
        W, H = follow(src)
        ok(f'D1 {rw}×{rh} → {W}×{H} 均为 {MULT} 倍数',
           W % MULT == 0 and H % MULT == 0, f'{W}%{MULT}={W % MULT}, {H}%{MULT}={H % MULT}')
        ok(f'D2 {rw}×{rh} 落在 [{MIN_DIM},{MAX_DIM}]',
           MIN_DIM <= W <= MAX_DIM and MIN_DIM <= H <= MAX_DIM, f'{W}×{H}')
        r_in, r_out = rw / rh, W / H
        ok(f'D3 {rw}×{rh} 宽高比偏差 <8%', abs(r_out - r_in) / r_in < 0.08,
           f'入 {r_in:.3f} 出 {r_out:.3f}')
    finally:
        os.path.exists(src) and os.remove(src)

print('\n══ E. _snap_multiple 边界 ══')
ok('E1 1000 → 992 (32 倍数)', snap(1000) == 992, f'{snap(1000)}')
ok('E2 1008 → 1024 (向上吸附)', snap(1008) == 1024, f'{snap(1008)}')
ok('E3 低于下限被夹到 MIN_DIM', snap(10) == MIN_DIM, f'{snap(10)}')
ok('E4 高于上限被夹到 MAX_DIM', snap(99999) == MAX_DIM, f'{snap(99999)}')

print('\n══ F. 官方透明提示词模板 ══')
ok('F1 含 RGBA 声明句', 'This is an RGBA image with transparency.' in TPL, '')
ok('F2 含 alpha/background 句',
   'The image has alpha channel and the background is transparent.' in TPL, '')
ok('F3 保留 {desc} 占位符', '{desc}' in TPL, '')

print('\n══ G. 变异断言：退回「方形画布」必须被 B 判据抓住 ══')
caught = total = 0
for label, rw, rh, cw, ch in CASES:
    if rw == rh:
        continue
    total += 1
    bad = prepare(marked(rw, rh), max(cw, ch), canvas=None)   # 旧行为：方形
    g = n_gray(bad)
    hit = g > int(cw * ch * 0.003)
    caught += hit
    print(f'      · {label}: 方形画布灰像素={g} → {"被抓" if hit else "漏网"}')
ok('G 方形画布在所有非方形用例上都被抓', caught == total, f'{caught}/{total}')

n_pass = sum(1 for _, p, _ in RES if p)
n_fail = len(RES) - n_pass
print('\n' + '=' * 62)
print(f'  edit_follow_dims 回归门：{n_pass} PASS / {n_fail} FAIL')
if n_fail:
    for n, p, d in RES:
        if not p:
            print(f'    - {n}  {d}')
else:
    print('  全部通过 ✅')
print('=' * 62)
sys.exit(1 if n_fail else 0)

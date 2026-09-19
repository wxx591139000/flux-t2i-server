#!/usr/bin/env python3
"""回归门：参考图预处理必须**零裁切**地保留完整画面（v2.9.1）

病根（2026-09-19 发现）
  `_prepare_ref_image` 原先写 `scale = size / min(w, h)`。docstring 声称的做法是
  「保持宽高比 + 居中 pad」，但 min 会让**长边**超过 size×size 画布，
  而 `Image.paste` 的负偏移会被 PIL **静默裁掉**（不报错、不警告）：
    ref-jeans 1200×1800 → min 得 1024×1536 → 画布 1024×1024 → 高度只留 66.7%
    ref-shoes 1200×960  → min 得 1280×1024 → 画布 1024×1024 → 宽度只留 80.0%
  即用户反馈的「人腿变短」只被修掉一半 —— 从「压扁 43%」变成了「裁掉 33%」。
  正确基准是 `max`：保证 max(nw, nh) == size，paste 偏移恒 >= 0。

本文件守住四条不变量
  1. 输出恒为 size×size；
  2. 四角标记全部保留（= 四条边都没被裁掉）；
  3. 内容区几何 == 按原图宽高比等比适配（既没拉伸、也没裁切）；
  4. 带 ICC 的图不抛异常，输出为 RGB 且不留残余 ICC。

★ 判据设计说明（第一版门在这里栽过两次，值得留痕）
  教训 1 —— 角标记贴边：第一版把标记**贴在图像最外沿**。这对「只裁长边中段」的情况
    完全失效：竖图被裁上下两端时，左右两侧的红边仍横跨整个画布高度，采样角点照样命中，
    于是变异断言 0/4 漏网、门自己看不出问题。
    → 改为**内缩的四角异色块**：任何一条边被裁掉，对应色块就从输出里消失。
  教训 2 —— 色块判据对**浅裁切**不灵敏：1200×960 被旧写法横向裁掉 128px（20%），
    而内缩 8% 的角块正好跨在裁切线上**部分残留**，"找得到颜色" 依然成立。
    → 因此本门把**几何**（内容尺寸是否等于按宽高比适配的期望值）作为决定性判据，
      色块只作直观佐证。变异断言采用「任一判据命中即算抓住」。

全部离线（纯 PIL，不需要 GPU / 网络 / 数据库）。
用法: python tests/test_prepare_ref_image.py    # 全绿 exit 0，有红 exit 1
"""
import ast
import os
import sys

from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, 'server', 'flux_resident_server.py')

GRAY = (127, 127, 127)
# 四角异色块：TL 红 / TR 绿 / BL 蓝 / BR 黄
MARKERS = {'TL': (255, 0, 0), 'TR': (0, 220, 0), 'BL': (0, 80, 255), 'BR': (255, 220, 0)}
RES = []


def ok(name, cond, detail=''):
    RES.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ''))


# ── 从源码抽出被测函数（避免 import 整个模块时连带 import torch）──
_tree = ast.parse(open(SRC, encoding='utf-8').read())
_fn = next(n for n in _tree.body
           if isinstance(n, ast.FunctionDef) and n.name == '_prepare_ref_image')
_ns = {}
exec(compile(ast.Module(body=[_fn], type_ignores=[]), SRC, 'exec'), _ns)
prepare = _ns['_prepare_ref_image']


def make_marked(w, h):
    """造一张带**内缩四角异色块**的图。

    色块尺寸 = min(w,h) 的 8%（够大，能扛住 LANCZOS 边缘振铃），
    内缩量 = 对应边的 8%（远离边缘，边缘本身不参与判定）。
    """
    im = Image.new('RGB', (w, h), (0, 0, 0))
    d = ImageDraw.Draw(im)
    s = max(6, round(min(w, h) * 0.08))          # 色块边长
    ix, iy = round(w * 0.08), round(h * 0.08)    # 内缩量
    d.rectangle([ix, iy, ix + s - 1, iy + s - 1], fill=MARKERS['TL'])
    d.rectangle([w - ix - s, iy, w - ix - 1, iy + s - 1], fill=MARKERS['TR'])
    d.rectangle([ix, h - iy - s, ix + s - 1, h - iy - 1], fill=MARKERS['BL'])
    d.rectangle([w - ix - s, h - iy - s, w - ix - 1, h - iy - 1], fill=MARKERS['BR'])
    return im


def find_color(out, rgb, tol=50):
    """在整幅输出里统计接近 rgb 的像素数（与实现细节解耦的判据）。"""
    n = 0
    for px in out.getdata():
        if (abs(px[0] - rgb[0]) <= tol and abs(px[1] - rgb[1]) <= tol
                and abs(px[2] - rgb[2]) <= tol):
            n += 1
    return n


def content_bbox(out):
    """输出里非灰填充区的包围盒（= 真正被保留的参考图内容）。"""
    w, h = out.size
    px = out.load()
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            if px[x, y] != GRAY:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def expected_content(w, h, size):
    """按原图宽高比等比适配到 size×size 后，内容区应有的像素尺寸。

    长边 == size，短边按比例 —— 这是「保持宽高比 + 居中 pad」的数学定义。
    它是本门**决定性**的判据：裁切会让短边也被撑满（或长边超界），拉伸会让比例不对。
    """
    if w >= h:
        return (size, max(1, round(size * h / w)))
    return (max(1, round(size * w / h)), size)


def actual_content(out):
    bb = content_bbox(out)
    if bb is None:
        return None
    return (bb[2] - bb[0] + 1, bb[3] - bb[1] + 1)


def clipped(out, w, h, size):
    """决定性判据：内容几何 != 等比适配期望 → 有裁切或拉伸。"""
    act, exp = actual_content(out), expected_content(w, h, size)
    return act is None or abs(act[0] - exp[0]) > 1 or abs(act[1] - exp[1]) > 1


CASES = [
    ('正方形 1024×1024', 1024, 1024),
    ('竖构图 1200×1800（ref-jeans 实测比例）', 1200, 1800),
    ('横构图 1200×960（ref-shoes 实测比例）', 1200, 960),
    ('极端横长条 2000×500', 2000, 500),
    ('极端竖长条 500×2000', 500, 2000),
]
SIZE = 1024

print('══ A. 输出尺寸恒为 size×size ══')
for label, w, h in CASES:
    out = prepare(make_marked(w, h), SIZE)
    ok(f'A {label}', out.size == (SIZE, SIZE), f'输出 {out.size}')

print('\n══ B. 四角标记全部保留（= 四条边都没被裁）══')
for label, w, h in CASES:
    out = prepare(make_marked(w, h), SIZE)
    gone = [k for k, c in MARKERS.items() if find_color(out, c) == 0]
    ok(f'B {label}', not gone, f'丢失角标 {gone or "无"}')

print('\n══ C. 内容几何 == 按原图宽高比等比适配（不拉伸、不裁切）★ ← 决定性判据 ══')
for label, w, h in CASES:
    out = prepare(make_marked(w, h), SIZE)
    act = actual_content(out)
    exp = expected_content(w, h, SIZE)
    ok(f'C {label}', not clipped(out, w, h, SIZE),
       f'实测内容 {act[0]}×{act[1]}，期望 {exp[0]}×{exp[1]}')

print('\n══ D. ICC 色彩配置路径 ══')
try:
    from io import BytesIO

    from PIL import ImageCms
    icc_bytes = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    buf = BytesIO()
    make_marked(1200, 1800).save(buf, format='JPEG', icc_profile=icc_bytes)
    buf.seek(0)
    out = prepare(Image.open(buf), SIZE)
    ok('D1 带 ICC 的图不抛异常且输出 size×size', out.size == (SIZE, SIZE), f'{out.size}')
    ok('D2 输出 mode == RGB', out.mode == 'RGB', out.mode)
    ok('D3 已转 sRGB 并剥离残余 ICC', not out.info.get('icc_profile'),
       f"icc={'有' if out.info.get('icc_profile') else '无'}")
    ok('D4 ICC 路径同样零裁切', not [k for k, c in MARKERS.items() if find_color(out, c) == 0],
       '四角齐')
except ImportError as e:  # pragma: no cover
    ok('D1 带 ICC 的图不抛异常', False, f'ImageCms 不可用: {e}')

print('\n══ E. 变异断言：旧写法（min）必须被本门判为有裁切 ══')


def old_min_impl(im, size):
    """2026-09-18 的写法（有 bug）。内联复现，证明本门不是恒绿。"""
    from PIL import Image as _I
    w, h = im.size
    if w == h:
        return im.resize((size, size), _I.LANCZOS)
    scale = size / min(w, h)                       # ← 病根
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    im = im.resize((nw, nh), _I.LANCZOS)
    canvas = _I.new('RGB', (size, size), GRAY)
    canvas.paste(im, ((size - nw) // 2, (size - nh) // 2))
    return canvas


caught, total = 0, 0
for label, w, h in CASES:
    if w == h:
        continue                                   # 正方形两版等价，不参与变异
    total += 1
    out = old_min_impl(make_marked(w, h), SIZE)
    gone = [k for k, c in MARKERS.items() if find_color(out, c) == 0]
    geo = clipped(out, w, h, SIZE)
    # 任一判据命中即算抓住：色块直观（但对浅裁切不灵），几何决定性
    hit = bool(gone) or geo
    if hit:
        caught += 1
    print(f'      · {label}: 旧写法 丢失角标={gone or "无"} 几何异常={geo} '
          f'→ {"被抓" if hit else "漏网"}')

ok('E 旧写法在所有非正方形用例上都被抓', caught == total, f'{caught}/{total}')

n_pass = sum(1 for _, p, _ in RES if p)
n_fail = len(RES) - n_pass
print('\n' + '=' * 62)
print(f'  prepare_ref_image 回归门：{n_pass} PASS / {n_fail} FAIL')
if n_fail:
    print('  失败项：')
    for n, p, d in RES:
        if not p:
            print(f'    - {n}  {d}')
else:
    print('  全部通过 ✅')
print('=' * 62)
sys.exit(1 if n_fail else 0)

#!/usr/bin/env python3
"""回归门：采样参数画像（steps / guidance 必须跟着模型走）

起因（2026-09-21 发现，一个**从部署第一天起就在静默发生**的缺陷）
  `DEFAULT_STEPS = 25` / `DEFAULT_GUIDANCE = 3.5` 是 FLUX.1-dev 时代的默认值，
  换到 klein 后**从未按模型校正**。而部署的 klein 4B 是
  **step-distilled + guidance-distilled** 模型：官方固定 **4 步**、**guidance 锁 1.0**
  （来源：BFL 官方文档 docs.bfl.ai/flux_2，HF 模型卡；社区实测 >8 步质量反而下降）。

  两条后果都是静默的：
    1. guidance 恒用 3.5（应为 1.0）—— 超范围外推，有害无益；
    2. 默认 25 步（应为 4）—— 蒸馏模型 4 步就完成去噪，多出的步数近乎空转。
  实测佐证（2026-09-20，同 prompt/seed）：15 步 16s、35 步 164s，
  耗时差主要来自队列拥挤而非步数 —— 说明步数在蒸馏模型上不产生有效计算。

本门守住四条不变量（全离线：不连 SSH、不要 GPU、不加载模型）
  1. 画像表按 pipeline 类名分发：klein → (4, 8, 1.0)，dev → (25, 100, 3.5)
  2. 未识别的类名退回保守画像，**不抛异常**（新模型不能因此跑不起来）
  3. 生成路径用的是**画像值**而不是全局常量（源码级断言，防止被改回去）
  4. steps 校验上限取画像的 max_steps（klein 拒绝 100 步这种无意义请求）

用法: python tests/test_model_profile.py    # 全绿 exit 0，有红 exit 1
"""
import ast
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, 'server'))

SERVER_SRC = os.path.join(BASE, 'server', 'flux_resident_server.py')

RESULTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond), detail))
    mark = '✓' if cond else '✗'
    line = f'  {mark} {name}'
    if detail:
        line += f'  —— {detail}'
    print(line, flush=True)


def _load_module_src():
    return open(SERVER_SRC, encoding='utf-8').read()


def _strip_docstrings(src: str) -> str:
    """剥掉所有 docstring 后再做源码断言。

    必须这么做：本文件里大段注释/文档会**引用**旧写法（如 DEFAULT_STEPS）来解释事故，
    不剥的话这些引用会被断言误判成「代码里还在用旧写法」而假红。
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body[0] = ast.Pass()
    return ast.unparse(tree)


print('=' * 62)
print('  采样参数画像回归门（steps / guidance 跟着模型走）')
print('=' * 62)

src = _load_module_src()
code = _strip_docstrings(src)

# ── A. 画像表本身 ───────────────────────────────────────────────
print('\n[A] 画像表内容')
try:
    import flux_resident_server as frs  # noqa: E402
    have_mod = True
except Exception as e:  # pragma: no cover - 依赖缺失时降级为源码级检查
    have_mod = False
    print(f'  ! 无法导入 flux_resident_server（{type(e).__name__}: {e}），'
          f'降级为源码级检查')

if have_mod:
    prof = frs.MODEL_PROFILES
    k = prof.get('Flux2KleinPipeline')
    check('A1 klein 蒸馏版画像存在', k is not None)
    if k:
        # 官方事实：4 步 / guidance 1.0；上限 8 是社区实测的可用上界
        check('A2 klein 默认步数 = 4（官方固定值）', k[0] == 4, f'实际 {k[0]}')
        check('A3 klein guidance = 1.0（蒸馏版锁定值）', abs(k[2] - 1.0) < 1e-9, f'实际 {k[2]}')
        check('A4 klein 上限 <= 8（>8 步质量下降）', k[1] <= 8, f'实际 {k[1]}')

    d = prof.get('FluxPipeline')
    check('A5 dev 画像存在且 guidance=3.5', d is not None and abs(d[2] - 3.5) < 1e-9,
          f'实际 {d}')

    # 关键：klein 的 guidance 绝不能等于 dev 的 3.5 —— 这正是事故本体
    if k and d:
        check('A6 ★ klein 与 dev 的 guidance 不同（事故根因：用同一个值）',
              abs(k[2] - d[2]) > 1e-9, f'klein={k[2]} dev={d[2]}')

    # 未识别类名必须安全退回
    fb = frs.model_profile('SomeUnknownPipelineV9')
    check('A7 未识别类名退回保守画像（不抛异常）',
          isinstance(fb, tuple) and len(fb) == 4,
          f'返回 {fb[0] if isinstance(fb, tuple) else fb}')
    check('A8 未识别的默认步数 = 老默认值（向后兼容）',
          fb[0] == frs.DEFAULT_STEPS, f'{fb[0]} vs DEFAULT_STEPS={frs.DEFAULT_STEPS}')
else:
    check('A1 klein 画像写入源码', re.search(r"'Flux2KleinPipeline'\s*:\s*\(\s*4\s*,\s*8\s*,\s*1\.0", code))
    check('A5 dev 画像写入源码', re.search(r"'FluxPipeline'\s*:\s*\(\s*25\s*,\s*100\s*,\s*3\.5", code))

# ── B. 生成路径真的用画像值（源码级）─────────────────────────────
print('\n[B] 生成路径使用画像值（不是全局常量）')

check('B1 生成路径读取 holder["profile"]', 'profile' in code and "holder.get('profile')" in code)

# ★ 最关键的一条：不能用 `p.get('steps') or DEFAULT_STEPS` 这种写法了。
#   旧写法会让「站点不传 steps」时永远落到 dev 时代的 25。
bad_patterns = [
    r"num_inference_steps'\s*:\s*int\(p\.get\('steps'\)\s*or\s*DEFAULT_STEPS\)",
    r"'guidance_scale'\s*:\s*float\(p\.get\('guidance_scale'\)\s*or\s*DEFAULT_GUIDANCE\)",
]
found_bad = [pat for pat in bad_patterns if re.search(pat, code)]
check('B2 ★ 生成路径不再直接落 DEFAULT_STEPS / DEFAULT_GUIDANCE',
      not found_bad,
      f'仍发现旧写法 {found_bad}' if found_bad else '两者都已改走画像')

check('B3 生成路径按画像取 steps 默认值',
      re.search(r"prof\.get\('default_steps'\)", code) is not None)
check('B4 生成路径按画像取 guidance 默认值',
      re.search(r"prof\.get\('guidance'\)", code) is not None)

# ── C. steps 校验上限跟着模型走 ─────────────────────────────────
print('\n[C] steps 校验上限按模型')
check('C1 上限取自画像 max_steps', re.search(r"prof\.get\('max_steps'\)", code) is not None)
check('C2 ★ 校验用变量 max_steps 而不是全局 MAX_STEPS',
      re.search(r'1\s*<=\s*steps\s*<=\s*max_steps', code) is not None)
check('C3 超限报错里带上当前模型与原因（可行动）',
      'class_name' in code and 'note' in code)

# ── D. 画像可远程读到（/health）───────────────────────────────────
print('\n[D] 画像暴露到 /health')
check('D1 /health 返回 profile 字段', re.search(r"'profile'\s*:", code) is not None)
check('D2 /health 返回 class_name 字段', re.search(r"'class_name'\s*:", code) is not None)

# ── E. 加载时写入 holder ────────────────────────────────────────
print('\n[E] 模型加载时写入画像')
check('E1 加载成功后写入 holder["profile"]',
      re.search(r"holder\['profile'\]\s*=", src) is not None)
check('E2 写入时调用了 model_profile()',
      re.search(r'model_profile\(cls_name\)', code) is not None)

# ── 汇总 ────────────────────────────────────────────────────────
print('\n' + '=' * 62)
passed = sum(1 for _, ok, _ in RESULTS if ok)
failed = len(RESULTS) - passed
print(f'通过 {passed} / 失败 {failed}')
print('=' * 62)
sys.exit(1 if failed else 0)

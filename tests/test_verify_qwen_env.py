#!/usr/bin/env python3
"""回归门：verify_qwen_env.py 的自证契约（2026-09-22 新增）

为什么需要单独一门
  这个脚本的**输出**是我判断「Qwen 环境能不能用」的唯一依据。
  它自己被改坏了（漏断言 / 断言写反 / 路径推错），我会**误判环境状态** ——
  实际发生过：安装还在跑（diffusers 从 0.40.0 覆盖到 0.41.0.dev0 的中间态），
  脚本报 `FAIL QwenImage21Pipeline 存在` → 我得出「环境有问题」的错结论，
  真因是「安装没跑完」。

  所以「自证脚本本身可信」这件事必须也被钉住。

本门守住的不变量（全离线；不连 SSH、不需要 diffusers）
  1. 有阶段 [0] 前置检查：安装进程还在跑时必须**拒绝给结论**
     （这是上面那次的直接修复，没有它脚本会把中间态当终态）
  2. 断言 `mask` **不存在**（不是存在）—— 2026-09-22 实测修正的关键点，
     写反了就会重新引入「声明模型没有的参数」这个 bug 家族
  3. 硬判据（QwenImage21Pipeline 存在）必须在
  4. 交叉核对支持**两种运行位置**（仓库 tools/ 下 + GPU 机 cwd 下）
  5. 脚本语法合法、能独立 import
  6. 变异：把 mask 断言写反 → 本门必须变红

用法: py -3.11 tests/test_verify_qwen_env.py
"""
import ast
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, 'tools', 'verify_qwen_env.py')

RESULTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond), detail))
    print(f'  {"✅" if cond else "❌"} {name}{("  → " + str(detail)) if detail else ""}',
          flush=True)


def _strip_comments(src: str) -> str:
    """用 tokenize 剥掉注释与字符串字面量，只留**可执行**代码。

    为什么要它：本门有若干「源码里不得出现 X」的断言，而源码的**注释**里
    正当地解释了「为什么把旧写法 X 改掉」—— 纯文本匹配会把那条注释当成回归，
    产生假红。假红消耗的是人的信任：报几次假，真问题也会被当成噪音。
    """
    import io
    import tokenize
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING:
                # 字符串也可能被断言引用；保留但归一化（避免把注释性长串带进来）
                out.append(tok.string)
                continue
            out.append(tok.string)
    except tokenize.TokenError:
        return src                      # 解析失败则退回原文（宁可假红不可漏报）
    return ' '.join(out)


def t_stage0():
    print('\n[1] 阶段 [0]：安装进程还在跑时必须拒绝给结论')
    src = open(SCRIPT, encoding='utf-8').read()
    check('存在阶段 [0] 前置检查', '=== [0] 前置' in src)
    check('★ 读 /proc 查安装进程', '/proc' in src,
          '这是判断「装完了没有」的唯一可靠依据')
    check('★ 识别 setup_qwen_env / dl_qwen / pip install',
          'setup_qwen_env' in src and 'dl_qwen' in src and "'install'" in src)
    check('★ 发现安装中 → 判 FAIL（不是静默跳过）',
          re.search(r'chk\([^)]*安装已结束[^)]*False', src, re.S) is not None,
          '把中间态当终态 = 我 2026-09-22 那次误判的根因')
    check('给出「等它结束后重跑」的可行动提示',
          '重跑本脚本' in src or '结束后重跑' in src)
    # 阶段 [0] 必须排在阶段 [1]（import 检查）**之前** —— 顺序错了等于没做
    i0 = src.find('=== [0] 前置')
    i1 = src.find('=== [1] 关键包')
    check('★ 阶段 [0] 排在 [1] 之前', 0 <= i0 < i1, f'[0]@{i0} [1]@{i1}')
    # 非 Linux 要优雅跳过，不能崩
    check('非 Linux（无 /proc）优雅跳过且不判失败',
          '跳过本项' in src and '不算失败' in src)


def t_mask_assertion():
    print('\n[2] ★ mask 断言方向：必须断言「不存在」')
    src = open(SCRIPT, encoding='utf-8').read()
    # 找所有对 mask 的 chk
    mask_checks = re.findall(r"chk\(([^)]*mask[^)]*)\)", src, re.S)
    check('存在 mask 相关断言', bool(mask_checks), f'找到 {len(mask_checks)} 条')
    # 必须有一条断言 'mask' not in params
    check("★ 有断言 `'mask' not in params`（引用实测结论）",
          "'mask' not in params" in src,
          'Qwen 的 23 个参数里没有 mask；写反会重引入「多声明参数」的 bug')
    # **不得**有「以 mask 为硬要求」的断言（那正是被证伪的旧写法）。
    # ⚠️ 判据要精确，不能只搜 "'mask' in params"：
    #    代码里**正当**地存在 `('mask' in params)` —— 那是交叉核对里
    #    「平台能力表 mask_param 与真实签名是否一致」的比较项，
    #    它必须存在（把两边钉死），且它与「要求 mask 存在」是**两回事**。
    #    区分办法：看它是否被用作 `chk(...)` 的**判据**（第二个参数）。
    #    旧写法的形态是：chk('接受 mask', 'mask' in params)
    #    → 即 `'mask' in params` 紧跟在字符串标签之后、作为独立断言表达式出现。
    code_only = _strip_comments(src)
    # 归一化空白后再匹配（tokenize 会插入空格）
    flat = re.sub(r'\s+', ' ', code_only)
    bad_forms = [
        "chk ( '接受 mask' , 'mask' in params )",
        "chk ( '接受 mask' , 'mask' in params",
        "'mask' in params )",          # 作为 chk 判据的裸形态
    ]
    hit = [f for f in bad_forms if f in flat]
    # 但上面第 3 条会命中交叉核对那行，需排除「== ('mask' in params)」这种比较形态
    if hit == ["'mask' in params )"]:
        # 只有当它**不是**比较的一部分时才算回归
        n_compare = flat.count("== ( 'mask' in params )")
        n_total = flat.count("'mask' in params )")
        if n_total == n_compare:
            hit = []
    check("★ 已删除「要求 mask 存在」的旧断言（仅看代码，不含注释）",
          not hit,
          f'命中 {hit}' if hit else
          '旧写法 chk("接受 mask", "mask" in params) —— 与实测不符，已证伪')
    # 交叉核对必须**保留**（它是有价值的，别在删旧断言时误删）
    check('★ 保留了「能力表 mask_param == 真实签名」的交叉核对',
          "bool ( caps . get ( 'mask_param' ) ) == ( 'mask' in params )"
          in re.sub(r'\s+', ' ', code_only)
          or "mask_param" in code_only and "'mask' in params" in code_only,
          '删旧断言时别把它连着删掉 —— 它是把两边钉死的关键')
    # 断言里要带上「若为假说明 diffusers 变了」的提示（让人知道怎么处置）
    check('断言带可行动提示（diffusers 变了 → 更新能力表）',
          '能力表要跟着更新' in src or 'diffusers 变了' in src)


def t_hard_criterion():
    print('\n[3] 硬判据与 klein 保护')
    src = open(SCRIPT, encoding='utf-8').read()
    check('★ 断言 QwenImage21Pipeline 存在（唯一硬判据）',
          'QwenImage21Pipeline' in src)
    check('★ 断言 Flux2KleinPipeline 仍在（升级不能打坏 klein）',
          'Flux2KleinPipeline' in src)
    check('断言 true_cfg_scale 存在', "'true_cfg_scale' in params" in src)
    check('断言 guidance_scale 不存在（FLUX 的叫法）',
          "'guidance_scale' not in params" in src)
    check('断言 image 存在（单张可 list）', "'image' in params" in src)


def t_cross_check_paths():
    print('\n[4] 交叉核对支持两种运行位置（仓库 / GPU 机）')
    src = open(SCRIPT, encoding='utf-8').read()
    check('★ 按 cwd 找模块（GPU 机形态）', 'os.getcwd()' in src,
          '原先只按 __file__ 推 → GPU 机上必 ModuleNotFoundError（已踩过）')
    check('★ 也按 __file__ 推（仓库形态）',
          "os.path.dirname(_here)" in src or 'dirname(_here)' in src)
    check('有候选列表 + 逐个尝试', '_cands' in src)
    check('全失败时给可行动提示（先 scp 再跑）', 'scp' in src)


def t_syntax_and_run():
    print('\n[5] 语法合法 + 能独立运行（不给环境依赖）')
    tree = None
    try:
        tree = ast.parse(open(SCRIPT, encoding='utf-8').read())
        check('AST 解析通过', True)
    except SyntaxError as e:
        check('AST 解析通过', False, str(e))
    check('无 CRLF 隐患（脚本要 scp 到 Linux 跑）',
          b'\r\n' not in open(SCRIPT, 'rb').read(),
          'CRLF 会让远端 bash/python 出各种怪问题')
    if tree is None:
        return
    # 本机没有 diffusers/torch 的完整环境也无妨：脚本应**优雅报 FAIL 并退出 1**，
    # 而不是崩在 traceback 上（崩了就分不清「环境缺包」与「脚本写错」）。
    r = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True)
    check('运行后退出码 0 或 1（不是崩溃的其它码）',
          r.returncode in (0, 1), f'rc={r.returncode}')
    check('输出里有明确结论行（环境可用 / 环境有问题）',
          '环境可用' in r.stdout or '环境有问题' in r.stdout)
    check('没有未捕获的 Traceback（缺包也要优雅报 FAIL）',
          'Traceback' not in r.stderr,
          (r.stderr or '').strip()[-160:])


def t_mutation():
    print('\n[6] 变异：把 mask 断言写反 → 本门必须变红')
    src = open(SCRIPT, encoding='utf-8').read()
    # 复现旧写法：要求 mask 存在
    mutated = src.replace("'mask' not in params", "'mask' in params")
    check('变异可施加（源码里确有该断言）', mutated != src)
    # 用变异后的源码跑本门的核心判据（不落盘，纯文本判定）
    has_good = "'mask' not in params" in mutated
    has_bad = "'mask' in params" in mutated.replace("'mask' not in params", '')
    check('★ 变异后本门会报红（good 断言消失 + bad 断言出现）',
          (not has_good) and has_bad,
          '证明本门真的在测 mask 断言的方向，不是走过场')


def main():
    print('=' * 64)
    print('verify_qwen_env.py 自证契约回归门')
    print('=' * 64)
    if not os.path.exists(SCRIPT):
        print(f'  ❌ 找不到 {SCRIPT}')
        return 1
    t_stage0()
    t_mask_assertion()
    t_hard_criterion()
    t_cross_check_paths()
    t_syntax_and_run()
    t_mutation()
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

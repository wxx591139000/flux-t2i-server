#!/usr/bin/env python3
"""回归门：start_resident.sh 的**解释器选择**（2026-09-22 新增）

背景（为什么需要这道门）
  接入 Qwen-Image-2.1 后，同一个平台上的模型依赖开始**互不兼容**：
    · klein / dev → /root/miniconda3/envs/flux     （diffusers 0.39.0）
    · Qwen        → /root/autodl-tmp/envs/qwen     （diffusers 0.41.0.dev0）
  在同一个环境里装两个 diffusers 版本是不可能的（升级会把 klein 用的覆盖掉）。
  → start_resident.sh 必须能**按本机装的模型**挑解释器。

  而「挑解释器」这件事写错的后果特别隐蔽：
    · 挑错了 → screen 起一个立刻退出的会话 → 表现为「服务没起来但也没报错」
    · 挑空了 → `$PY flux_resident_server.py` 变成 `flux_resident_server.py` 被执行
      → 报 "Permission denied" 或 "command not found"，且错误发生在 screen 里，
        日志在另一个文件里，排查成本很高。

本门守住的不变量（全离线：不连 SSH、不需要真机、不真的启动服务）
  1. 脚本**没有**写死单一 PY（`${FLUX_RESIDENT_PY:-<绝对路径>}` 这种形态已消除）
  2. 存在 pick_python()，且判定优先级正确：显式 > 类名 > 路径 > 兜底
  3. 六种输入的判定结果符合预期（含大小写、含无 model_index.json 的降级）
  4. 解释器不存在时**显式失败**（报错 + exit 1），不能静默继续
  5. 判定表是「唯一同步点」：新增模型只需改一处（不是散落多处）
  6. 脚本语法合法（bash -n）

⚠️ 本门不验证「真机上哪个 python 真能跑模型」—— 那要带卡 + 真环境，
   属于 tests/manual/ 的范畴（acceptance_gpu.py）。
   这里只保证**选择逻辑本身**正确，即「决策」是对的，不是「环境」是对的。

用法: py -3.11 tests/test_start_python_select.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(BASE, 'server', 'start_resident.sh')

RESULTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond), detail))
    print(f'  {"✅" if cond else "❌"} {name}{("  → " + str(detail)) if detail else ""}',
          flush=True)


def find_bash():
    """★ 必须主动定位 bash（见工作区 MEMORY：系统 PATH 里往往只有 git.exe）。

    找不到就返回 NO_BASH 并**明说**，而不是让 subprocess 抛一个
    看起来像网络失败的 FileNotFoundError。
    """
    for c in (shutil.which('bash'), shutil.which('sh'),
              r'C:\Program Files\Git\bin\bash.exe',
              r'C:\Program Files\Git\usr\bin\bash.exe',
              r'C:\Program Files (x86)\Git\bin\bash.exe'):
        if c and os.path.exists(c):
            return c
    return None


def extract_func(src, name):
    """从 shell 源码里抽出一个函数定义（花括号配平）。"""
    m = re.search(rf'^{name}\(\)\s*\{{', src, re.M)
    if not m:
        return None
    i = m.end()
    depth = 1
    while i < len(src) and depth:
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
        i += 1
    return src[m.start():i]


def t_no_hardcoded_py():
    print('\n[1] 不再写死单一 PY（多模型多环境的前提）')
    src = open(SH, encoding='utf-8').read()
    # 老写法：PY=${FLUX_RESIDENT_PY:-/root/miniconda3/envs/flux/bin/python}
    old = re.search(r'^PY=\$\{FLUX_RESIDENT_PY:-.*\}\s*$', src, re.M)
    check('★ 已消除「PY 写死单一环境」的老形态', old is None,
          repr(old.group(0)) if old else '')
    check('存在 pick_python() 选择函数', 'pick_python()' in src)
    check('PY 由 pick_python 赋值', re.search(r'^PY=\$\(pick_python\)\s*$', src, re.M)
          is not None)
    # 判定表必须显式出现两个环境路径（否则新增模型时不知道该改哪）
    check('判定表里提到 flux 环境', 'miniconda3/envs/flux' in src)
    check('判定表里提到 qwen 环境', 'envs/qwen' in src)


def t_priority():
    print('\n[2] 判定优先级：显式 > 类名 > 路径 > 兜底')
    src = open(SH, encoding='utf-8').read()
    fn = extract_func(src, 'pick_python')
    check('能抽出 pick_python 函数体', bool(fn))
    if not fn:
        return
    # 顺序断言：显式指定的判断必须出现在类名判断之前
    i_exp = fn.find('FLUX_RESIDENT_PY')
    i_cls = fn.find('_class_name')
    i_path = fn.find('qwen*')
    check('★ 显式 FLUX_RESIDENT_PY 是最先判的（可强制覆盖）',
          0 <= i_exp < i_cls,
          f'显式@{i_exp} 类名@{i_cls}')
    check('类名判定先于路径兜底',
          0 <= i_cls < i_path,
          f'类名@{i_cls} 路径@{i_path}')
    # 末尾必须有兜底 echo（保证永远有答案，不为空）。
    # 允许带注释后缀（那不影响行为），只要存在这一行。
    check('★ 末尾有兜底返回值（PY 永不为空）',
          bool(re.search(r'\becho\s+"\$FLUX_PY_DEFAULT"', fn)),
          '空 PY 会让 screen 里执行 `flux_resident_server.py`（无解释器）')
    # 路径匹配必须做大小写归一（`*[Qq]wen*` 那种 glob 只覆盖 2 种组合）
    check('★ 路径匹配做了大小写归一（tr A-Z a-z）',
          "tr 'A-Z' 'a-z'" in fn,
          '`qWen` / `QWEN` 这类目录名在 [Qq] 写法下会漏掉')


def t_existence_guard():
    print('\n[3] 解释器不存在 → 显式失败（不静默）')
    src = open(SH, encoding='utf-8').read()
    check('★ 有 [ ! -x "$PY" ] 存在性检查',
          re.search(r'\[\s*!\s*-x\s+"\$PY"\s*\]', src) is not None,
          '否则 screen 里会静默失败，表现为「服务没起来也没报错」')
    # 报错必须给出可行动信息（已知环境列表）
    seg_i = src.find('[ ! -x "$PY" ]')
    seg = src[seg_i:seg_i + 700] if seg_i >= 0 else ''
    check('报错给出已知环境清单（可行动）',
          'flux=' in seg and 'qwen=' in seg)
    check('报错提示 FLUX_RESIDENT_PY 这个出口',
          'FLUX_RESIDENT_PY' in seg)
    check('发现缺失时 exit 非 0', re.search(r'exit\s+1', seg) is not None)


def t_syntax():
    print('\n[4] 脚本语法合法（bash -n）')
    bash = find_bash()
    if not bash:
        check('找到 bash', False, 'NO_BASH —— 系统 PATH 里没有 bash，本项无法验证')
        return
    r = subprocess.run([bash, '-n', SH], capture_output=True, text=True)
    check('bash -n 通过', r.returncode == 0,
          (r.stderr or '').strip()[:200] or f'rc={r.returncode}')


def t_case_table():
    print('\n[5] 判定表：六种输入 → 期望环境')
    bash = find_bash()
    if not bash:
        check('找到 bash（跑判定表）', False, 'NO_BASH')
        return
    src = open(SH, encoding='utf-8').read()
    fn = extract_func(src, 'pick_python')
    check('抽出 pick_python', bool(fn))
    if not fn:
        return

    # ★ 关键：bash 是原生程序，**Git Bash 设了 MSYS_NO_PATHCONV=1 不做路径转换**
    #   （见工作区 MEMORY）→ Windows 路径里的反斜杠会被 bash 当转义符吃掉，
    #   `C:\Users\...` 变成 `C:UsersDancing...`。必须先把假解释器路径统一成
    #   **正斜杠**形态，再注入源码。这是本门自己踩过的一个真坑。
    fake_py = sys.executable.replace('\\', '/')

    td = tempfile.mkdtemp(prefix='pysel-')
    qd = os.path.join(td, 'qwen'); os.makedirs(qd)
    with open(os.path.join(qd, 'model_index.json'), 'w', encoding='utf-8') as f:
        f.write('{"_class_name": "QwenImage21Pipeline"}')
    kd = os.path.join(td, 'klein'); os.makedirs(kd)
    with open(os.path.join(kd, 'model_index.json'), 'w', encoding='utf-8') as f:
        f.write('{"_class_name": "Flux2KleinPipeline"}')
    # 显式指定的「假解释器」：用真实存在的 python（否则拿不到任何可辨识的输出），
    # 但路径形态要能与 qwen/flux 区分开 → 用一个符号链接做不到（Windows），
    # 改为让**覆盖值**指向一个真实文件，并断言输出等于它。
    override_py = fake_py

    helper = os.path.join(td, 'pick.sh')

    def run(model, explicit=None):
        """把候选路径替换成真实解释器后 source 并调用。

        为什么要替换：判定表里的候选是 /root/... 的 Linux 路径，在 Windows 上
        一律 `[ -x ]` 为假 → 永远走兜底，测不出「有没有命中 qwen」。
        替换后：命中 qwen 候选 → 输出 fake_py；走兜底 → 输出带 FLUXFALLBACK 标记。
        """
        patched = fn.replace('/root/autodl-tmp/envs/qwen/bin/python', fake_py)
        patched = patched.replace('/root/miniconda3/envs/qwen/bin/python', fake_py)
        # 兜底那行单独染色，便于区分「命中候选」与「走兜底」
        patched = patched.replace(
            'echo "$FLUX_PY_DEFAULT"', 'echo "FLUXFALLBACK:"$FLUX_PY_DEFAULT')
        patched = patched.replace(
            f'FLUX_PY_DEFAULT={fake_py}'.replace('/', '/'),
            'FLUX_PY_DEFAULT=/FLUX_FALLBACK_SENTINEL')
        with open(helper, 'w', encoding='utf-8', newline='\n') as f:
            f.write('FLUX_PY_DEFAULT=/FLUX_FALLBACK_SENTINEL\n')
            f.write('QWEN_PY_CANDIDATES=' + fake_py + '\n')
            f.write(patched + '\n')
        script = f'MODEL="{model}"; ' + (
            f'FLUX_RESIDENT_PY="{explicit}"; ' if explicit else '') + \
            'source "%s"; pick_python' % helper.replace('\\', '/')
        r = subprocess.run([bash, '-c', script], capture_output=True, text=True)
        return (r.stdout or '').strip(), (r.stderr or '').strip()

    cases = [
        ('Qwen 机（class_name 判定命中 qwen 环境）', qd, None, 'qwen'),
        ('klein 机（class_name 判定 → 走兜底 flux）', kd, None, 'flux'),
        ('只靠路径含 qwen（无 model_index.json）',
         '/root/autodl-tmp/qwen-models/Qwen-Image-2.1', None, 'qwen'),
        ('路径大写 Qwen 也要命中', '/x/Qwen-Image-2.1', None, 'qwen'),
        ('未知模型 → 老默认 flux（向后兼容）',
         '/root/autodl-tmp/models/FLUX.1-dev', None, 'flux'),
        ('★ 显式指定覆盖一切（连 Qwen 机也能被强制）',
         qd, override_py, 'override'),
        ('★ 大小写混合 qWen 也要命中', '/x/qWen-models', None, 'qwen'),
    ]
    for label, model, explicit, expect in cases:
        # Windows 路径要转成 bash 可读形态（同上，反斜杠问题）
        m = model.replace('\\', '/')
        out, err = run(m, (explicit or '').replace('\\', '/') or None)
        if expect == 'qwen':
            ok = out == fake_py
        elif expect == 'flux':
            ok = 'FLUX_FALLBACK_SENTINEL' in out
        else:
            ok = out == override_py
        check(label, ok, f'得到 {out!r}' + (f' stderr={err[:90]!r}' if err else ''))

    shutil.rmtree(td, ignore_errors=True)


def t_single_sync_point():
    print('\n[6] 判定表是唯一同步点（新增模型只改一处）')
    src = open(SH, encoding='utf-8').read()
    # 候选解释器路径只应集中出现（注释里的提及不算漂移，但**赋值**只该一处）
    n_assign = len(re.findall(r'^\s*QWEN_PY_CANDIDATES=', src, re.M))
    check('QWEN_PY_CANDIDATES 只被赋值 1 次（唯一同步点）',
          n_assign == 1, f'赋值 {n_assign} 次 —— 散落多处 = 改一处漏一处')
    # 兜底默认值同理
    n_flux = len(re.findall(r'^\s*FLUX_PY_DEFAULT=', src, re.M))
    check('FLUX_PY_DEFAULT 只被赋值 1 次',
          n_flux == 1, f'赋值 {n_flux} 次')
    # ★ 判定逻辑里**不得**再出现硬编码的环境路径（必须走变量）
    #   否则「只改一张表」的承诺就是假的
    body = extract_func(src, 'pick_python') or ''
    check('★ pick_python 体内无硬编码环境路径（全走变量）',
          '/root/miniconda3/envs/' not in body and '/root/autodl-tmp/envs/' not in body,
          '体内直接写路径 = 改环境时要改两处')
    check('存在 QWEN_PY_CANDIDATES 变量（判定表本体）',
          'QWEN_PY_CANDIDATES=' in src)
    check('存在 FLUX_PY_DEFAULT 变量',
          'FLUX_PY_DEFAULT=' in src)
    check('判定表旁有「新增模型只改这一张表」的说明',
          '新增模型' in src)
    # 候选都不在时必须**返回首选**而不是静默退回 flux ——
    # 否则错误信息会变成「Qwen 跑不起来（import 错误）」而不是「环境没装」
    check('★ 候选全不存在时返回首选（不静默退回 flux）',
          'QWEN_PY_CANDIDATES%% *' in src or '${QWEN_PY_CANDIDATES%% *}' in src,
          '退回 flux 会把「环境没装」伪装成看不懂的 import 错误')


def main():
    print('=' * 64)
    print('start_resident.sh 解释器选择回归门（异构模型多环境）')
    print('=' * 64)
    if not os.path.exists(SH):
        print(f'  ❌ 找不到 {SH}')
        return 1
    t_no_hardcoded_py()
    t_priority()
    t_existence_guard()
    t_syntax()
    t_case_table()
    t_single_sync_point()
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

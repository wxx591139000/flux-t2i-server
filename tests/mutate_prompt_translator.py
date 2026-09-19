#!/usr/bin/env python3
"""变异测试：证明 tests/test_prompt_translator.py 这道门**真能抓住回退**，不是永远绿。

做法：把 prompt_translator.py 临时改回有 bug / 有风险的写法，跑回归门，期望**变红**；
跑完立刻从内存原样恢复，并用 md5 校验字节级完好（必须用二进制读写，见下方注释）。

用法: python tests/mutate_prompt_translator.py    # 全部被抓住则 exit 0
"""
import hashlib
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(BASE, 'manager', 'prompt_translator.py')
TEST = os.path.join(BASE, 'tests', 'test_prompt_translator.py')
PY = sys.executable

MUTANTS = [
    ('① 英文也丢给 LLM（去掉纯英文直通）→ 用户自己写好的英文被擅自改写',
     '    if not has_chinese(zh_prompt):\n        return zh_prompt  # 纯英文不转换',
     '    if False:\n        return zh_prompt'),

    ('② 不再要求「翻译结果仍含中文 = 失败」→ 中文提示词被原样喂给 FLUX',
     '        if result and not has_chinese(result):',
     '        if result:'),

    ('③ 静默降级：翻译失败返回中文原文（旧版的灾难写法）',
     "    raise TranslationError(f'中文提示词翻译失败（已重试 {retries} 次）: {zh_prompt[:60]}')",
     '    return zh_prompt'),

    ('④ 去掉重试（LLM 偶发空返回时直接失败）→ 客户偶发无理由报错',
     '    for attempt in range(1, max(1, retries) + 1):',
     '    for attempt in range(1, 2):'),

    ('⑤ 不剥首尾引号 → 引号残留进 FLUX 提示词',
     "        return text.strip().strip('\"')",
     '        return text.strip()'),

    ('⑥ 删掉「正向措辞」约束 → FLUX 静默忽略负向词，出图变差且无报错',
     'Use POSITIVE PHRASING ONLY.',
     'Use clear wording.'),

    ('⑦ 删掉「忠实翻译、不得自造实体」约束 → 出图多出用户没要的东西',
     'TRANSLATE FAITHFULLY',
     'Translate naturally'),
]


def md5(path):
    return hashlib.md5(open(path, 'rb').read()).hexdigest()


# 读写一律走**二进制**：文本模式在 Windows 下会把 '\n' 写成 '\r\n'，
# 恢复后 md5 对不上（实测踩过：整个文件被 CRLF 化）。变异脚本改的是生产代码，
# 恢复必须字节级保真。
def read_src():
    return open(TARGET, 'rb').read().decode('utf-8')


def write_src(text):
    open(TARGET, 'wb').write(text.encode('utf-8'))


def run_test():
    r = subprocess.run([PY, TEST], capture_output=True, text=True, cwd=BASE, timeout=180)
    failed = [ln for ln in (r.stdout or '').splitlines() if ln.startswith('[FAIL]')]
    return r.returncode, failed


def main():
    original = read_src()
    md5_before = md5(TARGET)
    caught = 0
    try:
        for name, old, new in MUTANTS:
            if old not in original:
                print(f'[skip] {name}  —— 源码里找不到目标片段，变异脚本需更新')
                continue
            write_src(original.replace(old, new, 1))
            rc, failed = run_test()
            if rc != 0 and failed:
                caught += 1
                print(f'[抓住] {name}')
                for f in failed[:3]:
                    print(f'         {f[:96]}')
            else:
                print(f'[漏了] {name}  —— 门没抓住回退，这道断言无效！exit={rc}')
    finally:
        write_src(original)
        md5_after = md5(TARGET)
        intact = md5_before == md5_after
        print()
        print(f'源码已恢复: {"是" if intact else "否（!! 请立刻检查）"}  '
              f'md5 {md5_before[:8]} -> {md5_after[:8]}')
        if not intact:
            sys.exit(2)

    total = len([m for m in MUTANTS if m[1] in original])
    print(f'== 变异测试: {caught}/{total} 个回退被门抓住 ==')
    sys.exit(0 if caught == total else 1)


if __name__ == '__main__':
    main()

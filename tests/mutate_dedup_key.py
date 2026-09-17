#!/usr/bin/env python3
"""变异测试：证明 tests/test_dedup_key.py 这道门**真能抓住回退**，不是永远绿。

做法：把 flux_queue.py 临时改回三种有 bug 的写法，跑回归门，期望**变红**；
跑完立刻从备份恢复，并用 md5 校验文件完好无损。

用法: python tests/mutate_dedup_key.py     # 三种变异全被抓住则 exit 0
"""
import hashlib
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(BASE, 'manager', 'flux_queue.py')
TEST = os.path.join(BASE, 'tests', 'test_dedup_key.py')
PY = sys.executable

MUTANTS = [
    ('bug A 回退：worker 收尾键不含 seed/尺寸',
     "        key = _dedup_key(user_id, job.get('prompt'), job.get('seed'),\n"
     "                         job.get('width'), job.get('height'),\n"
     "                         original_prompt=job.get('original_prompt'))",
     "        key = _dedup_key(user_id, job.get('prompt'))"),
    ('bug B 回退：中文用翻译后文本做键（翻译漂移→连点出重复图）',
     '    base = original_prompt or prompt',
     '    base = prompt'),
    ('bug C 回退：去掉提交锁（并发连点放行多个）',
     '        with self._submit_lock:',
     '        if True:'),
    ('bug D 回退：不把 sqlite3.Row 转成 dict（Row 没有 .get()，真机打死 worker 线程）',
     "        # job_get() 返回的是 sqlite3.Row —— 它支持 [] 索引但**没有 .get()**。\n"
     "        # 统一转成 dict 再用 .get()：既兼容 Row，缺列时也不会抛 IndexError。\n"
     "        job = dict(job)\n"
     "        user_id = job['user_id']",
     "        user_id = job['user_id']"),
]


def md5(path):
    return hashlib.md5(open(path, 'rb').read()).hexdigest()


# 读写一律走**二进制**：用文本模式的话，Windows 下 open() 默认把 '\n' 写成
# '\r\n'，恢复后文件 md5 对不上（实测踩过：整个文件被 CRLF 化，455 行全变）。
# 变异脚本会临时改生产代码，恢复必须字节级保真。
def read_src():
    return open(TARGET, 'rb').read().decode('utf-8')


def write_src(text):
    open(TARGET, 'wb').write(text.encode('utf-8'))


def run_test():
    r = subprocess.run([PY, TEST], capture_output=True, text=True,
                       cwd=BASE, timeout=180)
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
                for f in failed:
                    print(f'         {f[:96]}')
            else:
                print(f'[漏了] {name}  —— 门没抓住回退，这道断言无效！exit={rc}')
    finally:
        write_src(original)
        md5_after = md5(TARGET)
        ok = md5_before == md5_after
        print()
        print(f'源码已恢复: {"是" if ok else "否（!! 请立刻检查）"}  '
              f'md5 {md5_before[:8]} -> {md5_after[:8]}')
        if not ok:
            sys.exit(2)

    total = len([m for m in MUTANTS if m[1] in original])
    print(f'== 变异测试: {caught}/{total} 个回退被门抓住 ==')
    sys.exit(0 if caught == total else 1)


if __name__ == '__main__':
    main()

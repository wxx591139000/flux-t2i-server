"""机器闸门：probe_full 的 /health 输出必须强制换行，防止与 MODELS_BEGIN 黏连。

背景（2026-09-28 Qwen 上线，10/10 探活失败的真因）：
  resident 的 /health 是 curl 裸输出、结尾无 '\n'。probe_full 把它和
  下一行 `echo MODELS_BEGIN` 拼在同一条 ssh 命令里，结果输出变成：
      ..."uptime_sec": 1051.9}MODELS_BEGIN
  解析侧（flux_server_manager.py 第 ~905 行）要求 JSON 行 `endswith('}')`，
  黏连后结尾是 '}MODELS_BEGIN' → 判 False → resident=False → server_down。

修法：/health 的 curl 后面紧跟 `printf '\\n'` 强制换行。
本测试守住两条不变量：
  A. 命令里 /health 的 curl 之后、MODELS_BEGIN 之前，必须存在 `printf` 换行。
  B. 解析逻辑的 JSON 行判定仍是 `endswith('}')`（即依赖 A 提供干净的换行，
     而不是靠解析侧去剥后缀 —— 解析侧剥后缀是「容器型修法」，容易再漏）。
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'manager' / 'flux_server_manager.py'

def _find_probe_full(src: str) -> ast.FunctionDef:
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == 'probe_full':
            return n
    raise AssertionError('未找到 probe_full 函数')

def _joined_str_value(n: ast.AST) -> str:
    """把函数里的 f-string 常量片段拼成可读文本（用于断言）。"""
    parts = []
    for node in ast.walk(n):
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
    return '\n'.join(parts)

def main():
    src = SRC.read_text(encoding='utf-8')
    fn = _find_probe_full(src)
    body = ast.get_source_segment(src, fn)

    results = []

    # A. 命令里 /health 的 curl 之后必须强制换行
    #    probe_full 里 remote 字符串由多个 f-string 拼接，抓「health 那段 curl」与
    #    紧跟其后的 printf 换行是否都存在。
    has_health_curl = 'curl -s -m 5 http://127.0.0.1:' in body and '/health' in body
    # 源码里写的是 f"printf '\\n'; " —— ast 拿到的是原始源码，`\\n` 是两个反斜杠+n。
    # 用「printf 后跟引号包裹的反斜杠 n」这种结构特征判断，不纠结转义层数。
    has_printf_newline = ('printf' in body and
                          ("'\\n'" in body or "'\\\\n'" in body or '"\\n"' in body))

    # 关键顺序断言：printf 换行必须出现在 /health curl 之后（同一段命令里）
    health_idx = body.find('/health')
    printf_idx = body.find('printf')
    results.append(('A1 probe_full 含 /health curl', has_health_curl, '缺少 /health 探活'))
    results.append(('A2 probe_full 含 printf 换行', has_printf_newline, '缺少强制换行，会黏连 MODELS_BEGIN'))
    results.append(('A3 printf 在 /health 之后', printf_idx > health_idx > 0,
                    f'printf({printf_idx}) 必须位于 /health({health_idx}) 之后'))

    # B. 解析侧仍是 endswith('}')（不靠剥后缀的容器型修法）
    parse_ok = "endswith('}')" in body or "endswith('}')" in src
    results.append(('B1 解析侧仍用 endswith(})', parse_ok, '解析逻辑被改动，需人工复核'))

    all_ok = True
    for name, ok, hint in results:
        mark = 'PASS' if ok else 'FAIL'
        if not ok:
            all_ok = False
        print(f'[{"PASS" if ok else "FAIL"}] {name}' + ('' if ok else f'  ← {hint}'))

    print(f'\n共 {len(results)} 项，通过 {sum(1 for _, o, _ in results if o)}，失败 {sum(1 for _, o, _ in results if not o)}')
    sys.exit(0 if all_ok else 1)

if __name__ == '__main__':
    main()

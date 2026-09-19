#!/usr/bin/env python3
"""回归门：中文提示词必须被正确翻译成 FLUX 需要的英文提示词（v2.9）

背景（为什么必须有这道门）
  FLUX.1-dev 是双**英文**编码器（CLIP-L + T5-XXL）。中文直发不是「效果差一点」，
  而是**完全跑偏**：实测同一句「一只白色陶瓷马克杯，置于无缝纯白影棚背景…」
  （同 seed 12345）直发中文出的是**动漫少女插画**，主体根本不是杯子。
  所以翻译层不是可选优化，是出图正确性的前置条件。

  同时翻译**不能静默降级成中文**：旧版 LLM 失败就返回中文原文，把这种灾难
  静默化了，客户只会觉得「这 AI 出图不行」。现在失败一律抛 TranslationError，
  由 submit() 拒掉任务并说明原因。

本文件守住五条不变量：
  1. 含中文 → 一定过 LLM，且返回英文；
  2. 纯英文 → 一定**不**过 LLM（原样直通，不擅自改写用户写好的英文）；
  3. 空输入 → 原样返回，不调用 LLM；
  4. LLM 不可用 / 返回仍含中文 → 重试后抛 TranslationError，**绝不**回退成中文；
  5. 输出必须符合 FLUX 方法论（正向措辞、忠实不添实体、静态场景、30-80 词）。

全部离线可跑（stub 掉 call_llm，不联网、不写库、不碰 GPU）。
用法: python tests/test_prompt_translator.py    # 全绿 exit 0，有红 exit 1
"""
import ast
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# 本地 stub 端点必须绕过系统代理，否则 urlopen 会把 127.0.0.1 发去代理。
# urllib 在首次 urlopen 时构建 opener 并读取代理环境变量，所以必须在调用前设好。
os.environ['no_proxy'] = '127.0.0.1,localhost'
os.environ['NO_PROXY'] = '127.0.0.1,localhost'

from manager import prompt_translator as pt  # noqa: E402

RESULTS = []


def ok(name, cond, detail=''):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f'  {detail}' if detail else ''))
    RESULTS.append(bool(cond))
    return bool(cond)


# ── 替身：把 call_llm 换成可编程的假 LLM ──
# 复刻真实返回值契约：成功返回 str（可能为空串），异常在内部被吞掉后返回 ''。
class FakeLLM:
    """按脚本依次返回预设值；同时记录调用次数与收到的 user_message。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, system_prompt, user_message, temperature=0.7):
        self.calls.append((system_prompt, user_message))
        if not self.script:
            return ''
        return self.script.pop(0)


_real_call_llm = pt.call_llm


def with_llm(script):
    """安装替身，返回 FakeLLM；调用方负责 finally 里 restore()。"""
    fake = FakeLLM(script)
    pt.call_llm = fake
    return fake


def restore():
    pt.call_llm = _real_call_llm


EN_OK = ('A white ceramic mug placed on a wooden table, illuminated by soft natural light. '
         'Photorealistic, professional product photography, diffused window lighting, '
         'shallow depth of field, clean minimalist composition, high detail.')

# ===========================================================================
# 1~2. 含中文必过 LLM；纯英文必不过 LLM
# ===========================================================================
try:
    fake = with_llm([EN_OK])
    out = pt.translate_to_flux_prompt('一只白色的陶瓷马克杯，置于木桌上，自然光')
    ok('T1 含中文 → 过 LLM 并返回英文', out == EN_OK and len(fake.calls) == 1,
       f'调用 {len(fake.calls)} 次')
    ok('T1b 送给 LLM 的 user_message 带着中文原文',
       bool(fake.calls) and '陶瓷马克杯' in fake.calls[0][1])
finally:
    restore()

try:
    fake = with_llm([EN_OK])
    en = 'a cute shiba inu running on a beach, cinematic lighting, photorealistic'
    out = pt.translate_to_flux_prompt(en)
    ok('T2 纯英文 → 原样直通，不调用 LLM', out == en and len(fake.calls) == 0,
       f'调用 {len(fake.calls)} 次（必须为 0）')
finally:
    restore()

# ===========================================================================
# 3. 空输入
# ===========================================================================
try:
    fake = with_llm([EN_OK])
    r1 = pt.translate_to_flux_prompt('')
    r2 = pt.translate_to_flux_prompt('   ')
    r3 = pt.translate_to_flux_prompt(None)
    ok('T3 空 / 空白 / None → 不调用 LLM（空白与空值都归一为 ""）',
       r1 == '' and r2 == '' and r3 == '' and len(fake.calls) == 0,
       f'调用 {len(fake.calls)} 次')
finally:
    restore()

# ===========================================================================
# 4. 失败必须抛错，绝不回退中文（这是本门最重要的一条）
# ===========================================================================
try:
    fake = with_llm(['', '', ''])
    raised = None
    try:
        pt.translate_to_flux_prompt('一只猫', retries=3)
    except pt.TranslationError as e:
        raised = e
    ok('T4 LLM 恒返回空 → 重试满 3 次后抛 TranslationError（不返回中文）',
       raised is not None and len(fake.calls) == 3,
       f'调用 {len(fake.calls)} 次, 异常={type(raised).__name__ if raised else "无"}')
finally:
    restore()

try:
    fake = with_llm(['一只猫在窗台上', '一只猫在窗台上', '一只猫在窗台上'])
    raised = None
    try:
        pt.translate_to_flux_prompt('一只猫在窗台上', retries=3)
    except pt.TranslationError:
        raised = True
    ok('T5 LLM 返回仍含中文 → 判失败并重试，最终抛错', raised is True,
       f'调用 {len(fake.calls)} 次')
finally:
    restore()

# ===========================================================================
# 5. 重试真的生效（第 1 次坏、第 2 次好 → 成功）
# ===========================================================================
try:
    fake = with_llm(['我翻译不出来了', EN_OK])
    out = pt.translate_to_flux_prompt('一只白色的陶瓷马克杯', retries=3)
    ok('T6 第 1 次返回中文、第 2 次干净 → 重试后成功', out == EN_OK and len(fake.calls) == 2,
       f'调用 {len(fake.calls)} 次')
finally:
    restore()

# ===========================================================================
# 6. 中英混排也要翻（B 链「商品描述 + 中文镜头指令」就是这个形态）
# ===========================================================================
try:
    fake = with_llm([EN_OK])
    out = pt.translate_to_flux_prompt('white canvas sneakers 帆布材质，浅灰鞋带')
    ok('T7 中英混排 → 也过 LLM', out == EN_OK and len(fake.calls) == 1,
       f'调用 {len(fake.calls)} 次')
finally:
    restore()

# ===========================================================================
# 7. call_llm 本身
#    上面 1~6 把 call_llm 替身掉了 —— 那等于把「响应解析 + 剥引号 + 错误吞掉」
#    这段**逻辑**也一起替身掉了（踩过的坑：替身只复刻数据形状、不复刻类型行为）。
#    这里 stub 的是 HTTP 端点这个**外部依赖**，放行真的 call_llm。
# ===========================================================================
import json as _json  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402


class _StubLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n)
        self.server.seen.append({
            'path': self.path,
            'auth': self.headers.get('Authorization'),
            'body': _json.loads(raw.decode('utf-8') or '{}'),
        })
        status, body = self.server.script.pop(0) if self.server.script else (200, {})
        payload = _json.dumps(body).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def _resp(text):
    return {'choices': [{'message': {'content': text}}]}


_srv = HTTPServer(('127.0.0.1', 0), _StubLLM)
_srv.seen, _srv.script = [], []
_srv_port = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()

_orig_base, _orig_key = pt.API_BASE, pt.API_KEY
pt.API_BASE = f'http://127.0.0.1:{_srv_port}/api/plan'
pt.API_KEY = 'test-key-123'
try:
    _srv.script = [(200, _resp('"' + EN_OK + '"'))]
    out = pt.call_llm('sys', 'user')
    ok('T8a call_llm 正常返回 → 首尾引号被剥掉', out == EN_OK, repr(out[:24]))

    req = _srv.seen[-1]
    ok('T8b 请求路径 = {BASE}/v3/chat/completions',
       req['path'] == '/api/plan/v3/chat/completions', req['path'])
    ok('T8c 带 Authorization: Bearer <API_KEY>',
       req['auth'] == 'Bearer test-key-123', str(req['auth']))
    roles = [m.get('role') for m in req['body'].get('messages', [])]
    ok('T8d 请求体含 system + user 两段 message（System Prompt 真的送出去了）',
       roles == ['system', 'user'], str(roles))

    _srv.script = [(500, {'error': 'boom'})]
    out = pt.call_llm('sys', 'user')
    ok('T8e HTTP 500 → 返回 "" 不抛异常（交给上层重试）', out == '', repr(out))

    _srv.script = [(200, {'unexpected': True})]
    out = pt.call_llm('sys', 'user')
    ok('T8f 响应缺 choices → 返回 "" 不抛异常', out == '', repr(out))
finally:
    pt.API_BASE, pt.API_KEY = _orig_base, _orig_key
    _srv.shutdown()
    _srv.server_close()

# ===========================================================================
# 8. has_chinese 判定
# ===========================================================================
ok('T9a 汉字被判定为中文', pt.has_chinese('一只猫') is True)
ok('T9b 英文不被判定为中文', pt.has_chinese('a cute cat on a windowsill') is False)
ok('T9c 纯数字/符号不被判定为中文', pt.has_chinese('4k, 8K -- 1:1') is False)

# ===========================================================================
# 9. 源码级断言：FLUX 方法论必须留在 System Prompt 里
#    这四条都是**实测踩坑换来的**，被删掉会静默降低出图质量，必须由门守住。
# ===========================================================================
SP = pt.FLUX_SYSTEM_PROMPT
ok('T10a System Prompt 要求「只用英文」', 'English only' in SP)
ok('T10b System Prompt 要求正向措辞（FLUX 忽略负向词）',
   'POSITIVE PHRASING ONLY' in SP and 'ignores negative' in SP)
ok('T10c System Prompt 要求忠实翻译、不得自造实体',
   'TRANSLATE FAITHFULLY' in SP and 'never invent entities' in SP)
ok('T10d System Prompt 要求静态场景（FLUX 出静态图）',
   'STATIC positions' in SP and 'Do NOT describe motion' in SP)
ok('T10e System Prompt 保留 30-80 词约束', '30-80 words' in SP)

# ===========================================================================
# 10. 连线断言：submit() 真的调了翻译层，且失败时拒任务
#     用 AST 定位 submit 函数体（不靠正则扫全文，避免注释/别处同名误判）。
# ===========================================================================
_q = os.path.join(BASE, 'manager', 'flux_queue.py')
_tree = ast.parse(open(_q, encoding='utf-8').read())


def _find_method(tree, cls_name, meth_name):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == meth_name:
                    return sub
    return None


def _stripped_body(fn):
    """剥掉 docstring —— 否则注释里的例子会被当成真实调用。"""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return body


_submit = _find_method(_tree, 'FluxQueueScheduler', 'submit')
ok('T11a 找到 FluxQueueScheduler.submit', _submit is not None)

if _submit:
    body = _stripped_body(_submit)
    src = ast.unparse(ast.Module(body=body, type_ignores=[]))

    ok('T11b submit() 调用 translate_to_flux_prompt（翻译层真的接在链路上）',
       'translate_to_flux_prompt(' in src)

    handler = next(
        (n for n in ast.walk(ast.Module(body=body, type_ignores=[]))
         if isinstance(n, ast.ExceptHandler) and n.type is not None
         and 'TranslationError' in ast.unparse(n.type)),
        None)
    ok('T11c submit() 捕获 TranslationError（不让异常冒到 HTTP 层变成 500）',
       handler is not None)

    ret_err = False
    if handler:
        for n in ast.walk(handler):
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict):
                keys = [k.value for k in n.value.keys if isinstance(k, ast.Constant)]
                if 'error' in keys:
                    ret_err = True
    ok('T11d 翻译失败时 return {"error": ...} 而不是继续入队', ret_err)

    ok('T11e 翻译发生在 _submit_lock 之外（LLM 调用约 5s，不该占着提交锁）',
       src.index('translate_to_flux_prompt(') < src.index('with self._submit_lock:')
       if 'with self._submit_lock:' in src else False,
       '翻译段必须在锁之前')

print()
print(f'== prompt_translator 回归门: {RESULTS.count(True)}/{len(RESULTS)} 通过 ==')
sys.exit(0 if all(RESULTS) else 1)

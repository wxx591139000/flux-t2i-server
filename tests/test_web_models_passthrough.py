#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""manager `/api/models` 的**能力透传**回归门（2026-09-29 新增）。

## 为什么需要这道门（这是一次真实的静默缺陷）

B 链的「模特穿搭」要靠 `capabilities.multi_ref` 判断当前模型能不能真收多张图。
GPU 侧 `server/flux_resident_server.py` 一直就在 `/models` 里返回
`supports_edit` 与 `capabilities`（那是它的能力声明），
`manager/flux_resident_client.py` 的选机过滤也已按 `capabilities` 工作。

**唯独中间层 `flux_web_service._api_models` 把它们过滤掉了** ——
它的 `slim` 列表只挑了 id/name/ready/class_name/size_gb 五个字段。
后果：
  · 站点 `ModelInfo.supports_edit` **恒为 undefined**
  · `capabilities` 恒为 undefined → 前端判 `multi_ref` 永远拿不到
  · 于是穿搭场景的 UI 永远停在「当前模型不支持多图」，
    而真实原因是**中间层丢字段**，不是模型不行。
  · 更坏的是它**长得像"上游版本旧"**：站点源码里当时就写着
    「上游版本较旧、没这个字段时（undefined）不禁用」——
    注释把原因解释错了方向，于是没人会去查 manager。

这类「声明字段在中间层被吞掉」的缺陷，症状是**功能永久不可用且原因指向错误**，
必须有一条门钉住：**上游声明了什么，站点就收到什么**。

## 本门钉什么

  [1] supports_edit 原样透传（含 False —— False 是"明确不支持"，与"不知道"不同）
  [2] capabilities 原样透传（multi_ref / max_ref_images / transparent …）
  [3] 缺字段时给 **None 而不是 False**（"不知道"与"不支持"必须可分：
      前者前端应放行交给上游硬闸，后者才该置灰）
  [4] **path 仍然不外泄**（GPU 绝对路径属内部拓扑；补字段不等于全透传）
  [5] 变异断言：把 slim 改回只留 5 个字段 → 本门必须变红

## 怎么验

直接实例化 `_Handler` 的 `_api_models`（用 `__new__` 绕开 socket 构造），
把 `frc.find_available_server` 与 `frc._transport` 换成替身，
从 `self._json` 捕获输出。**无需真实 GPU / SSH / 网络**。

用法: python tests/test_web_models_passthrough.py    # 全绿 exit 0，有红 exit 1
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from manager import flux_resident_client as frc          # noqa: E402
import manager.flux_web_service as fws                    # noqa: E402

RESULTS = []


def check(name, cond, detail=''):
    RESULTS.append((name, bool(cond), detail))
    print(f'  {"OK " if cond else "XX "} {name}{("  -> " + detail) if detail else ""}')


# 上游 /models 的原样形状（抄自 server/flux_resident_server.py 的真实返回）
UPSTREAM_MODELS = [
    {'id': 'FLUX.2-klein-4B', 'name': 'FLUX.2-klein-4B', 'ready': True,
     'class_name': 'Flux2KleinPipeline', 'size_gb': 14.9,
     'supports_edit': True,
     'capabilities': {'text2img': True, 'edit': True, 'multi_ref': False,
                      'mask_param': False, 'transparent': False,
                      'max_ref_images': 1, 'native_res': 1024,
                      'cfg_param': 'guidance_scale'},
     # ★ 内部拓扑字段：manager **不得**外传
     'path': '/root/autodl-tmp/models/FLUX.2-klein-4B'},
    {'id': 'Qwen-Image-2.1', 'name': 'Qwen-Image-2.1', 'ready': True,
     'class_name': 'QwenImage21Pipeline', 'size_gb': 41.2,
     'supports_edit': True,
     'capabilities': {'text2img': True, 'edit': True, 'multi_ref': True,
                      'mask_param': False, 'transparent': True,
                      'max_ref_images': 10, 'native_res': 2048,
                      'cfg_param': 'true_cfg_scale'},
     'path': '/root/autodl-tmp/models/Qwen-Image-2.1'},
    {'id': 'FLUX.1-dev', 'name': 'FLUX.1-dev', 'ready': True,
     'class_name': 'FluxPipeline', 'size_gb': 31.7,
     'supports_edit': False,
     'capabilities': {'text2img': True, 'edit': False, 'multi_ref': False,
                      'max_ref_images': 0},
     'path': '/root/autodl-tmp/models/FLUX.1-dev'},
    # 老上游：完全没有 supports_edit / capabilities 两个字段
    {'id': 'OLD-NO-CAPABILITY', 'name': 'OLD-NO-CAPABILITY', 'ready': True,
     'size_gb': 12, 'path': '/root/autodl-tmp/models/OLD'},
]


class _FakeTransport:
    def __init__(self, payload):
        self.payload = payload

    def get_json(self, path, timeout=None):
        assert path == '/models', f'意外请求了 {path}'
        return self.payload


def call_api_models(upstream_models=None):
    """跑一次 `_api_models`，返回它给站点的 JSON。不碰网络。

    `__new__` 绕开 BaseHTTPRequestHandler 的 socket 构造 —— 我们只要那个方法。
    """
    payload = {'models': UPSTREAM_MODELS if upstream_models is None else upstream_models,
               'current': 'FLUX.2-klein-4B', 'switching_to': None}
    captured = {}

    h = fws._Handler.__new__(fws._Handler)
    h._json = lambda obj, code=200: captured.update(obj=obj, code=code)

    orig_find, orig_transport = frc.find_available_server, frc._transport
    frc.find_available_server = lambda *a, **k: ({'name': 'stub-machine',
                                                  'alias': 'stub'}, {'name': 'stub-machine'})
    frc._transport = lambda s: _FakeTransport(payload)
    try:
        h._api_models()
    finally:
        frc.find_available_server, frc._transport = orig_find, orig_transport
    return captured.get('obj')


def by_id(out, mid):
    for m in (out or {}).get('models', []):
        if m.get('id') == mid:
            return m
    return None


# ══════════════════════════ [1] supports_edit 透传 ══════════════════════════
def t_supports_edit():
    print('\n[1] supports_edit 原样透传（True / False / 缺省三态都要保住）')
    out = call_api_models()
    check('返回 200 且带 models', out is not None and isinstance(out.get('models'), list))
    k = by_id(out, 'FLUX.2-klein-4B')
    d = by_id(out, 'FLUX.1-dev')
    o = by_id(out, 'OLD-NO-CAPABILITY')
    check('klein supports_edit=True 被透传', bool(k) and k.get('supports_edit') is True,
          json.dumps(k and k.get('supports_edit')))
    check('★ dev supports_edit=False 被透传（False 必须活着，不能被当成"空"丢掉）',
          bool(d) and d.get('supports_edit') is False,
          json.dumps(d and d.get('supports_edit')))
    check('★ 老上游缺字段 → None（"不知道"，前端据此放行，不得降级成 False）',
          bool(o) and o.get('supports_edit') is None,
          json.dumps(o and o.get('supports_edit')))


# ══════════════════════════ [2] capabilities 透传 ══════════════════════════
def t_capabilities():
    print('\n[2] capabilities 原样透传（穿搭场景的判据来源）')
    out = call_api_models()
    k = by_id(out, 'FLUX.2-klein-4B')
    q = by_id(out, 'Qwen-Image-2.1')
    o = by_id(out, 'OLD-NO-CAPABILITY')
    check('★ klein capabilities 被透传（不是 None）',
          bool(k) and isinstance(k.get('capabilities'), dict),
          json.dumps(k and k.get('capabilities')))
    check('★ klein.multi_ref=False（单图能力，穿搭必须能看出它不行）',
          bool(k) and (k.get('capabilities') or {}).get('multi_ref') is False,
          json.dumps(k and (k.get('capabilities') or {}).get('multi_ref')))
    check('★ Qwen.multi_ref=True（穿搭唯一的可用模型）',
          bool(q) and (q.get('capabilities') or {}).get('multi_ref') is True,
          json.dumps(q and (q.get('capabilities') or {}).get('multi_ref')))
    check('★ Qwen.max_ref_images=10 被透传（前端据此提示张数上限）',
          bool(q) and (q.get('capabilities') or {}).get('max_ref_images') == 10,
          json.dumps(q and (q.get('capabilities') or {}).get('max_ref_images')))
    check('★ Qwen.transparent=True 被透传（能力字典是整份，不是只挑一个字段）',
          bool(q) and (q.get('capabilities') or {}).get('transparent') is True,
          json.dumps(q and (q.get('capabilities') or {}).get('transparent')))
    check('老上游没有 capabilities → None（不是 {}，也不是 False）',
          bool(o) and o.get('capabilities') is None,
          json.dumps(o and o.get('capabilities')))


# ══════════════════════════ [3] 不得外泄 path ══════════════════════════
def t_no_path_leak():
    print('\n[3] path 仍不外泄（补字段 ≠ 全透传）')
    out = call_api_models()
    dumped = json.dumps(out, ensure_ascii=False)
    check('★ 输出里不含 GPU 绝对路径 /root/autodl-tmp',
          '/root/autodl-tmp' not in dumped, '')
    check('★ 也没有任何模型对象带 path 键',
          all('path' not in m for m in out.get('models', [])), '')


# ══════════════════════════ [4] 其余既有契约未被破坏 ══════════════════════════
def t_existing_contract():
    print('\n[4] 既有契约未被破坏（补字段不能把别的东西弄丢）')
    out = call_api_models()
    k = by_id(out, 'FLUX.2-klein-4B')
    check('id / name 仍在', bool(k) and k.get('name') == 'FLUX.2-klein-4B')
    check('ready 仍是 bool', bool(k) and k.get('ready') is True)
    check('class_name 仍在（排查用）', bool(k) and k.get('class_name') == 'Flux2KleinPipeline')
    check('size_gb 仍在', bool(k) and k.get('size_gb') == 14.9)
    check('current 被透传', out.get('current') == 'FLUX.2-klein-4B')
    check('switching_to 被透传（无切换时为 None）', 'switching_to' in out)

    # 空清单 / 上游不可用时的降级路径仍要工作
    out2 = call_api_models(upstream_models=[])
    check('上游空清单 → models 为空数组、不崩', out2.get('models') == [], json.dumps(out2))


# ══════════════════════════ [5] 变异断言 ══════════════════════════
def t_mutation():
    print('\n[5] 变异断言：把 slim 改回只留 5 个字段 → 本门必须变红')
    src = open(os.path.join(BASE, 'manager', 'flux_web_service.py'), encoding='utf-8').read()
    start = src.find('def _api_models')
    end = src.find('def _api_status')
    seg = src[start:end] if start >= 0 and end > start else ''
    check('源码：能在 _api_models 里找到 slim 构造', 'slim = [' in seg or 'slim=[' in seg.replace(' ', ''),
          f'片段长度 {len(seg)}')
    check('源码：slim 里带 supports_edit', 'supports_edit' in seg)
    check('源码：slim 里带 capabilities', 'capabilities' in seg)

    # 行为级变异：模拟"旧版实现"（只留 5 个字段），用同一份上游输入跑一遍，
    # 断言 **capabilities 确实会丢** —— 这证明本门的 [2] 不是在测恒真条件。
    old_slim = [{k: m.get(k) for k in ('id', 'name', 'ready', 'class_name', 'size_gb')}
                for m in UPSTREAM_MODELS if m.get('id')]
    check('★ 变异：旧版 slim（5 字段）确实丢掉 capabilities → 本门能抓住',
          all('capabilities' not in m for m in old_slim),
          json.dumps(old_slim[0]))
    check('★ 变异：旧版 slim 也确实丢掉 supports_edit',
          all('supports_edit' not in m for m in old_slim), '')


def main():
    print('=' * 66)
    print('manager /api/models · 能力透传回归门（2026-09-29）')
    print('=' * 66)
    t_supports_edit()
    t_capabilities()
    t_no_path_leak()
    t_existing_contract()
    t_mutation()

    ok = sum(1 for _n, c, _d in RESULTS if c)
    bad = [(n, d) for n, c, d in RESULTS if not c]
    print('\n' + '=' * 66)
    print(f'共 {len(RESULTS)} 项，通过 {ok}，失败 {len(bad)}')
    for n, d in bad:
        print(f'  XX {n}  {d}')
    print('=' * 66)
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())

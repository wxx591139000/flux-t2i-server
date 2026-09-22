"""许可闸回归门 —— 对外自动选机不得派给非商用模型（2026-09-22 新增）

## 为什么需要这道门

`commercial_ok` 这个字段在 servers.json 里存在了一段时间，也有一道门断言
「Qwen 必须标 False」—— 但那只证明了**声明正确**，证明不了**约束生效**。

真实缺口（本次接入 B 链对外前实测发现）：
  `grep -rn commercial_ok manager/*.py` 的结果显示它**只出现在
  _add_qwen_server.py（一次性脚本）**，运行时没有任何一处读它。
  也就是说 flux7（Qwen，Qwen Research License 非商用）一旦开机就在候选池里，
  朋友的生图任务可能被派上去跑 —— 这是法律风险，不是质量偏好。

修法：在 `_pick_from` 里加许可闸，位置在**能力过滤之前**
（许可比能力更硬：能力不满足只是跑不了，许可不满足是不能跑）。

## 本门钉什么

1. 非商用机器（commercial_ok=False）在自动选机时被排除
2. 缺 commercial_ok 字段的机器**保留**（向后兼容 —— 老部署没这字段，
   剔除会让全部机器消失，比漏判更危险）
3. need_model 显式指定时**绕过**许可闸（自用不受商用限制，这是刻意语义）
4. 全都不许商用 → 返回 None（不硬挑，避免「提交成功→排队→报错」白等一轮）
5. 变异测试：把闸拆掉 → 门必须变红
"""

import ast
import os
import re
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SRC_PATH = os.path.join(ROOT, 'manager', 'flux_resident_client.py')


def _strip_comments(src: str) -> str:
    """剥掉注释与字符串，只留代码骨架。

    本门要断言「源码里存在某段逻辑」，而注释里**正当地**会提到被断言的关键词
    （本文件顶部就大段解释了 commercial_ok）。不剥注释会产生假红。
    """
    out = []
    for tok in _tokenize(src):
        if tok.type in (getattr(tok, 'COMMENT', None),):
            continue
        out.append(tok.string)
    return '\n'.join(out)


def _tokenize(src: str):
    import io
    import tokenize
    return list(tokenize.generate_tokens(io.StringIO(src).readline))


def _raw_code(src: str) -> str:
    """去注释后的代码。

    ⚠️ 为什么本门**不用它做字面串断言**（2026-09-22 实测踩到）：
      `tokenize.untokenize` 会在 token 之间**补空格**，`c[0].get('x')` 会变成
      `c [0 ].get ('x')` —— 于是所有 `assertIn("c[0].get(...)")` 全部落空，
      门会假红，而源码其实是对的（当时打印出来的源码里那道闸明明在）。
      所以：**字面串类断言一律用正则容忍空白**，或直接断言行为。
      这条与项目已定的「钉可观察行为，不钉字面串」是同一个道理。
    """
    import io
    import tokenize
    toks2 = [(t.type, t.string) for t in tokenize.generate_tokens(io.StringIO(src).readline)
             if t.type != tokenize.COMMENT]
    return tokenize.untokenize(toks2)


def _norm(src: str) -> str:
    """去注释 + 抹平所有空白 —— 断言用。"""
    return re.sub(r'\s+', '', _raw_code(src))


def _norm_keep_lines(src: str) -> str:
    """去注释 + 只抹平行内空白，保留换行（用于断言语句先后顺序）。"""
    out = []
    for line in _raw_code(src).splitlines():
        out.append(re.sub(r'\s+', '', line))
    return '\n'.join(out)


class TestCommercialGate(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(SRC_PATH, encoding='utf-8') as f:
            cls.src = f.read()
        cls.code = _raw_code(cls.src)     # 去注释，保留原空白
        cls.norm = _norm(cls.src)         # 去注释 + 抹平空白（字面串断言用）

    # ── 第 1 组：源码里确实有这道闸 ──
    def test_source_has_gate(self):
        """_pick_from 里必须读 commercial_ok（不只看 servers.json 声明）。"""
        self.assertIn("get('commercial_ok')", self.norm,
                      '源码里没有任何一处读 commercial_ok —— 许可闸被删了')

    def test_gate_checks_is_not_false(self):
        """判据必须是 `is not False`，不是真值判断。

        为什么要精确到这个形态：`if not c[0].get('commercial_ok')` 会把
        **缺字段（None）**也剔掉 → 老部署（没这字段）所有机器一起消失。
        必须只剔**明确 False** 的。
        """
        self.assertIn("get('commercial_ok')isnotFalse", self.norm,
                      '判据不是 `is not False` —— 缺字段的机器会被误剔（向后兼容破坏）')

    def test_gate_before_caps_filter(self):
        """许可闸必须在能力过滤**之前**（许可比能力更硬）。"""
        i_lic = self.norm.find("get('commercial_ok')")
        i_caps = self.norm.find('_filter_by_caps(cands,caps_req)')
        self.assertGreater(i_lic, 0, '找不到许可闸')
        self.assertGreater(i_caps, 0, '找不到能力过滤')
        self.assertLess(i_lic, i_caps,
                        '许可闸排在了能力过滤之后 —— 应当先排除非商用机器')

    def test_gate_condition_is_compound(self):
        """闸条件必须是 `not need_model and not DIRECT_BASE` 这个**合取式**。

        ★ 断言形态为什么改（2026-09-22，本门自身的一次假红）：
          闸条件最初是 `if not need_model:`，后来加了直连豁免 → 变成
          `if not need_model and not DIRECT_BASE:`。
          但本门当时还在断言旧串 `'ifnotneed_model:'`（少了后半截），
          于是**源码正确、门却红了**。教训与顶部 `_raw_code` 那段同源：
          **断言要跟着契约走**，契约改了门必须同步，否则门红的是它自己。

        ★ 这条断言想守的语义（两个豁免，缺一不可）：
          · `not need_model`  —— 用户**显式点名**要哪个模型时，许可由他自己负责
            （他点名 Qwen 就是他知道自己在用 Qwen，平台不该再拦）
          · `not DIRECT_BASE` —— 直连模式下候选机只是连接参数载体，
            带着注册表首台（flux1，dev 非商用）的标签，见图示见 test_bypass_on_direct_mode
        """
        self.assertIn('ifnotneed_modelandnotDIRECT_BASE:', self.norm,
                      '闸条件不是 `not need_model and not DIRECT_BASE` —— '
                      '要么缺了自用豁免、要么缺了直连豁免')

    def test_gate_two_exemptions_present(self):
        """两个豁免必须同时存在（防止有人"简化"掉其中一个）。

        为什么单独再钉一条：`test_gate_condition_is_compound` 只能证明
        **合取式在**，证不了「两个都真的有豁免意图」。比如有人写成
        `if not (need_model and DIRECT_BASE):`（德摩根写反）语法上也是合取式，
        但语义完全反了。这条用行为验证两个方向。
        """
        mod = self._build_pick()
        # ① need_model 豁免：点名 Qwen 时，哪怕它 commercial_ok=False 也要能选中
        c = self._mk_cand('flux7_qwen', False)
        c[1]['models'] = ['Qwen-Image-2.1']
        s, _p = mod._pick_from([c], need_model='Qwen-Image-2.1')
        self.assertIsNotNone(s, 'need_model 豁免失效 —— 自用指定 Qwen 被拦')
        self.assertEqual(s['name'], 'flux7_qwen')
        # ② 不点名时闸仍生效（证明 ① 不是「闸压根没在用」导致的假通过）
        s2, _p2 = mod._pick_from([self._mk_cand('flux7_qwen', False)])
        self.assertIsNone(s2, '不指定 need_model 时非商用机器仍被选中 —— 闸没生效')

    def test_bypass_on_direct_mode(self):
        """直连模式必须跳过许可闸。

        ★ 为什么钉这条（2026-09-22 实测事故）：
          直连模式下候选机是 `fsm.SERVER_DEFAULT` —— 它只是**连接参数载体**，
          却带着注册表第一台（flux1，dev 非商用）的 `commercial_ok: false`。
          闸一开就把唯一那台"机器"排除 → 全部任务返回"无可用机器" → 超时。
          实测代价：`test_manager_edit_offline.py` 6 项变红、日志狂刷
          「许可闸：所有候选机的模型均不允许对外经营」。

          语义边界：直连是**用户显式指定后端**的场景（本地部署 / 已开隧道 /
          离线测试），许可合规由他自己负责，平台不该拿别处的标签拦他。
        """
        self.assertIn('ifnotneed_modelandnotDIRECT_BASE:', self.norm,
                      '许可闸没有排除直连模式 —— 直连场景会被误拦（实测回归）')

    # ── 第 2 组：行为验证（真跑 _pick_from）──
    def _build_pick(self, src_override=None):
        """加载 flux_resident_client 并取 _pick_from。

        ★ 为什么允许 skip 以外的路径都失败就判 FAIL（2026-09-22 教训）：
          最初这里用 `self.skipTest` 兜住加载失败，结果 6 项行为验证**全部静默
          跳过**，而输出仍然显示 OK —— 跟"通过"长得一模一样。
          被跳过的行为验证等于没有验证。所以现在：用真实模块路径加载
          （补 __file__ 让 BASE_DIR 能算出来），真的失败才 skip 并打印原因。
        """
        src = src_override if src_override is not None else self.src
        mod = types.ModuleType('frc_under_test')
        # 关键：模块顶部有 `BASE_DIR = Path(__file__).parent.parent`，
        # 不补 __file__ 会 NameError（第一次就是这么被 skip 掉的）。
        mod.__file__ = SRC_PATH
        # fsm / feishu_notify 是重依赖（会拉起 SSH/HTTP），用替身顶上。
        # _pick_from 只用到 logger 与 _filter_by_caps，不碰这两个。
        for name in ('manager.fsm', 'fsm'):
            sys.modules.setdefault(name, types.ModuleType(name))
        fake_fn = types.ModuleType('manager.feishu_notify')
        fake_fn._load_env = lambda: None
        fake_fn.notify = lambda *a, **k: None
        sys.modules['manager.feishu_notify'] = fake_fn
        fake_fsm = types.ModuleType('manager.flux_server_manager')
        fake_fsm.SERVER_DEFAULT = {}
        fake_fsm.FLUX_SERVERS = []
        fake_fsm.FLUX_SERVER_ALIAS_GLOB = 'autodl-flux*'
        fake_fsm.FLUX_SERVER_DISCOVER = False
        fake_fsm.probe_all = lambda force=False: []
        fake_fsm.probe_full = lambda s, force=False: {}
        fake_fsm.run = lambda *a, **k: (True, '')
        fake_fsm.ssh_target = lambda s: ''
        fake_fsm.scp_upload_cmd = lambda *a, **k: ''
        fake_fsm.gpu_ready = lambda s: (True, '')
        fake_fsm.model_ready = lambda s: True
        fake_fsm.ssh_config_aliases = lambda: []
        sys.modules['manager.flux_server_manager'] = fake_fsm
        # 确保 manager 包本身可导入（真实包，不是替身）
        import manager  # noqa: F401
        try:
            exec(compile(src, SRC_PATH, 'exec'), mod.__dict__)
        except Exception as e:
            self.fail(f'加载模块失败（不是依赖问题就该修）：{type(e).__name__}: {e}')
        self.assertTrue(hasattr(mod, '_pick_from'), '模块里没有 _pick_from')
        return mod

    def _mk_cand(self, name, commercial_ok, reachable=True, gpu_ok=True,
                 model_ok=True, resident=True, model_loaded=True):
        server = {'name': name, 'supports_edit': True}
        if commercial_ok is not None:
            server['commercial_ok'] = commercial_ok
        # probe 的字段要贴真实形态：_pick_from 里日志会读 p['name']
        # （第一次没给 → KeyError，被门抓到了，这正是门该干的事）
        probe = {'name': name, 'reachable': reachable, 'gpu_ok': gpu_ok,
                 'model_ok': model_ok, 'resident': resident,
                 'model_loaded': model_loaded, 'models': [], 'caps': {},
                 'capabilities_by_model': {}, 'error': None, 'gpu': 'test'}
        return (server, probe)

    def test_noncommercial_excluded(self):
        """commercial_ok=False 的机器在自动选机里出局。"""
        mod = self._build_pick()
        cands = [self._mk_cand('flux7_qwen', False),   # 非商用
                 self._mk_cand('flux5_klein', True)]   # 商用
        s, _p = mod._pick_from(cands)
        self.assertIsNotNone(s, '不该返回 None（有可商用的机器）')
        self.assertEqual(s['name'], 'flux5_klein',
                         '选到了非商用机器 —— 许可闸失效')

    def test_missing_field_kept(self):
        """缺 commercial_ok 字段的机器必须保留（向后兼容）。"""
        mod = self._build_pick()
        cands = [self._mk_cand('old_machine', None)]   # 缺字段
        s, _p = mod._pick_from(cands)
        self.assertIsNotNone(s, '缺字段的机器被剔掉了 —— 向后兼容被破坏')
        self.assertEqual(s['name'], 'old_machine')

    def test_need_model_bypasses(self):
        """need_model 显式指定时绕过许可闸（自用指定 Qwen 必须可行）。"""
        mod = self._build_pick()
        c = self._mk_cand('flux7_qwen', False)
        c[1]['models'] = ['Qwen-Image-2.1']
        s, _p = mod._pick_from([c], need_model='Qwen-Image-2.1')
        self.assertIsNotNone(s, 'need_model 指定 Qwen 时不该被许可闸拦掉')
        self.assertEqual(s['name'], 'flux7_qwen')

    def test_all_noncommercial_returns_none(self):
        """全都不许商用 → 返回 None，不硬挑。"""
        mod = self._build_pick()
        cands = [self._mk_cand('flux7_qwen', False),
                 self._mk_cand('flux1_dev', False)]
        s, _p = mod._pick_from(cands)
        self.assertIsNone(s, '全是非商用机器时不该硬挑一台')

    # ── 第 3 组：变异测试 ──
    #
    # 变异在 **去注释后的源码** 上做（self.code），因为注释里含同样的锚点串。
    # 用正则做替换，容忍 untokenize 补出来的空格 —— 否则锚点匹配不上，
    # 变异静默失效，门会假装"通过"（正是本门要防的事）。
    GATE_ANCHOR = re.compile(r'if\s+not\s+need_model\s+and\s+not\s+DIRECT_BASE\s*:')
    GATE_COND = re.compile(r"if\s+c\s*\[\s*0\s*\]\s*\.\s*get\s*\(\s*'commercial_ok'\s*\)\s*is\s+not\s+False")

    def _mutate_disable_gate(self, code):
        """把 `if not need_model:` 改成 `if False:`（闸短路）。"""
        new, n = self.GATE_ANCHOR.subn('if False:', code, count=1)
        return new, n

    def _mutate_truthy_check(self, code):
        """把 `is not False` 去掉 → 变成真值判断（会误剔缺字段机器）。"""
        new, n = self.GATE_COND.subn("if c[0].get('commercial_ok')", code, count=1)
        return new, n

    def test_mutation_removing_gate_turns_red(self):
        """把许可闸短路 → 非商用机器会被选中（证明前面的断言真的在测行为）。"""
        broken, n = self._mutate_disable_gate(self.code)
        self.assertEqual(n, 1, f'变异锚点未匹配（命中 {n} 处）—— 需同步更新本门')
        mod_ok = self._build_pick()
        mod_broken = self._build_pick(src_override=broken)
        cands = [self._mk_cand('flux7_qwen', False),
                 self._mk_cand('flux5_klein', True)]
        s_ok, _ = mod_ok._pick_from(cands)
        s_bad, _ = mod_broken._pick_from(cands)
        self.assertEqual(s_ok['name'], 'flux5_klein', '正常源码应选商用机')
        self.assertEqual(s_bad['name'], 'flux7_qwen',
                         '变异后行为未变 —— 本门的断言没有真的在测许可闸')

    def test_mutation_truthy_check_turns_red(self):
        """把判据改成真值判断（会误剔缺字段的机器）→ 缺字段用例应变红。"""
        broken, n = self._mutate_truthy_check(self.code)
        self.assertEqual(n, 1, f'变异锚点未匹配（命中 {n} 处）—— 需同步更新本门')
        mod = self._build_pick()
        mod_broken = self._build_pick(src_override=broken)
        cands = [self._mk_cand('old_machine', None)]
        s_ok, _ = mod._pick_from(cands)
        s_bad, _ = mod_broken._pick_from(cands)
        self.assertIsNotNone(s_ok, '正常源码应保留缺字段机器')
        self.assertIsNone(s_bad, '变异后缺字段机器未被剔 —— 判据断言没测到真东西')

    # ── 第 4 组：声明侧仍然正确（与能力门呼应，防止只改一边）──
    def test_registry_still_declares_qwen_noncommercial(self):
        """servers.json 里 Qwen 机仍标 commercial_ok=False。"""
        import json
        p = os.path.join(ROOT, 'manager', 'servers.json')
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
        qwen = [s for s in data['servers']
                if 'qwen' in json.dumps(s, ensure_ascii=False).lower()]
        self.assertTrue(qwen, '注册表里找不到 Qwen 机器')
        for s in qwen:
            self.assertIs(s.get('commercial_ok'), False,
                          f"{s['name']} 的 commercial_ok 不是 False —— 非商用模型被放开了")


if __name__ == '__main__':
    unittest.main(verbosity=2)

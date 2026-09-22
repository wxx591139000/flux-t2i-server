#!/usr/bin/env python3
"""Qwen 环境自证：证明「装成功了」而不只是「pip 说 Successfully installed」。

为什么单独写一个：pip 的成功输出**不等于**能 import、更不等于管线类存在。
真正要答的问题有四个：
  0. **环境安装是不是还在跑？**（2026-09-22 新增，因为我自己在这里栽过）
     —— 只看一次 `import` 的结果会把「装到一半的中间态」当成终态。
        实际事故：diffusers 正在从 0.40.0 覆盖到 0.41.0.dev0，
        我在覆盖过程中跑本脚本 → `QwenImage21Pipeline` 报 False →
        得出「环境有问题」的错误结论。**真因是安装还没跑完。**
     判据：`/proc/<pid>` 是否存在 + 安装日志尾部是否出现完成标记。
  1. 关键包 import 得动吗？（版本对不对）
  2. **QwenImage21Pipeline 这个类真的在 diffusers 里吗**？
     —— 这是「能不能跑 Qwen」的唯一硬判据；diffusers 版本够不代表类就有
  3. 它的 `__call__` 签名是否真的接受 `true_cfg_scale`？
     —— 平台能力表就是按这个声明的，对不上会真机 TypeError。
     ⚠️ **不要**要求签名里有 `mask`（2026-09-22 实测修正）：
        Qwen-Image-2.1 的 23 个参数里**没有 mask**，它的局部编辑是语义级的
        （把圈选/涂抹合成进参考图）。原先这里断言 mask 存在，是照 README
        推断的，与真实签名不符 —— 已在实测后改掉。

⚠️ 本脚本**只做只读检查**，不装包、不改配置。
"""
import inspect
import os
import sys

ok = True


def chk(name, cond, detail=''):
    global ok
    if not cond:
        ok = False
    print(f'  {"OK  " if cond else "FAIL"} {name}' + (f'  —— {detail}' if detail else ''))


# ── [0] 前置：安装进程是否还在跑（先于一切 import 检查）──────────
#    为什么放在最前面：进程还在跑时，下面任何 import 结论都**不可信**。
print('=== [0] 前置：环境安装是否已完成 ===')
_installing = []
try:
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmd = f.read().decode('utf-8', 'replace').replace('\x00', ' ').strip()
        except OSError:
            continue
        # 只认「安装脚本/下载脚本本身」，不认本脚本与 pytest 之类的旁观者
        if ('setup_qwen_env' in cmd or 'dl_qwen' in cmd
                or ('pip' in cmd and 'install' in cmd)):
            _installing.append((pid, cmd[:110]))
except OSError:
    print('  （非 Linux 或读不到 /proc，跳过本项 —— 不算失败）')

if _installing:
    for pid, cmd in _installing:
        print(f'  ⏳ pid={pid} 仍在运行: {cmd}')
    chk('★ 环境安装已结束（结论可信）', False,
        '安装**仍在进行** → 下面的 import 结果不可信，请等它结束后重跑本脚本。'
        '这正是 2026-09-22 我误判「环境有问题」的真因')
else:
    chk('★ 环境安装已结束（结论可信）', True)

print('\n=== [1] 关键包 ===')
pkg = {}
try:
    import torch
    pkg['torch'] = torch.__version__
except Exception as e:                       # noqa: BLE001
    chk('import torch', False, repr(e))
try:
    import diffusers
    pkg['diffusers'] = diffusers.__version__
except Exception as e:                       # noqa: BLE001
    chk('import diffusers', False, repr(e))
try:
    import transformers
    pkg['transformers'] = transformers.__version__
except Exception as e:                       # noqa: BLE001
    chk('import transformers', False, repr(e))

for k, v in pkg.items():
    print(f'  {k:14s} {v}')

torch = sys.modules.get('torch')
diffusers = sys.modules.get('diffusers')
chk('torch 可导入', torch is not None)
chk('diffusers 可导入', diffusers is not None)
chk('transformers 可导入', sys.modules.get('transformers') is not None)

if torch:
    # ⚠️ 无卡模式下 is_available()=False 是**预期**的，不是失败。
    #    这里只作信息打印，不参与 ok 判定 —— 否则无卡期永远红。
    print(f'  torch.cuda.is_available() = {torch.cuda.is_available()}'
          f'（无卡模式为 False 属预期）')

print('\n=== [2] 管线类存在性（硬判据）===')
qwen_cls = None
if diffusers:
    qwen_cls = getattr(diffusers, 'QwenImage21Pipeline', None)
    chk('diffusers.QwenImage21Pipeline 存在', qwen_cls is not None,
        '这是「能不能跑 Qwen」的唯一硬判据；版本够不代表类就有')
    chk('diffusers.Flux2KleinPipeline 仍在（没被降级破坏）',
        getattr(diffusers, 'Flux2KleinPipeline', None) is not None,
        '升级 diffusers 的主要风险就是打坏已有的 klein')

print('\n=== [3] __call__ 签名 vs 平台能力表 ===')
if qwen_cls is not None:
    params = set(inspect.signature(qwen_cls.__call__).parameters)
    chk('接受 true_cfg_scale（平台 cfg_param 声明）', 'true_cfg_scale' in params)
    chk('接受 image（可单张可 list）', 'image' in params)
    chk('接受 negative_prompt', 'negative_prompt' in params)
    # ★ 2026-09-22 实测修正：**不**要求 mask 存在，而是要求它**不存在**。
    #   能力表已把字段改名为 mask_param 并标 False（Qwen 局部编辑走合成图）。
    #   若将来 diffusers 真给 Qwen 加了 mask 入参，这条会变红 —— 那是**好事**，
    #   说明能力表该更新了，而不是默默放着让两处口径漂移。
    chk('★ **不**接受 mask（实测确认；能力表 mask_param=False）',
        'mask' not in params,
        '若这里为 False（即真有 mask），说明 diffusers 变了 → 能力表要跟着更新')
    chk('**不**接受 guidance_scale（FLUX 的叫法）', 'guidance_scale' not in params,
        '若它也有这参数，说明平台选名策略要重新评估')
    print(f'  签名参数共 {len(params)} 个：{sorted(params)}')
    # 与平台能力表逐项核对（把两边钉在一起，避免只测「类存在」）
    # ⚠️ 本脚本在**两处**运行，`flux_resident_server.py` 的位置不同：
    #    · 仓库里：  <repo>/tools/verify_qwen_env.py → <repo>/server/flux_resident_server.py
    #    · GPU 机上：/root/autodl-tmp/verify_qwen_env.py
    #                → 模块就在 cwd（=/root/autodl-tmp/flux-t2i）下
    #    只按第一种推路径 → 第二种必然 ModuleNotFoundError（我踩过一次）。
    #    所以两个候选都试，全失败才判 FAIL。
    _here = os.path.dirname(os.path.abspath(__file__))
    _cands = [
        os.path.join(os.path.dirname(_here), 'server'),   # 仓库形态
        os.getcwd(),                                       # 与模块同目录
        _here,
    ]
    _loaded = False
    for _p in _cands:
        if os.path.exists(os.path.join(_p, 'flux_resident_server.py')):
            sys.path.insert(0, _p)
            try:
                import flux_resident_server as _fr  # noqa: E402,F811
                _loaded = True
                break
            except Exception:                          # noqa: BLE001
                sys.path.pop(0)
    if not _loaded:
        chk('能加载平台能力表做交叉核对', False,
            f'在 {_cands} 都没找到 flux_resident_server.py '
            f'（先 scp 到机器上再跑，否则只能验环境、验不了口径一致性）')
    else:
        caps = _fr.model_capabilities('QwenImage21Pipeline')
        chk('平台能力表 ref_param 与真实签名一致',
            caps.get('ref_param') in params, f"表={caps.get('ref_param')!r}")
        chk('平台能力表 cfg_param 与真实签名一致',
            caps.get('cfg_param') in params, f"表={caps.get('cfg_param')!r}")
        chk('★ 平台能力表 mask_param 与真实签名一致',
            bool(caps.get('mask_param')) == ('mask' in params),
            f"表={caps.get('mask_param')!r} 签名={'mask' in params}")

print('\n=== 结论 ===')
print('  环境可用 ✅' if ok else '  环境有问题 ❌')
sys.exit(0 if ok else 1)

"""一次性脚本：给 servers.json 加 Qwen 机（flux7）并把 extra_models 补上。

为什么用脚本而不是手改 JSON：
  手改容易漏字段 / 破坏中文编码；脚本可复跑、可校验、可留痕。
  （改完这个脚本本身没用了，但留着能说明「当时改了什么」。）

★ 设计要点（与代码里的注释一致）：
  remote_model  = 该机**默认**模型（不变的老字段，兼容所有老逻辑）
  extra_models  = 该机**额外**还装了哪些（2026-09-22 新增）
  supports_edit = 给人看的冗余提示；能力的**权威来源**是 resident 的
                  /health.capabilities 与 /models[].capabilities。
                  保留它是为了「探针拿不到能力时」有兜底，不是为了替代能力表。
"""
import json
import pathlib
import sys

P = pathlib.Path(__file__).with_name('servers.json')
d = json.loads(P.read_text(encoding='utf-8'))
by = {s['name']: s for s in d['servers']}

# ── 1. 给现有机器补 extra_models（显式声明「这台机装了哪些模型」）──
#    以前只靠 remote_model 一个路径，一台机上的第二个模型对平台完全不可见。
if 'flux1' in by:
    by['flux1']['extra_models'] = []
    by['flux1'].setdefault('commercial_ok', False)   # dev 许可：FLUX.1-dev 非商用

for n in ('flux5', 'flux6'):
    if n in by:
        by[n]['extra_models'] = []
        # klein 4B = Apache 2.0，可商用 —— 这是唯一能用于对外经营的本地模型
        by[n]['commercial_ok'] = True

# 已停用机器也补齐字段：**字段缺失与「明确写 false」不是一回事**。
# 缺失会在下游 .get() 时静默变 None（既不是「是」也不是「否」），
# 而这种模糊值最容易在某个分支里被当成 True 用掉。
for n in ('flux2', 'flux3', 'flux4'):
    if n in by:
        by[n].setdefault('extra_models', [])
        # flux2/3 = dev（非商用）；flux4 = klein（可商用），但已退役
        by[n].setdefault('commercial_ok', n == 'flux4')

# ── 2. 新增 Qwen 机 ──
#    ⚠️ 这是**新开的克隆实例**，地址 westb/weste 要按实际填。
#    用户给的是 -p 13889 root@connect.weste.seetacloud.com
QWEN_NAME = 'flux7'
qwen = {
    'name': QWEN_NAME,
    'host': 'connect.weste.seetacloud.com',
    'port': 13889,
    'user': 'root',
    'identity_file': '~/.ssh/id_rsa_musetalk',
    'remote_base': '/root/autodl-tmp/flux-t2i',
    'remote_model': '/root/autodl-tmp/qwen-models/Qwen-Image-2.1',
    'extra_models': [],
    'offload': 'none',
    'supports_edit': True,
    # ★ commercial_ok：本地模型的**许可**是否允许对外经营。
    #   Qwen-Image-2.1 = Qwen Research License（非商用，商用需单独授权）→ False。
    #   这是**硬约束**，不是质量偏好：B 链是面向客户的生图服务，用它接单有法律风险。
    #   详见 docs/模型选型评估.md。
    'commercial_ok': False,
    'license': 'Qwen Research License（非商用；商用需单独授权）',
    'note': (
        'Qwen-Image-2.1 7B1（2026-09-22 新开，由 klein 实例克隆）。'
        '⚠️ 许可=Qwen Research License **非商用** → 仅限自用/内部验证，**不可**用于对外经营的 B 链订单。'
        '｜显存：bf16 约 30-35GB（7B 主干 14.2GB + Qwen3-VL 8B text_encoder ~16GB + RGBA VAE），'
        '32G 卡**吃紧**且**不能与 klein 同机**（两者同时驻留必 OOM）→ offload 按实测调。'
        '｜环境：必须独立 conda 环境 qwen（diffusers 需 ≥0.37 dev，含 QwenImage21Pipeline）；'
        '⚠️ 不能复用 flux 环境（那会把 klein 用的 diffusers 0.39.0 降级 → 破坏 klein）。'
        '｜能力：text2img + edit(多图最多10张) + 透明(RGBA)，原生 2K；'
        '⚠️ **无独立 mask 入参**（局部编辑走语义级：把圈选/涂抹合成进参考图）；'
        'CFG 参数名是 true_cfg_scale（不是 guidance_scale）；⚠️ 负向提示词需 CFG>1 才生效。'
        '｜权重路径在**数据盘** /root/autodl-tmp（系统盘只有 30G，装不下 33G 权重）。'
        '｜本条目的 host/port 是**无卡期**地址，切带卡后需更新。'
    ),
    'enabled': True,
}

# 幂等：已存在就更新（不重复追加）
if QWEN_NAME in by:
    by[QWEN_NAME].update(qwen)
    print(f'[i] {QWEN_NAME} 已存在 → 已更新')
else:
    d['servers'].append(qwen)
    print(f'[+] 新增 {QWEN_NAME}')

# ── 3. 校验：能力/许可字段齐全 ──
problems = []
for s in d['servers']:
    if 'extra_models' not in s:
        problems.append(f"{s['name']}: 缺 extra_models")
    if 'commercial_ok' not in s:
        problems.append(f"{s['name']}: 缺 commercial_ok（对外经营前必须明确）")
    if s.get('supports_edit') and s.get('remote_model', '').endswith('FLUX.1-dev'):
        problems.append(f"{s['name']}: 声明 supports_edit 但模型是 FLUX.1-dev（自相矛盾）")

P.write_text(json.dumps(d, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(f'[✓] 已写入 {P}')

if problems:
    print('[!] 校验发现问题：')
    for x in problems:
        print('   -', x)
    sys.exit(1)
print('[✓] 校验通过')

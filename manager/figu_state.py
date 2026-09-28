#!/usr/bin/env python3
"""飞书「图图」参数状态机 —— **纯逻辑、无 IO**（设计方案 §9 的 `figu_state.py`）。

为什么单独一个模块，而不是塞进 `feishu_bot.py`：
  「这条文本算不算对上一次提问的回答」是本功能里**最容易出错、也最值得单测**的一段判据，
  而 `feishu_bot.py` 一被 import 就会拉起 lark / 队列 / DB 一串依赖。
  抽成纯函数后，`tests/test_figu_params.py` 不碰任何真发送通道就能把判据钉死。

★ 会话 key 是**三元组** `(channel, chat_id, sender_id)`，不是二元组。
  小白（transcribe-bot）用的是 `(channel, sender_id)`，于是「群里 A 的待选项被 B 的回复
  消费」只能靠「额外跨会话校验 + 上传冷却」两处补丁兜住（orchestrator.py:380-383 / :470）。
  我们把 chat_id 直接编进 key —— 那个补丁就变成模型本身的一部分（设计方案 §2.2 决策 2）。

⚠️ 这个模块里的判据**必须严格**：图图的主用法是"发一句话出图"，
   判松了就会吞掉正常提示词（详见 `feishu_bot._match_command` 的注释）。
"""
import re

CHANNEL_FEISHU = 'feishu'

PENDING_TTL = 300      # 待选状态存活秒数（对齐小白 orchestrator 的 300s）
UPLOAD_COOLDOWN = 5    # 上传图片后 N 秒内的文本不当作"选择"（设计方案 §3.3 守卫 1；P3 用）

# ── 比例（设计方案 §3.4：/size 1:1|16:9|9:16|4:3|3:4）────────────────────────
#
# 像素值必须同时满足两个硬约束，否则会被上游 400 拒掉：
#   ① **16 的倍数**（FLUX 要求，见 flux_resident_server 的归一逻辑）
#   ② 落在 256~2048 之间（服务端硬边界）
# 16:9 取 1280×720 而不是 1024×576：前者是**真机实测跑通过的尺寸**
# （PITFALLS「model 档实测可用（单张 1280×720 推理 54s）」），不自己发明新档。
SIZE_OPTIONS = (
    ('1:1', 1024, 1024),
    ('16:9', 1280, 720),
    ('9:16', 720, 1280),
    ('4:3', 1024, 768),
    ('3:4', 768, 1024),
)
SIZE_LABELS = tuple(o[0] for o in SIZE_OPTIONS)
SIZE_MAP = {o[0]: (o[1], o[2]) for o in SIZE_OPTIONS}

# ── 张数（/n 1..4）──────────────────────────────────────────────────────────
COUNT_MAX = 4
COUNT_OPTIONS = tuple(str(i) for i in range(1, COUNT_MAX + 1))

# ── 模型（设计方案 §3.4：/model klein|dev|qwen）─────────────────────────────
#
# 值是 GPU 机上的**目录名**（= 跨层稳定标识 `model id`，见
# `flux_resident_server.scan_models` 的说明：id 写进 jobs.model → 透传到
# /generate 的 model 参数 → 服务端按 id 反查路径）。
# ⚠️ 这三个 id 是**一手核实**的目录名（`servers.json` / `_add_qwen_server.py` /
#    `模型选型评估.md`），不是猜的。别名→id 的映射**只此一处**。
MODEL_ALIASES = {
    'klein': 'FLUX.2-klein-4B',     # /root/klein-models/FLUX.2-klein-4B
    'dev':   'FLUX.1-dev',          # /root/autodl-tmp/models/FLUX.1-dev
    'qwen':  'Qwen-Image-2.1',      # /root/autodl-tmp/qwen-models/Qwen-Image-2.1
}
MODEL_ALIAS_LABELS = tuple(MODEL_ALIASES.keys())

# 允许直接写完整 model id（如 `FLUX.2-klein-4B`），但**必须有模型关键字**，
# 免得「模型 关系」这种正常中文被当成命令（中文本来就过不了这个正则）。
_MODEL_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{2,63}$')
_MODEL_ID_HINT = re.compile(r'(klein|dev|qwen|flux)', re.IGNORECASE)

# 激活码：8 位、去混淆字符集（flux_db._CODE_ALPHABET）。这里放宽到 6~16 位
# 数字字母，是为了容忍用户多打/少打几个字符时给出"激活码无效"这种**正常提示**，
# 而不是把它当提示词去出一张废图。
_CODE_RE = re.compile(r'^[A-Za-z0-9]{6,16}$')


def session_key(chat_id: str, sender_id: str, channel: str = CHANNEL_FEISHU) -> tuple:
    """会话三元组。缺 chat_id 时退化为 `'p2p:<open_id>'` ——

    仍要保证"两个不同的会话"能区分开：飞书 p2p 消息本该带 chat_id，
    但真出现空值时，用 open_id 兜底也比退化成二元组安全（不会跨用户串味）。
    """
    cid = (chat_id or '').strip() or f'p2p:{sender_id or ""}'
    return (channel or CHANNEL_FEISHU, cid, sender_id or '')


def size_to_wh(label: str):
    """比例标签 → (width, height)；未知返回 (None, None)（= 用服务端默认）。"""
    return SIZE_MAP.get((label or '').strip(), (None, None))


def parse_size(arg: str):
    """`16:9` → `'16:9'`；其余一律 None。

    ⚠️ 必须**整体匹配**：`比例 16:9 的画面` 里的 `16:9 的画面` 不是比例 →
      返回 None → 回落为提示词。若只取第一个 token，就会把「的画面」吃掉。
    """
    a = (arg or '').strip()
    return a if a in SIZE_MAP else None


def parse_count(arg: str):
    """`2` → `'2'`（1..4）；其余 None。"""
    a = (arg or '').strip()
    if a.isdigit() and 1 <= int(a) <= COUNT_MAX:
        return a
    return None


def parse_model_arg(arg: str):
    """别名或完整 id → **model id**；其余 None。

    返回 id（不是别名），因为 id 才是要写进 `jobs.model` 的东西，
    别名的解析只在这里发生一次。
    """
    a = (arg or '').strip()
    if not a:
        return None
    if a.lower() in MODEL_ALIASES:
        return MODEL_ALIASES[a.lower()]
    if _MODEL_ID_RE.match(a) and _MODEL_ID_HINT.search(a):
        return a
    return None


def model_label(model_id: str) -> str:
    """model id → 回显用的短名（别名优先，便于用户复述）。"""
    for alias, mid in MODEL_ALIASES.items():
        if mid == model_id:
            return alias
    return model_id or '默认'


def parse_code(arg: str):
    """激活码候选 → 大写码；不像激活码则 None（回落为提示词）。"""
    a = (arg or '').strip()
    if _CODE_RE.match(a):
        return a.upper()
    return None


def is_slash_command(text: str) -> bool:
    """以 `/` 开头 = 命令写法（命令**优先放行**，不消费待选状态）。"""
    return bool((text or '').lstrip().startswith('/'))


def should_consume(text: str, *, now: float = None, uploaded_at: float = None,
                   cooldown: int = UPLOAD_COOLDOWN):
    """一条文本能不能用来消费待选状态？返回 `(ok, reason)`。

    守卫（设计方案 §3.3，照抄小白的教训，一条都不能省）：
      1. **空文本**不消费 —— 飞书偶尔会推空 content。
      2. **`/` 开头不消费** —— 命令优先放行；否则「/help」会被当成"选择第 1 项"。
      3. **上传图片后的冷却期内不消费** —— 飞书会把「图片 + 附言」拆成两条事件，
         附言（正常提示词）不该被当成"选 2"。P3 接图片后才真正生效，
         但守卫现在就立住（写晚了必然忘）。
      4. **超时**不消费 —— 由 DB 侧 `expires_at` 负责（这里不重复判）。
    """
    if not (text or '').strip():
        return False, 'empty'
    if is_slash_command(text):
        return False, 'slash'
    if uploaded_at is not None and now is not None and (now - uploaded_at) < cooldown:
        return False, 'cooldown'
    return True, ''


def resolve_choice(text: str, options):
    """把用户回复解析成 `options` 里的下标（0-based）；不命中返回 None。

    接受两种写法，都属于**完全匹配**（不做模糊）：
      · 纯数字序号：`2` → 第 2 项（1-based，超出范围不算命中）
      · 选项本身的标签：`16:9` → 命中标签为 `16:9` 的那一项
    把标签写法也收进来，是因为用户看到列表后往往会直接回标签而不是回序号。
    """
    t = (text or '').strip()
    if not t:
        return None
    if t.isdigit():
        i = int(t)
        if 1 <= i <= len(options):
            return i - 1
        return None
    for idx, label in enumerate(options):
        if t == str(label):
            return idx
    return None


# ── 提交前确认（2026-09-28 用户需求）──────────────────────────────────────
#
# 背景：图图的主用法是「发一句话出图」，但用户发来的消息**并不都是生图提示词**
#   —— 可能是随口一句、可能是打错字、可能本来想发别的。原行为是「凡不是命令的
#   文本一律提交出图」，一次误发就**真扣一次额度、出一张毫不相干的图**。
# 现在：非命令文本先回一条确认（「要把这句提交为生图任务吗」），用户明确回「是」
#   才建任务；回「否」放弃；不回（或说别的）则作废重来。
#
# ⚠️ 确认词必须**完全匹配**（strip + lower 后全等），绝不做子串 / 包含 / 前缀判断。
#    判松了就会吞掉正常提示词 ——「好可爱的一只猫」不该被当成「好」，
#    「不是这只猫」也不该被当成「不」。这与 `feishu_bot._match_command` 的严格
#    判据同一个道理：确认词与提示词**共用同一条输入通道**，只能靠全等区分。
CONFIRM_KIND = 'confirm_submit'

# 全部小写存放（比较前把输入也 lower，于是 `OK` / `Ok` / `ok` 一律命中）
CONFIRM_YES = (
    '是', '是的', '对', '对的', '嗯', '确认', '确定', '好', '好的', '可以',
    '提交', '出图', '开始', 'ok', 'okay', 'yes', 'y',
)
CONFIRM_NO = (
    '否', '不是', '不对', '不', '不用', '不要', '算了', '放弃', '取消',
    'no', 'n',
)


def parse_confirm(text: str):
    """把一条文本解析成确认意图：`True`=确认 / `False`=放弃 / `None`=都不是。

    只认**全等**：`是` 是确认，`是个猫` 不是（返回 None → 走正常提示词流程）。
    `取消` 也在放弃词里，但实际到不了这里 —— `取消` 会更早被命令判据接走
    （命令优先，见 `feishu_bot._handle_prompt` ①）；保留它只为双保险。
    """
    t = (text or '').strip().lower()
    if not t:
        return None
    if t in CONFIRM_YES:
        return True
    if t in CONFIRM_NO:
        return False
    return None

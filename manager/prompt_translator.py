#!/usr/bin/env python3
"""
manager/prompt_translator.py — 中文提示词 → FLUX 友好英文提示词翻译器

借鉴短剧项目 short-drama-pipeline/scripts/prompt_optimizer.py 的 FLUX 提示词方法论：
  - 描述角色静态位置（运动前）
  - 含镜头角度、光线、构图、角色位置、环境
  - 只用英文
  - 30-80 词最佳（对 FLUX）
  - 不含运动（FLUX 生成静态图）
  - 加技术质量标记（cinematic lighting / photorealistic / professional）

不复制短剧代码，仅提炼其 System Prompt 方法论 + 精简版 LLM 调用（火山方舟 Ark/Anthropic API）。

用法:
  from manager.prompt_translator import translate_to_flux_prompt
  en = translate_to_flux_prompt("一个人跟一只黑毛狗在田边打架，狗在咬人")
"""
import os
import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger('manager.prompt_translator')

# LLM 配置（复用 settings.json env，与短剧项目同源，不硬编码）
# 短剧项目 pipeline.py 的调用方式：{BASE}/v3/chat/completions（OpenAI 兼容格式）
import json as _json
import os as _os

_API_SETTINGS = None
_settings_path = _os.path.expanduser('~/.claude/settings.json')
if _os.path.isfile(_settings_path):
    try:
        with open(_settings_path, encoding='utf-8') as _f:
            _API_SETTINGS = _json.load(_f).get('env', {})
    except Exception:
        _API_SETTINGS = {}
_API_SETTINGS = _API_SETTINGS or {}

API_KEY = _API_SETTINGS.get('ANTHROPIC_AUTH_TOKEN', '') or os.environ.get('ANTHROPIC_AUTH_TOKEN', '')
API_BASE = _API_SETTINGS.get('ANTHROPIC_BASE_URL', '') or os.environ.get('ANTHROPIC_BASE_URL', '')
MODEL = _API_SETTINGS.get('ANTHROPIC_MODEL', '') or os.environ.get('ANTHROPIC_MODEL', 'deepseek-v4-flash-260425')

# 是否有中文字符
_CHINESE_RE = __import__('re').compile(r'[一-鿿]')


def has_chinese(text: str) -> bool:
    """判断文本是否含中文字符"""
    return bool(_CHINESE_RE.search(text or ''))


# ===========================================================================
# FLUX 提示词方法论（借鉴短剧 prompt_optimizer.py 的 FLUX_SYSTEM_PROMPT）
# ===========================================================================
FLUX_SYSTEM_PROMPT = """You are an expert prompt translator for the FLUX.1-dev image generation model.
Translate the user's Chinese description into an English static-scene image prompt.

Rules:
1. Describe the scene in STATIC positions (no motion)
2. Include: subject, camera angle, lighting, composition, environment
3. English only
4. 30-80 words is optimal for FLUX
5. Do NOT describe motion — Flux generates a static image
6. Add technical quality markers like "cinematic lighting", "photorealistic", "professional"
7. TRANSLATE FAITHFULLY — never invent entities the user did not mention.
   If the user asks for a mug, do not add coffee, a wooden table or a cafe interior.
   You may add only visual qualifiers (lighting, camera angle, material, composition),
   never new objects or settings.
   (实测：旧版把「马克杯」自行译出 filled with fresh coffee / rustic wooden table / upscale cafe)
8. Use POSITIVE PHRASING ONLY. FLUX.1-dev is guidance-distilled and ignores negative
   prompts, so "no shadows" / "without text" have no effect whatever and can push the
   result the wrong way. Restate the intent as what you DO want — write
   "evenly lit seamless white backdrop, clean uncluttered surface" instead of
   "no shadows, no clutter, no reflections".
   (实测：negative_prompt 与不加它在同一 seed 下出的图完全一致)

Make explicit everything the user wants in the image (all people, animals, objects, actions).
If the user describes a dynamic action (e.g. fighting, running), capture the frozen pose of that action.

Output ONLY the English prompt. No explanations."""


def call_llm(system_prompt: str, user_message: str, temperature: float = 0.7) -> str:
    """
    调用 LLM（火山方舟 / OpenAI 兼容 /chat/completions 格式，对齐短剧项目 pipeline.py）。
    成功返回文本，失败返回空串。
    """
    if not API_KEY:
        logger.warning('⚠️ 未设置 ANTHROPIC_AUTH_TOKEN，无法翻译')
        return ''

    url = f"{API_BASE.rstrip('/')}/v3/chat/completions"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {API_KEY}',
    }
    body = {
        'model': MODEL,
        'max_tokens': 1024,
        'temperature': temperature,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_message},
        ],
    }

    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        text = result['choices'][0]['message']['content']
        return text.strip().strip('"')
    except Exception as e:
        logger.error(f'LLM 调用失败: {e}')
        return ''


class TranslationError(RuntimeError):
    """中文提示词翻译失败（LLM 重试后仍不可用）。

    刻意做成异常而不是静默降级：FLUX 吃中文会**完全跑偏**（见下），
    让调用方拿到一个能上报的错误，好过让客户拿到一张莫名其妙的图。
    """


def translate_to_flux_prompt(zh_prompt: str, retries: int = 3) -> str:
    """
    中文提示词 → FLUX 英文提示词。失败重试 retries 次，仍失败抛 TranslationError。

    ⚠️ 为什么不能像旧版那样「LLM 失败就返回中文原文」：
    FLUX.1-dev 是双**英文**编码器（CLIP-L + T5-XXL），中文进去等于让它猜。
    实测同一句「一只白色陶瓷马克杯，置于无缝纯白影棚背景…」（同 seed 12345）：
      - 直发中文   → 出的是**动漫少女插画**，主体根本不是杯子；PNG 从 300KB 涨到 1.04MB
      - 过翻译层   → 干净的白色马克杯电商图
    旧兜底把这种灾难静默化了，客户只会觉得「这 AI 出图不行」。
    且实测 LLM 会偶发空返回（同一句连打 5 次里出现过 1 次），所以必须重试。
    """
    zh_prompt = (zh_prompt or '').strip()
    if not zh_prompt:
        return zh_prompt
    if not has_chinese(zh_prompt):
        return zh_prompt  # 纯英文不转换

    logger.info(f'🌐 中文提示词 → FLUX 英文提示词: {zh_prompt[:40]}...')
    user_msg = f"Translate this Chinese description into a FLUX image prompt:\n{zh_prompt}"
    for attempt in range(1, max(1, retries) + 1):
        result = call_llm(FLUX_SYSTEM_PROMPT, user_msg, temperature=0.8)
        # 仍含中文 = 这次没翻干净，同样不算成功
        if result and not has_chinese(result):
            tag = f'（第 {attempt} 次）' if attempt > 1 else ''
            logger.info(f'✅ 翻译完成{tag}: {result[:60]}...')
            return result
        logger.warning(f'⚠️ 第 {attempt}/{retries} 次翻译失败（LLM 无返回或仍含中文）')
    raise TranslationError(f'中文提示词翻译失败（已重试 {retries} 次）: {zh_prompt[:60]}')


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    test = sys.argv[1] if len(sys.argv) > 1 else '一个人跟一只黑毛狗在田边打架，狗在咬人，人也在咬狗'
    print(f'输入: {test}')
    print(f'输出: {translate_to_flux_prompt(test)}')
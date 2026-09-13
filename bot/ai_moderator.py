import json
import logging
import re
from typing import List, Dict, Any, Optional
from google import genai
from google.genai import types
from bot.config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

_client = None

# Список моделей в порядке предпочтения
MODELS_TO_TRY = ['gemini-3.6-flash', 'gemini-3.5-flash-lite', 'gemini-flash-latest']


def _get_client() -> Optional[genai.Client]:
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            logger.warning("GEMINI_API_KEY is not set — AI features disabled.")
            return None
        try:
            _client = genai.Client(api_key=GEMINI_API_KEY)
        except Exception as e:
            logger.error("Failed to initialize Gemini Client: %s", e)
            return None
    return _client


SYSTEM_MODERATION_PROMPT = """Ты — интеллектуальный модератор русскоязычного чата Telegram.
Твоя задача — объективно оценить сообщение участника на нарушения правил.

Категории нарушений:
1. "profanity" — явный или скрытый русский мат, грубые нецензурные ругательства, обсценная лексика.
2. "toxicity" — прямые оскорбления собеседников, унижения, агрессия, буллинг, пожелания вреда.
3. "spam" — спам, повторяющиеся бессмысленные сообщения, флуд символами, реклама казино/крипты/скама.
4. "ad" — несанкционированные ссылки на сторонние Telegram-каналы, группы, сайты, продажу товаров или услуг.
5. "none" — обычное нормальное общение, юмор, вопросы, обсуждения без мата и оскорблений.

Формат ответа СТРОГО в виде JSON без markdown-кавычек:
{"violation": true, "reason": "profanity"}
или
{"violation": false, "reason": "none"}
"""

SYSTEM_CHAT_PROMPT = """Ты — «Ботик», дружелюбный, умный и остроумный маскот и модератор супергруппы в Telegram.
Ты общаешься с участниками чата, помогаешь им освоиться, объясняешь правила, шутишь, отвечаешь на любые вопросы и поддерживаешь беседу.
Общайся непринужденно, вежливо, на русском языке, используй уместные эмодзи.
"""

REASON_MAP = {
    "toxicity": "Токсичность и оскорбления",
    "spam": "Спам и флуд",
    "profanity": "Нецензурная лексика (мат)",
    "ad": "Несанкционированная реклама",
    "none": "—",
}

_JSON_RE = re.compile(r'\{[^{}]*\}')


async def check_message(text: str) -> dict:
    """Анализирует текст сообщения на нарушения с помощью Gemini."""
    client = _get_client()
    if client is None:
        logger.warning("Gemini client unavailable, skipping AI check")
        return {"violation": False, "reason": "none"}

    for model_name in MODELS_TO_TRY:
        try:
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=text,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_MODERATION_PROMPT,
                    temperature=0.1,
                ),
            )

            raw = (response.text or "").strip()
            if raw.startswith("```"):
                raw = re.sub(r'^```(?:json)?\s*', '', raw)
                raw = re.sub(r'\s*```$', '', raw)

            try:
                parsed = json.loads(raw)
                logger.info("AI check for '%s...': %s", text[:40], parsed)
                return parsed
            except json.JSONDecodeError:
                match = _JSON_RE.search(raw)
                if match:
                    parsed = json.loads(match.group())
                    logger.info("AI check (regex parsed) for '%s...': %s", text[:40], parsed)
                    return parsed

            logger.warning("Could not parse AI response: %s", raw)
            return {"violation": False, "reason": "none"}

        except Exception as e:
            logger.warning("Model %s failed with: %s. Trying next...", model_name, e)
            continue

    logger.error("All Gemini models failed for check_message")
    return {"violation": False, "reason": "none"}


async def chat_with_bot(user_message: str, history: Optional[List[Dict[str, str]]] = None) -> str:
    """Генерирует ответ Ботика в диалоге с пользователем."""
    client = _get_client()
    if client is None:
        return "Извини, сервис ИИ временно недоступен. Проверь GEMINI_API_KEY."

    for model_name in MODELS_TO_TRY:
        try:
            # Формируем контекст беседы
            prompt = user_message
            if history:
                conversation_parts = []
                for msg in history[-6:]:
                    role = "Пользователь" if msg.get("role") == "user" else "Ботик"
                    conversation_parts.append(f"{role}: {msg.get('text', '')}")
                conversation_parts.append(f"Пользователь: {user_message}")
                prompt = "\n".join(conversation_parts)

            response = await client.aio.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_CHAT_PROMPT,
                    temperature=0.7,
                ),
            )
            return response.text or "Я здесь! Чем могу помочь?"
        except Exception as e:
            logger.warning("Chat model %s failed: %s", model_name, e)
            continue

    return "Ой, что-то пошло не так при связи с нейросетью. Попробуй ещё разок!"


async def format_issue_report(raw_text: str, user_name: str) -> str:
    """Перефразирует и структурирует проблему/баг-репорт для админов с помощью Gemini."""
    client = _get_client()
    if client is None:
        return f"💡 <b>Идея / Сообщение о проблеме:</b>\n\n{raw_text}"

    system_inst = (
        "Ты — технический аналитик Telegram-бота модератора. "
        "Пользователь прислал жалобу, проблему или предложение по боту на русском языке. "
        "Твоя задача — вежливо, чётко и структурированно переформулировать суть проблемы/идеи, "
        "чтобы администраторам и разработчикам было максимально понятно, что произошло или что предлагается улучшить.\n"
        "Формат ответа:\n"
        "📌 <b>Суть:</b> (кратко одной строкой)\n"
        "🔍 <b>Детали и описание:</b> (понятное развёрнутое описание)\n"
        "💡 <b>Рекомендация:</b> (что можно предпринять / проверить)\n"
        "Не добавляй markdown-символы вроде ```, пиши только готовый текст с тегами <b>, <i>, если уместно."
    )

    for model_name in MODELS_TO_TRY:
        try:
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=f"Пользователь {user_name} сообщил:\n{raw_text}",
                config=types.GenerateContentConfig(
                    system_instruction=system_inst,
                    temperature=0.3,
                ),
            )
            if response.text:
                return response.text.strip()
        except Exception as e:
            logger.warning("Issue formatter model %s failed: %s", model_name, e)
            continue

    return f"📌 <b>Суть:</b> Сообщение о проблеме\n🔍 <b>Описание:</b> {raw_text}"


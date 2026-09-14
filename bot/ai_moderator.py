import difflib
import json
import logging
import re
from typing import List, Dict, Any, Optional, Tuple
from google import genai
from google.genai import types
import bot.config as config

logger = logging.getLogger(__name__)

_client = None
_key_is_invalid = False

# Официальные актуальные модели Gemini API в порядке предпочтения
MODELS_TO_TRY = [
    'gemini-3.5-flash-lite',
    'gemini-3.6-flash',
    'gemini-3.8-flash',
    'gemini-3.5-flash',
    'gemini-3.7-flash',
    'gemini-flash-latest'
]


def normalize_text(text: str) -> str:
    """Удаляет знаки препинания, лишние пробелы и приводит к нижнему регистру."""
    t = text.lower().strip()
    t = re.sub(r'[^\w\s]', '', t)
    return re.sub(r'\s+', ' ', t).strip()


def calculate_similarity(text1: str, text2: str) -> float:
    """Вычисляет степень сходства двух текстов (от 0.0 до 1.0)."""
    norm1 = normalize_text(text1)
    norm2 = normalize_text(text2)
    if not norm1 or not norm2:
        return 0.0
    if norm1 == norm2:
        return 1.0

    seq_ratio = difflib.SequenceMatcher(None, norm1, norm2).ratio()
    words1 = set(norm1.split())
    words2 = set(norm2.split())
    if words1 and words2:
        intersection = len(words1 & words2)
        union = len(words1 | words2)
        jaccard_ratio = intersection / union if union > 0 else 0.0
    else:
        jaccard_ratio = 0.0

    return max(seq_ratio, jaccard_ratio)


def _get_client() -> Optional[genai.Client]:
    global _client
    key = (config.GEMINI_API_KEY or "").strip()
    if not key:
        logger.warning("GEMINI_API_KEY is not set — AI features disabled.")
        return None
    if _client is None:
        try:
            _client = genai.Client(api_key=key)
        except Exception as e:
            logger.error("Failed to initialize Gemini Client: %s", e)
            return None
    return _client


async def test_gemini_connection(custom_key: Optional[str] = None) -> Tuple[bool, str]:
    """Проверяет подключение к Google Gemini API и возвращает (успех, сообщение)."""
    global _key_is_invalid
    key = (custom_key or config.GEMINI_API_KEY or "").strip()
    if not key:
        return False, "Ключ GEMINI_API_KEY не установлен."

    try:
        test_client = genai.Client(api_key=key)
        last_err = ""
        for model in MODELS_TO_TRY:
            try:
                resp = await test_client.aio.models.generate_content(
                    model=model,
                    contents="Ответь одним словом: Работает",
                    config=types.GenerateContentConfig(temperature=0.1)
                )
                text = (resp.text or "").strip()
                _key_is_invalid = False
                return True, f"✅ Успешно! Модель {model} ответила: «{text}»"
            except Exception as e:
                err_str = str(e)
                last_err = err_str
                if "401" in err_str or "UNAUTHENTICATED" in err_str or "ACCESS_TOKEN_TYPE_UNSUPPORTED" in err_str:
                    return False, (
                        "❌ Ошибка 401 UNAUTHENTICATED: Ключ не авторизован в Google AI Studio.\n"
                        "Убедитесь, что вы скопировали ключ из https://aistudio.google.com/app/apikey (он начинается на AIzaSy...)."
                    )
                continue
        return False, f"❌ Все модели Gemini вернули ошибку: {last_err[:200]}"
    except Exception as e:
        return False, f"❌ Ошибка инициализации клиента Google GenAI: {e}"


async def set_new_gemini_key(new_key: str) -> Tuple[bool, str]:
    """Проверяет и сохраняет новый API-ключ Gemini."""
    global _client, _key_is_invalid
    cleaned = new_key.strip()
    ok, msg = await test_gemini_connection(cleaned)
    if not ok:
        return False, msg

    config.update_gemini_api_key(cleaned)
    _key_is_invalid = False
    _client = genai.Client(api_key=cleaned)
    logger.info("Successfully updated Gemini API key and reinitialized client.")
    return True, msg


SYSTEM_MODERATION_PROMPT = """Ты — интеллектуальный модератор русскоязычного чата Telegram.
Твоя задача — объективно оценить сообщение участника на нарушения правил.

Категории нарушений:
1. "profanity" — явный или скрытый русский мат, грубые нецензурные ругательства, обсценная лексика.
2. "toxicity" — прямые оскорбления собеседников, унижения, агрессия, буллинг, пожелания вреда.
3. "spam" — спам, реклама казино/крипты/скама, бессмысленный флуд.
4. "ad" — несанкционированные ссылки на сторонние Telegram-каналы, группы, сайты, продажу товаров или услуг.
5. "duplicate" — сообщение повторяет смысл, суть или вопрос недавнего сообщения того же пользователя (семантический дубликат, повторный вопрос, флуд одинаковыми или похожими фразами).
6. "none" — обычное нормальное общение, юмор, вопросы, обсуждения без нарушений и повторов.

Формат ответа СТРОГО в виде JSON без markdown-кавычек:
{"violation": true, "reason": "profanity"}
или
{"violation": true, "reason": "duplicate"}
или
{"violation": false, "reason": "none"}
"""

SYSTEM_CHAT_PROMPT = """Ты — высокоинтеллектуальный, разносторонний и дружелюбный ИИ-ассистент «Ботик», работающий на передовой нейросети Google Gemini.
Ты общаешься с пользователем в персональном чате (Telegram Web App и личные сообщения).

Твои возможности и стиль общения:
1. Ты — полноценный искусственный интеллект Gemini. Ты можешь свободно общаться на абсолютно любые темы: отвечать на сложные вопросы, рассуждать о философии, науке, технологиях, психологии, жизни, отношениях, играх, фильмах и музыке.
2. Ты умеешь профессионально писать код на любых языках (Python, JS, C++, Go, HTML/CSS и др.), объяснять логику, находить ошибки и предлагать красивые решения.
3. Ты умеешь создавать любые тексты: сочинять стихи, сценарии, статьи, эссе, придумывать идеи для проектов, шутить и поддерживать живой разговор.
4. Если пользователь спрашивает о чате, супергруппе или создателе — используй знания из памяти бота (создатель супергруппы — @ArTeM_aoao).
5. Не ограничивай свои ответы только правилами чата или модерацией — отвечай как всесторонне развитый, умный и интересный собеседник Gemini!
6. Отвечай подробно, красиво, структурированно, используй списки, выделения и эмодзи.
"""

REASON_MAP = {
    "toxicity": "Токсичность и оскорбления",
    "spam": "Спам и флуд",
    "profanity": "Нецензурная лексика (мат)",
    "ad": "Несанкционированная реклама",
    "duplicate": "Повторяющиеся / похожие по смыслу сообщения",
    "none": "—",
}

_JSON_RE = re.compile(r'\{[^{}]*\}')

# Локальные регулярные выражения для резервной модерации, если API временно недоступно
MAT_REGEX = re.compile(
    r'\b(?:ху[йиеяё]|п[ие]зд|еб[аеёуо]|[бп]ля[тд]|муд[аое]|сучк|сук[аи]|пидор|гандон|залуп|чмо)\w*',
    re.IGNORECASE
)
SPAM_REGEX = re.compile(
    r'(?:t\.me\/(?:\+|joinchat\/)|bit\.ly\/|заработ[а-я]* в интернете|раскрутк[а-я]*|казино|1xbet|ставки на спорт|сигнал[ыов] крипт)',
    re.IGNORECASE
)


async def check_message(text: str, recent_messages: Optional[List[str]] = None) -> dict:
    """Анализирует текст сообщения на нарушения с помощью Gemini или локального фильтра."""
    clean_text = text.strip()

    # 1. Быстрая локальная проверка на одинаковые или похожие сообщения
    if recent_messages:
        for prev in recent_messages:
            prev_clean = prev.strip()
            if not prev_clean:
                continue
            sim = calculate_similarity(clean_text, prev_clean)
            if sim >= 0.72 and len(clean_text) >= 4:
                logger.info("Local duplicate detection: similarity %.2f between '%s' and '%s'", sim, clean_text[:30], prev_clean[:30])
                return {"violation": True, "reason": "duplicate", "similarity": sim}

    # 2. ИИ-проверка через Gemini
    client = _get_client()
    if client is not None:
        prompt_content = clean_text
        if recent_messages and len(recent_messages) > 0:
            prev_formatted = "\n".join([f"- «{m[:150]}»" for m in recent_messages[-3:]])
            prompt_content = (
                f"Предыдущие недавние сообщения этого пользователя:\n{prev_formatted}\n\n"
                f"Новое сообщение пользователя:\n«{clean_text}»\n\n"
                f"Проверь новое сообщение на нарушения. Если новое сообщение несёт тот же самый смысл, вопрос или суть, "
                f"что и любое из предыдущих (семантический повтор/дубликат/флуд похожими фразами), укажи "
                f"\"violation\": true, \"reason\": \"duplicate\"."
            )

        for model_name in MODELS_TO_TRY:
            try:
                response = await client.aio.models.generate_content(
                    model=model_name,
                    contents=prompt_content,
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
                    logger.info("AI check (%s) for '%s...': %s", model_name, clean_text[:40], parsed)
                    return parsed
                except json.JSONDecodeError:
                    match = _JSON_RE.search(raw)
                    if match:
                        parsed = json.loads(match.group())
                        logger.info("AI check regex (%s) for '%s...': %s", model_name, clean_text[:40], parsed)
                        return parsed

            except Exception as e:
                logger.warning("Moderation model %s failed: %s. Trying next...", model_name, e)
                continue

    # 3. Локальная резервная проверка (если Gemini недоступен)
    if MAT_REGEX.search(clean_text):
        logger.info("Local fallback detected profanity: %s...", clean_text[:30])
        return {"violation": True, "reason": "profanity"}
    if SPAM_REGEX.search(clean_text):
        logger.info("Local fallback detected spam: %s...", clean_text[:30])
        return {"violation": True, "reason": "spam"}

    return {"violation": False, "reason": "none"}


def get_smart_fallback_response(user_message: str) -> str:
    """Интеллектуальная база знаний для ответов Ботика при недоступности внешнего API."""
    lowered = user_message.lower().strip()

    # Приветствия
    if any(w in lowered for w in ["привет", "здравствуй", "добрый день", "добрый вечер", "доброе утро", "салют", "хай", "ку", "йоу"]):
        return (
            "👋 Привет! Я <b>Ботик</b> — виртуальный помощник и модератор нашей супергруппы!\n\n"
            "Я слежу за порядком, считаю варны, подсказываю правила и умею играть в Тетрис. Чем могу помочь?"
        )

    # Правила
    if any(w in lowered for w in ["правил", "запрещен", "нельзя"]):
        return (
            "📜 <b>Правила супергруппы:</b>\n\n"
            "1. 🚫 <b>Мат и грубость:</b> Нецензурные выражения запрещены в любом виде.\n"
            "2. 🚫 <b>Оскорбления и токсичность:</b> Уважайте собеседников. Буллинг и травля пресекаются.\n"
            "3. 🚫 <b>Спам и флуд:</b> Запрещены ссылки на сторонние каналы, крипто-скам, казино и бесконечные стикеры.\n"
            "4. 🚫 <b>Реклама:</b> Любой пиар только по согласованию с администрацией.\n\n"
            "⚠️ <b>Система нарушений:</b> 1-е нарушение — предупреждение (варн 1/2), 2-е — мут на 24 часа."
        )

    # Мут
    if any(w in lowered for w in ["мут", "замут", "за что мут", "ограничени"]):
        return (
            "🔇 <b>За что дают мут?</b>\n\n"
            "Мут выдаётся автоматически за повторное нарушение (при получении 2-го предупреждения 2/2), "
            "либо вручную администраторами за грубые нарушения.\n\n"
            "⏱ Во время мута вы не сможете отправлять сообщения и медиа в группу."
        )

    # Варны
    if any(w in lowered for w in ["варн", "предупрежден", "снять варн", "снять предупрежд"]):
        return (
            "⚠️ <b>Предупреждения (варны):</b>\n\n"
            "Каждый участник имеет лимит: максимум 2 варна.\n"
            "• При 1-м варне вы получаете предупреждение.\n"
            "• При 2-м варне бот отправляет в мут на 24 часа.\n\n"
            "🔓 <b>Как снять варн?</b> Администраторы могут сбросить варны в админ-панели Ботика."
        )

    # Команды и помощь
    if any(w in lowered for w in ["команд", "помощь", "help", "что умеешь", "функци"]):
        return (
            "🤖 <b>Функции и команды Ботика:</b>\n\n"
            "• <code>✨ Открыть Ботик ✨</code> — персональный кабинет: профиль, чат со мной, Тетрис и админка\n"
            "• <code>/set_botik</code> — закрепить стартовую карточку Ботика в топике\n"
            "• <code>/set_ideas</code> — привязать ветку для сбора идей и баг-репортов\n"
            "• <code>/check_ai</code> — диагностика и проверка статуса нейросети Gemini\n"
            "• <code>/set_gemini &lt;ключ&gt;</code> — быстрая установка рабочего API ключа (для админов)"
        )

    # Идеи и баги
    if any(w in lowered for w in ["иде", "баг", "предложен", "ошибк"]):
        return (
            "💡 <b>Есть идея или заметили баг?</b>\n\n"
            "Откройте вкладку <b>«💡 Идеи»</b> в верхнем меню приложения, опишите мысль своими словами и отправьте. "
            "Бот оформит технический репорт и отправит его прямо в топик «Идеи» супергруппы!"
        )

    # Тетрис и игры
    if any(w in lowered for w in ["тетрис", "игр", "рекорд"]):
        return (
            "🎮 <b>Тетрис прямо в Telegram!</b>\n\n"
            "Перейдите во вкладку <b>«🎮 Тетрис»</b> в верхней панели. "
            "Игра оптимизирована под мобильные экраны (сенсорные кнопки снизу) и компьютеры (стрелки, WASD, Пробел — сброс, P — пауза). "
            "Попробуйте побить рекорд!"
        )

    # Анекдоты и юмор
    if any(w in lowered for w in ["анекдот", "шутк", "пошути", "рассмеши", "юмор"]):
        return (
            "😄 <b>Держи анекдот:</b>\n\n"
            "— Товарищ модератор, а почему вы меня забанили?\n"
            "— За оффтоп и спам.\n"
            "— Но я же просто спросил, какая сегодня погода!\n"
            "— В чате «Клуб любителей квантовой физики в темноте» погода всегда одна — абсолютный ноль! ❄️"
        )

    # Информация о создателе / боте
    if any(w in lowered for w in ["кто ты", "о боте", "создател", "админ"]):
        return (
            "👑 <b>О Ботике v2.5:</b>\n\n"
            "Я персональный модератор супергруппы <code>KareloAI</code>. Разработан для автоматизации модерации, "
            "поддержки участников и соревновательных игр. Мой создатель — @ArTeM_aoao."
        )

    # Сообщение по умолчанию
    return (
        "🤖 <b>Ботик (Gemini AI):</b>\n\n"
        "Я готов обсудить любую тему, помочь с кодом, ответить на сложный вопрос или просто поболтать! "
        "О чём хочешь поговорить?"
    )


async def chat_with_bot(
    user_message: str,
    history: Optional[List[Dict[str, str]]] = None,
    user_id: int = 0,
    chat_id: int = 0
) -> str:
    """Генерирует ответ Ботика в диалоге с пользователем с учетом долгосрочной памяти."""
    from bot.database import get_memories, get_dialog_history, save_dialog_message

    # 1. Загружаем сохранённую память и базу знаний
    memories = await get_memories(user_id=user_id, chat_id=chat_id, limit=20)
    memory_section = ""
    if memories:
        mem_lines = [f"• [{m.get('category', 'факт')}]: {m.get('key_phrase')}: {m.get('content')}" for m in memories]
        memory_section = "\n\nДолговременная память и база знаний супергруппы:\n" + "\n".join(mem_lines)

    system_instruction = SYSTEM_CHAT_PROMPT + memory_section

    # 2. Если история не передана от клиента, достаём последние 10 сообщений из БД
    conv_history = history
    if not conv_history and user_id > 0:
        conv_history = await get_dialog_history(user_id=user_id, limit=10)

    client = _get_client()
    reply_text = ""

    if client is not None:
        for model_name in MODELS_TO_TRY:
            try:
                # Формируем контекст беседы
                prompt = user_message
                if conv_history:
                    conversation_parts = []
                    for msg in conv_history[-10:]:
                        role = "Пользователь" if msg.get("role") == "user" else "Ботик"
                        conversation_parts.append(f"{role}: {msg.get('text', '')}")
                    conversation_parts.append(f"Пользователь: {user_message}")
                    prompt = "\n".join(conversation_parts)

                response = await client.aio.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.75,
                    ),
                )
                if response.text:
                    reply_text = response.text.strip()
                    break
            except Exception as e:
                logger.warning("Chat model %s failed: %s", model_name, e)
                continue

    # Если Gemini недоступен или выдал ошибку — используем умный фолбэк
    if not reply_text:
        reply_text = get_smart_fallback_response(user_message)

    # 3. Сохраняем сообщение в историю диалогов для долгосрочной памяти
    if user_id > 0:
        try:
            await save_dialog_message(user_id=user_id, chat_id=chat_id, role="user", message=user_message)
            await save_dialog_message(user_id=user_id, chat_id=chat_id, role="bot", message=reply_text)
        except Exception:
            pass

    return reply_text


async def format_issue_report(raw_text: str, user_name: str) -> str:
    """Перефразирует и структурирует проблему/баг-репорт для админов с помощью Gemini."""
    global _key_is_invalid
    client = _get_client()

    if client is not None:
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
                err_str = str(e)
                if "401" in err_str or "UNAUTHENTICATED" in err_str or "ACCESS_TOKEN_TYPE_UNSUPPORTED" in err_str:
                    _key_is_invalid = True
                    break
                logger.warning("Issue formatter model %s failed: %s", model_name, e)
                continue

    # Локальное структурирование
    return (
        f"📌 <b>Суть:</b> Обращение от участника {user_name}\n\n"
        f"🔍 <b>Текст обращения:</b>\n{raw_text}\n\n"
        f"💡 <b>Рекомендация:</b> Администрации ознакомиться и дать обратную связь пользователю."
    )

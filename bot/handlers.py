import json
import logging
import datetime
from typing import Optional

from aiogram import Router, Bot, F
from aiogram.types import (
    Message,
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    ChatMemberUpdated,
    MessageReactionUpdated
)
from aiogram.filters import (
    CommandStart,
    Command
)

from bot.ai_moderator import (
    check_message,
    REASON_MAP,
    chat_with_bot,
    test_gemini_connection,
    set_new_gemini_key
)
from bot.database import (
    get_warnings,
    add_warning,
    reset_warnings,
    log_violation,
    increment_stat,
    get_user_status,
    get_admin_stats,
    set_botik_thread,
    get_botik_thread,
    set_ideas_thread,
    get_ideas_thread,
    upsert_member,
    add_manual_member,
    save_memory,
    get_memories,
    clear_memories,
    set_bot_setting,
    get_bot_setting
)
from bot.sheets_sync import (
    test_sheet_connection,
    sync_knowledge_from_sheet,
    append_row_to_sheet,
    APPS_SCRIPT_CODE
)
from bot.config import ADMIN_IDS, WEB_APP_URL, MODERATE_ALL

logger = logging.getLogger(__name__)
router = Router()

MUTE_DURATION = datetime.timedelta(seconds=86400)
BOT_NAME = "🤖 Ботик"
BOT_USERNAME = "kareloai_bot"

# Трекер антифлуда короткими сообщениями и стикерами: (chat_id, user_id) -> [timestamps]
_short_flood_history: dict[tuple[int, int], list[float]] = {}
FLOOD_WINDOW_SECONDS = 180.0  # Окно: 3 минуты (1-3 мин)
FLOOD_LIMIT = 3               # Лимит: 3 сообщения подряд


def get_webapp_url() -> str:
    return WEB_APP_URL


def get_botik_card_text() -> str:
    return (
        "🤖 <b>ПЕРСОНАЛЬНЫЙ БОТИК v3.0</b>\n\n"
        "Личный кабинет каждого участника супергруппы.\n\n"
        "• 👤 <b>Профиль:</b> статус, предупреждения (варны) и история\n"
        "• 💬 <b>ИИ Ботик:</b> умный помощник с нейросетью Gemini\n"
        "• 🧠 <b>Память:</b> бот запоминает факты и правила группы\n"
        "• 🎮 <b>Тетрис:</b> соревновательная игра прямо в Telegram\n"
        "• 👥 <b>Админка:</b> список участников, аватары и модерация\n\n"
        "💡 <b>Команды для админов:</b>\n"
        "• <code>/set_ideas</code> — привязать топик «Идеи»\n"
        "• <code>/remember факт</code> — добавить факт в память бота\n"
        "• <code>/memory</code> — посмотреть память бота\n"
        "• <code>/set_sheet</code> — подключить Google Таблицу\n\n"
        "👇 Нажмите кнопку ниже, чтобы открыть ваш личный Ботик:"
    )


def make_group_keyboard(user_id: int, chat_id: Optional[int] = None) -> InlineKeyboardMarkup:
    app_link = f"https://t.me/{BOT_USERNAME}/app?startapp={user_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✨ Открыть Ботик ✨",
                    url=app_link
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить карточку",
                    callback_data="refresh_botik_card"
                )
            ]
        ]
    )




def make_private_keyboard(user_id: int, chat_id: Optional[int] = None) -> InlineKeyboardMarkup:
    target_url = f"{get_webapp_url()}?uid={user_id}&cid={chat_id or ''}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✨ Открыть Ботик ✨",
                    web_app=WebAppInfo(url=target_url)
                )
            ]
        ]
    )


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        is_adm = member.status in ("administrator", "creator")
        if is_adm:
            await upsert_member(user_id, chat_id, member.user.username or "", member.user.full_name or "", True)
        return is_adm
    except Exception as e:
        logger.error("Error checking admin status: %s", e)
        return False


# ──────────────────────────────────────────────
# АВТО-СБОР 1: Реакции на сообщения (эмодзи ❤️, 👍, 🔥 и др.)
# ──────────────────────────────────────────────
@router.message_reaction()
async def on_message_reaction(reaction: MessageReactionUpdated, bot: Bot) -> None:
    user = reaction.user
    if user and not user.is_bot:
        is_adm = await is_chat_admin(bot, reaction.chat.id, user.id)
        await upsert_member(
            user_id=user.id,
            chat_id=reaction.chat.id,
            username=user.username or "",
            full_name=user.full_name or "",
            is_admin=is_adm
        )
        logger.info("Auto-collected member from reaction: %s (%s)", user.full_name, user.id)


# ──────────────────────────────────────────────
# АВТО-СБОР 2: Обновления статуса участника (вход, выход, смена прав)
# ──────────────────────────────────────────────
@router.chat_member()
async def on_chat_member_updated(event: ChatMemberUpdated, bot: Bot) -> None:
    user = event.new_chat_member.user
    if user and not user.is_bot:
        is_adm = event.new_chat_member.status in ("administrator", "creator")
        await upsert_member(
            user_id=user.id,
            chat_id=event.chat.id,
            username=user.username or "",
            full_name=user.full_name or "",
            is_admin=is_adm
        )
        logger.info("Auto-collected member from chat_member event: %s (%s)", user.full_name, user.id)


# ──────────────────────────────────────────────
# /sync — принудительный сбор
# ──────────────────────────────────────────────
@router.message(Command("sync"))
async def cmd_sync(message: Message, bot: Bot) -> None:
    try:
        admins = await bot.get_chat_administrators(message.chat.id)
        for a in admins:
            if not a.user.is_bot:
                await upsert_member(a.user.id, message.chat.id, a.user.username or "", a.user.full_name or "", True)
    except Exception as ex:
        logger.warning("Scan admins error: %s", ex)

    await bot.send_message(
        chat_id=message.chat.id,
        text=(
            "👥 <b>Синхронизация участников супергруппы</b>\n\n"
            "Нажмите кнопку ниже один раз, чтобы ваш профиль и аватар "
            "моментально отобразились в базе данных и в <b>Админке Ботика</b> 👇"
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Добавить мой профиль в список",
                        callback_data="sync_member_click"
                    )
                ]
            ]
        )
    )


@router.callback_query(F.data == "sync_member_click")
async def cb_sync_member(query: CallbackQuery, bot: Bot) -> None:
    user = query.from_user
    chat_id = query.message.chat.id if query.message else -1003955632241
    is_adm = await is_chat_admin(bot, chat_id, user.id)
    await upsert_member(user.id, chat_id, user.username or "", user.full_name or "", is_adm)
    await query.answer("✅ Ваш профиль успешно синхронизирован с Ботиком!", show_alert=True)


# ──────────────────────────────────────────────
# /scan
# ──────────────────────────────────────────────
@router.message(Command("scan"))
async def cmd_scan_members(message: Message, bot: Bot) -> None:
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Команда /scan работает только в группах.")
        return

    try:
        admins = await bot.get_chat_administrators(message.chat.id)
        scanned_count = 0
        for a in admins:
            if not a.user.is_bot:
                await upsert_member(
                    user_id=a.user.id,
                    chat_id=message.chat.id,
                    username=a.user.username or "",
                    full_name=a.user.full_name or "",
                    is_admin=True
                )
                scanned_count += 1

        total = await bot.get_chat_member_count(message.chat.id)

        await message.answer(
            f"✅ <b>Сканирование завершено!</b>\n\n"
            f"Всего участников в группе: <b>{total}</b>\n"
            f"Администраторов в базе: <b>{scanned_count}</b>.\n"
            f"Остальные участники фиксируются автоматически при любых сообщениях и реакциях.\n\n"
            f"💡 Чтобы добавить участника по юзернейму, введите: <code>/add @username</code>",
            reply_markup=make_group_keyboard(message.from_user.id, message.chat.id)
        )
    except Exception as e:
        logger.error("Scan error: %s", e)
        await message.answer(f"Ошибка при сканировании: {e}")


# ──────────────────────────────────────────────
# /add @username — ручное добавление в базу
# ──────────────────────────────────────────────
@router.message(Command("add"))
async def cmd_add_member(message: Message, bot: Bot) -> None:
    is_adm = await is_chat_admin(bot, message.chat.id, message.from_user.id)
    if not is_adm:
        await message.answer("❌ Только администраторы могут добавлять участников вручную.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "ℹ️ <b>Как добавить участника:</b>\n"
            "Напишите: <code>/add @username</code> или <code>/add username</code>\n\n"
            "Пример: <code>/add @durov</code>"
        )
        return

    usernames = parts[1].strip().split()
    added = []
    for raw_u in usernames:
        u = raw_u.strip().lstrip("@")
        if not u:
            continue
        await add_manual_member(username=u, full_name=f"@{u}", chat_id=message.chat.id)
        added.append(f"@{u}")

    if added:
        await message.answer(
            f"✅ Участник(и) {', '.join(added)} успешно добавлены в <b>Админку</b>!\n"
            f"Теперь вы можете открыть Web App и управлять ими (выдать варн/мут).",
            reply_markup=make_group_keyboard(message.from_user.id, message.chat.id)
        )
    else:
        await message.answer("❌ Не указан корректный юзернейм.")


# ──────────────────────────────────────────────
# /set_botik
# ──────────────────────────────────────────────
@router.message(Command("set_botik"))
async def cmd_set_botik(message: Message, bot: Bot) -> None:
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Эта команда используется внутри супергруппы в разделе «Ботик».")
        return

    thread_id = message.message_thread_id or 0
    await set_botik_thread(message.chat.id, thread_id)

    try:
        await message.delete()
    except Exception:
        pass

    card_text = get_botik_card_text()

    try:
        try:
            await bot.unpin_chat_message(chat_id=message.chat.id)
        except Exception:
            pass

        sent_msg = await bot.send_message(
            chat_id=message.chat.id,
            message_thread_id=thread_id if thread_id != 0 else None,
            text=card_text,
            reply_markup=make_group_keyboard(message.from_user.id, message.chat.id)
        )
        try:
            await bot.pin_chat_message(
                chat_id=message.chat.id,
                message_id=sent_msg.message_id,
                disable_notification=True
            )
        except Exception:
            pass
    except Exception as e:
        logger.error("Error sending set_botik message: %s", e)


# ──────────────────────────────────────────────
# Callback: 🔄 Обновить карточку Ботика
# ──────────────────────────────────────────────
@router.callback_query(F.data == "refresh_botik_card")
async def on_refresh_botik_card(callback: CallbackQuery, bot: Bot) -> None:
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    is_adm = await is_chat_admin(bot, chat_id, user_id)
    if not is_adm:
        await callback.answer("❌ Только администраторы могут обновлять карточку Ботика.", show_alert=True)
        return

    thread_id = callback.message.message_thread_id or 0
    card_text = get_botik_card_text()

    try:
        await callback.message.edit_text(
            text=card_text,
            reply_markup=make_group_keyboard(user_id, chat_id)
        )
        await callback.answer("✅ Карточка Ботика успешно обновлена и синхронизирована!", show_alert=True)
    except Exception:
        try:
            sent_msg = await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id if thread_id != 0 else None,
                text=card_text,
                reply_markup=make_group_keyboard(user_id, chat_id)
            )
            await bot.pin_chat_message(chat_id=chat_id, message_id=sent_msg.message_id, disable_notification=True)
            await callback.answer("✅ Отправлена и закреплена свежая карточка!", show_alert=True)
        except Exception as ex:
            await callback.answer(f"Ошибка: {ex}", show_alert=True)


# ──────────────────────────────────────────────
# /del_botik — удаление/открепление кнопки
# ──────────────────────────────────────────────
@router.message(Command("del_botik"))
async def cmd_del_botik(message: Message, bot: Bot) -> None:
    if message.chat.type not in ("group", "supergroup"):
        return
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await bot.unpin_chat_message(chat_id=message.chat.id)
    except Exception:
        pass


# ──────────────────────────────────────────────
# /set_ideas — привязка раздела «Идеи»
# ──────────────────────────────────────────────
@router.message(Command("set_ideas"))
async def cmd_set_ideas(message: Message, bot: Bot) -> None:
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Эта команда используется внутри супергруппы в разделе «Идеи».")
        return

    is_adm = await is_chat_admin(bot, message.chat.id, message.from_user.id)
    if not is_adm:
        await message.answer("❌ Только администраторы могут привязать топик «Идеи».")
        return

    thread_id = message.message_thread_id or 0
    await set_ideas_thread(message.chat.id, thread_id)

    await message.answer(
        f"✅ <b>Раздел «Идеи» успешно привязан!</b> (ID топика: <code>{thread_id}</code>)\n\n"
        f"Теперь сообщения о проблемах и предложения пользователей из вкладки "
        f"<b>«💡 Проблемы бота»</b> будут автоматически структурироваться ИИ и пересылаться сюда."
    )



# ──────────────────────────────────────────────
# /check_ai — проверка статуса нейросети
# ──────────────────────────────────────────────
@router.message(Command("check_ai"))
async def cmd_check_ai(message: Message, bot: Bot) -> None:
    is_adm = False
    if message.chat.type in ("group", "supergroup"):
        is_adm = await is_chat_admin(bot, message.chat.id, message.from_user.id)
    else:
        is_adm = message.from_user.id in ADMIN_IDS or message.from_user.id == 5851158445

    if not is_adm:
        await message.answer("❌ Эта команда доступна только администраторам супергруппы.")
        return

    status_msg = await message.answer("⏳ Тестирую подключение к Google Gemini API...")
    ok, details = await test_gemini_connection()

    if ok:
        await status_msg.edit_text(
            f"🤖 <b>Статус нейросети Gemini:</b> 🟢 АКТИВНА\n\n"
            f"{details}\n\n"
            f"ИИ-модерация сообщений и диалоги с Ботиком работают на полную мощность!"
        )
    else:
        await status_msg.edit_text(
            f"🤖 <b>Статус нейросети Gemini:</b> 🔴 ТРЕБУЕТ НАСТРОЙКИ\n\n"
            f"{details}\n\n"
            f"💡 <b>Как настроить ключ за 30 секунд:</b>\n"
            f"1. Откройте <a href=\"https://aistudio.google.com/app/apikey\">Google AI Studio</a>\n"
            f"2. Создайте и скопируйте бесплатный ключ (начинается на <code>AIzaSy...</code>)\n"
            f"3. Отправьте боту команду:\n"
            f"<code>/set_gemini ВАШ_КЛЮЧ</code>",
            disable_web_page_preview=True
        )


# ──────────────────────────────────────────────
# /set_gemini — установка ключа Gemini на лету
# ──────────────────────────────────────────────
@router.message(Command("set_gemini"))
async def cmd_set_gemini(message: Message, bot: Bot) -> None:
    is_adm = False
    if message.chat.type in ("group", "supergroup"):
        is_adm = await is_chat_admin(bot, message.chat.id, message.from_user.id)
    else:
        is_adm = message.from_user.id in ADMIN_IDS or message.from_user.id == 5851158445

    if not is_adm:
        await message.answer("❌ Только администраторы могут изменять настройки нейросети.")
        return

    args = message.text.replace("/set_gemini", "", 1).strip()
    if not args:
        await message.answer(
            "ℹ️ <b>Установка API ключа Gemini:</b>\n\n"
            "Использование: <code>/set_gemini AIzaSy...</code>\n\n"
            "Получить бесплатный ключ можно в <a href=\"https://aistudio.google.com/app/apikey\">Google AI Studio</a>.",
            disable_web_page_preview=True
        )
        return

    # Удаляем сообщение с ключом из чата ради безопасности, если это группа
    if message.chat.type in ("group", "supergroup"):
        try:
            await message.delete()
        except Exception:
            pass

    status_msg = await message.answer("⏳ Проверяю валидность ключа в Google AI Studio...")
    ok, msg = await set_new_gemini_key(args)

    if ok:
        await status_msg.edit_text(
            f"🎉 <b>Ключ успешно активирован!</b>\n\n"
            f"{msg}\n\n"
            f"Нейросеть Gemini подключена и готова к работе во всех разделах Ботика!"
        )
    else:
        await status_msg.edit_text(
            f"❌ <b>Не удалось активировать ключ:</b>\n\n"
            f"{msg}\n\n"
            f"Пожалуйста, убедитесь, что ключ скопирован целиком без лишних пробелов."
        )


# ──────────────────────────────────────────────
# /set_sheet — подключение Google Таблицы
# ──────────────────────────────────────────────
@router.message(Command("set_sheet"))
async def cmd_set_sheet(message: Message, bot: Bot) -> None:
    is_adm = False
    if message.chat.type in ("group", "supergroup"):
        is_adm = await is_chat_admin(bot, message.chat.id, message.from_user.id)
    else:
        is_adm = message.from_user.id in ADMIN_IDS or message.from_user.id == 5851158445

    if not is_adm:
        await message.answer("❌ Только администраторы могут подключать Google Таблицу.")
        return

    args = (message.text or "").replace("/set_sheet", "", 1).strip()

    # Если без аргументов — показать инструкцию
    if not args:
        await message.answer(
            "📊 <b>Подключение Google Таблицы для памяти бота</b>\n\n"
            "Это добавит боту долгосрочную бесплатную память через Google Sheets!\n\n"
            "<b>Инструкция (3 шага):</b>\n"
            "1️⃣ Создайте <a href=\"https://sheets.new\">новую Google Таблицу</a>\n"
            "2️⃣ Откройте <b>Расширения → Apps Script</b>, вставьте код ниже и разверните как веб-приложение (Доступ: Все)\n"
            "3️⃣ Скопируйте URL вебхука и отправьте:\n"
            "<code>/set_sheet https://script.google.com/.../exec</code>\n\n"
            "📋 <b>Код Apps Script:</b>",
            disable_web_page_preview=True
        )
        # Отправляем код в отдельном сообщении для копирования
        code_text = APPS_SCRIPT_CODE.strip()
        if len(code_text) > 4000:
            code_text = code_text[:4000] + "\n// ... (продолжение в документации)"
        await message.answer(f"<pre>{code_text}</pre>")
        return

    # Удаляем сообщение с URL из группы
    if message.chat.type in ("group", "supergroup"):
        try:
            await message.delete()
        except Exception:
            pass

    status_msg = await message.answer("⏳ Проверяю подключение к Google Таблице...")
    ok, details = await test_sheet_connection(args)

    if ok:
        await set_bot_setting("google_sheet_url", args)
        await status_msg.edit_text(
            f"🎉 <b>Google Таблица успешно подключена!</b>\n\n"
            f"✅ {details}\n\n"
            f"Теперь бот будет использовать таблицу как дополнительную память.\n"
            f"Чтобы синхронизировать знания из таблицы: <code>/sync_sheet</code>"
        )
    else:
        await status_msg.edit_text(
            f"❌ <b>Не удалось подключить таблицу</b>\n\n"
            f"{details}\n\n"
            f"Убедитесь, что URL правильный и веб-приложение развёрнуто с доступом «Все»."
        )


# ──────────────────────────────────────────────
# /sync_sheet — синхронизация знаний из таблицы
# ──────────────────────────────────────────────
@router.message(Command("sync_sheet"))
async def cmd_sync_sheet(message: Message, bot: Bot) -> None:
    is_adm = False
    if message.chat.type in ("group", "supergroup"):
        is_adm = await is_chat_admin(bot, message.chat.id, message.from_user.id)
    else:
        is_adm = message.from_user.id in ADMIN_IDS or message.from_user.id == 5851158445

    if not is_adm:
        await message.answer("❌ Только администраторы могут синхронизировать память.")
        return

    status_msg = await message.answer("⏳ Синхронизирую базу знаний из Google Таблицы...")
    ok, count, details = await sync_knowledge_from_sheet()

    if ok:
        await status_msg.edit_text(
            f"🧠 <b>Память синхронизирована!</b>\n\n"
            f"✅ {details}\n\n"
            f"Бот теперь помнит {count} фактов из таблицы и будет использовать их при ответах."
        )
    else:
        await status_msg.edit_text(
            f"❌ <b>Ошибка синхронизации</b>\n\n{details}\n\n"
            f"💡 Сначала подключите таблицу: <code>/set_sheet URL</code>"
        )


# ──────────────────────────────────────────────
# /remember — добавить факт в память бота
# ──────────────────────────────────────────────
@router.message(Command("remember"))
async def cmd_remember(message: Message, bot: Bot) -> None:
    is_adm = False
    if message.chat.type in ("group", "supergroup"):
        is_adm = await is_chat_admin(bot, message.chat.id, message.from_user.id)
    else:
        is_adm = message.from_user.id in ADMIN_IDS or message.from_user.id == 5851158445

    if not is_adm:
        await message.answer("❌ Только администраторы могут добавлять факты в память.")
        return

    args = (message.text or "").replace("/remember", "", 1).strip()
    if not args:
        await message.answer(
            "🧠 <b>Как добавить факт в память бота:</b>\n\n"
            "Напишите: <code>/remember Ваш факт или инструкция</code>\n\n"
            "Примеры:\n"
            "• <code>/remember Создатель супергруппы — @ArTeM_aoao</code>\n"
            "• <code>/remember Правило: нельзя спамить стикерами</code>\n"
            "• <code>/remember Ботик создан в сентябре 2026</code>"
        )
        return

    # Определяем ключевую фразу и содержание
    if ":" in args:
        parts = args.split(":", 1)
        key_phrase = parts[0].strip()
        content = parts[1].strip()
    else:
        key_phrase = args[:60].strip()
        content = args

    mem_id = await save_memory(
        key_phrase=key_phrase,
        content=content,
        category="fact",
        user_id=message.from_user.id,
        chat_id=message.chat.id
    )

    await message.answer(
        f"🧠 <b>Запомнил!</b> (ID: {mem_id})\n\n"
        f"📌 <b>{key_phrase}</b>\n"
        f"{content}\n\n"
        f"Теперь буду использовать этот факт при ответах в чате."
    )

    # Дублируем в Google Таблицу, если подключена
    try:
        await append_row_to_sheet(
            "Память",
            ["fact", key_phrase, content, str(message.from_user.id)],
            ["Категория", "Ключ / Тема", "Значение", "Добавил (ID)"]
        )
    except Exception:
        pass


# ──────────────────────────────────────────────
# /memory — показать текущую память бота
# ──────────────────────────────────────────────
@router.message(Command("memory"))
async def cmd_memory(message: Message, bot: Bot) -> None:
    is_adm = False
    if message.chat.type in ("group", "supergroup"):
        is_adm = await is_chat_admin(bot, message.chat.id, message.from_user.id)
    else:
        is_adm = message.from_user.id in ADMIN_IDS or message.from_user.id == 5851158445

    if not is_adm:
        await message.answer("❌ Только администраторы могут просматривать память бота.")
        return

    args = (message.text or "").replace("/memory", "", 1).strip()

    # /memory clear — очистить память
    if args.lower() == "clear":
        count = await clear_memories(chat_id=message.chat.id)
        await message.answer(f"🗑️ Память очищена! Удалено записей: <b>{count}</b>")
        return

    memories = await get_memories(chat_id=message.chat.id, limit=20)
    if not memories:
        await message.answer(
            "🧠 <b>Память пуста</b>\n\n"
            "Добавьте факты командой: <code>/remember ваш факт</code>\n"
            "Или подключите Google Таблицу: <code>/set_sheet</code>"
        )
        return

    lines = []
    for i, m in enumerate(memories[:15], 1):
        cat = m.get("category", "факт")
        key = m.get("key_phrase", "")
        lines.append(f"{i}. [{cat}] <b>{key}</b>")

    await message.answer(
        f"🧠 <b>Память бота ({len(memories)} записей):</b>\n\n" +
        "\n".join(lines) +
        "\n\n💡 <code>/memory clear</code> — очистить всю память"
    )


# ──────────────────────────────────────────────
# /start
# ──────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    user = message.from_user
    username = f"@{user.username}" if user and user.username else (user.full_name if user else "Участник")
    user_id = user.id if user else 0

    status_info = await get_user_status(user_id)
    warns = status_info.get("warnings", 0)
    warn_text = f"Ваши предупреждения: <b>{warns}/2 ⚠️</b>" if warns > 0 else "У вас нет активных предупреждений ✅"

    if message.chat.type == "private":
        await message.answer(
            f"👋 Привет, {username}!\n\n"
            f"Я <b>{BOT_NAME}</b> — модератор вашей супергруппы.\n"
            f"🛡️ {warn_text}\n\n"
            "Нажмите кнопку ниже, чтобы открыть Mini App прямо в Telegram 👇",
            reply_markup=make_private_keyboard(user_id, message.chat.id)
        )
    else:
        await message.answer(
            f"👋 {BOT_NAME} активен в чате!\n{warn_text}",
            reply_markup=make_group_keyboard(user_id, message.chat.id)
        )


# ──────────────────────────────────────────────
# /test_mod
# ──────────────────────────────────────────────
@router.message(Command("test_mod"))
async def cmd_test_mod(message: Message) -> None:
    text = message.text.replace("/test_mod", "", 1).strip()
    if not text:
        await message.answer("ℹ️ Использование: <code>/test_mod спам казино выиграй миллион</code>")
        return

    res = await check_message(text)
    is_viol = res.get("violation", False)
    reason = res.get("reason", "none")
    reason_ru = REASON_MAP.get(reason, reason)

    if is_viol:
        verdict = f"🚨 <b>Нарушение найдено!</b>\nКатегория: <code>{reason_ru}</code>"
    else:
        verdict = "✅ <b>Нарушений нет</b> (сообщение чистое)"

    await message.answer(
        f"🔍 <b>Тест ИИ-модератора</b>\n"
        f"Текст: <i>{text[:100]}</i>\n\n"
        f"{verdict}"
    )


# ──────────────────────────────────────────────
# /status
# ──────────────────────────────────────────────
@router.message(Command("status"))
async def cmd_status(message: Message, bot: Bot) -> None:
    admin_data = await get_admin_stats()
    stats = admin_data.get("stats", {})
    violators = admin_data.get("active_violators_count", 0)
    members_count = admin_data.get("members_count", 0)

    botik_tid = await get_botik_thread(message.chat.id)
    botik_info = f"ID раздела «Ботик»: <code>{botik_tid}</code>" if botik_tid is not None else "Раздел «Ботик» не назначен (напишите <code>/set_botik</code> в нём)"

    await message.answer(
        f"📊 <b>Статус Ботика</b>\n\n"
        f"Чат: <b>{message.chat.title}</b>\n"
        f"Проверено сообщений: <b>{stats.get('checked_messages', 0)}</b>\n"
        f"Нарушений зафиксировано: <b>{stats.get('total_violations', 0)}</b>\n"
        f"Участников в базе: <b>{members_count}</b>\n"
        f"Нарушителей на контроле: <b>{violators}</b>\n"
        f"{botik_info}",
        reply_markup=make_group_keyboard(message.from_user.id, message.chat.id)
    )


# ──────────────────────────────────────────────
# Личный чат с Ботиком
# ──────────────────────────────────────────────
@router.message(F.chat.type == "private")
async def private_chat_handler(message: Message) -> None:
    text = message.text or ""
    if not text or text.startswith("/"):
        return
    user_id = message.from_user.id if message.from_user else 0
    reply = await chat_with_bot(text, user_id=user_id, chat_id=message.chat.id)
    await message.answer(reply, reply_markup=make_private_keyboard(user_id, message.chat.id))


# ──────────────────────────────────────────────
# ГЛАВНЫЙ ОБРАБОТЧИК: СУПЕРГРУППА (АВТО-СБОР ПРИ ЛЮБОМ СООБЩЕНИИ)
# ──────────────────────────────────────────────
@router.message(F.chat.type.in_({"group", "supergroup"}))
async def group_message_handler(message: Message, bot: Bot) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id or 0
    user = message.from_user
    user_id = user.id
    username = f"@{user.username}" if user.username else user.full_name

    # АВТОМАТИЧЕСКИЙ СБОР 3:
    # Сохраняем участника ПРИ ЛЮБОМ СООБЩЕНИИ (текст, стикер, фото, кружочек, аудио, файл)
    is_admin = await is_chat_admin(bot, chat_id, user_id)
    await upsert_member(
        user_id=user_id,
        chat_id=chat_id,
        username=user.username or "",
        full_name=user.full_name or "",
        is_admin=is_admin
    )

    botik_thread_id = await get_botik_thread(chat_id)

    # 1. Если сообщение написано ВНУТРИ раздела «Ботик»:
    if botik_thread_id is not None and thread_id == botik_thread_id:
        text = message.text or message.caption or ""
        if not text.startswith("/set_botik") and not text.startswith("/scan") and not text.startswith("/sync"):
            try:
                await message.delete()
            except Exception:
                pass
        return

    # 2. ПРОВЕРКА НА АНТИФЛУД (3 подряд сообщения короче 3 символов или стикеров за 1-3 минуты)
    is_sticker = (message.sticker is not None) or (message.animation is not None)
    raw_text = (message.text or message.caption or "").strip()
    is_short_text = bool(raw_text) and len(raw_text) < 3 and not ("http://" in raw_text or "https://" in raw_text)
    is_flood_item = is_sticker or is_short_text

    flood_key = (chat_id, user_id)
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

    if not is_admin or MODERATE_ALL:
        if is_flood_item:
            recent_stamps = [ts for ts in _short_flood_history.get(flood_key, []) if (now_ts - ts) <= FLOOD_WINDOW_SECONDS]
            recent_stamps.append(now_ts)
            _short_flood_history[flood_key] = recent_stamps

            if len(recent_stamps) >= FLOOD_LIMIT:
                _short_flood_history.pop(flood_key, None)
                reason_ru = "Флуд короткими сообщениями / стикерами (3 подряд)"
                logger.warning("🚨 ФЛУД от %s в топике %s: 3 коротких сообщения/стикера подряд", username, thread_id)

                try:
                    await message.delete()
                except Exception as e:
                    logger.error("Failed to delete flood message: %s", e)

                current_warns = await get_warnings(user_id, chat_id)

                if current_warns == 0:
                    await add_warning(user_id, chat_id, user.username or "", user.full_name or "")
                    await log_violation(user_id, chat_id, username, reason_ru, raw_text or "[Стикер]", "warning_1")

                    notice_text = (
                        f"⚠️ <b>Предупреждение [1/2] для {username}</b>\n\n"
                        f"Причина: <b>{reason_ru}</b>.\n"
                        f"Пожалуйста, не спамьте короткими фразами или стикерами подряд.\n"
                        f"Предупреждение занесено в профиль Ботика. При повторном нарушении — <b>мут на 24 часа</b>."
                    )

                    if botik_thread_id is not None:
                        try:
                            await bot.send_message(
                                chat_id=chat_id,
                                message_thread_id=botik_thread_id if botik_thread_id != 0 else None,
                                text=notice_text,
                                reply_markup=make_group_keyboard(user_id, chat_id)
                            )
                        except Exception:
                            pass

                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            message_thread_id=thread_id if thread_id != 0 else None,
                            text=f"⚠️ {username}, предупреждение <b>1/2</b> за флуд короткими сообщениями/стикерами (3 подряд).",
                            reply_markup=make_group_keyboard(user_id, chat_id)
                        )
                    except Exception:
                        pass

                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"⚠️ <b>Предупреждение от Ботика [1/2]</b>\n\n"
                                f"Вы получили предупреждение за флуд короткими сообщениями или стикерами (3 подряд за 3 минуты).\n"
                                f"При следующем нарушении — мут на 24 часа."
                            ),
                            reply_markup=make_private_keyboard(user_id, chat_id)
                        )
                    except Exception:
                        pass

                else:
                    until = datetime.datetime.now(datetime.timezone.utc) + MUTE_DURATION
                    try:
                        await bot.restrict_chat_member(
                            chat_id=chat_id,
                            user_id=user_id,
                            permissions=ChatPermissions(can_send_messages=False),
                            until_date=until,
                        )
                    except Exception as ex:
                        logger.error("Failed to mute member for flood: %s", ex)

                    await reset_warnings(user_id, chat_id)
                    await log_violation(user_id, chat_id, username, reason_ru, raw_text or "[Стикер]", "mute_24h")

                    mute_text = (
                        f"🔇 <b>{username} отправлен(а) в мут на 24 часа!</b>\n\n"
                        f"Причина: повторный флуд короткими сообщениями / стикерами (получено <b>2/2 предупреждений</b>).\n"
                        f"Статус зафиксирован в приложении <b>Ботик</b>."
                    )

                    if botik_thread_id is not None:
                        try:
                            await bot.send_message(
                                chat_id=chat_id,
                                message_thread_id=botik_thread_id if botik_thread_id != 0 else None,
                                text=mute_text,
                                reply_markup=make_group_keyboard(user_id, chat_id)
                            )
                        except Exception:
                            pass

                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            message_thread_id=thread_id if thread_id != 0 else None,
                            text=mute_text,
                            reply_markup=make_group_keyboard(user_id, chat_id)
                        )
                    except Exception:
                        pass

                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text=f"🔇 Вам выдан мут на 24 часа за повторный флуд короткими сообщениями или стикерами (2/2 предупреждений).",
                            reply_markup=make_private_keyboard(user_id, chat_id)
                        )
                    except Exception:
                        pass

                return
        else:
            if raw_text and len(raw_text) >= 3:
                _short_flood_history.pop(flood_key, None)

    # Если это был стикер (и флуда нет), дальше текстовую модерацию не запускаем
    if is_sticker:
        return

    # 3. Если это не текстовое сообщение или системная команда:
    text = raw_text
    if not text or text.startswith("/"):
        return
    if len(text) < 3 and not ("http://" in text or "https://" in text):
        return

    await increment_stat('checked_messages')

    if is_admin and not MODERATE_ALL:
        return

    try:
        result = await check_message(text)
    except Exception as e:
        logger.error("AI check error: %s", e)
        return

    if not result.get("violation"):
        return

    reason = result.get("reason", "none")
    reason_ru = REASON_MAP.get(reason, reason)
    logger.warning("🚨 НАРУШЕНИЕ от %s в топике %s: %s [%s]", username, thread_id, text[:40], reason_ru)

    try:
        await message.delete()
    except Exception as e:
        logger.error("Failed to delete violation message: %s", e)

    current_warns = await get_warnings(user_id, chat_id)

    if current_warns == 0:
        new_warns = await add_warning(user_id, chat_id, user.username or "", user.full_name or "")
        await log_violation(user_id, chat_id, username, reason_ru, text, "warning_1")

        notice_text = (
            f"⚠️ <b>Предупреждение [1/2] для {username}</b>\n\n"
            f"Ваше сообщение удалено за: <b>{reason_ru}</b>.\n"
            f"Предупреждение занесено во вкладку <b>«👤 Профиль»</b> в приложении <b>Ботик</b>.\n"
            f"При повторном нарушении — мут на 24 часа."
        )

        if botik_thread_id is not None:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=botik_thread_id if botik_thread_id != 0 else None,
                    text=notice_text,
                    reply_markup=make_group_keyboard(user_id, chat_id)
                )
            except Exception as ex:
                logger.error("Post to botik thread error: %s", ex)

        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id if thread_id != 0 else None,
                text=f"⚠️ {username}, ваше сообщение удалено за: <b>{reason_ru}</b>. Предупреждение <b>1/2</b>.",
                reply_markup=make_group_keyboard(user_id, chat_id)
            )
        except Exception as ex:
            logger.error("Post to source thread error: %s", ex)

        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ <b>Предупреждение от Ботика</b>\n\n"
                    f"Ваше сообщение в супергруппе было удалено за: <b>{reason_ru}</b>.\n"
                    f"Удалённый текст: <i>«{text[:150]}»</i>\n\n"
                    f"Счётчик: <b>1/2</b>. Нажмите кнопку ниже, чтобы открыть приложение 👇"
                ),
                reply_markup=make_private_keyboard(user_id, chat_id)
            )
        except Exception:
            pass

    else:
        until = datetime.datetime.now(datetime.timezone.utc) + MUTE_DURATION
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
        except Exception as ex:
            logger.error("Failed to mute member: %s", ex)

        await reset_warnings(user_id, chat_id)
        await log_violation(user_id, chat_id, username, reason_ru, text, "mute_24h")

        mute_text = (
            f"🔇 <b>{username} отправлен(а) в мут на 24 часа!</b>\n\n"
            f"Причина: повторное нарушение (<b>{reason_ru}</b>).\n"
            f"Статус зафиксирован в приложении <b>Ботик</b>."
        )

        if botik_thread_id is not None:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=botik_thread_id if botik_thread_id != 0 else None,
                    text=mute_text,
                    reply_markup=make_group_keyboard(user_id, chat_id)
                )
            except Exception:
                pass

        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id if thread_id != 0 else None,
                text=mute_text,
                reply_markup=make_group_keyboard(user_id, chat_id)
            )
        except Exception:
            pass

        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"🔇 Вам выдан мут на 24 часа за повторное нарушение (<b>{reason_ru}</b>).",
                reply_markup=make_private_keyboard(user_id, chat_id)
            )
        except Exception:
            pass

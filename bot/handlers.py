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

from bot.ai_moderator import check_message, REASON_MAP, chat_with_bot
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
    add_manual_member
)
from bot.config import ADMIN_IDS, WEB_APP_URL, MODERATE_ALL

logger = logging.getLogger(__name__)
router = Router()

MUTE_DURATION = datetime.timedelta(seconds=86400)
BOT_NAME = "🤖 Ботик"
BOT_USERNAME = "kareloai_bot"


def get_webapp_url() -> str:
    return WEB_APP_URL


def make_group_keyboard(user_id: int, chat_id: Optional[int] = None) -> InlineKeyboardMarkup:
    target_url = f"{get_webapp_url()}?uid={user_id}&cid={chat_id or ''}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Открыть Ботик (Telegram Mini App)",
                    url=target_url
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
                    text="🚀 Открыть Ботик",
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

    card_text = (
        "🤖 <b>ПЕРСОНАЛЬНЫЙ БОТИК</b>\n\n"
        "Личный кабинет каждого участника супергруппы.\n\n"
        "• 👤 <b>Профиль:</b> статус, варны и история нарушений\n"
        "• 💬 <b>ИИ Ботик:</b> ответы на любые вопросы\n"
        "• 🎮 <b>Тетрис:</b> игра прямо в Telegram без скролла\n"
        "• 👥 <b>Админка:</b> список всех участников и модерация\n\n"
        "Нажмите кнопку ниже, чтобы открыть ваш личный Ботик 👇"
    )

    try:
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
    reply = await chat_with_bot(text)
    user_id = message.from_user.id if message.from_user else 0
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

    # 2. Если это не текстовое сообщение или системная команда:
    text = message.text or message.caption or ""
    if not text or text.startswith("/"):
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

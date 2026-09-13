import json
import logging
import os
import datetime
from typing import Optional, Dict, Any, List
from aiohttp import web, ClientSession
from aiogram import Bot
from aiogram.types import ChatPermissions

from bot.config import ADMIN_IDS, WEB_PORT, WEB_HOST, BOT_TOKEN
from bot.database import (
    get_user_status,
    get_admin_stats,
    add_warning,
    reset_warnings,
    log_violation,
    upsert_member,
    get_all_members,
    get_member_by_username,
    add_manual_member,
    get_ideas_thread
)
from bot.ai_moderator import chat_with_bot, format_issue_report

logger = logging.getLogger(__name__)

_avatar_cache: Dict[int, str] = {}


def setup_cors(response: web.Response) -> web.Response:
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response


async def options_handler(request: web.Request) -> web.Response:
    resp = web.Response(text='ok')
    return setup_cors(resp)


async def serve_index(request: web.Request) -> web.Response:
    html_path = os.path.join(os.path.dirname(__file__), "..", "webapp", "index.html")
    if not os.path.exists(html_path):
        return web.Response(text="OK", status=200)
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    resp = web.Response(text=content, content_type="text/html", status=200)
    return setup_cors(resp)


async def serve_health(request: web.Request) -> web.Response:
    return setup_cors(web.Response(text="200 OK", status=200))


async def api_avatar(request: web.Request) -> web.Response:
    bot: Bot = request.app['bot']
    user_id_str = request.match_info.get('user_id') or request.query.get('user_id')
    if not user_id_str or not user_id_str.isdigit():
        return web.Response(status=400)

    user_id = int(user_id_str)

    file_url = _avatar_cache.get(user_id)
    if not file_url:
        try:
            photos = await bot.get_user_profile_photos(user_id, limit=1)
            if photos.total_count > 0 and photos.photos:
                file = await bot.get_file(photos.photos[0][-1].file_id)
                file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
                _avatar_cache[user_id] = file_url
        except Exception as e:
            logger.debug("Failed to get profile photo for %s: %s", user_id, e)

    if file_url:
        try:
            async with ClientSession() as session:
                async with session.get(file_url) as resp:
                    if resp.status == 200:
                        image_data = await resp.read()
                        response = web.Response(body=image_data, content_type="image/jpeg")
                        response.headers['Cache-Control'] = 'public, max-age=86400'
                        return setup_cors(response)
        except Exception as ex:
            logger.debug("Proxy avatar error: %s", ex)

    return setup_cors(web.Response(status=404))


async def check_admin_permission(bot: Bot, user_id: int, chat_id: Optional[int] = None) -> bool:
    if user_id in ADMIN_IDS:
        return True

    target_chats = [chat_id] if chat_id else [-1003955632241]

    for cid in target_chats:
        try:
            member = await bot.get_chat_member(cid, user_id)
            if member.status in ("administrator", "creator"):
                await upsert_member(user_id, cid, member.user.username or "", member.user.full_name or "", True)
                return True
        except Exception as e:
            logger.debug("Check admin for %s in %s: %s", user_id, cid, e)

    status = await get_user_status(user_id)
    return status.get("is_admin", False)


def format_last_seen(last_seen_iso: Optional[str]) -> tuple[bool, str]:
    if not last_seen_iso:
        return False, "Недавно"

    try:
        dt = datetime.datetime.fromisoformat(last_seen_iso)
        now = datetime.datetime.now(datetime.timezone.utc)
        diff = now - dt

        is_online = diff.total_seconds() < 900
        seconds = int(diff.total_seconds())
        if seconds < 60:
            seen_str = "Только что"
        elif seconds < 3600:
            seen_str = f"{seconds // 60} мин назад"
        elif seconds < 86400:
            seen_str = f"{seconds // 3600} ч назад"
        else:
            seen_str = dt.strftime("%d.%m %H:%M")

        return is_online, seen_str
    except Exception:
        return False, "Недавно"


async def api_user_status(request: web.Request) -> web.Response:
    bot: Bot = request.app['bot']
    user_id_str = request.query.get("user_id")
    chat_id_str = request.query.get("chat_id")

    if not user_id_str:
        return setup_cors(web.json_response({"error": "user_id is required"}, status=400))

    try:
        user_id = int(user_id_str)
        chat_id = int(chat_id_str) if chat_id_str and chat_id_str.lstrip("-").isdigit() else -1003955632241
        data = await get_user_status(user_id, chat_id)

        is_adm = await check_admin_permission(bot, user_id, chat_id)
        data["is_admin"] = is_adm

        await upsert_member(user_id, chat_id, data.get("username", ""), data.get("full_name", ""), is_adm)

        return setup_cors(web.json_response(data))
    except Exception as e:
        logger.error("api_user_status error: %s", e)
        return setup_cors(web.json_response({"error": str(e)}, status=500))


async def api_admin_stats(request: web.Request) -> web.Response:
    bot: Bot = request.app['bot']
    user_id_str = request.query.get("user_id")
    chat_id_str = request.query.get("chat_id")

    user_id = int(user_id_str) if user_id_str and user_id_str.isdigit() else 0
    chat_id = int(chat_id_str) if chat_id_str and chat_id_str.lstrip("-").isdigit() else -1003955632241

    if user_id:
        is_admin = await check_admin_permission(bot, user_id, chat_id)
        if not is_admin:
            return setup_cors(web.json_response({"error": "Доступ запрещен. Только для администраторов."}, status=403))

    try:
        # Получаем реальное число участников в группе из Telegram API
        total_count = 0
        if chat_id:
            try:
                total_count = await bot.get_chat_member_count(chat_id)
            except Exception as ex:
                logger.warning("get_chat_member_count error: %s", ex)

            try:
                admins = await bot.get_chat_administrators(chat_id)
                for a in admins:
                    if not a.user.is_bot:
                        await upsert_member(a.user.id, chat_id, a.user.username or "", a.user.full_name or "", True)
            except Exception as ex:
                logger.warning("Auto scan admins error: %s", ex)

        data = await get_admin_stats(chat_id)
        data["total_group_members"] = total_count

        now = datetime.datetime.now(datetime.timezone.utc)
        for m in data.get("members", []):
            is_online, seen_str = format_last_seen(m.get("last_seen"))
            m["is_online"] = is_online
            m["last_seen_str"] = seen_str

        return setup_cors(web.json_response(data))
    except Exception as e:
        logger.error("api_admin_stats error: %s", e)
        return setup_cors(web.json_response({"error": str(e)}, status=500))


async def api_add_member(request: web.Request) -> web.Response:
    """Поиск и добавление участника в список модерации по ID или @username."""
    bot: Bot = request.app['bot']
    try:
        body = await request.json()
        raw_query = str(body.get("query", "")).strip()
        chat_id = int(body.get("chat_id", -1003955632241))

        if not raw_query:
            return setup_cors(web.json_response({"success": False, "message": "Введите @username или Telegram ID"}))

        query = raw_query.lstrip("@")

        # 1. Если введен числовой ID
        if query.isdigit():
            target_id = int(query)
            try:
                member = await bot.get_chat_member(chat_id, target_id)
                is_adm = member.status in ("administrator", "creator")
                await upsert_member(
                    user_id=member.user.id,
                    chat_id=chat_id,
                    username=member.user.username or "",
                    full_name=member.user.full_name or "",
                    is_admin=is_adm
                )
                return setup_cors(web.json_response({
                    "success": True,
                    "message": f"Участник {member.user.full_name} успешно добавлен в список!"
                }))
            except Exception as ex:
                return setup_cors(web.json_response({
                    "success": False,
                    "message": f"Участник с ID {target_id} не найден в группе: {ex}"
                }))

        # 2. Если введен юзернейм (@username или просто username)
        # Проверим, есть ли он уже в базе
        existing = await get_member_by_username(query, chat_id)
        if existing:
            return setup_cors(web.json_response({
                "success": True,
                "message": f"Участник @{query} ({existing.get('full_name', '')}) уже есть в списке!"
            }))

        # Если ещё нет в базе, добавляем по юзернейму
        pseudo_id = await add_manual_member(username=query, full_name=f"@{query}", chat_id=chat_id)
        return setup_cors(web.json_response({
            "success": True,
            "message": f"Участник @{query} успешно добавлен в админку! Теперь можно выдать варн или мут."
        }))

    except Exception as e:
        logger.error("api_add_member error: %s", e)
        return setup_cors(web.json_response({"success": False, "message": str(e)}, status=500))


async def api_chat(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        message = body.get("message", "").strip()
        history = body.get("history", [])
        if not message:
            return setup_cors(web.json_response({"reply": "Напиши мне что-нибудь!"}))

        reply = await chat_with_bot(message, history)
        return setup_cors(web.json_response({"reply": reply}))
    except Exception as e:
        logger.error("api_chat error: %s", e)
        return setup_cors(web.json_response({"reply": "Ошибка обработки сообщения."}, status=500))


async def api_admin_action(request: web.Request) -> web.Response:
    bot: Bot = request.app['bot']
    try:
        body = await request.json()
        admin_id = body.get("admin_id")
        action = body.get("action")
        target_raw = str(body.get("user_id", "")).strip().lstrip("@")
        chat_id_raw = body.get("chat_id")
        duration_seconds = int(body.get("duration_seconds", 86400))
        unmute_type = body.get("unmute_type", "all")

        target_user_id = int(target_raw) if (target_raw.isdigit() or (target_raw.startswith("-") and target_raw[1:].isdigit())) else 0
        chat_id = int(chat_id_raw) if chat_id_raw and str(chat_id_raw).lstrip("-").isdigit() else -1003955632241

        if admin_id:
            is_adm = await check_admin_permission(bot, int(admin_id), chat_id)
            if not is_adm:
                return setup_cors(web.json_response({"success": False, "message": "Доступ запрещен. Вы не администратор."}))

        if not target_user_id and target_raw:
            # Попробуем найти по username
            existing = await get_member_by_username(target_raw, chat_id)
            if existing:
                target_user_id = existing["user_id"]

        if not target_user_id:
            return setup_cors(web.json_response({"success": False, "message": "Не найден участник для действия"}))

        if action == "warn":
            new_count = await add_warning(target_user_id, chat_id)
            await log_violation(target_user_id, chat_id, "", "Ручной варн из админки", "Admin Panel", f"warning_{new_count}")
            return setup_cors(web.json_response({
                "success": True,
                "message": f"⚠️ Предупреждение выдано пользователю {target_user_id} ({new_count}/2)"
            }))

        elif action == "mute":
            until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=duration_seconds)
            time_labels = {
                3600: "1 час",
                7200: "2 часа",
                10800: "3 часа",
                14400: "4 часа",
                18000: "5 часов",
                86400: "24 часа",
                259200: "3 дня",
                604800: "1 неделю"
            }
            time_str = time_labels.get(duration_seconds, f"{duration_seconds // 3600}ч")

            try:
                await bot.restrict_chat_member(
                    chat_id,
                    target_user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until,
                )
            except Exception as ex:
                logger.warning("Telegram API restrict error: %s", ex)

            await log_violation(target_user_id, chat_id, "", f"Мут от админа на {time_str}", "Admin Panel", f"mute_{duration_seconds}s")
            return setup_cors(web.json_response({
                "success": True,
                "message": f"🔇 Пользователь {target_user_id} замучен на {time_str}"
            }))

        elif action == "unmute":
            msg_parts = []
            if unmute_type in ("all", "mute_only"):
                try:
                    await bot.restrict_chat_member(
                        chat_id,
                        target_user_id,
                        permissions=ChatPermissions(
                            can_send_messages=True,
                            can_send_media_messages=True,
                            can_send_other_messages=True,
                            can_add_web_page_previews=True,
                        ),
                    )
                    msg_parts.append("Мут снят")
                except Exception as ex:
                    logger.warning("Telegram API unrestrict error: %s", ex)

            if unmute_type in ("all", "warnings_only"):
                await reset_warnings(target_user_id, chat_id)
                msg_parts.append("Варны сброшены (0/2)")

            return setup_cors(web.json_response({
                "success": True,
                "message": f"🔓 Ограничения пользователя {target_user_id} обновлены: {', '.join(msg_parts)}"
            }))

        return setup_cors(web.json_response({"success": False, "message": "Неизвестное действие"}))
    except Exception as e:
        logger.error("api_admin_action error: %s", e)
        return setup_cors(web.json_response({"success": False, "message": str(e)}, status=500))


async def api_report_issue(request: web.Request) -> web.Response:
    """Принимает баг-репорт/идею от пользователя, улучшает с помощью ИИ и отправляет в топик «Идеи»."""
    bot: Bot = request.app['bot']
    try:
        body = await request.json()
        raw_text = str(body.get("text", "")).strip()
        user_id = int(body.get("user_id", 0))
        chat_id = int(body.get("chat_id", -1003955632241))
        user_name = body.get("user_name", "Участник")
        username = body.get("username", "")

        if not raw_text or len(raw_text) < 5:
            return setup_cors(web.json_response({
                "success": False,
                "message": "Пожалуйста, опишите проблему или предложение подробнее (минимум 5 символов)."
            }))

        # Перефразируем проблему через Gemini
        user_tag = f"@{username}" if username else user_name
        ai_formatted = await format_issue_report(raw_text, f"{user_name} ({user_tag})")

        # Определяем ID топика «Идеи»
        ideas_thread_id = await get_ideas_thread(chat_id)

        report_message = (
            f"💡 <b>НОВОЕ ОБРАЩЕНИЕ: ПРОБЛЕМА / ИДЕЯ ПО БОТУ</b>\n\n"
            f"👤 <b>Отправитель:</b> {user_name} ({user_tag}, <code>{user_id}</code>)\n"
            f"📝 <b>Исходный текст:</b>\n<i>«{raw_text}»</i>\n\n"
            f"🤖 <b>Анализ и структурирование от ИИ:</b>\n"
            f"{ai_formatted}"
        )

        sent = False
        # 1. Сначала пробуем отправить в привязанный топик «Идеи»
        if ideas_thread_id is not None:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=ideas_thread_id if ideas_thread_id != 0 else None,
                    text=report_message
                )
                sent = True
            except Exception as ex:
                logger.warning("Failed to send report to ideas thread %s: %s", ideas_thread_id, ex)

        # 2. Если топик «Идеи» не настроен или ошибка, отправляем админам в ЛС
        if not sent:
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=f"📢 [В топик Идеи не удалось отправить (укажите <code>/set_ideas</code> в супергруппе)]\n\n{report_message}"
                    )
                    sent = True
                except Exception:
                    pass

        return setup_cors(web.json_response({
            "success": True,
            "message": "Спасибо! Ваше обращение обработано нейросетью и отправлено администрации в раздел «Идеи»."
        }))

    except Exception as e:
        logger.error("api_report_issue error: %s", e)
        return setup_cors(web.json_response({"success": False, "message": str(e)}, status=500))


def create_web_app(bot: Bot) -> web.Application:
    app = web.Application()
    app['bot'] = bot

    app.router.add_route('OPTIONS', '/{tail:.*}', options_handler)
    app.router.add_get('/', serve_index)
    app.router.add_get('/health', serve_health)
    app.router.add_get('/ping', serve_health)
    app.router.add_get('/index.html', serve_index)
    app.router.add_get('/webapp/index.html', serve_index)
    app.router.add_get('/api/avatar/{user_id}', api_avatar)
    app.router.add_get('/api/user_status', api_user_status)
    app.router.add_get('/api/admin_stats', api_admin_stats)
    app.router.add_post('/api/add_member', api_add_member)
    app.router.add_post('/api/chat', api_chat)
    app.router.add_post('/api/admin/action', api_admin_action)
    app.router.add_post('/api/report_issue', api_report_issue)

    return app

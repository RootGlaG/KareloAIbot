import asyncio
import logging
import sys
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp, WebAppInfo, MenuButtonDefault

from bot.config import BOT_TOKEN, WEB_PORT, WEB_HOST, WEB_APP_URL
from bot.database import init_db
from bot.handlers import router
from bot.web_server import create_web_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def main() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set. Check your .env file.")
        sys.exit(1)

    # 1. Инициализируем базу данных
    await init_db()

    # 2. Инициализируем Telegram Bot
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    # 3. Настраиваем кнопку меню Web App (если задан WEB_APP_URL)
    if WEB_APP_URL and WEB_APP_URL.startswith("https://"):
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="✨ Открыть Ботик ✨",
                    web_app=WebAppInfo(url=WEB_APP_URL)
                )
            )
            logger.info("Menu button set to WebApp: %s", WEB_APP_URL)
        except Exception as e:
            logger.warning("Could not set WebApp menu button: %s", e)

    # 4. Создаем и запускаем встроенный Web-сервер (aiohttp)
    web_app = create_web_app(bot)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    logger.info("Web App & API server running on http://%s:%s", WEB_HOST, WEB_PORT)

    # 5. Синхронизируем администраторов чата и восстанавливаем участников
    try:
        from bot.database import upsert_member, save_members_cache
        default_chat_id = -1003955632241
        admins = await bot.get_chat_administrators(default_chat_id)
        for a in admins:
            if not a.user.is_bot:
                await upsert_member(
                    user_id=a.user.id,
                    chat_id=default_chat_id,
                    username=a.user.username or "",
                    full_name=a.user.full_name or f"@{a.user.username}",
                    is_admin=True
                )
        await save_members_cache()
        logger.info("Startup sync: loaded %d chat administrators into database", len(admins))
    except Exception as e:
        logger.warning("Startup admin sync skipped: %s", e)

    # 6. Запускаем Telegram Polling
    try:
        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "edited_message",
                "message_reaction",
                "chat_member",
                "my_chat_member",
            ],
        )
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

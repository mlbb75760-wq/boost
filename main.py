import asyncio
import logging
import sys

if sys.version_info < (3, 10):
    sys.exit("❌ Нужен Python 3.10 или новее (сейчас %s)." % sys.version.split()[0])

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp, WebAppInfo, ErrorEvent
from config import BOT_TOKEN, SITE_URL, REDIS_URL, DROP_PENDING_UPDATES, ADMIN_CHAT_ID, ADMIN_USER_IDS
import database as db
from handlers import start, menu, orders, balance, admin
from middlewares import CallbackGuardMiddleware
from storage import PostgresStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def build_storage():
    if REDIS_URL:
        from aiogram.fsm.storage.redis import RedisStorage
        return RedisStorage.from_url(REDIS_URL)
    # по умолчанию состояния диалогов лежат в Postgres и не теряются при перезапуске
    return PostgresStorage()


async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=build_storage())

    dp.callback_query.outer_middleware(CallbackGuardMiddleware())

    @dp.errors()
    async def on_error(event: ErrorEvent):
        """Любое необработанное исключение: пишем в лог и сообщаем пользователю,
        вместо молчаливого «бот не отвечает»."""
        logging.error("Необработанная ошибка", exc_info=event.exception)
        update = event.update
        try:
            if update.callback_query:
                await update.callback_query.answer("⚠️ Произошла ошибка. Попробуйте ещё раз.", show_alert=True)
            elif update.message:
                await update.message.answer("⚠️ Произошла ошибка. Попробуйте ещё раз.")
        except Exception:
            pass
        return True

    dp.include_router(admin.router)   # админ-команды и кнопки выше остальных
    dp.include_router(start.router)
    dp.include_router(orders.router)
    dp.include_router(balance.router)
    dp.include_router(menu.router)

    await db.init_db()
    if ADMIN_CHAT_ID < 0 and not ADMIN_USER_IDS:
        logging.warning(
            "ADMIN_CHAT_ID — группа, а ADMIN_USER_IDS не задан: управлять ботом сможет любой участник группы. "
            "Задайте ADMIN_USER_IDS (user_id админов через запятую)."
        )
    try:
        # По умолчанию накопленные за простой сообщения НЕ сбрасываются
        # (раньше терялись чеки и заявки, присланные во время перезапуска).
        await bot.delete_webhook(drop_pending_updates=DROP_PENDING_UPDATES)
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="🌐 Наш сайт", web_app=WebAppInfo(url=SITE_URL))
        )
        await dp.start_polling(bot)
    finally:
        await db.close_db()
        await dp.storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from db import init_db
from middlewares import CallbackAnswerMiddleware

from routers.user import user_r
from routers.admin import admin_r

logger = logging.getLogger(__name__)

async def _health(request: web.Request) -> web.Response:
    return web.Response(text="OK")

async def start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info("health server listening on 0.0.0.0:%s", port)
    return runner

async def main():
    logging.basicConfig(
        level=logging.INFO, 
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
        )
    logger.info("starting bot")

    await init_db()
    dp = Dispatcher(storage=MemoryStorage())
    dp.callback_query.outer_middleware(CallbackAnswerMiddleware())

    dp.include_router(user_r)
    dp.include_router(admin_r)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    runner = await start_health_server()

    logger.info("bot is running (polling)")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        logger.info("bot stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Stopped.")

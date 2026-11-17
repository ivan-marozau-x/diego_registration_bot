from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery
from aiogram.exceptions import TelegramAPIError
from aiogram.dispatcher.event.bases import UNHANDLED

class CallbackAnswerMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: CallbackQuery, data):
        try:
            result = await handler(event, data)
        except Exception:
            try: await event.answer("Ошибка. Попробуйте позже.", show_alert=True)
            except TelegramAPIError: pass
            raise
        try:
            if result is UNHANDLED:
                await event.answer("Кнопка устарела. Нажмите /start", show_alert=True)
            else:
                await event.answer()
        except TelegramAPIError:
            pass
        return result
    
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message


class CallbackGuardMiddleware(BaseMiddleware):
    """1) Нажатие кнопки под слишком старым сообщением (Telegram отдаёт его как
    InaccessibleMessage без методов edit_text/answer) раньше валило обработчик —
    теперь пользователь получает понятный ответ.
    2) Если обработчик сам не ответил на callback, отвечаем после него, чтобы у кнопки
    не крутился индикатор загрузки."""

    async def __call__(self, handler, event: CallbackQuery, data):
        if not isinstance(event.message, Message):
            await event.answer("Сообщение устарело. Откройте меню заново.", show_alert=True)
            return None
        result = await handler(event, data)
        try:
            await event.answer()
        except TelegramBadRequest:
            pass  # уже отвечено обработчиком
        return result

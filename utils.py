import datetime

from config import TZ_OFFSET_HOURS

BANNED_TEXT = "⛔ Вы заблокированы за нарушение правил (некорректные данные). Обратитесь в поддержку."


def user_ref(user_id: int, username: str | None) -> str:
    """Единый формат ссылки на пользователя для админ-уведомлений.
    Если username не задан (в Telegram это не обязательное поле),
    возвращает id без "(@None)"."""
    if username:
        return f"{user_id} (@{username})"
    return f"{user_id} (без username)"


def to_local(dt: datetime.datetime) -> datetime.datetime:
    """UTC (как хранится в БД) -> местное время для показа людям."""
    return dt + datetime.timedelta(hours=TZ_OFFSET_HOURS)


def fmt_local(dt: datetime.datetime, fmt: str = "%d.%m.%Y %H:%M") -> str:
    return to_local(dt).strftime(fmt)


def parse_uint(text: str | None) -> int | None:
    """Неотрицательное целое из строки. Только ASCII-цифры: str.isdigit() пропускает
    символы вроде '²', на которых int() падает с ValueError."""
    if text and text.isascii() and text.isdigit():
        return int(text)
    return None


def parse_int(text: str | None) -> int | None:
    """Целое со знаком (для ручных корректировок баланса)."""
    if not text:
        return None
    if text[0] == "-":
        value = parse_uint(text[1:])
        return -value if value is not None else None
    return parse_uint(text)


async def safe_delete(message) -> None:
    """Удаление сообщения, которое не должно ломать сценарий (старше 48 ч, уже удалено и т.п.)."""
    try:
        await message.delete()
    except Exception:
        pass

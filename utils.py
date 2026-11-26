from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from config import settings
from constants import SKIP_WORDS
from aiogram.exceptions import TelegramBadRequest

import html

async def safe_edit_text(message, text: str, reply_markup=None, parse_mode="HTML"):
    """Безопасно редактирует сообщение, игнорируя ошибку 'message is not modified'."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            pass
        else:
            raise


def esc(text: str | None) -> str:
    """Экранирует HTML-теги для безопасного вывода в Telegram."""
    if not text:
        return ""
    return html.escape(text, quote=False)

def parse_datetime_maybe(s: str) -> datetime | None:
    s = s.strip()
    if s.lower() in SKIP_WORDS:
        return None
    fmts = ["%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d", "%d.%m.%Y"]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            try:
                return dt.replace(tzinfo=ZoneInfo(settings.tz))
            except Exception:
                return dt
        except ValueError:
            continue
    return None

def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    try:
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo(settings.tz))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(dt)


def to_utc(dt: datetime | None) -> datetime | None:
    """Приводит дату к UTC. Наивные даты считаются по BOT_TZ."""
    if not dt:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(settings.tz)).astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)
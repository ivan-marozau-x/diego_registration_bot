from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from config import settings
from constants import SKIP_WORDS

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
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

DEFAULT_DB_URL = "sqlite+aiosqlite:///./bot.db"
DEFAULT_TZ = "Europe/Minsk"
ENVS = (".env.dev", ".env")

@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: tuple[int, ...]
    database_url: str = DEFAULT_DB_URL
    tz: str = DEFAULT_TZ

    def __post_init__(self) -> None:
        if not self.bot_token or not self.bot_token.strip():
            raise ValueError("BOT_TOKEN should not be empty")
        if not self.database_url or not self.database_url.strip():
            raise ValueError("BASE_URL should not be empty")
        try:
            ZoneInfo(self.tz)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Incorrect timezone: {self.tz!r}") from exc

def _resolve_env_file(envs: tuple[str, ...] = ENVS) -> Path | None:
    for filename in envs:
        path = Path(filename)
        if path.is_file():
            return path
    return None

def _parse_admin_ids(raw: str | None) -> tuple[int, ...]:
    if not raw or not raw.strip():
        return tuple()

    parsed: list[int] = []
    for item in raw.split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        if not cleaned.isdigit():
            raise ValueError (f"Incorrect admin's id in ADMIN_IDS: {cleaned!r}")
        parsed.append(int(cleaned))
    return tuple(parsed)

def load_settings(env_path: str | Path | None = None) -> Settings:
    target_env = Path(env_path) if env_path is not None else _resolve_env_file()
    if target_env is not None and target_env.is_file():
        load_dotenv(dotenv_path=target_env, override=False)

    raw_token = os.environ.get("BOT_TOKEN")
    if not raw_token:
        raise RuntimeError("BOT_TOKEN is not set for the environment")

    return Settings(
        bot_token=raw_token.strip(),
        admin_ids=_parse_admin_ids(os.environ.get("ADMIN_IDS")),
        database_url=os.environ.get("DATABASE_URL", DEFAULT_DB_URL).strip(),
        tz=os.environ.get("BOT_TZ", DEFAULT_TZ).strip(),
        )

settings = load_settings()

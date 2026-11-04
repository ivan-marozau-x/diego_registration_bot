from typing import Final

# ---- Статусы регистрации ----
STATUS_CONFIRMED: Final = "confirmed"
STATUS_WAITLIST: Final = "waitlist"

# ---- Ввод "пропустить шаг" ----
SKIP_WORDS: Final = frozenset({"skip", "пропуск", "не", "нет", ""})

# ---- Лимиты отображения ----
PAGE_SIZE: Final = 8
TEAMS_LIST_LIMIT: Final = 30

# ---- Пороги валидации ----
TEAM_NAME_MIN: Final = 2
TEAM_NAME_MAX: Final = 80

GAME_TITLE_MIN = 2
GAME_TITLE_MAX = 200
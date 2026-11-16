from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from core import is_admin, list_active_games, session_scope
from keyboards import admin_main_kb, admin_back_kb, games_list_kb
from utils import fmt_dt

async def _show_admin_home(target: Message, edit: bool = False):
    text = "Панель администратора"
    kb = admin_main_kb()
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


async def _show_user_home(target: Message, user_id: int, edit: bool = False):
    async with session_scope() as s:
        data = await _get_active_games_data(s)

    admin_flag = is_admin(user_id)
    if not data:
        text = "Привет! Пока нет открытых игр для регистрации. Загляни позже."
        kb = admin_back_kb() if admin_flag else None
        if edit:
            await target.edit_text(text, reply_markup=kb)
        else:
            await target.answer(text, reply_markup=kb)
        return

    text = "Выбери игру для регистрации:"
    kb = games_list_kb(data, page=1, is_admin=admin_flag)
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)

async def _get_active_games_data(s: AsyncSession) -> list[tuple[int, str]]:
    games = await list_active_games(s)
    return [(g.id, f"{g.title} ({fmt_dt(g.when)})" if g.when else g.title) for g in games]
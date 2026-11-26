from aiogram.enums import ParseMode
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from core import is_admin, list_active_games, session_scope, get_game_stats
from keyboards import admin_main_kb, admin_back_kb, games_list_kb, admin_game_actions_kb
from models import Game
from render import admin_game_brief
from utils import fmt_dt, safe_edit_text

async def show_admin_home(target: Message, edit: bool = False):
    text = "Панель администратора"
    kb = admin_main_kb()
    if edit:
        await safe_edit_text(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


async def show_user_home(target: Message, user_id: int, edit: bool = False):
    async with session_scope() as s:
        data = await get_active_games_data(s)

    admin_flag = is_admin(user_id)
    if not data:
        text = "Привет! Пока нет открытых игр для регистрации. Загляни позже."
        kb = admin_back_kb() if admin_flag else None
        if edit:
            await safe_edit_text(target, text, reply_markup=kb)
        else:
            await target.answer(text, reply_markup=kb)
        return

    text = "Выбери игру для регистрации:"
    kb = games_list_kb(data, page=1, is_admin=admin_flag)
    if edit:
        await safe_edit_text(target, text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)

async def get_active_games_data(s: AsyncSession) -> list[tuple[int, str]]:
    games = await list_active_games(s)
    return [(g.id, f"{g.title} ({fmt_dt(g.when)})" if g.when else g.title) for g in games]

async def render_admin_game_card(message, gid: int):
    """Рендерит карточку игры (в существующее сообщение)."""
    async with session_scope() as s:
        g = await s.get(Game, gid)
        if not g:
            await safe_edit_text(message, "Игра не найдена.", reply_markup=admin_back_kb())
            return
        stats = await get_game_stats(s, gid)
        text = admin_game_brief(g, stats)
        active = g.is_active
    await safe_edit_text(
        message, text,
        reply_markup=admin_game_actions_kb(gid, active),
        parse_mode=ParseMode.HTML,
    )
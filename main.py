import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, Message, CallbackQuery, FSInputFile

from backup_db import generate_backup
from config import settings
from db import engine, init_db, SessionLocal
from models import Game, Registration
from states import RegisterFlow, EditNameFlow, EditPlayersFlow, AddGameFlow, AdminAddTeamFlow
from keyboards import (
    games_list_kb, reg_manage_kb, cancel_kb,
    admin_main_kb, admin_games_kb, admin_game_actions_kb, admin_teams_list_kb,
    admin_back_kb,
)
from utils import parse_datetime_maybe, fmt_dt, esc
from constants import (
    STATUS_CONFIRMED, STATUS_WAITLIST, SKIP_WORDS,
    TEAM_NAME_MIN, TEAM_NAME_MAX,
    GAME_TITLE_MIN, GAME_TITLE_MAX, TEAMS_LIST_LIMIT,
)

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ----------------------------
# Вспомогательные
# ----------------------------

def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


@asynccontextmanager
async def session_scope():
    async with SessionLocal() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            logger.exception("session_scope failed")
            raise


async def list_active_games(s: AsyncSession) -> list[Game]:
    res = await s.execute(
        select(Game).where(Game.is_active == True).order_by(Game.when.is_(None), Game.when.asc(), Game.id.desc())
    )
    return list(res.scalars())

async def _get_active_games_data(s: AsyncSession) -> list[tuple[int, str]]:
    games = await list_active_games(s)
    return [(g.id, f"{g.title} ({fmt_dt(g.when)})" if g.when else g.title) for g in games]


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



async def count_confirmed_teams(s: AsyncSession, game_id: int) -> int:
    q = select(func.count(Registration.id)).where(
        Registration.game_id == game_id,
        Registration.status == STATUS_CONFIRMED
    )
    return (await s.execute(q)).scalar_one()


async def sum_confirmed_people(s: AsyncSession, game_id: int, exclude_reg_id: int | None = None) -> int:
    conds = [Registration.game_id == game_id, Registration.status == STATUS_CONFIRMED]
    if exclude_reg_id is not None:
        conds.append(Registration.id != exclude_reg_id)
    q = select(func.coalesce(func.sum(Registration.players), 0)).where(*conds)
    return (await s.execute(q)).scalar_one()

@dataclass(frozen=True)
class GameStats:
    confirmed_teams: int
    confirmed_people: int
    waitlist_teams: int

async def get_game_stats(s: AsyncSession, game_id: int) -> GameStats:
    confirmed_teams = await count_confirmed_teams(s, game_id)
    confirmed_people = await sum_confirmed_people(s, game_id)
    q_wait = select(func.count(Registration.id)).where(
        Registration.game_id == game_id, Registration.status == STATUS_WAITLIST
    )
    waitlist_teams = (await s.execute(q_wait)).scalar_one()
    return GameStats(confirmed_teams, confirmed_people, waitlist_teams)


# Брифы: для пользователя (без лимитов) и для админа (с лимитами)
def user_game_brief(g: Game, stats: GameStats) -> str:
    when = fmt_dt(g.when)
    loc = g.location or "—"
    return (
        f"<b>{esc(g.title)}</b>\n"
        f"📅 {when}\n"
        f"📍 {esc(loc)}\n"
        f"👥 Подтверждённых команд: {stats.confirmed_teams}\n" 
        f"    (Список ожидания: {stats.waitlist_teams})\n"
        f"🧑‍🤝‍🧑 Подтверждённых людей: {stats.confirmed_people}\n"
    )


def admin_game_brief(g: Game, stats: GameStats) -> str:
    teams_cap = f"{g.teams_capacity}" if g.teams_capacity is not None else "∞"
    people_cap = f"{g.people_capacity}" if g.people_capacity is not None else "∞"
    maxpt = f"{g.max_players_per_team}" if g.max_players_per_team is not None else "∞"
    when = fmt_dt(g.when)
    loc = g.location or "—"
    return (
        f"<b>{esc(g.title)}</b>\n"
        f"📅 {when}\n"
        f"📍 {esc(loc)}\n"
        f"👥 Команд: {stats.confirmed_teams} / {teams_cap}\n" 
        f"    (Список ожидания: {stats.waitlist_teams})\n"
        f"🧑‍🤝‍🧑 Людей: {stats.confirmed_people} / {people_cap} | Макс/команду: {maxpt}\n"
    )


async def teams_list_text(s: AsyncSession, game_id: int, limit: int = TEAMS_LIST_LIMIT) -> str:
    res = await s.execute(
        select(Registration)
        .where(Registration.game_id == game_id)
        .order_by(Registration.status.asc(), Registration.created_at.asc())
        .limit(limit)
    )
    regs = list(res.scalars())
    if not regs:
        return "Пока нет зарегистрированных команд."
    lines = []
    for i, r in enumerate(regs, start=1):
        row = format_team_row(r)
        lines.append(f"{i}. {esc(row)}")
    cnt_all = (await s.execute(
        select(func.count(Registration.id)).where(Registration.game_id == game_id)
    )).scalar_one()
    tail = f"\n… и ещё {cnt_all - len(regs)} команд(ы)." if cnt_all > len(regs) else ""
    return "\n".join(lines) + tail

def format_team_row(r: Registration) -> str:
    mark = "✅" if r.status == STATUS_CONFIRMED else "⌛"
    return f"{r.team_name} — {r.players} чел. {mark}"

def build_teams_view(regs: list[Registration], title: str) -> tuple[str, list[tuple[int, str]]]:
    lines: list[str] = [f"<b>{esc(title)}</b> — список команд:"]
    pairs: list[tuple[int, str]] = []
    for i, r in enumerate(regs, start=1):
        row = format_team_row(r)
        lines.append(f"{i}. {esc(row)}")
        pairs.append((r.id, f"#{i} {row}"))
    return "\n".join(lines), pairs

async def _load_teams_for_admin(gid: int) -> tuple[Game | None, list[Registration]]:
    async with session_scope() as s:
        g = await s.get(Game, gid)
        if not g:
            return None, []
        res = await s.execute(
            select(Registration)
            .where(Registration.game_id == gid)
            .order_by(Registration.status.asc(), Registration.created_at.asc())
        )
        return g, list(res.scalars())


async def next_admin_user_id(s: AsyncSession) -> int:
    """
    Возвращает следующий уникальный отрицательный user_id для админских ручных добавлений:
    -1, -2, -3, ...
    Это обходит UNIQUE(game_id, user_id), чтобы можно было добавлять много админских команд на одну игру.
    """
    q = select(func.min(Registration.user_id))
    min_uid = (await s.execute(q)).scalar_one()
    if min_uid is None or min_uid >= 0:
        return -1
    return min_uid - 1

async def _health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_health_server() -> web.AppRunner:
    """
    Минимальный HTTP-сервер, чтобы Render увидел открытый порт
    и не убивал деплой как Web Service. Заодно годится как endpoint
    для cron-пинга, чтобы бот не засыпал.
    """
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


# ----------------------------
# Роутер пользователя
# ----------------------------

user_r = Router()


@user_r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    if is_admin(m.from_user.id):
        await _show_admin_home(m)
    else:
        await _show_user_home(m, m.from_user.id)


@user_r.message(Command("whoami"))
async def whoami(m: Message):
    await m.answer(f"Твой Telegram user_id: <code>{m.from_user.id}</code>", parse_mode=ParseMode.HTML)


@user_r.callback_query(F.data.startswith("page:"))
async def paginate_games(cq: CallbackQuery):
    page = int(cq.data.split(":")[1])
    async with session_scope() as s:
        data = await _get_active_games_data(s)
    kb = games_list_kb(data, page=page, is_admin=is_admin(cq.from_user.id))
    await cq.message.edit_reply_markup(reply_markup=kb)
    await cq.answer()


@user_r.callback_query(F.data == "my_regs")
async def my_regs(cq: CallbackQuery):
    uid = cq.from_user.id
    async with session_scope() as s:
        res = await s.execute(
            select(Registration, Game).join(Game, Registration.game_id == Game.id).where(Registration.user_id == uid)
        )
        rows = res.all()
        if not rows:
            await cq.message.answer("У тебя пока нет регистраций.")
            await cq.answer()
            return

        for reg, game in rows:
            stats = await get_game_stats(s, game.id)
            brief = user_game_brief(game, stats)
            teams_text = await teams_list_text(s, game.id, limit=60)

            text = (
                f"{brief}"
                f"<b>Твоя регистрация</b>\n"
                f"• Команда: <b>{esc(reg.team_name)}</b>\n"
                f"• Игроков: <b>{reg.players}</b>\n"
                f"• Статус: <b>{reg.status}</b>\n\n"
                f"<b>Уже зарегистрированы:</b>\n{teams_text}"
            )
            await cq.message.answer(text, reply_markup=reg_manage_kb(reg.id), parse_mode=ParseMode.HTML)
    await cq.answer()


@user_r.callback_query(F.data.startswith("game:"))
async def choose_game(cq: CallbackQuery, state: FSMContext):
    game_id = int(cq.data.split(":")[1])
    async with session_scope() as s:
        game = await s.get(Game, game_id)
        if not game or not game.is_active:
            await cq.answer("Эта игра недоступна.", show_alert=True)
            return
        uid = cq.from_user.id
        existing = await s.execute(select(Registration).where(Registration.user_id == uid, Registration.game_id == game_id))
        if existing.scalar_one_or_none():
            await cq.message.answer("У тебя уже есть регистрация на эту игру. Открой «Мои регистрации», чтобы изменить или удалить.")
            await cq.answer()
            return

    await state.set_state(RegisterFlow.entering_team_name)
    await state.update_data(game_id=game_id)

    async with session_scope() as s:
        g = await s.get(Game, game_id)
        stats = await get_game_stats(s, game_id)
        brief = user_game_brief(g, stats)
        teams_text = await teams_list_text(s, game_id, limit=60)

    await cq.message.answer(brief + "\n<b>Уже зарегистрированы:</b>\n" + teams_text, parse_mode=ParseMode.HTML)
    await cq.message.answer(f"Теперь введи <b>название команды</b> ({TEAM_NAME_MIN}–{TEAM_NAME_MAX} символов):", reply_markup=cancel_kb(), parse_mode=ParseMode.HTML)
    await cq.answer()


@user_r.callback_query(F.data == "cancel")
async def cancel_any(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.message.answer("Действие отменено.")
    await cq.answer()


@user_r.message(RegisterFlow.entering_team_name)
async def team_name_step(m: Message, state: FSMContext):
    name = (m.text or "").strip()
    if not (TEAM_NAME_MIN <= len(name) <= TEAM_NAME_MAX):
        await m.answer(f"Название команды должно быть {TEAM_NAME_MIN}–{TEAM_NAME_MAX} символов. Попробуй снова:")
        return
    data = await state.get_data()
    game_id = data["game_id"]
    async with session_scope() as s:
        d = await s.execute(select(Registration).where(Registration.game_id == game_id, Registration.team_name == name))
        if d.scalar_one_or_none():
            await m.answer("Это имя уже занято в этой игре. Введи другое имя команды:")
            return
    await state.update_data(team_name=name)
    await state.set_state(RegisterFlow.entering_players)

    async with session_scope() as s:
        g = await s.get(Game, game_id)
        if g and g.max_players_per_team:
            await m.answer("Сколько человек в команде? (установлен лимит, введите положительное число):")
        else:
            await m.answer("Сколько человек в команде? (введите положительное число):")


@user_r.message(RegisterFlow.entering_players)
async def players_step(m: Message, state: FSMContext):
    try:
        players = int((m.text or "").strip())
    except ValueError:
        await m.answer("Нужно положительное число. Попробуй ещё раз:")
        return
    if players <= 0:
        await m.answer("Число должно быть положительным. Попробуй ещё раз:")
        return

    data = await state.get_data()
    game_id = data["game_id"]
    team_name = data["team_name"]
    uid = m.from_user.id
    chat_id = m.chat.id

    async with session_scope() as s:
        game = await s.get(Game, game_id)
        if not game or not game.is_active:
            await m.answer("Игра больше недоступна для регистрации.")
            await state.clear()
            return

        # Лимит людей на команду (скрытый для пользователя)
        if game.max_players_per_team is not None and players > game.max_players_per_team:
            await m.answer("Число превышает допустимый предел для одной команды. Введите меньшее число:")
            return

        status = STATUS_CONFIRMED
        confirmed_teams = await count_confirmed_teams(s, game_id)
        confirmed_people = await sum_confirmed_people(s, game_id)

        teams_over = (game.teams_capacity is not None) and (confirmed_teams >= game.teams_capacity)
        people_over = (game.people_capacity is not None) and ((confirmed_people + players) > game.people_capacity)

        if teams_over or people_over:
            status = STATUS_WAITLIST

        reg = Registration(
            user_id=uid,
            chat_id=chat_id,
            game_id=game_id,
            team_name=team_name,
            players=players,
            status=status,
        )
        s.add(reg)
        try:
            await s.flush()
        except IntegrityError:
            await m.answer("Похоже, такая регистрация уже существует или имя занято. Попробуй снова.")
            return

        await state.clear()
        await m.answer(
            f"✅ Регистрация создана!\n\n"
            f"Игра: <b>{esc(game.title)}</b>\n"
            f"Когда: {fmt_dt(game.when)} | Где: {game.location or '—'}\n"
            f"Команда: <b>{esc(team_name)}</b>\n"
            f"Игроков: <b>{players}</b>\n"
            f"Статус: <b>{status}</b>",
            parse_mode=ParseMode.HTML
        )


# --------- Редактирование / удаление регистрации пользователем ----------

@user_r.callback_query(F.data.startswith("edit_name:"))
async def edit_name_start(cq: CallbackQuery, state: FSMContext):
    reg_id = int(cq.data.split(":")[1])
    await state.set_state(EditNameFlow.entering_new_name)
    await state.update_data(reg_id=reg_id)
    await cq.message.answer(f"Введи новое имя команды ({TEAM_NAME_MIN}–{TEAM_NAME_MAX} символов):", reply_markup=cancel_kb())
    await cq.answer()


@user_r.message(EditNameFlow.entering_new_name)
async def edit_name_apply(m: Message, state: FSMContext):
    name = (m.text or "").strip()
    if not (TEAM_NAME_MIN <= len(name) <= TEAM_NAME_MAX):
        await m.answer(f"Имя {TEAM_NAME_MIN}–{TEAM_NAME_MAX} символов. Попробуй ещё раз:")
        return
    data = await state.get_data()
    reg_id = data["reg_id"]
    uid = m.from_user.id
    async with session_scope() as s:
        reg = await s.get(Registration, reg_id)
        if not reg or reg.user_id != uid:
            await m.answer("Регистрация не найдена.")
            await state.clear()
            return
        exists = await s.execute(
            select(Registration).where(
                Registration.game_id == reg.game_id,
                Registration.team_name == name,
                Registration.id != reg.id
            )
        )
        if exists.scalar_one_or_none():
            await m.answer("Это имя занято в этой игре. Введи другое:")
            return
        reg.team_name = name
        try:
            await s.flush()
        except IntegrityError:
            await m.answer("Не удалось сохранить. Попробуй другое имя.")
            return

    await state.clear()
    await m.answer("Имя команды обновлено.")


@user_r.callback_query(F.data.startswith("edit_players:"))
async def edit_players_start(cq: CallbackQuery, state: FSMContext):
    reg_id = int(cq.data.split(":")[1])
    await state.set_state(EditPlayersFlow.entering_new_players)
    await state.update_data(reg_id=reg_id)
    await cq.message.answer("Введи новое число игроков (положительное):", reply_markup=cancel_kb())
    await cq.answer()


@user_r.message(EditPlayersFlow.entering_new_players)
async def edit_players_apply(m: Message, state: FSMContext):
    try:
        players = int((m.text or "").strip())
    except ValueError:
        await m.answer("Нужно положительное число. Попробуй ещё раз:")
        return
    if players <= 0:
        await m.answer("Нужно положительное число. Попробуй ещё раз:")
        return
    data = await state.get_data()
    reg_id = data["reg_id"]
    uid = m.from_user.id
    async with session_scope() as s:
        reg = await s.get(Registration, reg_id)
        if not reg or reg.user_id != uid:
            await m.answer("Регистрация не найдена.")
            await state.clear()
            return

        game = await s.get(Game, reg.game_id)
        if game.max_players_per_team is not None and players > game.max_players_per_team:
            await m.answer("Число превышает допустимый предел для одной команды. Введите меньшее число:")
            return

        reg.players = players

        confirmed_teams = await count_confirmed_teams(s, reg.game_id)
        confirmed_people_excl = await sum_confirmed_people(s, reg.game_id, exclude_reg_id=reg.id)

        teams_over = (game.teams_capacity is not None) and (confirmed_teams > game.teams_capacity)
        people_over = (game.people_capacity is not None) and ((confirmed_people_excl + players) > game.people_capacity)

        reg.status = STATUS_WAITLIST if (teams_over or people_over) else STATUS_CONFIRMED
        await s.flush()
    await state.clear()
    await m.answer("Число игроков обновлено.")


@user_r.callback_query(F.data.startswith("delete_reg:"))
async def delete_registration(cq: CallbackQuery):
    reg_id = int(cq.data.split(":")[1])
    uid = cq.from_user.id
    async with session_scope() as s:
        reg = await s.get(Registration, reg_id)
        if not reg or reg.user_id != uid:
            await cq.answer("Не найдено.", show_alert=True)
            return
        await s.delete(reg)
    await cq.message.answer("Регистрация удалена.")
    await cq.answer("Удалено")


# ----------------------------
# Роутер администратора
# ----------------------------

admin_r = Router()
admin_r.message.filter(lambda m: m.from_user is not None and is_admin(m.from_user.id))
admin_r.callback_query.filter(lambda cq: cq.from_user is not None and is_admin(cq.from_user.id))


@admin_r.message(Command("admin"))
async def admin_home(m: Message, state: FSMContext):
    await state.clear()
    await _show_admin_home(m)

@admin_r.callback_query(F.data == "admin:back")
async def admin_back(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_admin_home(cq.message, edit=True)
    await cq.answer()

@admin_r.callback_query(F.data == "admin:backup")
async def admin_backup_db(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return
    await cq.answer("Генерирую бэкап...")
    
    # Генерируем JSON в памяти
    backup_bytes = await generate_backup(engine)
    
    # Отправляем файл
    filename = f"backup_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.json"
    document = BufferedInputFile(backup_bytes, filename=filename)
    
    await cq.message.answer_document(
        document, 
        caption="📦 Слепок базы данных готов."
    )

@admin_r.callback_query(F.data == "admin:user_view")
async def admin_user_view(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_user_home(cq.message, cq.from_user.id, edit=True)
    await cq.answer()

@admin_r.callback_query(F.data == "admin:list_games")
async def admin_list_games(cq: CallbackQuery):
    async with session_scope() as s:
        res = await s.execute(select(Game).order_by(Game.id.desc()))
        games = list(res.scalars())
    items = [(g.id, f"{g.title} ({fmt_dt(g.when)})" if g.when else g.title, g.is_active) for g in games]
    text = "Список игр (нажми, чтобы управлять)"
    await cq.message.edit_text(text, reply_markup=admin_games_kb(items))
    await cq.answer()


@admin_r.callback_query(F.data.startswith("admin:game:"))
async def admin_game_open(cq: CallbackQuery):
    gid = int(cq.data.split(":")[2])
    async with session_scope() as s:
        g = await s.get(Game, gid)
        if not g:
            await cq.answer("Игра не найдена", show_alert=True)
            return
        stats = await get_game_stats(s, gid)
        text = admin_game_brief(g, stats)
        active = g.is_active
    await cq.message.edit_text(text, reply_markup=admin_game_actions_kb(gid, active), parse_mode=ParseMode.HTML)
    await cq.answer()


@admin_r.callback_query(F.data.startswith("admin:toggle:"))
async def admin_toggle_game(cq: CallbackQuery):
    gid = int(cq.data.split(":")[2])
    async with session_scope() as s:
        g = await s.get(Game, gid)
        if not g:
            await cq.answer("Не найдено", show_alert=True); return
        g.is_active = not g.is_active
        await s.flush()
        stats = await get_game_stats(s, gid)
        text = admin_game_brief(g, stats)
        active = g.is_active
    await cq.message.edit_text(text, reply_markup=admin_game_actions_kb(gid, active), parse_mode=ParseMode.HTML)
    await cq.answer("Готово")


@admin_r.callback_query(F.data.startswith("admin:teams:"))
async def admin_show_teams(cq: CallbackQuery):
    gid = int(cq.data.split(":")[2])
    g, regs = await _load_teams_for_admin(gid)
    if not g:
        await cq.answer("Игра не найдена", show_alert=True); return
    if not regs:
        await cq.message.answer("Нет регистраций на эту игру.")
        await cq.answer(); return

    text, pairs = build_teams_view(regs, g.title)

    await cq.message.answer(text, reply_markup=admin_teams_list_kb(pairs, gid), parse_mode=ParseMode.HTML)
    await cq.answer()


@admin_r.callback_query(F.data.startswith("admin:add_team:"))
async def admin_add_team_start(cq: CallbackQuery, state: FSMContext):
    gid = int(cq.data.split(":")[2])
    await state.set_state(AdminAddTeamFlow.entering_team_name)
    await state.update_data(game_id=gid)
    await cq.message.answer("Введите имя новой команды:", reply_markup=cancel_kb())
    await cq.answer()


@admin_r.message(AdminAddTeamFlow.entering_team_name)
async def admin_add_team_name(m: Message, state: FSMContext):
    name = (m.text or "").strip()
    if not (TEAM_NAME_MIN <= len(name) <= TEAM_NAME_MAX):
        await m.answer(f"Имя {TEAM_NAME_MIN}–{TEAM_NAME_MAX}  символов. Попробуйте снова:")
        return
    await state.update_data(team_name=name)
    await state.set_state(AdminAddTeamFlow.entering_players)
    await m.answer("Сколько игроков в команде? (положительное число):")


@admin_r.message(AdminAddTeamFlow.entering_players)
async def admin_add_team_players(m: Message, state: FSMContext):
    try:
        players = int((m.text or "").strip())
    except ValueError:
        await m.answer("Нужно положительное число. Попробуйте снова:")
        return
    if players <= 0:
        await m.answer("Число должно быть положительным. Попробуйте снова:")
        return

    data = await state.get_data()
    gid = data["game_id"]
    name = data["team_name"]

    async with session_scope() as s:
        g = await s.get(Game, gid)
        if not g:
            await m.answer("Игра не найдена."); await state.clear(); return

        # Проверка уникальности имени в рамках игры
        exists = await s.execute(select(Registration).where(Registration.game_id == gid, Registration.team_name == name))
        if exists.scalar_one_or_none():
            await m.answer("Имя уже занято. Введите другое:")
            await state.set_state(AdminAddTeamFlow.entering_team_name)
            return

        # Лимит на людей в одной команде
        if g.max_players_per_team is not None and players > g.max_players_per_team:
            await m.answer("Число превышает допустимый предел для одной команды. Введите меньшее число:")
            return

        # Решение о статусе (как для обычной регистрации)
        status = STATUS_CONFIRMED
        confirmed_teams = await count_confirmed_teams(s, gid)
        confirmed_people = await sum_confirmed_people(s, gid)

        teams_over = (g.teams_capacity is not None) and (confirmed_teams >= g.teams_capacity)
        people_over = (g.people_capacity is not None) and ((confirmed_people + players) > g.people_capacity)

        if teams_over or people_over:
            status = STATUS_WAITLIST

        # 🔧 КЛЮЧЕВАЯ ПРАВКА: уникальный отрицательный user_id для админских команд
        admin_uid = await next_admin_user_id(s)

        reg = Registration(
            user_id=admin_uid,
            chat_id=0,
            game_id=gid,
            team_name=name,
            players=players,
            status=status
        )
        s.add(reg)
        try:
            await s.flush()
        except IntegrityError:
            await m.answer("Не удалось сохранить. Попробуйте ещё раз.")
            return

        await m.answer(f"Команда «{esc(name)}» добавлена ({players} чел., статус: {status}).")
    await state.clear()


@admin_r.callback_query(F.data.startswith("admin:delteam:"))
async def admin_delete_team(cq: CallbackQuery):
    reg_id = int(cq.data.split(":")[2])
    async with session_scope() as s:
        reg = await s.get(Registration, reg_id)
        if not reg:
            await cq.answer("Команда не найдена", show_alert=True); return
        gid = reg.game_id
        await s.delete(reg)
    await cq.answer("Удалено")

    g, regs = await _load_teams_for_admin(gid)
    if not g:
        await cq.message.answer("Игра не найдена.")
        return
    if not regs:
        await cq.message.answer("Список команд пуст.")
        return

    text, pairs = build_teams_view(regs, g.title)

    await cq.message.answer(text, reply_markup=admin_teams_list_kb(pairs, gid), parse_mode=ParseMode.HTML)


@admin_r.callback_query(F.data.startswith("admin:export:"))
async def admin_export_csv(cq: CallbackQuery):
    import pandas as pd
    gid = int(cq.data.split(":")[2])
    async with session_scope() as s:
        res = await s.execute(
            select(Registration).where(Registration.game_id == gid).order_by(Registration.created_at.asc())
        )
        regs = list(res.scalars())
        g = await s.get(Game, gid)
    if not g:
        await cq.answer("Игра не найдена", show_alert=True); return
    if not regs:
        await cq.answer("Нет данных для экспорта", show_alert=True); return
    df = pd.DataFrame([{
        "team_name": r.team_name,
        "players": r.players,
        "status": r.status,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "user_id": r.user_id,
        "chat_id": r.chat_id
    } for r in regs])
    filename = f"export_game_{gid}.csv"
    df.to_csv(filename, index=False)
    await cq.message.answer_document(document=FSInputFile(filename), caption=f"Экспорт по игре: {esc(g.title)}")
    await cq.answer("Экспорт готов")


@admin_r.callback_query(F.data.startswith("admin:delete:"))
async def admin_delete_game(cq: CallbackQuery):
    gid = int(cq.data.split(":")[2])
    async with session_scope() as s:
        g = await s.get(Game, gid)
        if not g:
            await cq.answer("Не найдено", show_alert=True); return
        await s.delete(g)  # ondelete=CASCADE + PRAGMA foreign_keys=ON
    await cq.message.answer("Игра и все её регистрации удалены.")
    await cq.answer("Готово")


# ---- Мастер добавления игры ----

@admin_r.callback_query(F.data == "admin:add_game")
async def add_game_start(cq: CallbackQuery, state: FSMContext):
    await state.set_state(AddGameFlow.entering_title)
    await cq.message.answer("Введи <b>название</b> игры:", parse_mode=ParseMode.HTML, reply_markup=cancel_kb())
    await cq.answer()


@admin_r.message(AddGameFlow.entering_title)
async def add_game_title(m: Message, state: FSMContext):
    title = (m.text or "").strip()
    if not (GAME_TITLE_MIN <= len(title) <= GAME_TITLE_MAX):
        await m.answer("Название 2–200 символов. Попробуй ещё раз:")
        return
    await state.update_data(title=title)
    await state.set_state(AddGameFlow.entering_when)
    await m.answer("Когда? Введи дату/время (напр. 2025-10-01 19:00) или напиши «skip»:")


@admin_r.message(AddGameFlow.entering_when)
async def add_game_when(m: Message, state: FSMContext):
    dt = parse_datetime_maybe(m.text or "")
    await state.update_data(when=dt.isoformat() if dt else None)
    await state.set_state(AddGameFlow.entering_location)
    await m.answer("Где проходит игра? (или «skip»)")


@admin_r.message(AddGameFlow.entering_location)
async def add_game_location(m: Message, state: FSMContext):
    loc = (m.text or "").strip()
    if loc.lower() in SKIP_WORDS:
        loc = None
    await state.update_data(location=loc)
    await state.set_state(AddGameFlow.entering_teams_capacity)
    await m.answer("Лимит по <b>числу команд</b>? Введи число или «skip»:", parse_mode=ParseMode.HTML)


@admin_r.message(AddGameFlow.entering_teams_capacity)
async def add_game_teams_capacity(m: Message, state: FSMContext):
    raw = (m.text or "").strip().lower()
    teams_cap: Optional[int] = None
    if raw not in SKIP_WORDS:
        try:
            teams_cap = int(raw)
            if teams_cap <= 0:
                await m.answer("Должно быть положительное число или «skip». Попробуй снова:")
                return
        except ValueError:
            await m.answer("Нужно число или «skip». Попробуй снова:")
            return
    await state.update_data(teams_capacity=teams_cap)
    await state.set_state(AddGameFlow.entering_people_capacity)
    await m.answer("Лимит по <b>общему числу людей</b>? Введи число или «skip»:", parse_mode=ParseMode.HTML)


@admin_r.message(AddGameFlow.entering_people_capacity)
async def add_game_people_capacity(m: Message, state: FSMContext):
    raw = (m.text or "").strip().lower()
    people_cap: Optional[int] = None
    if raw not in SKIP_WORDS:
        try:
            people_cap = int(raw)
            if people_cap <= 0:
                await m.answer("Должно быть положительное число или «skip». Попробуй снова:")
                return
        except ValueError:
            await m.answer("Нужно число или «skip». Попробуй снова:")
            return
    await state.update_data(people_capacity=people_cap)
    await state.set_state(AddGameFlow.entering_max_per_team)
    await m.answer("Макс. людей <b>в одной команде</b>? Введи число или «skip»:", parse_mode=ParseMode.HTML)


@admin_r.message(AddGameFlow.entering_max_per_team)
async def add_game_max_per_team(m: Message, state: FSMContext):
    raw = (m.text or "").strip().lower()
    maxpt: Optional[int] = None
    if raw not in SKIP_WORDS:
        try:
            maxpt = int(raw)
            if maxpt <= 0:
                await m.answer("Должно быть положительное число или «skip». Попробуй снова:")
                return
        except ValueError:
            await m.answer("Нужно число или «skip». Попробуй снова:")
            return
    await state.update_data(max_players_per_team=maxpt)
    await state.set_state(AddGameFlow.confirming_active)
    await m.answer("Активировать приём регистраций сейчас? (да/нет)")


@admin_r.message(AddGameFlow.confirming_active)
async def add_game_confirm(m: Message, state: FSMContext):
    from datetime import datetime
    ans = (m.text or "").strip().lower()
    active = ans in {"да", "yes", "y", "д", "ага", "включить"}
    data = await state.get_data()
    when_iso = data.get("when")
    dt = datetime.fromisoformat(when_iso) if when_iso else None
    async with session_scope() as s:
        g = Game(
            title=data["title"],
            when=dt,
            location=data.get("location"),
            teams_capacity=data.get("teams_capacity"),
            people_capacity=data.get("people_capacity"),
            max_players_per_team=data.get("max_players_per_team"),
            is_active=active
        )
        s.add(g)
        await s.flush()

        stats = await get_game_stats(s, g.id)

        await m.answer("Игра добавлена:\n" + admin_game_brief(g, stats), parse_mode=ParseMode.HTML)
    await state.clear()


# ----------------------------
# /help
# ----------------------------

@user_r.message(Command("help"))
async def help_cmd(m: Message):
    text = (
        "Что я умею:\n"
        "• /start — главное меню (для игроков — выбор игры, для админов — панель управления)\n"
        "• /whoami — показать твой user_id\n"
        "• Мои регистрации — изменить имя/кол-во игроков или удалить (показывает список команд на игре)\n"
    )
    if is_admin(m.from_user.id):
        text += "• /admin — вернуться в панель администратора\n"
    await m.answer(text)



# ----------------------------
# Точка входа
# ----------------------------

async def main():
    logging.basicConfig(
        level=logging.INFO, 
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
        )
    logger.info("starting bot")

    await init_db()
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(user_r)
    dp.include_router(admin_r)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    # 1) Открываем HTTP-порт для Render (иначе деплой упадёт с "Timed Out")
    runner = await start_health_server()

    # 2) Запускаем polling
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

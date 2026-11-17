from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core import (
    is_admin, session_scope, get_game_stats,
    count_confirmed_teams, sum_confirmed_people,
    get_teams_for_text
)
from keyboards import games_list_kb, reg_manage_kb, cancel_kb
from models import Game, Registration
from render import user_game_brief, render_teams_list, status_label
from screens import show_user_home, show_admin_home, get_active_games_data
from states import RegisterFlow, EditNameFlow, EditPlayersFlow
from utils import esc, fmt_dt

from constants import (
    TEAM_NAME_MIN, TEAM_NAME_MAX,
    STATUS_CONFIRMED, STATUS_WAITLIST
    )


user_r = Router()

@user_r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    if is_admin(m.from_user.id):
        await show_admin_home(m)
    else:
        await show_user_home(m, m.from_user.id)

@user_r.message(Command("whoami"))
async def whoami(m: Message):
    await m.answer(f"Твой Telegram user_id: <code>{m.from_user.id}</code>", parse_mode=ParseMode.HTML)

@user_r.callback_query(F.data.startswith("page:"))
async def paginate_games(cq: CallbackQuery):
    page = int(cq.data.split(":")[1])
    async with session_scope() as s:
        data = await get_active_games_data(s)
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
            regs, cnt_all = await get_teams_for_text(s, game.id, limit=60)
            teams_text = render_teams_list(regs, cnt_all)

            text = (
                f"{brief}"
                f"<b>Твоя регистрация</b>\n"
                f"• Команда: <b>{esc(reg.team_name)}</b>\n"
                f"• Игроков: <b>{reg.players}</b>\n"
                f"• Статус: <b>{status_label(reg.status)}</b>\n\n"
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
        regs, cnt_all = await get_teams_for_text(s, game.id, limit=60)
        teams_text = render_teams_list(regs, cnt_all)

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
            f"Когда: {fmt_dt(game.when)} | Где: {esc(game.location) or '—'}\n"
            f"Команда: <b>{esc(team_name)}</b>\n"
            f"Игроков: <b>{players}</b>\n"
            f"Статус: <b>{status_label(status)}</b>",
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

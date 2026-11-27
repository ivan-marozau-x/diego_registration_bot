from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sqlalchemy import select

from core import (
    is_admin, session_scope, get_game_stats,
    get_teams_for_text
)
from keyboards import games_list_kb, reg_manage_kb, cancel_kb
from models import Game, Registration
from render import user_game_brief, render_teams_list, status_label
from screens import (
    show_user_home, show_admin_home, get_active_games_data,
    render_admin_game_card)
from services.registration_service import (
    create_registration,
    rename_team,
    update_players,
    validate_team_name,
    InvalidTeamNameError,
    PlayersLimitError,
    RegistrationError,
    TeamNameTakenError,
)
from states import RegisterFlow, EditNameFlow, EditPlayersFlow
from utils import esc, fmt_dt, safe_edit_text

from constants import (
    TEAM_NAME_MIN, TEAM_NAME_MAX,
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
    current_state = await state.get_state()
    data = await state.get_data()
    await state.clear()

    if current_state and current_state.startswith("AddGameFlow"):
        # Отменил создание игры — назад в админ-панель
        await show_admin_home(cq.message, edit=True)

    elif current_state and current_state.startswith("AdminAddTeamFlow"):
        # Отменил добавление команды — назад в карточку игры
        gid = data.get("game_id")
        if gid:
            await render_admin_game_card(cq.message, gid)
        else:
            await show_admin_home(cq.message, edit=True)

    elif current_state and current_state.startswith("RegisterFlow"):
        # Отменил регистрацию — назад к списку игр
        await show_user_home(cq.message, cq.from_user.id, edit=True)

    elif current_state and current_state.startswith(("EditNameFlow", "EditPlayersFlow")):
        # Отменил редактирование команды — сообщение с кнопкой возврата к "Мои регистрации"
        kb = InlineKeyboardBuilder()
        kb.button(text="🗂 Мои регистрации", callback_data="my_regs")
        if is_admin(cq.from_user.id):
            kb.button(text="⚙️ В админку", callback_data="admin:back")
        kb.adjust(1)
        await safe_edit_text(
            cq.message,
            "Изменения отменены.",
            reply_markup=kb.as_markup(),
        )

    else:
        # Fallback: главное меню
        if is_admin(cq.from_user.id):
            await show_admin_home(cq.message, edit=True)
        else:
            await show_user_home(cq.message, cq.from_user.id, edit=True)

    await cq.answer("Отменено")

@user_r.message(RegisterFlow.entering_team_name)
async def team_name_step(m: Message, state: FSMContext):
    data = await state.get_data()
    game_id = data["game_id"]

    async with session_scope() as s:
        game = await s.get(Game, game_id)
        if not game or not game.is_active:
            await m.answer("Игра больше недоступна для регистрации.")
            await state.clear()
            return

        try:
            clean_name = await validate_team_name(s, game_id, m.text or "")
        except (TeamNameTakenError, InvalidTeamNameError) as e:
            await m.answer(f"{esc(e.message)} Введи другое название:")
            return

        max_players = game.max_players_per_team

    await state.update_data(team_name=clean_name)
    await state.set_state(RegisterFlow.entering_players)

    limit_hint = f" (максимум {max_players})" if max_players is not None else ""
    await m.answer(f"Сколько игроков в команде{limit_hint}?:")


@user_r.message(RegisterFlow.entering_players)
async def players_step(m: Message, state: FSMContext):
    try:
        players = int((m.text or "").strip())
        if players <= 0:
            raise ValueError
    except ValueError:
        await m.answer("Нужно положительное число. Попробуйте ещё раз:")
        return

    data = await state.get_data()
    game_id = data["game_id"]
    team_name = data["team_name"]
    uid = m.from_user.id
    chat_id = m.chat.id

    async with session_scope() as s:
        try:
            reg = await create_registration(
                s,
                user_id=uid,
                chat_id=chat_id,
                game_id=game_id,
                team_name=team_name,
                players=players,
                source="user",
            )
            game = await s.get(Game, game_id)

            game_title = game.title
            game_when = fmt_dt(game.when)
            game_location = esc(game.location) or "—"
            final_name = reg.team_name
            final_status = reg.status

        except (TeamNameTakenError, InvalidTeamNameError) as e:
            await m.answer(f"{esc(e.message)} Введите другое название:")
            await state.set_state(RegisterFlow.entering_team_name)
            return

        except PlayersLimitError as e:
            await m.answer(f"{esc(e.message)} Введите меньшее число:")
            return

        except RegistrationError as e:
            await m.answer(esc(e.message))
            await state.clear()
            return

    await state.clear()
    await m.answer(
        f"✅ Регистрация создана!\n\n"
        f"Игра: <b>{esc(game_title)}</b>\n"
        f"Когда: {game_when} | Где: {game_location}\n"
        f"Команда: <b>{esc(final_name)}</b>\n"
        f"Игроков: <b>{players}</b>\n"
        f"Статус: <b>{status_label(final_status)}</b>",
        parse_mode=ParseMode.HTML,
    )


# --------- Редактирование / удаление регистрации пользователем ----------

@user_r.callback_query(F.data.startswith("edit_name:"))
async def edit_name_start(cq: CallbackQuery, state: FSMContext):
    reg_id = int(cq.data.split(":")[1])
    await state.set_state(EditNameFlow.entering_new_name)
    await state.update_data(reg_id=reg_id)
    await cq.message.answer(f"Введи новое название команды ({TEAM_NAME_MIN}–{TEAM_NAME_MAX} символов):", reply_markup=cancel_kb())
    await cq.answer()


@user_r.message(EditNameFlow.entering_new_name)
async def edit_name_apply(m: Message, state: FSMContext):
    name = (m.text or "").strip()
    data = await state.get_data()
    reg_id = data["reg_id"]
    uid = m.from_user.id

    async with session_scope() as s:
        try:
            await rename_team(
                s,
                reg_id=reg_id,
                user_id=uid,
                new_name=name,
            )
        except (TeamNameTakenError, InvalidTeamNameError) as e:
            await m.answer(f"{esc(e.message)} Введите другое название:")
            return
        except RegistrationError as e:
            await m.answer(esc(e.message))
            await state.clear()
            return

    await state.clear()
    await m.answer("✅ Название команды обновлено.")


@user_r.callback_query(F.data.startswith("edit_players:"))
async def edit_players_start(cq: CallbackQuery, state: FSMContext):
    reg_id = int(cq.data.split(":")[1])
    max_players = None

    async with session_scope() as s:
        reg = await s.get(Registration, reg_id)
        if reg:
            game = await s.get(Game, reg.game_id)
            if game:
                max_players = game.max_players_per_team

    await state.set_state(EditPlayersFlow.entering_new_players)
    await state.update_data(reg_id=reg_id)

    limit_hint = f" (максимум {max_players})" if max_players is not None else ""
    await cq.message.answer(
        f"Введите новое число игроков{limit_hint}:",
        reply_markup=cancel_kb(),
    )
    await cq.answer()


@user_r.message(EditPlayersFlow.entering_new_players)
async def edit_players_apply(m: Message, state: FSMContext):
    try:
        players = int((m.text or "").strip())
        if players <= 0:
            raise ValueError
    except ValueError:
        await m.answer("Нужно положительное число. Попробуй ещё раз:")
        return

    data = await state.get_data()
    reg_id = data["reg_id"]
    uid = m.from_user.id

    async with session_scope() as s:
        try:
            reg = await update_players(
                s,
                reg_id=reg_id,
                user_id=uid,
                players=players,
            )
            final_status = reg.status

        except PlayersLimitError as e:
            await m.answer(f"{esc(e.message)} Введи меньшее число:")
            return

        except RegistrationError as e:
            await m.answer(esc(e.message))
            await state.clear()
            return

    await state.clear()
    await m.answer(
        f"✅ Число игроков обновлено (статус: <b>{status_label(final_status)}</b>).",
        parse_mode=ParseMode.HTML,
    )

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

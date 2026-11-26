from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError


from backup_db import generate_backup
from core import (
    is_admin, session_scope, get_game_stats,
    load_teams_for_admin, count_confirmed_teams,
    sum_confirmed_people, next_admin_user_id
    )
from db import engine
from keyboards import (
    admin_games_kb, admin_game_actions_kb,
    admin_teams_list_kb, cancel_kb,
    )
from models import Game, Registration
from render import admin_game_brief, build_teams_view
from screens import show_admin_home, show_user_home
from states import AddGameFlow, AdminAddTeamFlow
from utils import safe_edit_text, esc, fmt_dt, parse_datetime_maybe, to_utc

from constants import (
    TEAM_NAME_MIN, TEAM_NAME_MAX,
    STATUS_CONFIRMED, STATUS_WAITLIST,
    GAME_TITLE_MIN, GAME_TITLE_MAX, SKIP_WORDS
)

admin_r = Router()
admin_r.message.filter(lambda m: m.from_user is not None and is_admin(m.from_user.id))
admin_r.callback_query.filter(lambda cq: cq.from_user is not None and is_admin(cq.from_user.id))


@admin_r.message(Command("admin"))
async def admin_home(m: Message, state: FSMContext):
    await state.clear()
    await show_admin_home(m)

@admin_r.callback_query(F.data == "admin:back")
async def admin_back(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_admin_home(cq.message, edit=True)
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
    await show_user_home(cq.message, cq.from_user.id, edit=True)
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
    g, regs = await load_teams_for_admin(gid)
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

    g, regs = await load_teams_for_admin(gid)
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
        await s.delete(g)
    
    # Редактируем текущее сообщение, убирая кнопки
    await cq.message.edit_text("🗑 Игра и все её регистрации удалены.", reply_markup=None)
    await cq.answer("Готово")


# ---- Мастер добавления игры ----

@admin_r.callback_query(F.data == "admin:add_game")
async def add_game_start(cq: CallbackQuery, state: FSMContext):
    await state.set_state(AddGameFlow.entering_title)

    msg = await cq.message.answer(
        "Введи <b>название</b> игры (или нажми /cancel):",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb()
        )
    await state.update_data(ui_msg_id=msg.message_id)
    await cq.answer()


@admin_r.message(AddGameFlow.entering_title)
async def add_game_title(m: Message, state: FSMContext):
    data = await state.get_data()
    ui_msg_id = data.get("ui_msg_id")
    title = (m.text or "").strip()

    try: await m.delete()
    except: pass

    if not (GAME_TITLE_MIN <= len(title) <= GAME_TITLE_MAX):
        await m.bot.edit_message_text(
            chat_id=m.chat.id, message_id=ui_msg_id,
            text=f"Название {GAME_TITLE_MIN}–{GAME_TITLE_MAX} символов. Попробуй ещё раз:",
            parse_mode=ParseMode.HTML, reply_markup=cancel_kb()
        )
        return
    
    await state.update_data(title=title)
    await state.set_state(AddGameFlow.entering_when)


    await m.bot.edit_message_text(
        chat_id=m.chat.id, message_id=ui_msg_id,
        text="Когда? Введи дату/время (напр. 2025-10-01 19:00) или напиши «skip»:",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb()
    )

@admin_r.message(AddGameFlow.entering_when)
async def add_game_when(m: Message, state: FSMContext):
    data = await state.get_data()
    ui_msg_id = data.get("ui_msg_id")
    dt = parse_datetime_maybe(m.text or "")

    try: await m.delete()
    except: pass

    await state.update_data(when=dt.isoformat() if dt else None)
    await state.set_state(AddGameFlow.entering_location)

    await m.bot.edit_message_text(
        chat_id=m.chat.id, message_id=ui_msg_id,
        text="Где проходит игра? (или «skip»)",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb()
    )


@admin_r.message(AddGameFlow.entering_location)
async def add_game_location(m: Message, state: FSMContext):
    data = await state.get_data()
    ui_msg_id = data.get("ui_msg_id")
    loc = (m.text or "").strip()
    if loc.lower() in SKIP_WORDS: loc = None

    try: await m.delete()
    except: pass

    await state.update_data(location=loc)
    await state.set_state(AddGameFlow.entering_teams_capacity)

    await m.bot.edit_message_text(
        chat_id=m.chat.id, message_id=ui_msg_id,
        text="Лимит по <b>числу команд</b>? Введи число или «skip»:",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb()
    )


@admin_r.message(AddGameFlow.entering_teams_capacity)
async def add_game_teams_capacity(m: Message, state: FSMContext):
    data = await state.get_data()
    ui_msg_id = data.get("ui_msg_id")
    raw = (m.text or "").strip().lower()
    teams_cap: Optional[int] = None

    try: await m.delete()
    except: pass

    if raw not in SKIP_WORDS:
        try:
            teams_cap = int(raw)
            if teams_cap <= 0:
                await m.bot.edit_message_text(chat_id=m.chat.id, message_id=ui_msg_id, text="Должно быть положительное число или «skip». Попробуй снова:", reply_markup=cancel_kb())
                return
        except ValueError:
            await m.bot.edit_message_text(chat_id=m.chat.id, message_id=ui_msg_id, text="Нужно число или «skip». Попробуй снова:", reply_markup=cancel_kb())
            return
        
    await state.update_data(teams_capacity=teams_cap)
    await state.set_state(AddGameFlow.entering_people_capacity)

    await m.bot.edit_message_text(
        chat_id=m.chat.id, message_id=ui_msg_id,
        text="Лимит по <b>общему числу людей</b>? Введи число или «skip»:",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb()
    )


@admin_r.message(AddGameFlow.entering_people_capacity)
async def add_game_people_capacity(m: Message, state: FSMContext):
    data = await state.get_data()
    ui_msg_id = data.get("ui_msg_id")
    raw = (m.text or "").strip().lower()
    people_cap: Optional[int] = None

    try: await m.delete()
    except: pass

    if raw not in SKIP_WORDS:
        try:
            people_cap = int(raw)
            if people_cap <= 0:
                await m.bot.edit_message_text(chat_id=m.chat.id, message_id=ui_msg_id, text="Должно быть положительное число или «skip». Попробуй снова:", reply_markup=cancel_kb())
                return
        except ValueError:
            await m.bot.edit_message_text(chat_id=m.chat.id, message_id=ui_msg_id, text="Нужно число или «skip». Попробуй снова:", reply_markup=cancel_kb())
            return
        
    await state.update_data(people_capacity=people_cap)
    await state.set_state(AddGameFlow.entering_max_per_team)

    await m.bot.edit_message_text(
        chat_id=m.chat.id, message_id=ui_msg_id,
        text="Макс. людей <b>в одной команде</b>? Введи число или «skip»:",
        parse_mode=ParseMode.HTML, reply_markup=cancel_kb()
    )

@admin_r.message(AddGameFlow.entering_max_per_team)
async def add_game_max_per_team(m: Message, state: FSMContext):
    data = await state.get_data()
    ui_msg_id = data.get("ui_msg_id")
    raw = (m.text or "").strip().lower()
    maxpt: Optional[int] = None

    try: await m.delete()
    except: pass

    if raw not in SKIP_WORDS:
        try:
            maxpt = int(raw)
            if maxpt <= 0:
                await m.bot.edit_message_text(chat_id=m.chat.id, message_id=ui_msg_id, text="Должно быть положительное число или «skip». Попробуй снова:", reply_markup=cancel_kb())
                return
        except ValueError:
            await m.bot.edit_message_text(chat_id=m.chat.id, message_id=ui_msg_id, text="Нужно число или «skip». Попробуй снова:", reply_markup=cancel_kb())
            return
        
    await state.update_data(max_players_per_team=maxpt)
    await state.set_state(AddGameFlow.confirming_active)

    await m.bot.edit_message_text(
        chat_id=m.chat.id, message_id=ui_msg_id,
        text="Активировать приём регистраций сейчас? (да/нет)",
        reply_markup=cancel_kb()
    )


@admin_r.message(AddGameFlow.confirming_active)
async def add_game_confirm(m: Message, state: FSMContext):
    data = await state.get_data()
    ui_msg_id = data.get("ui_msg_id")
    ans = (m.text or "").strip().lower()
    active = ans in {"да", "yes", "y", "д", "ага", "включить"}
    
    try: await m.delete()
    except: pass
    
    when_iso = data.get("when")
    dt = to_utc(datetime.fromisoformat(when_iso)) if when_iso else None

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
        
        # Вместо отправки нового сообщения, редактируем текущее
        text = "✅ Игра добавлена:\n" + admin_game_brief(g, stats)
        await m.bot.edit_message_text(
            chat_id=m.chat.id, message_id=ui_msg_id,
            text=text, parse_mode=ParseMode.HTML,
            reply_markup=admin_game_actions_kb(g.id, g.is_active)
        )
    await state.clear()

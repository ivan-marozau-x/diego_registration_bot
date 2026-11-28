from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from aiogram.types import (
    BufferedInputFile, CallbackQuery, FSInputFile, Message, 
    InlineKeyboardButton, InlineKeyboardMarkup
)
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
from aiogram_calendar.schemas import SimpleCalAct
from config import settings

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from typing import Optional

from sqlalchemy import select

from backup_db import generate_backup
from core import (
    is_admin, session_scope, get_game_stats,
    load_teams_for_admin,
    )
from db import engine
from keyboards import (
    admin_games_kb, admin_game_actions_kb,
    admin_teams_list_kb, cancel_kb, time_kb,
    skip_kb, yes_no_kb, admin_back_kb
    )
from models import Game, Registration
from render import admin_game_brief, build_teams_view, status_label
from screens import show_admin_home, show_user_home, render_admin_game_card
from services.registration_service import (
    validate_team_name,
    create_registration, 
    RegistrationError,
    TeamNameTakenError,
    InvalidTeamNameError, 
    PlayersLimitError
    )
from states import AddGameFlow, AdminAddTeamFlow
from utils import safe_edit_text, esc, fmt_dt, to_utc

from constants import (
    GAME_TITLE_MIN, GAME_TITLE_MAX, SKIP_WORDS
)

admin_r = Router()
admin_r.message.filter(lambda m: m.from_user is not None and is_admin(m.from_user.id))
admin_r.callback_query.filter(lambda cq: cq.from_user is not None and is_admin(cq.from_user.id))


class CustomCalendar(SimpleCalendar):
    async def start_calendar(
        self,
        year: int | None = None,
        month: int | None = None,
    ) -> InlineKeyboardMarkup:
        now = datetime.now(ZoneInfo(settings.tz))
        kb = await super().start_calendar(
            year=now.year if year is None else year,
            month=now.month if month is None else month,
            )
        
        for row in kb.inline_keyboard:
            for index, button in enumerate(row):
                if not button.callback_data:
                    continue
                try:
                    calendar_action = SimpleCalendarCallback.unpack(button.callback_data).act
                    if calendar_action == SimpleCalAct.cancel:
                        row[index] = InlineKeyboardButton(
                            text="⏭ Пропустить",
                            callback_data="wiz:skip"
                        )
                except Exception:
                    pass
        
        return kb

def build_game_calendar() -> CustomCalendar:
    today = datetime.now(ZoneInfo(settings.tz)).date()
    calendar = CustomCalendar(locale="ru_RU")
    calendar.set_dates_range(
        min_date=datetime.combine(today, time.min),
        max_date=datetime.combine(today + timedelta(days=365), time.max)
        )
    return calendar

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
    await render_admin_game_card(cq.message, gid)
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
    await cq.message.answer("Введите название новой команды:", reply_markup=cancel_kb())
    await cq.answer()


@admin_r.message(AdminAddTeamFlow.entering_team_name)
async def admin_add_team_name(m: Message, state: FSMContext):
    data = await state.get_data()
    gid = data["game_id"]

    async with session_scope() as s:
        game = await s.get(Game, gid)
        if not game:
            await m.answer("Игра не найдена.")
            await state.clear()
            return

        try:
            clean_name = await validate_team_name(s, gid, m.text or "")
        except (TeamNameTakenError, InvalidTeamNameError) as e:
            await m.answer(f"{esc(e.message)} Введите другое название:")
            return

        max_players = game.max_players_per_team

    await state.update_data(team_name=clean_name)
    await state.set_state(AdminAddTeamFlow.entering_players)

    limit_hint = f" (максимум {max_players})" if max_players is not None else ""
    await m.answer(f"Сколько игроков в команде{limit_hint}?:")


@admin_r.message(AdminAddTeamFlow.entering_players)
async def admin_add_team_players(m: Message, state: FSMContext):
    try:
        players = int((m.text or "").strip())
        if players <= 0:
            raise ValueError
    except ValueError:
        await m.answer("Нужно положительное число. Попробуйте снова:")
        return
    
    data = await state.get_data()
    gid = data["game_id"]
    name = data["team_name"]

    async with session_scope() as s:
        try:
            reg = await create_registration(
                s,
                user_id=None,
                chat_id=0,
                game_id=gid,
                team_name=name,
                players=players,
                source="admin",
            )
            # Сохраняем нужные поля до закрытия сессии и коммита
            final_name = reg.team_name
            final_status = reg.status

        except TeamNameTakenError as e:
            await m.answer(f"{esc(e.message)} Введите другое название:")
            await state.set_state(AdminAddTeamFlow.entering_team_name)
            return

        except PlayersLimitError as e:
            # Не сбрасываем state, просим ввести число заново
            await m.answer(f"{esc(e.message)} Введите другое количество игроков:")
            return

        except RegistrationError as e:
            await m.answer(esc(e.message))
            await state.clear()
            return

    await state.clear()
    await m.answer(
        f"✅ Команда «{esc(final_name)}» добавлена ({players} чел., "
        f"статус: {status_label(final_status)})."
    )


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
    await safe_edit_text(
        cq.message,
        "🗑 Игра и все её регистрации удалены.", 
        reply_markup=admin_back_kb())
    await cq.answer("Готово")


# ---- Мастер добавления игры ----

@admin_r.callback_query(F.data == "admin:add_game")
async def add_game_start(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddGameFlow.entering_title)

    await safe_edit_text(
        cq.message,
        "Введи <b>название</b> игры (или нажми ❌ Отмена):",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_kb()
        )
    await state.update_data(ui_msg_id=cq.message.message_id)
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

    calendar = build_game_calendar()
    calendar_kb = await calendar.start_calendar()
    
    await m.bot.edit_message_text(
        chat_id=m.chat.id, message_id=ui_msg_id,
        text="Выбери дату игры:",
        reply_markup=calendar_kb
    )


@admin_r.callback_query(
    SimpleCalendarCallback.filter(),
    AddGameFlow.entering_when
)
async def process_simple_calendar(
    cq: CallbackQuery,
    callback_data: SimpleCalendarCallback,
    state: FSMContext
):
    calendar = build_game_calendar()
    selected, selected_date = await calendar.process_selection(
        cq,
        callback_data
    )

    if not selected:
        if callback_data.act not in {SimpleCalAct.ignore, SimpleCalAct.day}:
            await cq.answer()
        return

    selected_day = (
        selected_date.date()
        if isinstance(selected_date, datetime)
        else selected_date
    )
    today = datetime.now(ZoneInfo(settings.tz)).date()

    if selected_day < today:
        await cq.answer(
            "Нельзя выбрать прошедшую дату.",
            show_alert=True
        )
        return

    await state.update_data(when_date=selected_day.isoformat())
    await state.set_state(AddGameFlow.entering_when_time)

    await safe_edit_text(
        cq.message,
        f"Дата: {selected_day.strftime('%d.%m.%Y')}.\n"
        "Выбери время игры:",
        reply_markup=time_kb()
    )

    await cq.answer()
        
@admin_r.callback_query(F.data.startswith("time:"), AddGameFlow.entering_when_time)
async def process_time_choice(cq: CallbackQuery, state: FSMContext):
    time_val = cq.data.split(":", maxsplit=1)[1]
    
    if time_val == "manual":
        await cq.message.edit_text(
            "Введи время в формате ЧЧ:ММ (например, 19:15):",
            reply_markup=cancel_kb()
        )
        await cq.answer()
        return
    
    await save_time_and_next(cq.message, state, time_val)
    await cq.answer()

@admin_r.message(AddGameFlow.entering_when_time)
async def add_game_manual_time(m: Message, state: FSMContext):
    time_val = (m.text or "").strip()
    
    try:
        parsed_time = datetime.strptime(time_val, "%H:%M")
        time_val = parsed_time.strftime("%H:%M")
    except ValueError:
        await m.answer(
            "Некорректное время. Введи время в формате ЧЧ:ММ "
            "(например, 19:15)."
        )
        return
    
    try:
        await m.delete()
    except Exception:
        pass

    await save_time_and_next(m, state, time_val)
    

async def save_time_and_next(msg_obj: Message, state: FSMContext, time_val: str):
    data = await state.get_data()
    date_str = data.get("when_date")
    ui_msg_id = data.get("ui_msg_id")

    if not date_str or not ui_msg_id:
        await state.clear()
        await msg_obj.answer(
            "Сессия создания игры устарела. "
            "Начни создание заново через /admin."
        )
        return

    try:
        game_datetime = datetime.strptime(
            f"{date_str} {time_val}",
            "%Y-%m-%d %H:%M"
        )
    except ValueError:
        await state.clear()
        await msg_obj.answer(
            "Не удалось определить дату или время. "
            "Начни создание игры заново через /admin."
        )
        return
    
    game_datetime_utc = to_utc(game_datetime)
    if game_datetime_utc <= datetime.now(timezone.utc):
        await msg_obj.bot.edit_message_text(
            chat_id=msg_obj.chat.id,
            message_id=ui_msg_id,
            text="Это время уже прошло. Выбери другое время игры:",
            reply_markup=time_kb()
        )
        return

    dt_str = game_datetime.isoformat(timespec="seconds")

    await state.update_data(when=dt_str)
    await state.set_state(AddGameFlow.entering_location)

    await msg_obj.bot.edit_message_text(
        chat_id=msg_obj.chat.id,
        message_id=ui_msg_id,
        text="Где проходит игра? (или нажми «Пропустить»):",
        reply_markup=skip_kb()
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
        parse_mode=ParseMode.HTML, reply_markup=skip_kb()
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
        parse_mode=ParseMode.HTML, reply_markup=skip_kb()
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
        parse_mode=ParseMode.HTML, reply_markup=skip_kb()
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
        reply_markup=yes_no_kb()
    )


@admin_r.callback_query(F.data == "wiz:skip")
async def wizard_skip(cq: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    data = await state.get_data()
    ui_msg_id = data.get("ui_msg_id")

    if not ui_msg_id:
        await cq.answer("Сессия устарела. Нажми /admin и начни заново.", show_alert=True)
        await state.clear()
        return

    # В зависимости от того, где мы находимся, пропускаем шаг и идем дальше
    if current_state in {
        AddGameFlow.entering_when.state,
        AddGameFlow.entering_when_time.state,
        }:
        await state.update_data(
            when=None,
            when_date=None
        )
        await state.set_state(AddGameFlow.entering_location)
        text = "Где проходит игра? (или нажми «Пропустить»):"
        kb = skip_kb()
        
    elif current_state == AddGameFlow.entering_location.state:
        await state.update_data(location=None)
        await state.set_state(AddGameFlow.entering_teams_capacity)
        text = "Лимит по <b>числу команд</b>? Введи число или нажми «Пропустить»:"
        kb = skip_kb()
        
    elif current_state == AddGameFlow.entering_teams_capacity.state:
        await state.update_data(teams_capacity=None)
        await state.set_state(AddGameFlow.entering_people_capacity)
        text = "Лимит по <b>общему числу людей</b>? Введи число или нажми «Пропустить»:"
        kb = skip_kb()
        
    elif current_state == AddGameFlow.entering_people_capacity.state:
        await state.update_data(people_capacity=None)
        await state.set_state(AddGameFlow.entering_max_per_team)
        text = "Максимум игроков <b>в одной команде</b>? Введи число или нажми «Пропустить»:"
        kb = skip_kb()
        
    elif current_state == AddGameFlow.entering_max_per_team.state:
        await state.update_data(max_players_per_team=None)
        await state.set_state(AddGameFlow.confirming_active)
        text = "Активировать приём регистраций сейчас?"
        kb = yes_no_kb()
        
    else:
        await cq.answer()
        return

    # Редактируем то же самое сообщение бота
    await safe_edit_text(cq.message, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await cq.answer("Пропущено")


@admin_r.callback_query(F.data.in_({"wiz:yes", "wiz:no"}))
async def wizard_confirm_active(cq: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state != AddGameFlow.confirming_active.state:
        await cq.answer("Эта кнопка устарела.", show_alert=True)
        return

    active = (cq.data == "wiz:yes")
    data = await state.get_data()
    
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
        
        # Финальный экран
        text = "✅ Игра добавлена:\n" + admin_game_brief(g, stats)
        await safe_edit_text(
            cq.message, text, 
            reply_markup=admin_game_actions_kb(g.id, g.is_active), 
            parse_mode=ParseMode.HTML
        )
        
    await state.clear()
    await cq.answer("Готово")
    
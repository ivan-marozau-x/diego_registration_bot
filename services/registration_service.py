from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Game, Registration
from utils import esc
from constants import TEAM_NAME_MIN, TEAM_NAME_MAX, STATUS_CONFIRMED, STATUS_WAITLIST

class RegistrationError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

async def validate_team_name(
        session: AsyncSession,
        game_id: int,
        name: str,
        exclude_reg_id: int | None = None,
) -> None:
    if not (TEAM_NAME_MIN <= len(name) <= TEAM_NAME_MAX):
        raise RegistrationError(
            f"Название команды должно быть от {TEAM_NAME_MIN} до {TEAM_NAME_MAX} символов."
        )
    q = select(Registration.id).where(
        Registration.game_id == game_id,
        Registration.team_name == name,
    )
    if exclude_reg_id is not None:
        q = q.where(Registration.id != exclude_reg_id)
    if (await session.execute(q)).scalar_one_or_none():
        raise RegistrationError(f"Команда '{esc(name)}' уже зарегистрирована на эту игру")

async def create_registration(
        session: AsyncSession,
        *,
        user_id: int | None,
        chat_id: int,
        game_id: int,
        team_name: str,
        players: int,
        source: str = "user",
) -> Registration:
    await validate_team_name(session, game_id, team_name)

    if source == "user" and user_id is not None:
        exists = await session.execute(
            select(Registration.id).where(
                Registration.user_id == user_id,
                Registration.game_id == game_id,
            )
        )
        if exists.scalar_one_or_none():
            raise RegistrationError("Вы уже зарегистрированы на эту игру.")

    game = await session.get(Game, game_id)
    if not game or not game.is_active:
        raise RegistrationError("Игра недоступна.")

    if game.max_players_per_team is not None and players > game.max_players_per_team:
        raise RegistrationError(
            f"Максимум – {game.max_players_per_team} игроков в команде."
        )

    from core import count_confirmed_teams, sum_confirmed_people, next_admin_user_id
    confirmed_teams = await count_confirmed_teams(session, game_id)
    confirmed_people = await sum_confirmed_people(session, game_id)
    teams_over = (
        game.teams_capacity is not None
        and confirmed_teams >= game.teams_capacity
    )
    people_over = (
        game.people_capacity is not None
        and (confirmed_people + players) > game.people_capacity
    )
    status = STATUS_WAITLIST if (teams_over or people_over) else STATUS_CONFIRMED

    if status == "admin":
        user_id = await next_admin_user_id(session)
        chat_id = 0

    reg = Registration(
        user_id=user_id,
        chat_id=chat_id,
        game_id=game_id,
        team_name=team_name,
        players=players,
        status=status,
    )

    try:
        async with session.begin_nested():
            session.add(reg)
            await session.flush()
    except IntegrityError:
        raise RegistrationError(f"Название '{esc(team_name)}' уже занято. Попробуй другое.")

    return reg

async def rename_team(
        session: AsyncSession,
        *,
        reg_id: int,
        user_id: int,
        new_name: str,
) -> Registration:
    reg = await session.get(Registration, reg_id)
    if not reg or reg.user_id != user_id:
        raise RegistrationError("Регистрация не найдена")

    await validate_team_name(
        session, reg.game_id, new_name, exclude_reg_id=reg.id
    )

    try:
        async with session.begin_nested():
            reg.team_name = new_name
            await session.flush()
    except IntegrityError:
        raise RegistrationError(f"Название '{esc(new_name)}' уже занято.")

    return reg
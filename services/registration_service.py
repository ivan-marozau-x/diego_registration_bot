from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import Game, Registration
from constants import (
    TEAM_NAME_MIN,
    TEAM_NAME_MAX,
    STATUS_CONFIRMED,
    STATUS_WAITLIST,
    )
from core import count_confirmed_teams, sum_confirmed_people, next_admin_user_id

# Exceptions

class RegistrationError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

class TeamNameTakenError(RegistrationError):
    pass

class InvalidTeamNameError(RegistrationError):
    pass

class PlayersLimitError(RegistrationError):
    pass

# Helpers

def normalize_team_name(name: str) -> str:
    name = name.strip()

    if not name:
        raise InvalidTeamNameError(
            "Название команды не может быть пустым."
        )

    if not (TEAM_NAME_MIN <= len(name) <= TEAM_NAME_MAX):
        raise InvalidTeamNameError(
            f"Название команды должно быть от {TEAM_NAME_MIN} до {TEAM_NAME_MAX} символов."
        )

    return name


def get_constraint_name(exc: IntegrityError) -> str | None:
    orig = exc.orig
    constraint_name = getattr(orig, "constraint_name", None)

    if not constraint_name:
        diag = getattr(orig, "diag", None)
        constraint_name = getattr(diag, "constraint_name", None)

    return constraint_name


def map_registration_integrity_error(
    exc: IntegrityError,
) -> RegistrationError | None:
    constraint = get_constraint_name(exc)

    if constraint == "uix_reg_game_team":
        return TeamNameTakenError(
            "Название команды уже занято."
        )

    if constraint == "uix_reg_game_user":
        return RegistrationError(
            "Вы уже зарегистрированы на эту игру."
        )

    return None

# Validation

async def validate_team_name(
        session: AsyncSession,
        game_id: int,
        name: str,
        exclude_reg_id: int | None = None,
) -> str:
    name = normalize_team_name(name)

    q = select(Registration.id).where(
        Registration.game_id == game_id,
        Registration.team_name == name,
    )

    if exclude_reg_id is not None:
        q = q.where(Registration.id != exclude_reg_id)

    if (await session.execute(q)).scalar_one_or_none():
        raise TeamNameTakenError(
            f"Команда с таким названием уже зарегистрирована на эту игру."
    )

    return name

# Services

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
    
    if source not in {"user", "admin"}:
        raise RegistrationError("Неизвестный источник регистрации.")

    if source == "user" and (user_id is None or chat_id == 0):
        raise RegistrationError(
            "Не удалось определить пользователя или чат."
            )

    if not isinstance(players, int) or isinstance(players, bool) or players <= 0:
        raise RegistrationError(
            "Количество игроков должно быть положительным целым числом."
            )

    stmt = select(Game).where(Game.id == game_id).with_for_update()
    game = (await session.execute(stmt)).scalar_one_or_none()

    if not game or not game.is_active:
        raise RegistrationError("Игра недоступна.")

    team_name = await validate_team_name(session, game_id, team_name)

    if source == "user" and user_id is not None:
        exists = await session.execute(
            select(Registration.id).where(
                Registration.user_id == user_id,
                Registration.game_id == game_id,
            )
        )
        if exists.scalar_one_or_none():
            raise RegistrationError("Вы уже зарегистрированы на эту игру.")


    if game.max_players_per_team is not None and players > game.max_players_per_team:
        raise PlayersLimitError(
            f"Максимум – {game.max_players_per_team} игроков в команде."
        )

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

    if source == "admin":
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
    except IntegrityError as exc:
        mapped_error = map_registration_integrity_error(exc)
        if mapped_error is not None:
            raise mapped_error from exc
        raise

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

    new_name = await validate_team_name(
        session,
        reg.game_id,
        new_name,
        exclude_reg_id=reg.id
    )

    try:
        async with session.begin_nested():
            reg.team_name = new_name
            await session.flush()
    except IntegrityError as exc:
        mapped_error = map_registration_integrity_error(exc)

        if mapped_error is not None:
            raise mapped_error from exc

        raise

    return reg

async def update_players(
    session: AsyncSession,
    *,
    reg_id: int,
    user_id: int,
    players: int,
) -> Registration:
    if not isinstance(players, int) or isinstance(players, bool) or players <= 0:
        raise RegistrationError("Количество игроков должно быть положительным целым числом.")

    reg = await session.get(Registration, reg_id)
    if not reg or reg.user_id != user_id:
        raise RegistrationError("Регистрация не найдена.")

    stmt = select(Game).where(Game.id == reg.game_id).with_for_update()
    game = (await session.execute(stmt)).scalar_one_or_none()
    if not game or not game.is_active:
        raise RegistrationError("Игра недоступна.")

    if game.max_players_per_team is not None and players > game.max_players_per_team:
        raise PlayersLimitError(
            f"Максимум — {game.max_players_per_team} игроков в команде."
        )

    reg.players = players

    confirmed_teams = await count_confirmed_teams(session, reg.game_id)
    confirmed_people_excl = await sum_confirmed_people(
        session, reg.game_id, exclude_reg_id=reg.id
    )

    # Если команда уже была подтверждена, она входит в confirmed_teams,
    # поэтому проверяем строгое превышение (>), иначе (>=)
    if reg.status == STATUS_CONFIRMED:
        teams_over = (
            game.teams_capacity is not None
            and confirmed_teams > game.teams_capacity
        )
    else:
        teams_over = (
            game.teams_capacity is not None
            and confirmed_teams >= game.teams_capacity
        )

    people_over = (
        game.people_capacity is not None
        and (confirmed_people_excl + players) > game.people_capacity
    )

    reg.status = STATUS_WAITLIST if (teams_over or people_over) else STATUS_CONFIRMED
    await session.flush()

    return reg

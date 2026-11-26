import pytest
from datetime import datetime, timezone
from sqlalchemy import select

from models import Game, Registration
from services.registration_service import (
    create_registration,
    validate_team_name,
    rename_team,
    RegistrationError
)
from constants import STATUS_CONFIRMED, STATUS_WAITLIST

# --------------- helpers ---------------

async def _make_game(session, *, capacity_teams=None, capacity_people=None,
                     max_per_team=None, is_active=True) -> Game:
    g = Game(
        title="Test Game",
        when=datetime.now(timezone.utc),
        teams_capacity=capacity_teams,
        people_capacity=capacity_people,
        max_players_per_team=max_per_team,
        is_active=is_active,
    )
    session.add(g)
    await session.flush()
    return g

# --------------- validate_team_name ---------------

@pytest.mark.asyncio
async def test_validate_name_ok(db_session):
    game = await _make_game(db_session)
    await validate_team_name(db_session, game.id, "Квизеры")

@pytest.mark.asyncio
async def test_validate_name_too_short(db_session):
    game = await _make_game(db_session)
    with pytest.raises(RegistrationError, match="символ"):
        await validate_team_name(db_session, game.id, "a")

@pytest.mark.asyncio
async def test_validate_name_too_long(db_session):
    game = await _make_game(db_session)
    with pytest.raises(RegistrationError, match="символ"):
        await validate_team_name(db_session, game.id, "a"*120)

@pytest.mark.asyncio
async def test_validate_name_duplicate(db_session):
    game = await _make_game(db_session)
    db_session.add(Registration(
        user_id=1, chat_id=1, game_id=game.id,
        team_name="Квизеры", players=4, status=STATUS_CONFIRMED,
    ))
    await db_session.flush()

    with pytest.raises(RegistrationError, match="уже"):
        await validate_team_name(db_session, game.id, "Квизеры")

# --------------- create_registration ---------------

@pytest.mark.asyncio
async def test_create_user_happy(db_session):
    game = await _make_game(db_session)
    reg = await create_registration(
        db_session,
        user_id=100, chat_id=100,
        game_id=game.id, team_name="The Best", players=5,
        source="user",
    )
    assert reg.id is not None
    assert reg.status == STATUS_CONFIRMED
    assert reg.user_id == 100

@pytest.mark.asyncio
async def test_create_admin_happy(db_session):
    game = await _make_game(db_session)
    reg = await create_registration(
        db_session,
        user_id=None, chat_id=0,
        game_id=game.id, team_name="Команда", players=3,
        source="admin",
    )
    assert reg.id is not None
    assert reg.user_id < 0
    assert reg.chat_id == 0
    assert reg.status == STATUS_CONFIRMED

@pytest.mark.asyncio
async def test_create_user_already_registered(db_session):
    game = await _make_game(db_session)
    await create_registration(
        db_session, user_id=42, chat_id=42,
        game_id=game.id, team_name="Firsts", players=2,
    )
    with pytest.raises(RegistrationError, match="уже зарегистрирован"):
        await create_registration(
                db_session, user_id=42, chat_id=42,
                game_id=game.id, team_name="Seconds", players=4,
            )

@pytest.mark.asyncio
async def test_create_waitlist_when_full(db_session):
    game = await _make_game(db_session, capacity_teams=2)
    for i in range(2):
        db_session.add(Registration(
            user_id=i+1, chat_id=i+1, game_id=game.id,
            team_name=f"Team{i}", players=4, status=STATUS_CONFIRMED,
        ))
    await db_session.flush()

    reg = await create_registration(
        db_session, user_id=999, chat_id=999,
        game_id=game.id, team_name="Команда Команда", players=5,
    )
    assert reg.status == STATUS_WAITLIST

@pytest.mark.asyncio
async def test_create_inactive_game(db_session):
    game = await _make_game(db_session, is_active=False)
    with pytest.raises(RegistrationError, match="недоступна"):
        await create_registration(
            db_session, user_id=1, chat_id=1,
            game_id=game.id, team_name="TestTeam", players=2,
        )

# --------------- rename_team ---------------

@pytest.mark.asyncio
async def test_rename_happy(db_session):
    game = await _make_game(db_session)
    reg = await create_registration(
        db_session, user_id=7, chat_id=7,
        game_id=game.id, team_name="Old Spice", players=6,
    )

    await rename_team(
        db_session, reg_id=reg.id, user_id=7, new_name="New Spice",
    )
    await db_session.refresh(reg)
    assert reg.team_name == "New Spice"

@pytest.mark.asyncio
async def test_rename_to_taken_name(db_session):
    game = await _make_game(db_session)
    reg = await create_registration(
        db_session, user_id=1, chat_id=1,
        game_id=game.id, team_name="Team X", players=3,
    )
    await create_registration(
        db_session, user_id=2, chat_id=2,
        game_id=game.id, team_name="Team Y", players=5,
    )
    with pytest.raises(RegistrationError, match="уже"):
        await rename_team(
            db_session, reg_id=reg.id, user_id=1, new_name="Team Y",
    )
    await db_session.refresh(reg)
    assert reg.team_name == "Team X"
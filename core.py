import logging

from contextlib import asynccontextmanager
from dataclasses import dataclass
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone, timedelta

from config import settings
from db import SessionLocal
from models import Game, Registration

from constants import (
    STATUS_CONFIRMED, STATUS_WAITLIST, TEAMS_LIST_LIMIT
)

logger = logging.getLogger(__name__)

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

async def list_active_games(s: AsyncSession) -> list[Game]:
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=3)

    res = await s.execute(
        select(Game).where(
            Game.is_active == True,
            (Game.when == None) | (Game.when >= cutoff)
        ).order_by(Game.when.is_(None), Game.when.asc(), Game.id.desc())
    )
    return list(res.scalars())


async def load_teams_for_admin(gid: int) -> tuple[Game | None, list[Registration]]:
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
    q = select(func.min(Registration.user_id))
    min_uid = (await s.execute(q)).scalar_one()
    if min_uid is None or min_uid >= 0:
        return -1
    return min_uid - 1

async def get_teams_for_text(s: AsyncSession, game_id: int, limit: int = TEAMS_LIST_LIMIT):
    res = await s.execute(
        select(Registration)
        .where(Registration.game_id == game_id)
        .order_by(Registration.status.asc(), Registration.created_at.asc())
        .limit(limit)
    )
    regs = list(res.scalars())
    cnt_all = (await s.execute(
        select(func.count(Registration.id)).where(Registration.game_id == game_id)
    )).scalar_one()
    return regs, cnt_all
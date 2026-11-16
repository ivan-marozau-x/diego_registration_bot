from models import Game, Registration
from core import GameStats
from utils import esc, fmt_dt
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from constants import STATUS_CONFIRMED, TEAMS_LIST_LIMIT

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
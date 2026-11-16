import pytest
from core import _load_teams_for_admin

@pytest.mark.asyncio
async def test_load_teams_returns_tuple_not_none():
    """Регрессия на BUG-01: функция не должна возвращать None."""
    # Тестируем несуществующую игру
    g, teams = await _load_teams_for_admin(999)
    
    assert g is None
    assert teams == []
    assert isinstance(teams, list)
"""즐겨찾기 목록 위젯 `FavoriteList`의 다시 그리기 · 고른 줄 알림 (#646)."""

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from app.widgets.favorite_list import NO_SELECTION, FavoriteList


def _entries(count: int) -> list[dict]:
    return [{"name": f"폴더 {n}", "path": f"/videos/f{n}"} for n in range(count)]


@pytest.fixture
def favorites(qapp: QApplication, qtbot: QtBot) -> FavoriteList:
    widget = FavoriteList()
    qtbot.addWidget(widget)
    return widget


def _record(widget: FavoriteList) -> list[int]:
    seen: list[int] = []
    widget.selectionChanged.connect(seen.append)
    return seen


def test_emptying_a_list_with_row_zero_selected_reports_no_selection(
    favorites: FavoriteList,
) -> None:
    """0번 줄을 고른 목록을 비우면 selectionChanged(NO_SELECTION)를 한 번 내야 한다.

    setEntries(2줄, 0) 뒤 setEntries([], 0)
    -> 알림 [NO_SELECTION], selected() = NO_SELECTION
    """
    favorites.setEntries(_entries(2), 0)
    seen = _record(favorites)
    favorites.setEntries([], 0)
    assert seen == [NO_SELECTION]
    assert favorites.selected() == NO_SELECTION


def test_redrawing_with_the_same_selection_sends_no_signal(favorites: FavoriteList) -> None:
    """같은 번호를 고른 채 다시 그리면 selectionChanged를 내지 않아야 한다.

    setEntries(3줄, 1) 뒤 setEntries(3줄, 1)
    -> 알림 [], selected() = 1, 1번 줄만 selected 속성 참
    """
    favorites.setEntries(_entries(3), 1)
    seen = _record(favorites)
    favorites.setEntries(_entries(3), 1)
    assert seen == []
    assert favorites.selected() == 1
    assert [favorites.row(n).property("selected") for n in range(3)] == [False, True, False]


@pytest.mark.parametrize("selected", [-1, -5, 2, 9])
def test_out_of_range_selection_picks_the_last_row(favorites: FavoriteList, selected: int) -> None:
    """범위 밖 번호(음수 포함)로 다시 그리면 마지막 줄을 골라야 한다.

    setEntries(2줄, selected) — selected는 -1 · -5 · 2 · 9
    -> selected() = 1, 1번 줄만 selected 속성 참
    """
    favorites.setEntries(_entries(2), selected)
    assert favorites.selected() == 1
    assert [favorites.row(n).property("selected") for n in range(2)] == [False, True]

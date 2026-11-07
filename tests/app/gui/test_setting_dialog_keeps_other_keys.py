"""설정 창을 연 채 다른 곳이 바꾼 설정을 설정 창의 OK가 되돌리지 않는지.

설정 창은 열 때 설정을 읽어 둔다. OK가 읽어 둔 설정 전체를 저장하면, 창을 연 뒤 메인 창이
바꾼 키(정렬 · 최근 위치 · 즐겨찾기)가 창을 열 때의 값으로 돌아간다. 설정 메뉴 단계에서는 설정 화면을
연 채 정렬 · 저장 위치를 바꿀 수 있다 — 그 상황을 설정 창을 모달 없이 띄워 만든다.

판정은 설정 DB를 직접 읽는다(제품의 읽기 함수를 거치지 않는다).
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Never

import pytest
from pytestqt.qtbot import QtBot
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from app.views.dialog import SettingDialog
from app.views.mainWindow import VodDownloader
from tests.support.settings_db import read_settings


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """카드의 썸네일 · 크기 조회 스레드가 실네트워크에 나가지 않게 막는다."""

    class _FailingSession:
        def head(self, *args: object, **kwargs: object) -> Never:
            raise RuntimeError("network disabled in tests")

        def get(self, *args: object, **kwargs: object) -> Never:
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())


@pytest.fixture
def window(qapp: QApplication, tmp_path: Path) -> Iterator[VodDownloader]:
    """메인 창 — 저장 위치를 임시 폴더로 골라 둔다."""
    win = VodDownloader()
    assert win.saveLocations.choose(str(tmp_path))
    yield win
    win.hide()
    win.deleteLater()
    QApplication.processEvents()


@pytest.fixture
def open_dialog(window: VodDownloader, qtbot: QtBot) -> Iterator[SettingDialog]:
    """메인 창 위에 모달 없이 띄운 설정 창 — 열린 채 메인 창을 조작할 수 있다."""
    dialog = SettingDialog(parent=window)
    dialog.show()
    qtbot.waitExposed(dialog)
    yield dialog
    dialog.hide()
    dialog.deleteLater()
    QApplication.processEvents()


def _press_ok(dialog: SettingDialog) -> None:
    """설정 창의 OK 버튼을 누른다."""
    dialog.dialogButtonBox.button(QDialogButtonBox.StandardButton.Ok).click()
    QApplication.processEvents()


def _pick_sort(win: VodDownloader, by: str, direction: str) -> None:
    """정렬 메뉴를 열어 기준을 누르고, 다시 열어 방향을 누른다."""
    for key in (("by", by), ("direction", direction)):
        button = win.sortButton
        menu = button.menuWidget
        menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
        QApplication.processEvents()
        next(a for a in menu.choices() if tuple(a.data()) == key).trigger()
        menu.hide()
    QApplication.processEvents()


def test_ok_keeps_a_sort_chosen_after_the_dialog_opened(
    window: VodDownloader, open_dialog: SettingDialog
) -> None:
    """설정 창을 연 뒤 고른 정렬은 설정 창의 OK 뒤에도 남아 있어야 한다.

    설정 창 열림 → 정렬 메뉴에서 채널 · 내림차순 → 설정 창 OK -> 저장된 sortOrder "channel:desc"
    """
    _pick_sort(window, "channel", "desc")
    assert read_settings()["sortOrder"] == "channel:desc", "전제: 정렬을 고르면 저장된다"
    _press_ok(open_dialog)
    assert read_settings()["sortOrder"] == "channel:desc"


def test_ok_keeps_a_location_chosen_after_the_dialog_opened(
    window: VodDownloader, open_dialog: SettingDialog, tmp_path: Path
) -> None:
    """설정 창을 연 뒤 고른 저장 위치는 설정 창의 OK 뒤에도 최근 위치에 남아 있어야 한다.

    설정 창 열림 → 저장 위치 새 폴더 고름 → 설정 창 OK -> 저장된 recentPaths 맨 앞이 새 폴더
    """
    chosen = tmp_path / "새 폴더"
    chosen.mkdir()
    assert window.saveLocations.choose(str(chosen))
    first = read_settings()["recentPaths"][0]
    assert Path(first) == chosen, "전제: 고른 위치가 최근 위치 맨 앞에 저장된다"
    _press_ok(open_dialog)
    assert read_settings()["recentPaths"][0] == first


def test_ok_keeps_a_favorite_added_after_the_dialog_opened(
    window: VodDownloader, open_dialog: SettingDialog, tmp_path: Path
) -> None:
    """설정 창을 연 뒤 더한 즐겨찾기는 설정 창의 OK 뒤에도 남아 있어야 한다.

    설정 창 열림 → 즐겨찾기 "작업"(임시 폴더) 더함 → 설정 창 OK -> 저장된 favoritePaths에 "작업" 하나
    """
    assert window.saveLocations.add_favorite("작업", str(tmp_path)) == ""
    saved = read_settings()["favoritePaths"]
    assert [entry["name"] for entry in saved] == ["작업"], "전제: 더한 즐겨찾기가 저장된다"
    _press_ok(open_dialog)
    assert read_settings()["favoritePaths"] == saved

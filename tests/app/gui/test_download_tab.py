"""설정 창 다운로드 탭 — 기본 저장 위치 · 즐겨찾기 관리 · 기본 해상도 (#646).

실제 배선을 탄다: 본 창의 설정 버튼 동작(`onSetting`)으로 연 설정 창에서 버튼을 누르고 저장 버튼을 누른다.
폴더 선택 창 · 별칭 입력 창 · 경고 창만 바꿔 끼운다. 씨앗과 판정은 설정 DB를 직접 읽고 쓴다
(`tests.support.settings_db`). 헤더 저장 위치 메뉴는 실제로 열어(`popup` → `aboutToShow`) 줄을 읽는다.
"""

from collections.abc import Callable, Iterator

import pytest
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QMessageBox,
)
from pytestqt.qtbot import QtBot

import app.theme as theme
import app.viewmodels.content_viewmodel as content_viewmodel_module
import CVDv2 as main_module
from app.views.dialog import TAB_KEYS, SettingDialog
from app.views.mainWindow import VodDownloader
from app.widgets.favorite_list import NO_SELECTION
from app.widgets.header_controls import ChoiceMenu
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels
from tests.support.settings_db import read_settings, write_settings

DIALOG_WAIT_MS = 3000  # 설정 창이 뜨기를 기다리는 한도(ms)
POLL_MS = 10  # 설정 창이 떴는지 다시 보는 간격(ms)
CARD_WAIT_MS = 3000  # 등록한 카드의 조회 결과가 들어오기를 기다리는 한도(ms)
FAVORITES_MAX = 10  # 즐겨찾기 최대 수 — SPEC §9.2 `favoritePaths`


@pytest.fixture(autouse=True)
def _production_style(qapp: QApplication) -> Iterator[None]:
    """실제 전역 QSS · 다크 테마로 잰다. 끝에 빈 QSS로 되돌린다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))
    yield
    qapp.setStyleSheet("")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())


@pytest.fixture(autouse=True)
def _drop_windows(qapp: QApplication) -> Iterator[None]:
    """테스트가 만든 최상위 창을 끝에 파괴한다."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture
def folders(tmp_path: object) -> Callable[[str], str]:
    """이름 붙인 실존 폴더를 만든다 — `folders("가")` → 경로 문자열."""

    def make(name: str) -> str:
        path = tmp_path / name  # type: ignore[operator]
        path.mkdir(exist_ok=True)
        return str(path)

    return make


@pytest.fixture
def open_window(qapp: QApplication) -> Iterator[Callable[[], VodDownloader]]:
    """본 창을 만들어 보인다 — 씨앗을 쓴 뒤에 부른다."""
    made: list[VodDownloader] = []

    def make() -> VodDownloader:
        win = VodDownloader()
        win.resize(900, 700)
        win.show()
        QApplication.processEvents()
        made.append(win)
        return win

    yield make
    for win in made:
        win.saveLocationButton.menuWidget.hide()
        win.hide()


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """설정 창의 경고 창을 띄우지 않고 글만 모은다."""
    shown: list[str] = []

    def fake_warning(parent: object, title: str, text: str, *args: object) -> None:
        shown.append(text)

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(fake_warning))
    return shown


def _pick_folder(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """폴더 선택 창이 `path`를 고른 것으로 한다."""
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: path))


def _type_name(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """별칭 입력 창에 `name`을 넣고 확인한 것으로 한다."""
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: (name, True)))


def _in_settings(
    win: VodDownloader, change: Callable[[SettingDialog], None], button: str = "save"
) -> None:
    """본 창의 설정 동작으로 설정 창을 열어 다운로드 탭에서 `change`한 뒤 저장(또는 취소)을 누른다."""
    seen: dict = {"done": False, "error": None}
    tries = DIALOG_WAIT_MS // POLL_MS

    def act() -> None:
        nonlocal tries
        dialog = getattr(win, "dialog", None)
        if not isinstance(dialog, SettingDialog) or not dialog.isVisible():
            tries -= 1
            if tries > 0:
                QTimer.singleShot(POLL_MS, act)
            return
        try:
            dialog.showTab(TAB_KEYS.index("download"))
            change(dialog)
        except Exception as error:  # noqa: BLE001 — 창을 닫아 테스트가 멈추지 않게 하고 아래에서 다시 올린다
            seen["error"] = error
            dialog.reject()
            return
        boxButton = (
            QDialogButtonBox.StandardButton.Save
            if button == "save"
            else QDialogButtonBox.StandardButton.Cancel
        )
        target = dialog.dialogButtonBox.button(boxButton)
        if not target.isEnabled():
            dialog.reject()  # 바뀐 값이 없다 — 창을 닫아 테스트가 멈추지 않게 하고 아래 단언이 실패한다
            return
        target.click()
        seen["done"] = True

    QTimer.singleShot(POLL_MS, act)
    win.onSetting()
    QApplication.processEvents()
    if seen["error"] is not None:
        raise seen["error"]
    assert seen["done"], "설정 창이 뜨지 않았거나 바뀐 값이 없었다"


def _click(name: str) -> Callable[[SettingDialog], None]:
    """다운로드 탭의 그 버튼을 누르는 동작."""

    def change(dialog: SettingDialog) -> None:
        dialog.favoriteButtons[name].click()

    return change


def _menu_favorites(win: VodDownloader) -> list[tuple[str, str]]:
    """헤더 저장 위치 메뉴를 실제로 열어 즐겨찾기 줄의 (별칭, 경로)를 위에서부터 읽는다."""
    button = win.saveLocationButton
    menu = button.menuWidget
    menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
    QApplication.processEvents()
    rows = [
        (ChoiceMenu.nameOf(action), tuple(action.data())[1])
        for action in menu.choices()
        if tuple(action.data())[0] == "favorite"
    ]
    menu.hide()
    return rows


def _favorites(*names: str, folders: Callable[[str], str]) -> list[dict]:
    """별칭마다 같은 이름의 실존 폴더를 가리키는 즐겨찾기 목록."""
    return [{"name": name, "path": folders(name)} for name in names]


# ================================================================ 즐겨찾기 — 헤더 메뉴와 같은 목록


def test_added_favorite_is_saved_and_shown_in_the_header_menu(
    open_window: Callable[[], VodDownloader],
    folders: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """다운로드 탭에서 즐겨찾기를 더하고 저장하면 설정 행과 헤더 저장 위치 메뉴가 같은 목록이어야 한다.

    즐겨찾기 [가], 더하기(폴더 「나」 · 별칭 「나 별칭」) 저장
    -> favoritePaths [가, 나 별칭], 메뉴 즐겨찾기 줄 [가, 나 별칭] 같은 차례
    """
    write_settings({"favoritePaths": _favorites("가", folders=folders)})
    win = open_window()
    added = folders("나")
    _pick_folder(monkeypatch, added)
    _type_name(monkeypatch, "나 별칭")
    _in_settings(win, _click("favoriteAdd"))
    saved = read_settings()["favoritePaths"]
    assert saved == [{"name": "가", "path": folders("가")}, {"name": "나 별칭", "path": added}]
    assert _menu_favorites(win) == [(entry["name"], entry["path"]) for entry in saved]


def test_header_menu_favorite_shows_in_the_download_tab(
    open_window: Callable[[], VodDownloader],
    folders: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """헤더 메뉴로 더한 즐겨찾기는 그 뒤 연 설정 창 다운로드 탭 목록의 마지막 줄이어야 한다.

    지금 위치 「다」, 메뉴 「즐겨찾기에 더하기」(별칭 「다」) -> 설정 창 목록 줄 수 1, 첫 줄 별칭 「다」
    """
    win = open_window()
    assert win.saveLocations.choose(folders("다"))
    _type_name(monkeypatch, "다")
    button = win.saveLocationButton
    button.menuWidget.popup(button.mapToGlobal(button.rect().bottomLeft()))
    QApplication.processEvents()
    next(a for a in button.menuWidget.actions() if a.objectName() == "addFavoriteAction").trigger()
    button.menuWidget.hide()
    rows: list[str] = []

    def look(dialog: SettingDialog) -> None:
        rows.extend(
            dialog.favoriteList.row(n).nameLabel.text() for n in range(dialog.favoriteList.count())
        )

    _in_settings(win, look, button="cancel")
    assert rows == ["다"]


@pytest.mark.parametrize(
    ("case", "name", "folder", "expected_warning"),
    [
        ("same-path", "새 별칭", "가", "This folder is already a favorite."),
        ("same-name", "가", "나", "A favorite with this name already exists."),
        ("empty-name", "   ", "나", "Enter a name for the favorite."),
    ],
)
def test_add_is_refused_by_the_header_menu_rules(
    open_window: Callable[[], VodDownloader],
    folders: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
    case: str,
    name: str,
    folder: str,
    expected_warning: str,
) -> None:
    """겹치는 경로 · 겹치는 별칭 · 빈 별칭으로 더하면 헤더 메뉴와 같은 경고가 뜨고 목록이 그대로여야 한다.

    즐겨찾기 [가] -> 같은 경로 · 같은 별칭 · 빈 별칭 더하기 → 경고 글 = 메뉴 문구, 줄 수 1, 저장 꺼짐
    """
    write_settings({"favoritePaths": _favorites("가", folders=folders)})
    win = open_window()
    _pick_folder(monkeypatch, folders(folder))
    _type_name(monkeypatch, name)
    seen: dict = {}

    def change(dialog: SettingDialog) -> None:
        dialog.favoriteButtons["favoriteAdd"].click()
        seen["rows"] = dialog.favoriteList.count()
        seen["save"] = dialog.saveButton.isEnabled()

    _in_settings(win, change, button="cancel")
    assert warnings == [expected_warning]
    assert seen == {"rows": 1, "save": False}


def test_add_is_off_when_ten_favorites_exist(
    open_window: Callable[[], VodDownloader], folders: Callable[[str], str]
) -> None:
    """즐겨찾기가 10개면 더하기 버튼이 꺼지고, 하나를 지우면 다시 켜져야 한다.

    즐겨찾기 f0 ~ f9 -> 더하기 꺼짐, 지우기 뒤 켜짐 · 줄 수 9
    """
    write_settings(
        {"favoritePaths": _favorites(*(f"f{n}" for n in range(FAVORITES_MAX)), folders=folders)}
    )
    win = open_window()
    seen: dict = {}

    def change(dialog: SettingDialog) -> None:
        add = dialog.favoriteButtons["favoriteAdd"]
        seen["full"] = add.isEnabled()
        dialog.favoriteButtons["favoriteRemove"].click()
        seen["after"] = (add.isEnabled(), dialog.favoriteList.count())

    _in_settings(win, change, button="cancel")
    assert seen == {"full": False, "after": (True, FAVORITES_MAX - 1)}


def test_rename_to_another_favorites_name_is_refused_and_own_name_is_kept(
    open_window: Callable[[], VodDownloader],
    folders: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
) -> None:
    """다른 줄의 별칭으로 고치면 경고가 뜨고 그대로이며, 자기 별칭 그대로 확인하면 경고 없이 저장이 꺼져 있어야 한다.

    즐겨찾기 [가, 나], 「나」 줄 고르고 별칭 「가」 -> 경고 1, 별칭 그대로 / 별칭 「나」 -> 경고 없음, 저장 꺼짐
    """
    write_settings({"favoritePaths": _favorites("가", "나", folders=folders)})
    win = open_window()
    seen: dict = {}

    def change(dialog: SettingDialog) -> None:
        dialog.favoriteList.select(1)
        _type_name(monkeypatch, "가")
        dialog.favoriteButtons["favoriteRename"].click()
        seen["taken"] = (list(warnings), dialog.favoriteList.row(1).nameLabel.text())
        _type_name(monkeypatch, "나")
        dialog.favoriteButtons["favoriteRename"].click()
        seen["own"] = (len(warnings), dialog.saveButton.isEnabled())

    _in_settings(win, change, button="cancel")
    assert seen["taken"] == (["A favorite with this name already exists."], "나")
    assert seen["own"] == (1, False)


def test_renamed_favorite_is_saved_and_shown_in_the_header_menu(
    open_window: Callable[[], VodDownloader],
    folders: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """고른 줄의 별칭을 고치고 저장하면 설정 행과 헤더 메뉴의 그 줄 별칭이 새 별칭이어야 한다.

    즐겨찾기 [가, 나], 「가」 줄 별칭 → 「새 이름」 저장 -> favoritePaths [새 이름(가 경로), 나], 메뉴 같은 목록
    """
    write_settings({"favoritePaths": _favorites("가", "나", folders=folders)})
    win = open_window()
    _type_name(monkeypatch, "새 이름")
    _in_settings(win, _click("favoriteRename"))
    expected = [("새 이름", folders("가")), ("나", folders("나"))]
    saved = read_settings()["favoritePaths"]
    assert [(entry["name"], entry["path"]) for entry in saved] == expected
    assert _menu_favorites(win) == expected


# ================================================================ 순서 · 지우기


@pytest.mark.parametrize(
    ("start", "button", "order", "selected"),
    [
        (0, "favoriteDown", ["나", "가", "다"], 1),
        (2, "favoriteUp", ["가", "다", "나"], 1),
    ],
)
def test_moving_a_favorite_saves_the_new_order_for_the_header_menu(
    open_window: Callable[[], VodDownloader],
    folders: Callable[[str], str],
    start: int,
    button: str,
    order: list[str],
    selected: int,
) -> None:
    """위 · 아래 버튼으로 옮기고 저장하면 설정 행과 헤더 메뉴가 새 차례이고 고른 줄이 따라가야 한다.

    [가, 나, 다], 0번 아래로 -> [나, 가, 다] · 고른 줄 1 / 2번 위로 -> [가, 다, 나] · 고른 줄 1
    """
    write_settings({"favoritePaths": _favorites("가", "나", "다", folders=folders)})
    win = open_window()
    seen: dict = {}

    def change(dialog: SettingDialog) -> None:
        dialog.favoriteList.select(start)
        dialog.favoriteButtons[button].click()
        seen["selected"] = dialog.favoriteList.selected()

    _in_settings(win, change)
    assert seen["selected"] == selected
    assert [entry["name"] for entry in read_settings()["favoritePaths"]] == order
    assert [name for name, _ in _menu_favorites(win)] == order


def test_up_and_down_are_off_at_the_ends(
    open_window: Callable[[], VodDownloader], folders: Callable[[str], str]
) -> None:
    """첫 줄을 고르면 위가, 끝 줄을 고르면 아래가 꺼져야 한다.

    [가, 나] -> 0번: 위 꺼짐 · 아래 켬, 1번: 위 켬 · 아래 꺼짐
    """
    write_settings({"favoritePaths": _favorites("가", "나", folders=folders)})
    win = open_window()
    seen: list[tuple[bool, bool]] = []

    def change(dialog: SettingDialog) -> None:
        buttons = dialog.favoriteButtons
        for index in (0, 1):
            dialog.favoriteList.select(index)
            seen.append((buttons["favoriteUp"].isEnabled(), buttons["favoriteDown"].isEnabled()))

    _in_settings(win, change, button="cancel")
    assert seen == [(False, True), (True, False)]


def test_removing_the_last_favorite_empties_the_list_and_turns_buttons_off(
    open_window: Callable[[], VodDownloader], folders: Callable[[str], str]
) -> None:
    """마지막 즐겨찾기를 지우고 저장하면 행이 빈 목록이고, 창에서는 빈 안내가 보이며 줄 버튼이 꺼져야 한다.

    [가], 지우기 -> 빈 안내 보임, 고른 줄 없음, 별칭 · 위 · 아래 · 지우기 꺼짐 / 저장 뒤 favoritePaths [], 메뉴 줄 없음
    """
    write_settings({"favoritePaths": _favorites("가", folders=folders)})
    win = open_window()
    seen: dict = {}

    def change(dialog: SettingDialog) -> None:
        dialog.favoriteButtons["favoriteRemove"].click()
        buttons = dialog.favoriteButtons
        seen["empty"] = dialog.favoriteList.emptyLabel.isVisible()
        seen["selected"] = dialog.favoriteList.selected()
        seen["on"] = [
            buttons[name].isEnabled()
            for name in ("favoriteRename", "favoriteUp", "favoriteDown", "favoriteRemove")
        ]

    _in_settings(win, change)
    assert seen == {"empty": True, "selected": NO_SELECTION, "on": [False] * 4}
    assert read_settings()["favoritePaths"] == []
    assert _menu_favorites(win) == []


def test_arrow_keys_move_the_selected_row(
    open_window: Callable[[], VodDownloader], folders: Callable[[str], str]
) -> None:
    """목록에 포커스가 있을 때 아래 · 위 · End · Home 키는 고른 줄을 그 차례로 옮겨야 한다.

    [가, 나, 다], 고른 줄 0 -> 아래 1 · 아래 2 · 아래 2(끝) · 위 1 · Home 0 · End 2
    """
    write_settings({"favoritePaths": _favorites("가", "나", "다", folders=folders)})
    win = open_window()
    seen: list[int] = []

    def change(dialog: SettingDialog) -> None:
        favorites = dialog.favoriteList
        favorites.setFocus(Qt.FocusReason.TabFocusReason)
        for key in (Qt.Key.Key_Down, Qt.Key.Key_Down, Qt.Key.Key_Down, Qt.Key.Key_Up):
            QTest.keyClick(favorites, key)
            seen.append(favorites.selected())
        for key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            QTest.keyClick(favorites, key)
            seen.append(favorites.selected())

    _in_settings(win, change, button="cancel")
    assert seen == [1, 2, 2, 1, 0, 2]


def test_cancel_after_editing_favorites_writes_nothing(
    open_window: Callable[[], VodDownloader],
    folders: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """즐겨찾기를 더하고 · 옮기고 · 지운 뒤 취소하면 설정 행과 헤더 메뉴가 연 때 그대로여야 한다.

    [가, 나], 더하기 「다」 · 0번 아래로 · 지우기 후 취소 -> favoritePaths [가, 나], 메뉴 [가, 나]
    """
    seed = _favorites("가", "나", folders=folders)
    write_settings({"favoritePaths": seed})
    win = open_window()
    _pick_folder(monkeypatch, folders("다"))
    _type_name(monkeypatch, "다")

    def change(dialog: SettingDialog) -> None:
        dialog.favoriteButtons["favoriteAdd"].click()
        dialog.favoriteList.select(0)
        dialog.favoriteButtons["favoriteDown"].click()
        dialog.favoriteButtons["favoriteRemove"].click()
        assert dialog.saveButton.isEnabled(), "전제: 바뀐 값이 있다"

    _in_settings(win, change, button="cancel")
    assert read_settings()["favoritePaths"] == seed
    assert _menu_favorites(win) == [(entry["name"], entry["path"]) for entry in seed]


# ================================================================ 기본 저장 위치


def test_new_default_location_moves_the_header_when_it_was_at_the_default(
    open_window: Callable[[], VodDownloader],
    folders: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """헤더 저장 위치가 기본 위치일 때 기본 저장 위치를 바꿔 저장하면 헤더 위치도 새 위치여야 한다.

    downloadPath 「옛」, 헤더 = 기본 위치, 찾기… → 「새」 저장 -> downloadPath 「새」, 헤더 지금 위치 「새」 · 기본 위치
    """
    old, new = folders("옛"), folders("새")
    write_settings({"downloadPath": old})
    win = open_window()
    assert win.saveLocations.current() == old, "전제: 헤더가 기본 위치"
    _pick_folder(monkeypatch, new)
    _in_settings(win, lambda dialog: dialog.downloadPathBrowse.click())
    assert read_settings()["downloadPath"] == new
    assert (win.saveLocations.current(), win.saveLocations.is_default()) == (new, True)


def test_new_default_location_keeps_a_chosen_header_location(
    open_window: Callable[[], VodDownloader],
    folders: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """헤더에서 다른 위치를 골라 둔 채 기본 저장 위치를 바꿔 저장하면 헤더 위치는 고른 위치 그대로여야 한다.

    downloadPath 「옛」, 헤더에서 「고른」 선택, 찾기… → 「새」 저장 -> 헤더 지금 위치 「고른」, 기본 위치 「새」
    """
    old, chosen, new = folders("옛"), folders("고른"), folders("새")
    write_settings({"downloadPath": old})
    win = open_window()
    assert win.saveLocations.choose(chosen)
    _pick_folder(monkeypatch, new)
    _in_settings(win, lambda dialog: dialog.downloadPathBrowse.click())
    assert (win.saveLocations.current(), win.saveLocations.default()) == (chosen, new)


def test_default_location_button_clears_the_saved_path(
    open_window: Callable[[], VodDownloader], folders: Callable[[str], str]
) -> None:
    """「기본 위치」를 누르고 저장하면 기본 저장 위치 행이 빈 값이고, 누른 뒤 그 버튼은 꺼져야 한다.

    downloadPath 「옛」 -> 「기본 위치」 누름: 버튼 꺼짐, 경로 줄 = 시스템 다운로드 폴더 / 저장 뒤 downloadPath ""
    """
    write_settings({"downloadPath": folders("옛")})
    win = open_window()
    seen: dict = {}

    def change(dialog: SettingDialog) -> None:
        assert dialog.downloadPathDefault.isEnabled(), "전제: 저장된 경로가 있다"
        dialog.downloadPathDefault.click()
        seen["enabled"] = dialog.downloadPathDefault.isEnabled()
        seen["tip"] = dialog.downloadPath.toolTip()
        seen["system"] = dialog.systemDownloadPath()

    _in_settings(win, change)
    assert seen["enabled"] is False
    assert seen["tip"] == seen["system"]
    assert read_settings()["downloadPath"] == ""


def test_browse_cancelled_keeps_the_path_and_save_off(
    open_window: Callable[[], VodDownloader],
    folders: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """폴더 선택 창을 고르지 않고 닫으면 기본 저장 위치가 그대로이고 저장이 꺼져 있어야 한다.

    downloadPath 「옛」, 찾기… → 빈 값 -> 경로 줄 툴팁 「옛」, 저장 꺼짐
    """
    old = folders("옛")
    write_settings({"downloadPath": old})
    win = open_window()
    _pick_folder(monkeypatch, "")
    seen: dict = {}

    def change(dialog: SettingDialog) -> None:
        dialog.downloadPathBrowse.click()
        seen["path"] = (dialog.downloadPath.toolTip(), dialog.saveButton.isEnabled())

    _in_settings(win, change, button="cancel")
    assert seen["path"] == (old, False)


# ================================================================ 기본 해상도


class _StreamsFetchJob(QObject):
    """FetchJob과 같은 시그널 계약의 가짜 조회 — 해상도 360 · 720 · 1080 목록을 낸다(네트워크 없음)."""

    finished = Signal(object, str)
    error = Signal(str)

    def __init__(self, vod_url: str, cookies: dict, downloadPath: str) -> None:
        super().__init__()
        self.vod_url = vod_url
        self.downloadPath = downloadPath

    def run(self) -> None:
        reps = [[360, "u360"], [720, "u720"], [1080, "u1080"]]
        metadata = {
            "title": "제목",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 60,
        }
        result = (self.vod_url, metadata, reps, 1080, "u1080", self.downloadPath, None)
        self.finished.emit(result, "video")


@pytest.fixture
def fetch_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """조회 작업을 가짜로 바꾸고 카드의 크기 조회 · 그림 받기가 네트워크로 나가지 않게 한다."""

    class _FailingSession:
        def head(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("network disabled in tests")

        def get(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(content_viewmodel_module, "FetchJob", _StreamsFetchJob)
    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())


def _register(qtbot: QtBot, win: VodDownloader, number: int) -> tuple[int, str]:
    """URL 하나를 등록하고 조회 결과 카드의 (해상도, 주소)를 돌려준다."""
    model = win.contentManager.model
    before = model.rowCount()
    win.urlInput.setText(f"https://chzzk.naver.com/video/{number}")
    win.fetchButton.click()
    win.contentManager.threadpool.waitForDone(CARD_WAIT_MS)
    qtbot.waitUntil(lambda: not win.contentManager.isFetching(), timeout=CARD_WAIT_MS)
    assert model.rowCount() == before + 1
    item = model.itemAt(before)
    return item.resolution, item.base_url


def _choose_resolution(value: str | int) -> Callable[[SettingDialog], None]:
    """기본 해상도 칸에서 그 값을 고르는 동작."""

    def change(dialog: SettingDialog) -> None:
        combo = dialog.defaultResolution
        combo.setCurrentIndex(combo.findData(value))

    return change


@pytest.mark.usefixtures("fetch_streams")
@pytest.mark.parametrize(
    ("choice", "expected"),
    [(720, (720, "u720")), (480, (360, "u360")), (144, (360, "u360"))],
)
def test_cards_added_after_saving_use_the_default_resolution(
    qtbot: QtBot,
    open_window: Callable[[], VodDownloader],
    folders: Callable[[str], str],
    choice: int,
    expected: tuple[int, str],
) -> None:
    """기본 해상도를 저장한 뒤 추가하는 카드는 그 해상도(없으면 낮은 것 중 가장 높은 것)여야 한다.

    목록 360 · 720 · 1080, 저장 전 카드 = 1080 / 720 저장 → 720, 480 → 360, 144 → 360(낮은 것 없음 → 가장 낮은 것)
    / 저장 전 카드는 1080 그대로, 행 defaultResolution = 고른 값
    """
    win = open_window()
    assert win.saveLocations.choose(folders("받기"))
    assert _register(qtbot, win, 1) == (1080, "u1080"), "전제: 기본값은 가장 높은 것"
    _in_settings(win, _choose_resolution(choice))
    assert read_settings()["defaultResolution"] == choice
    assert _register(qtbot, win, 2) == expected
    assert win.contentManager.model.itemAt(0).resolution == 1080


@pytest.mark.usefixtures("fetch_streams")
def test_highest_resolution_choice_saves_best_and_picks_the_top(
    qtbot: QtBot, open_window: Callable[[], VodDownloader], folders: Callable[[str], str]
) -> None:
    """기본 해상도를 「가장 높은 것」으로 돌려 저장하면 행이 "best"이고 새 카드가 가장 높은 해상도여야 한다.

    defaultResolution 720, 「가장 높은 것」 저장 -> 행 "best", 새 카드 1080
    """
    write_settings({"defaultResolution": 720})
    win = open_window()
    assert win.saveLocations.choose(folders("받기"))
    assert _register(qtbot, win, 1) == (720, "u720"), "전제: 저장된 720을 따른다"
    _in_settings(win, _choose_resolution("best"))
    assert read_settings()["defaultResolution"] == "best"
    assert _register(qtbot, win, 2) == (1080, "u1080")

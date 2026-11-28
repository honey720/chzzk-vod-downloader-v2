"""설정 창 5탭 틀 — 탭 전환 · 항목 자리 · 바뀐 값 저장 · 취소 · 닫기 확인 · 탭 줄 폭 (#644).

씨앗과 판정은 설정 DB를 직접 읽고 쓴다(`tests.support.settings_db` — 제품의 읽기 · 쓰기 함수를 거치지
않는다). 버튼 · 키는 실제 위젯에 누름 · 키 이벤트를 보내 시그널 배선을 탄다.
"""

import json
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator

import pytest
from pytestqt.qtbot import QtBot
from PySide6.QtCore import Qt, QTimer, QTranslator
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QPushButton, QWidget

import app.system.settings as settings
import app.theme as theme
import CVDv2 as main_module
from app.views.dialog import SettingDialog
from tests.app.gui.card_helpers import hold_style
from tests.qt_platform import NATIVE_SWITCH
from tests.support import os_key_input
from tests.support.settings_db import read_rows, read_settings, write_rows

# 모든 테스트가 `opened`로 설정 창을 활성으로 만든다 — 실제 화면 병렬 실행에서는 건너뛴다(#523)
pytestmark = pytest.mark.os_window

ACTIVE_WAIT_MS = (
    3000  # 창 활성 · 확인 대화상자를 기다리는 한도(ms) — X11 · xvfb는 활성이 비동기로 온다
)
PROMPT_POLL_MS = 10  # 닫기 확인 대화상자가 떴는지 다시 보는 간격(ms)
TAB_ORDER = ["general", "download", "display", "account", "advanced"]  # 탭 차례 — 설계 §14.3
#: 옮긴 항목 → 있어야 할 탭(#644). 라벨이 있는 칸은 라벨도 같은 탭이다
ITEM_TABS = {
    "language": "general",
    "languageLabel": "general",
    "afterDownload": "general",
    "afterDownloadLabel": "general",
    "nidaut": "account",
    "nidautLabel": "account",
    "nidses": "account",
    "nidsesLabel": "account",
    "helpButton": "account",
    "logsFolder": "advanced",
    "logsFolderLabel": "advanced",
    # 화면 탭 체크박스(#645) — 체크박스 글이 라벨이다
    "cardStateTint": "display",
    "cardCompactThumbnail": "display",
    "animations": "display",
}
TAB_NAMES = {
    "ko_KR": ["일반", "다운로드", "화면", "계정", "고급"],
    "en_US": ["General", "Download", "Display", "Account", "Advanced"],
}
#: 창을 열 때 저장돼 있는 값 — 칸마다 맨 위 선택지가 아니다
SAVED = {
    "cookies": {"NID_AUT": "aut-saved", "NID_SES": "ses-saved"},
    "afterDownload": "sleep",
    "language": "ko_KR",
    # 화면 탭(#645) — 표 기본값(켬 · 끔 · null)과 다른 값
    "cardStateTint": False,
    "cardCompactThumbnail": True,
    "animations": False,
}


def _seed() -> None:
    """기본값 위에 `SAVED`를 행마다 직접 쓴다 — 제품이 내지 않을 모양(들여쓰기 4)이라 다시 쓰면 문자열이 바뀐다."""
    values = settings.default_settings() | SAVED
    write_rows({key: json.dumps(value, indent=4) for key, value in values.items()})


def _change_nidaut(dialog: SettingDialog) -> None:
    dialog.nidaut.setText("aut-typed")


def _change_nidses(dialog: SettingDialog) -> None:
    dialog.nidses.setText("ses-typed")


def _change_after_download(dialog: SettingDialog) -> None:
    dialog.afterDownload.setCurrentIndex(dialog.afterDownload.findData("shutdown"))


def _change_language(dialog: SettingDialog) -> None:
    dialog.language.setCurrentIndex(dialog.language.findData("en_US"))


def _toggle(key: str) -> Callable[[SettingDialog], None]:
    """화면 탭 체크박스 하나를 뒤집는 동작(#645)."""
    return lambda dialog: dialog.displayChecks[key].toggle()


#: 칸 → (값을 바꾸는 함수, 그 칸이 쓰는 키, 바꾼 뒤 저장될 값)
FIELDS: dict[str, tuple[Callable[[SettingDialog], None], str, object]] = {
    "nidaut": (_change_nidaut, "cookies", {"NID_AUT": "aut-typed", "NID_SES": "ses-saved"}),
    "nidses": (_change_nidses, "cookies", {"NID_AUT": "aut-saved", "NID_SES": "ses-typed"}),
    "afterDownload": (_change_after_download, "afterDownload", "shutdown"),
    "language": (_change_language, "language", "en_US"),
    # 화면 탭(#645) — `SAVED`의 값을 뒤집은 값
    "cardStateTint": (_toggle("cardStateTint"), "cardStateTint", True),
    "cardCompactThumbnail": (_toggle("cardCompactThumbnail"), "cardCompactThumbnail", False),
    "animations": (_toggle("animations"), "animations", True),
}


def _shown_value(dialog: SettingDialog, key: str) -> object:
    """설정 창 칸이 보이는 값 — 키마다 저장할 꼴로."""
    if key == "cookies":
        return {"NID_AUT": dialog.nidaut.text(), "NID_SES": dialog.nidses.text()}
    if key in dialog.displayChecks:
        return dialog.displayChecks[key].isChecked()
    return getattr(dialog, key).currentData()


@pytest.fixture
def app_style(qapp: QApplication) -> Iterator[None]:
    """앱 스타일 · 실제 QSS(다크). 끝에 빈 QSS로 되돌린다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))
    yield
    qapp.setStyleSheet("")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())


@pytest.fixture
def opened(qtbot: QtBot, app_style: None) -> Callable[[], SettingDialog]:
    """`SAVED`로 연 설정 창을 만드는 함수 — 보이고 활성이다. 정리 때 닫기 전에 숨긴다(닫기 확인이 뜨지 않게)."""

    def open_() -> SettingDialog:
        dialog = SettingDialog()
        qtbot.addWidget(dialog, before_close_func=lambda widget: widget.hide())
        dialog.show()
        qtbot.waitExposed(dialog)
        dialog.activateWindow()
        qtbot.waitUntil(dialog.isActiveWindow, timeout=ACTIVE_WAIT_MS)
        return dialog

    _seed()
    return open_


def _save_keys() -> QKeySequence:
    """저장 단축키(Ctrl+S, macOS Cmd+S) — Qt 표준 키에서 읽는다(앱이 있어야 플랫폼 키를 안다)."""
    return QKeySequence(QKeySequence.StandardKey.Save)


def _save(dialog: SettingDialog) -> QPushButton:
    return dialog.dialogButtonBox.button(QDialogButtonBox.StandardButton.Save)


def _cancel(dialog: SettingDialog) -> QPushButton:
    return dialog.dialogButtonBox.button(QDialogButtonBox.StandardButton.Cancel)


def _close_answering(
    qtbot: QtBot, dialog: SettingDialog, answer: Callable[[QDialog], None]
) -> dict:
    """창을 닫고(`close`) 뜬 닫기 확인 대화상자에 answer를 한다 — 본 것(떴는지 · 기본 · Esc 버튼)을 돌려준다.

    대화상자는 `exec`로 떠서 `close`가 돌아오지 않으므로, 타이머가 떴는지 보고 답한다. 한도 안에 뜨지
    않으면 아무것도 누르지 않는다(떴는지 = 거짓).
    """
    seen: dict = {"shown": False}
    tries = ACTIVE_WAIT_MS // PROMPT_POLL_MS

    def poll() -> None:
        nonlocal tries
        box = dialog.closePrompt
        if box is None or not box.isVisible():
            tries -= 1
            if tries > 0:
                QTimer.singleShot(PROMPT_POLL_MS, poll)
            return
        seen["shown"] = True
        seen["default"] = box.defaultButton().objectName()
        seen["escape"] = box.escapeButton().objectName()
        answer(box)

    QTimer.singleShot(PROMPT_POLL_MS, poll)
    dialog.close()
    QApplication.processEvents()
    return seen


def _click(name: str) -> Callable[[QDialog], None]:
    """닫기 확인 대화상자의 그 버튼을 누르는 답."""
    return lambda box: box.findChild(QPushButton, name).click()


# ================================================================ 탭


@pytest.mark.parametrize("key", TAB_ORDER)
def test_clicking_a_tab_shows_its_page(opened: Callable[[], SettingDialog], key: str) -> None:
    """탭을 누르면 그 탭만 고른 채 그 탭 쪽이 보여야 한다.

    탭 버튼 왼쪽 단추 누름 -> 고른 탭 = 그 탭 하나, 보이는 쪽 = `<key>Page`
    """
    dialog = opened()
    QTest.mouseClick(dialog.tabs[key], Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert [k for k, tab in dialog.tabs.items() if tab.isChecked()] == [key]
    assert dialog.pages.currentWidget().objectName() == f"{key}Page"
    assert dialog.pages.currentWidget().isVisible()


def test_window_opens_on_the_general_tab(opened: Callable[[], SettingDialog]) -> None:
    """설정 창이 열리면 일반 탭이 골라져 있고 초점이 그 탭에 있어야 한다.

    창을 연다 -> currentTab() == "general", 일반 탭에 초점
    """
    dialog = opened()
    assert dialog.currentTab() == "general"
    assert dialog.tabs["general"].hasFocus()


def test_ctrl_tab_moves_to_the_next_and_ctrl_shift_tab_to_the_previous_tab(
    opened: Callable[[], SettingDialog],
) -> None:
    """다음 탭 키는 다음 탭으로, 앞 탭 키는 앞 탭으로 넘겨야 하고 끝에서는 돌아야 한다.

    키는 Qt 표준 키 NextChild · PreviousChild(Windows Ctrl+Tab · Ctrl+Shift+Tab — OS마다 다르다)
    일반에서 다음 탭 키 -> 다운로드, 그 뒤 앞 탭 키 두 번 -> 고급
    """
    dialog = opened()
    QTest.keySequence(dialog, QKeySequence(QKeySequence.StandardKey.NextChild))
    assert dialog.currentTab() == "download"
    for _ in range(2):
        QTest.keySequence(dialog, QKeySequence(QKeySequence.StandardKey.PreviousChild))
    assert dialog.currentTab() == "advanced"


#: 물리 Control 키의 Qt 수정자 — macOS에서 Qt Ctrl은 Command 키이고 Control 키는 Meta다
_PHYSICAL_CTRL = (
    Qt.KeyboardModifier.MetaModifier
    if sys.platform == "darwin"
    else Qt.KeyboardModifier.ControlModifier
)


def test_ctrl_shift_tab_as_the_tab_key_moves_to_the_previous_tab(
    opened: Callable[[], SettingDialog],
) -> None:
    """물리 키 Tab에 Control · Shift를 얹은 키를 받으면 앞 탭으로 넘겨야 한다(#644).

    키 = Key_Tab + 물리 Control + Shift(OS 키 매퍼가 실제 Ctrl+Shift+Tab에서 만드는 단축키 후보)
    다운로드 탭에서 -> 일반
    """
    dialog = opened()
    dialog.showTab(TAB_ORDER.index("download"))
    QTest.keyClick(dialog, Qt.Key.Key_Tab, _PHYSICAL_CTRL | Qt.KeyboardModifier.ShiftModifier)
    assert dialog.currentTab() == "general"


_NEXT_TAB = _PHYSICAL_CTRL  # 다음 탭 키 Control+Tab의 수정자(Key_Tab과 함께)
_PREVIOUS_TAB = _PHYSICAL_CTRL | Qt.KeyboardModifier.ShiftModifier  # 앞 탭 키 Control+Shift+Tab


def _key(modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> None:
    """지금 포커스 위젯에 Tab 키(수정자 포함)를 한 번 보낸다 — 단축키 · Tab 이동 모두 실제 키 처리를 탄다."""
    focused = QApplication.focusWidget()
    assert focused is not None, "전제: 포커스 위젯이 있다"
    QTest.keyClick(focused, Qt.Key.Key_Tab, modifiers)
    QApplication.processEvents()


def _focus_name() -> str:
    """지금 포커스 위젯의 objectName(없으면 빈 문자열)."""
    focused = QApplication.focusWidget()
    return "" if focused is None else focused.objectName()


def _open_settled(opened: Callable[[], SettingDialog], qtbot: QtBot) -> SettingDialog:
    """설정 창을 열고 창 열림 구간이 끝날 때까지 기다린다 — 그 뒤 받은 포커스는 받은 까닭을 따른다.

    창 열림 구간(`KeyFocusMarker` — 보임부터 그 이벤트 루프 턴 끝까지)에 받은 포커스는 창을 연 입력의
    종류를 따른다. 창이 이미 노출 · 활성이면 `opened`가 이벤트 루프를 돌리지 않고 돌아와(macOS 실제 화면)
    첫 키가 그 구간 안에서 처리된다. 사람은 창이 보이는 그 턴 안에 키를 누를 수 없다.
    """
    dialog = opened()
    qtbot.waitUntil(lambda: not dialog._keyFocus._opening, timeout=ACTIVE_WAIT_MS)
    return dialog


#: 탭 버튼 포커스에서 누른 키 → (앞서 누를 키들, 누를 키, 넘어간 탭). 창은 일반 탭 버튼 포커스로 열린다
TAB_BUTTON_SWITCHES = {
    "next": ([], _NEXT_TAB, "download"),
    "previous-wraps": ([], _PREVIOUS_TAB, "advanced"),
    "next-wraps": ([_PREVIOUS_TAB], _NEXT_TAB, "general"),
    "previous": ([_NEXT_TAB, _NEXT_TAB], _PREVIOUS_TAB, "download"),
}


@pytest.mark.parametrize("case", sorted(TAB_BUTTON_SWITCHES))
def test_tab_switch_key_on_a_tab_button_moves_keyboard_focus_to_the_new_tab_button(
    opened: Callable[[], SettingDialog], qtbot: QtBot, case: str
) -> None:
    """탭 버튼에 포커스가 있을 때 탭 넘기기 키를 누르면 포커스가 새 탭 버튼으로 가고 focus ring이 그려져야 한다(#659).

    `TAB_BUTTON_SWITCHES[case]`의 키를 차례로 Key_Tab 키 이벤트로 보낸다
    -> currentTab() == 넘어간 탭, 포커스 = 그 탭 버튼, keyFocus True
    """
    before, modifiers, expected = TAB_BUTTON_SWITCHES[case]
    dialog = _open_settled(opened, qtbot)
    for earlier in before:
        _key(earlier)
    assert QApplication.focusWidget() in dialog.tabs.values(), "전제: 탭 버튼 포커스"
    _key(modifiers)
    assert dialog.currentTab() == expected
    assert _focus_name() == f"settingsTab_{expected}"
    assert dialog.tabs[expected].isVisible()
    assert dialog.tabs[expected].property("keyFocus") is True


#: 탭 안 칸 포커스에서 다음 탭 키 → (칸까지 가는 키들, 칸, 넘어간 탭, 포커스가 갈 위젯).
#: 창은 일반 탭 버튼 포커스로 열리고 Tab은 고른 탭 버튼에서 그 탭 쪽 첫 칸으로 간다
FIELD_SWITCHES = {
    "to-first-field": (
        [_NEXT_TAB] * 3 + [Qt.KeyboardModifier.NoModifier],
        "nidaut",
        "advanced",
        "logsFolder",
    ),
    "wraps-to-first-field": (
        [_PREVIOUS_TAB, Qt.KeyboardModifier.NoModifier],
        "logsFolder",
        "general",
        "language",
    ),
    # 다운로드 탭은 #646에서 칸이 생겼다 — 첫 Tab 순서 위젯은 기본 저장 위치 「찾기…」다. Tab이 멈출 위젯이
    # 없는 탭의 갈래는 test_tab_switch_key_skips_controls_tab_does_not_stop_at가 잰다
    "to-download-first-field": (
        [Qt.KeyboardModifier.NoModifier],
        "language",
        "download",
        "downloadPathBrowse",
    ),
}


@pytest.mark.parametrize("case", sorted(FIELD_SWITCHES))
def test_tab_switch_key_in_a_field_moves_focus_to_the_new_tab(
    opened: Callable[[], SettingDialog], qtbot: QtBot, case: str
) -> None:
    """탭 안 칸에 포커스가 있을 때 다음 탭 키를 누르면 새 탭의 첫 Tab 위젯(칸이 없으면 탭 버튼)으로 가야 한다(#659).

    `FIELD_SWITCHES[case]`의 키로 칸까지 간 뒤 Control+Tab 키 이벤트
    -> currentTab() == 넘어간 탭, 포커스 = 정한 위젯(보임), 버튼이면 keyFocus True
    """
    keys, field, expected_tab, expected_focus = FIELD_SWITCHES[case]
    dialog = _open_settled(opened, qtbot)
    for earlier in keys:
        _key(earlier)
    assert _focus_name() == field, "전제: 칸 포커스"
    _key(_NEXT_TAB)
    assert dialog.currentTab() == expected_tab
    assert _focus_name() == expected_focus
    focused = QApplication.focusWidget()
    assert focused.isVisible()
    if isinstance(focused, QPushButton):
        assert focused.property("keyFocus") is True


def test_tab_switch_key_skips_controls_tab_does_not_stop_at(
    opened: Callable[[], SettingDialog], qtbot: QtBot
) -> None:
    """Tab이 글 칸에만 가는 규칙에서 Tab이 멈추지 않는 위젯만 있는 탭으로 넘기면 포커스가 탭 버튼으로 가야 한다(#659).

    tabFocusBehavior = TabFocusTextControls(macOS 「키보드 탐색」 꺼짐), logsFolder 포커스 정책 = TabFocus
    계정 탭 nidaut 포커스에서 Control+Tab -> 고급 탭, 포커스 = 고급 탭 버튼
    """
    dialog = _open_settled(opened, qtbot)
    dialog.logsFolder.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # Tab만 받는 위젯 — macOS 단추 정책
    for earlier in [_NEXT_TAB] * 3 + [Qt.KeyboardModifier.NoModifier]:
        _key(earlier)
    assert _focus_name() == "nidaut", "전제: 칸 포커스"
    hints = QGuiApplication.styleHints()
    before = hints.tabFocusBehavior()
    if not isinstance(before, Qt.TabFocusBehavior):
        before = Qt.TabFocusBehavior(int(before))  # macOS 기본값 3은 열거 이름이 없다
    hints.setTabFocusBehavior(Qt.TabFocusBehavior.TabFocusTextControls)
    try:
        _key(_NEXT_TAB)
    finally:
        hints.setTabFocusBehavior(before)
    assert dialog.currentTab() == "advanced"
    assert _focus_name() == "settingsTab_advanced"


def test_clicking_a_tab_gives_it_mouse_focus(opened: Callable[[], SettingDialog]) -> None:
    """탭을 마우스로 누르면 포커스가 그 탭 버튼에 가고 focus ring은 그려지지 않아야 한다.

    일반 탭 버튼 포커스 -> 계정 탭 왼쪽 단추 누름 -> currentTab() == "account", 포커스 = 계정 탭 버튼,
    keyFocus False
    """
    dialog = opened()
    QTest.mouseClick(dialog.tabs["account"], Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert dialog.currentTab() == "account"
    assert _focus_name() == "settingsTab_account"
    assert dialog.tabs["account"].property("keyFocus") is False


#: 실제 키 입력 경우 — (OS, 수정자, 키, 다운로드 탭에서 누른 뒤 탭). 물리 키 이름(`os_key_input`)
REAL_TAB_KEYS = [
    ("win32", [os_key_input.CTRL], "tab", "display"),
    ("win32", [os_key_input.CTRL, os_key_input.SHIFT], "tab", "general"),
    ("darwin", [os_key_input.CTRL], "tab", "display"),
    ("darwin", [os_key_input.CTRL, os_key_input.SHIFT], "tab", "general"),
    ("darwin", [os_key_input.CMD, os_key_input.SHIFT], "]", "display"),
    ("darwin", [os_key_input.CMD, os_key_input.SHIFT], "[", "general"),
]


@pytest.mark.skipif(
    os.environ.get(NATIVE_SWITCH) != "1" or not os_key_input.SUPPORTED,
    reason="OS 키 입력은 Windows · macOS 실제 화면에서만 낸다",
)
@pytest.mark.parametrize(
    ("platform", "modifiers", "key", "expected"),
    [case for case in REAL_TAB_KEYS if case[0] == sys.platform],
    ids=lambda value: "+".join(value) if isinstance(value, list) else str(value),
)
def test_real_tab_switch_keys_move_between_tabs(
    opened: Callable[[], SettingDialog],
    qtbot: QtBot,
    platform: str,
    modifiers: list[str],
    key: str,
    expected: str,
) -> None:
    """OS 입력 경로로 누른 탭 넘기기 키는 `REAL_TAB_KEYS`의 탭으로 넘겨야 한다(#644).

    다운로드 탭 · 초점은 다운로드 탭 버튼 → OS 키 경로(Windows 창 키 메시지 · macOS CGEventPostToPid)
    -> currentTab() == expected
    """
    dialog = opened()
    dialog.showTab(TAB_ORDER.index("download"))
    dialog.tabs["download"].setFocus()
    os_key_input.press(
        int(dialog.winId()),
        modifiers,
        key,
        lambda: qtbot.waitUntil(lambda: dialog.currentTab() == expected, timeout=ACTIVE_WAIT_MS),
    )


#: 실제 키 입력으로 포커스가 따라가는 경우 — id → (OS, 앞서 누를 (수정자, 키)들, 수정자, 키, 포커스가 갈 위젯).
#: 창은 일반 탭 버튼 포커스로 열린다. 수정자 없는 Tab은 일반 탭 버튼에서 일반 탭 쪽 첫 칸으로 간다
REAL_FOCUS_KEYS: dict[str, tuple[str, list[tuple[list[str], str]], list[str], str, str]] = {
    f"{platform}-{name}": (platform, before, modifiers, key, focus)
    for platform in ("win32", "darwin")
    for name, before, modifiers, key, focus in (
        ("button-next", [], [os_key_input.CTRL], "tab", "settingsTab_download"),
        (
            "button-previous",
            [],
            [os_key_input.CTRL, os_key_input.SHIFT],
            "tab",
            "settingsTab_advanced",
        ),
        ("field-to-download", [([], "tab")], [os_key_input.CTRL], "tab", "downloadPathBrowse"),
    )
} | {
    "darwin-button-cmd-next": (
        "darwin",
        [],
        [os_key_input.CMD, os_key_input.SHIFT],
        "]",
        "settingsTab_download",
    )
}


@pytest.mark.skipif(
    os.environ.get(NATIVE_SWITCH) != "1" or not os_key_input.SUPPORTED,
    reason="OS 키 입력은 Windows · macOS 실제 화면에서만 낸다",
)
@pytest.mark.parametrize(
    "case", sorted(case for case, value in REAL_FOCUS_KEYS.items() if value[0] == sys.platform)
)
def test_real_tab_switch_keys_move_keyboard_focus_to_the_new_tab(
    opened: Callable[[], SettingDialog], qtbot: QtBot, case: str
) -> None:
    """OS 입력 경로로 누른 탭 넘기기 키 뒤 포커스는 `REAL_FOCUS_KEYS`의 위젯이고 focus ring이 그려져야 한다(#659).

    일반 탭 버튼 포커스 → `before` 키(OS 키 경로) → 탭 넘기기 키(OS 키 경로)
    -> 포커스 위젯 objectName == focus, keyFocus True
    """
    _, before, modifiers, key, focus = REAL_FOCUS_KEYS[case]
    dialog = _open_settled(opened, qtbot)
    hwnd = int(dialog.winId())
    for earlier_modifiers, earlier_key in before:
        os_key_input.press(
            hwnd,
            earlier_modifiers,
            earlier_key,
            lambda: qtbot.waitUntil(lambda: _focus_name() == "language", timeout=ACTIVE_WAIT_MS),
        )

    def arrived() -> bool:
        focused = QApplication.focusWidget()
        return focused is not None and focused.objectName() == focus

    os_key_input.press(
        hwnd, modifiers, key, lambda: qtbot.waitUntil(arrived, timeout=ACTIVE_WAIT_MS)
    )
    assert QApplication.focusWidget().property("keyFocus") is True


@pytest.mark.parametrize("name", sorted(ITEM_TABS))
def test_each_moved_item_sits_on_its_tab(opened: Callable[[], SettingDialog], name: str) -> None:
    """옮긴 항목은 정한 탭 쪽 안에 있어야 하고, 그 탭을 고르면 보여야 한다.

    항목 objectName -> 조상에 `<탭>Page`, 그 탭을 누르면 isVisible()
    """
    dialog = opened()
    found = [w for w in dialog.findChildren(QWidget, name) if w.objectName() == name]
    assert len(found) == 1, f"{name}: {len(found)}개"
    widget = found[0]
    page = dialog.findChild(QWidget, f"{ITEM_TABS[name]}Page")
    assert page is not None and page.isAncestorOf(widget), f"{name}: {ITEM_TABS[name]} 탭에 없다"
    QTest.mouseClick(dialog.tabs[ITEM_TABS[name]], Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert widget.isVisible()


def test_every_widget_shows_tips_in_the_bubble(opened: Callable[[], SettingDialog]) -> None:
    """설정 창의 위젯 모두에 말풍선 필터가 걸려 있어야 한다(#629 — 편집 창과 같다).

    창 · 자식 위젯 전부 -> property("tipInstalled") 참
    """
    dialog = opened()
    missing = [
        w.objectName() or type(w).__name__
        for w in (dialog, *dialog.findChildren(QWidget))
        if not w.property("tipInstalled")
    ]
    assert missing == []


# ================================================================ 저장 · 취소 켜짐


def test_save_is_off_and_cancel_on_when_nothing_changed(
    opened: Callable[[], SettingDialog],
) -> None:
    """칸을 바꾸지 않으면 저장은 꺼지고 취소는 켜져 있어야 한다(오너 2026-11-27).

    `SAVED`로 연다 -> 저장 isEnabled() 거짓, 취소 isEnabled() 참
    """
    dialog = opened()
    assert not _save(dialog).isEnabled()
    assert _cancel(dialog).isEnabled()


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_changing_a_field_turns_save_on_and_keeps_cancel_on(
    opened: Callable[[], SettingDialog], field: str
) -> None:
    """칸 하나를 바꾸면 저장이 켜지고 취소는 켜진 채여야 한다.

    칸 값을 `FIELDS`의 값으로 -> 저장 · 취소 isEnabled() 참
    """
    dialog = opened()
    FIELDS[field][0](dialog)
    assert _save(dialog).isEnabled()
    assert _cancel(dialog).isEnabled()


def test_typing_back_the_saved_value_turns_save_off_and_keeps_cancel_on(
    opened: Callable[[], SettingDialog],
) -> None:
    """바꾼 칸을 연 때의 값으로 되돌리면 저장은 다시 꺼지고 취소는 켜진 채여야 한다.

    NID_AUT "aut-typed" → "aut-saved", 다운로드 후 동작 shutdown → sleep -> 저장 꺼짐, 취소 켜짐
    """
    dialog = opened()
    _change_nidaut(dialog)
    _change_after_download(dialog)
    dialog.nidaut.setText("aut-saved")
    dialog.afterDownload.setCurrentIndex(dialog.afterDownload.findData("sleep"))
    assert not _save(dialog).isEnabled()
    assert _cancel(dialog).isEnabled()


# ================================================================ 저장


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_save_writes_only_the_changed_key(opened: Callable[[], SettingDialog], field: str) -> None:
    """칸 하나를 바꾸고 저장을 누르면 그 키의 행만 바뀌고 창이 닫혀야 한다.

    칸 값을 바꾼다 → 저장 누름 -> 문자열이 달라진 행 = 그 키 하나, 값 = `FIELDS`의 값, 창 숨음
    """
    dialog = opened()
    before = read_rows()
    change, key, value = FIELDS[field]
    change(dialog)
    QTest.mouseClick(_save(dialog), Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    after = read_rows()
    assert {k for k in after if after[k] != before.get(k)} == {key}
    assert read_settings()[key] == value
    assert not dialog.isVisible()
    assert dialog.result() == QDialog.DialogCode.Accepted


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_saved_value_is_shown_when_the_window_opens_again(
    opened: Callable[[], SettingDialog], field: str
) -> None:
    """저장한 뒤 설정 창을 다시 열면 칸이 저장한 값을 보여야 한다.

    칸 값을 바꾼다 → 저장 → 다시 연다 -> 칸 값 = `FIELDS`의 값
    """
    dialog = opened()
    change, key, value = FIELDS[field]
    change(dialog)
    _save(dialog).click()
    QApplication.processEvents()
    assert _shown_value(opened(), key) == value


def test_ctrl_s_saves_the_changed_key_and_closes(opened: Callable[[], SettingDialog]) -> None:
    """칸을 바꾸고 Ctrl+S를 누르면 그 키가 저장되고 창이 닫혀야 한다.

    다운로드 후 동작 shutdown → Ctrl+S(StandardKey.Save) -> 저장 값 "shutdown", 창 숨음
    """
    dialog = opened()
    _change_after_download(dialog)
    QTest.keySequence(dialog, _save_keys())
    QApplication.processEvents()
    assert read_settings()["afterDownload"] == "shutdown"
    assert not dialog.isVisible()


def test_ctrl_s_without_changes_writes_nothing_and_stays_open(
    opened: Callable[[], SettingDialog],
) -> None:
    """칸을 바꾸지 않고 Ctrl+S를 누르면 아무 행도 바뀌지 않고 창이 열려 있어야 한다.

    Ctrl+S -> 행 문자열 전부 그대로, 창 보임
    """
    dialog = opened()
    before = read_rows()
    QTest.keySequence(dialog, _save_keys())
    QApplication.processEvents()
    assert read_rows() == before
    assert dialog.isVisible()


# ================================================================ 취소 · Esc


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_cancel_writes_nothing_and_the_window_opens_again_on_the_saved_value(
    opened: Callable[[], SettingDialog], field: str
) -> None:
    """칸을 바꾸고 취소를 누르면 아무 행도 바뀌지 않고, 다시 연 창은 저장돼 있던 값을 보여야 한다.

    칸 값을 바꾼다 → 취소 누름 -> 행 문자열 전부 그대로, 창 숨음, 다시 연 창의 칸 = `SAVED`의 값
    """
    dialog = opened()
    before = read_rows()
    change, key, _value = FIELDS[field]
    change(dialog)
    QTest.mouseClick(_cancel(dialog), Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert read_rows() == before
    assert not dialog.isVisible()
    assert _shown_value(opened(), key) == SAVED[key]


def test_cancel_without_changes_writes_nothing_and_closes(
    opened: Callable[[], SettingDialog],
) -> None:
    """칸을 바꾸지 않고 취소를 누르면 아무 행도 바뀌지 않고 확인 없이 창이 닫혀야 한다(오너 2026-11-27).

    취소 누름 -> 행 문자열 전부 그대로, 창 숨음, closePrompt 없음, result() == Rejected
    """
    dialog = opened()
    before = read_rows()
    QTest.mouseClick(_cancel(dialog), Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert read_rows() == before
    assert not dialog.isVisible()
    assert dialog.closePrompt is None
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_escape_writes_nothing_and_closes(opened: Callable[[], SettingDialog]) -> None:
    """칸을 바꾸고 Esc를 누르면 아무 행도 바뀌지 않고 확인 없이 창이 닫혀야 한다.

    네 칸을 모두 바꾼다 → Esc -> 행 문자열 전부 그대로, 창 숨음, result() == Rejected
    """
    dialog = opened()
    before = read_rows()
    for change, _key, _value in FIELDS.values():
        change(dialog)
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    QApplication.processEvents()
    assert read_rows() == before
    assert not dialog.isVisible()
    assert dialog.result() == QDialog.DialogCode.Rejected


# ================================================================ 닫기 확인


def test_closing_without_changes_closes_without_asking(
    opened: Callable[[], SettingDialog], qtbot: QtBot
) -> None:
    """칸을 바꾸지 않고 창을 닫으면 확인 대화상자 없이 닫혀야 한다.

    close() -> 확인 대화상자 뜨지 않음, 창 숨음, 행 그대로
    """
    dialog = opened()
    before = read_rows()
    seen = _close_answering(qtbot, dialog, _click("closeBack"))
    assert seen["shown"] is False
    assert not dialog.isVisible()
    assert read_rows() == before


def test_close_prompt_defaults_to_go_back(
    opened: Callable[[], SettingDialog], qtbot: QtBot
) -> None:
    """바뀐 채로 닫을 때 뜨는 확인 대화상자의 기본 버튼과 Esc 버튼은 돌아가기여야 한다.

    NID_AUT를 바꾼다 → close() -> 기본 = closeBack, Esc = closeBack
    """
    dialog = opened()
    _change_nidaut(dialog)
    seen = _close_answering(qtbot, dialog, _click("closeBack"))
    assert seen["shown"] is True
    assert (seen["default"], seen["escape"]) == ("closeBack", "closeBack")


def test_close_prompt_save_writes_the_changed_key_and_closes(
    opened: Callable[[], SettingDialog], qtbot: QtBot
) -> None:
    """확인 대화상자에서 저장을 누르면 바뀐 키만 저장되고 창이 닫혀야 한다.

    언어 en_US → close() → 저장 -> 달라진 행 = language 하나, 값 "en_US", 창 숨음
    """
    dialog = opened()
    before = read_rows()
    _change_language(dialog)
    seen = _close_answering(qtbot, dialog, _click("closeSave"))
    assert seen["shown"] is True
    after = read_rows()
    assert {k for k in after if after[k] != before.get(k)} == {"language"}
    assert read_settings()["language"] == "en_US"
    assert not dialog.isVisible()


def test_close_prompt_dont_save_writes_nothing_and_closes(
    opened: Callable[[], SettingDialog], qtbot: QtBot
) -> None:
    """확인 대화상자에서 저장 안 함을 누르면 아무 행도 바뀌지 않고 창이 닫혀야 한다.

    언어 en_US → close() → 저장 안 함 -> 행 문자열 전부 그대로, 창 숨음
    """
    dialog = opened()
    before = read_rows()
    _change_language(dialog)
    seen = _close_answering(qtbot, dialog, _click("closeDiscard"))
    assert seen["shown"] is True
    assert read_rows() == before
    assert not dialog.isVisible()


def test_close_prompt_save_failure_keeps_the_window_open_with_the_changed_value(
    opened: Callable[[], SettingDialog], qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """확인 대화상자에서 저장을 눌렀는데 저장이 DB 오류로 실패하면 창이 바꾼 값 그대로 열려 있어야 한다.

    언어 en_US → `update_settings`가 sqlite3.OperationalError → close() → 저장
    -> close()가 OperationalError를 냄, 행 그대로, 창 보임, 언어 칸 en_US
    """
    dialog = opened()
    before = read_rows()
    _change_language(dialog)

    def locked(changes: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(settings, "update_settings", locked)
    answered: list[str] = []
    save = _click("closeSave")

    def answer(box: QDialog) -> None:
        answered.append("closeSave")
        save(box)

    with pytest.raises(sqlite3.OperationalError):
        _close_answering(qtbot, dialog, answer)
    assert answered == ["closeSave"]
    assert read_rows() == before
    assert dialog.isVisible()
    assert dialog.language.currentData() == "en_US"


@pytest.mark.parametrize("how", ["button", "escape"])
def test_close_prompt_go_back_keeps_the_window_open_with_the_changed_value(
    opened: Callable[[], SettingDialog], qtbot: QtBot, how: str
) -> None:
    """확인 대화상자에서 돌아가기(버튼 · Esc)를 고르면 아무 행도 바뀌지 않고 창이 바꾼 값 그대로 열려 있어야 한다.

    언어 en_US → close() → 돌아가기 누름 / Esc -> 행 그대로, 창 보임, 언어 칸 en_US, 저장 켜짐
    """
    dialog = opened()
    before = read_rows()
    _change_language(dialog)
    if how == "button":
        answer = _click("closeBack")
    else:

        def answer(box: QDialog) -> None:
            QTest.keyClick(box, Qt.Key.Key_Escape)

    seen = _close_answering(qtbot, dialog, answer)
    assert seen["shown"] is True
    assert read_rows() == before
    assert dialog.isVisible()
    assert dialog.language.currentData() == "en_US"
    assert _save(dialog).isEnabled()


# ================================================================ 탭 줄 폭


@pytest.fixture
def language(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[str]:
    """`CVDv2.set_language`로 그 언어의 번역기를 건다 — 끝나면 건 번역기를 전부 뗀다."""
    code = request.param
    monkeypatch.setattr(main_module, "app", qapp, raising=False)
    cfg = settings.default_settings() | {"language": code}
    installed = main_module.set_language(cfg, QTranslator())
    yield code
    for translator in installed:
        qapp.removeTranslator(translator)
    main_module._qt_translators.clear()
    QApplication.processEvents()


@pytest.mark.parametrize("language", sorted(TAB_NAMES), indirect=True)
def test_tab_names_follow_the_language(opened: Callable[[], SettingDialog], language: str) -> None:
    """탭 이름은 앱 언어의 이름이어야 한다.

    ko_KR -> 일반 · 다운로드 · 화면 · 계정 · 고급, en_US -> General · Download · Display · Account · Advanced
    """
    dialog = opened()
    assert [dialog.tabs[key].text() for key in TAB_ORDER] == TAB_NAMES[language]


@pytest.mark.parametrize("language", sorted(TAB_NAMES), indirect=True)
def test_tab_row_fits_inside_the_minimum_width(
    opened: Callable[[], SettingDialog], language: str
) -> None:
    """창을 최소폭으로 줄여도 탭 다섯이 제 sizeHint 폭 그대로 창 안쪽 오른쪽 끝 안에 들어야 한다.

    최소폭으로 resize -> 실제 폭 = 최소폭, 탭마다 폭 = sizeHint 폭 · 겹치지 않음,
    마지막 탭 오른쪽 끝 ≤ 창 폭 − 바깥 오른쪽 여백
    """
    dialog = opened()
    minimum = dialog.minimumSizeHint().width()
    dialog.resize(minimum, dialog.height())
    QApplication.processEvents()
    assert dialog.width() == minimum, "전제: 최소폭으로 줄었다"
    tabs = [dialog.tabs[key] for key in TAB_ORDER]
    assert all(tab.isVisible() for tab in tabs), "전제: 탭이 보인다"
    lefts = [tab.mapTo(dialog, tab.rect().topLeft()).x() for tab in tabs]
    for tab, left, next_left in zip(tabs, lefts, [*lefts[1:], None], strict=True):
        assert tab.width() == tab.sizeHint().width(), f"{tab.text()}: 눌렸다"
        if next_left is not None:
            assert left + tab.width() <= next_left, f"{tab.text()}: 다음 탭과 겹친다"
    inner_right = dialog.width() - dialog.layout().contentsMargins().right()
    assert lefts[-1] + tabs[-1].width() <= inner_right

"""앱 창에 Qt 콤보박스가 남지 않았다 — 선택 칸은 모두 `ChoiceCombo`다 (#620).

본 창 · 설정 창 · 카드 편집 창 · 구간 창을 실제 배선으로 띄운 뒤 살아 있는 위젯 전부(`allWidgets`)에서
`QComboBox`인데 `ChoiceCombo`가 아닌 것을 센다. 새 선택 칸이 Qt 콤보박스로 들어오면 이 게이트가 걸린다.
"""

import pytest
from PySide6.QtWidgets import QApplication, QComboBox, QWidget
from pytestqt.qtbot import QtBot

from app.views.dialog import SettingDialog
from app.widgets.choice_combo import ChoiceCombo
from tests.app.gui.test_section_editor import (  # noqa: F401 — 픽스처는 이름으로 쓴다
    _apply_production_qss,
    _destroy_windows,
    _make_item,
    _no_network,
    _pump,
    basis,
    open_editor,
    open_window,
)


def _plain_qt_combos(window: QWidget) -> list[str]:
    """그 창에 든 위젯 가운데 `ChoiceCombo`가 아닌 `QComboBox`의 objectName(없으면 클래스 이름)."""
    return [
        w.objectName() or type(w).__name__
        for w in QApplication.allWidgets()
        if isinstance(w, QComboBox)
        and not isinstance(w, ChoiceCombo)
        and (w is window or window.isAncestorOf(w))
    ]


def _choice_combos(window: QWidget) -> list[str]:
    """그 창에 든 `ChoiceCombo`의 objectName."""
    return [
        w.objectName()
        for w in QApplication.allWidgets()
        if isinstance(w, ChoiceCombo) and window.isAncestorOf(w)
    ]


def test_the_counter_finds_a_plain_qt_combo(qtbot: QtBot) -> None:
    """Qt 콤보박스 하나와 ChoiceCombo 하나를 둔 창에서 세면 Qt 콤보박스만 나와야 한다.

    창 하나에 QComboBox("plain") · ChoiceCombo("choice") -> ["plain"]
    """
    window = QWidget()
    qtbot.addWidget(window)
    QComboBox(window).setObjectName("plain")
    ChoiceCombo(window).setObjectName("choice")
    assert _plain_qt_combos(window) == ["plain"]
    assert _choice_combos(window) == ["choice"]


def test_main_window_has_no_plain_qt_combo(qtbot: QtBot, tmp_path) -> None:
    """대기 카드가 든 본 창에 Qt 콤보박스가 없어야 한다.

    대기 카드 1개 -> 본 창 보임, Qt 콤보박스 []
    """
    win = open_window(tmp_path, _make_item(str(tmp_path)))
    assert win.isVisible(), "전제: 본 창이 보인다"
    assert _plain_qt_combos(win) == []


def test_setting_dialog_has_no_plain_qt_combo(qtbot: QtBot, tmp_path) -> None:
    """본 창이 띄운 설정 창에 Qt 콤보박스가 없고 선택 칸 둘이 ChoiceCombo여야 한다.

    설정 창(부모 = 본 창) -> Qt 콤보박스 [], ChoiceCombo ["afterDownload", "language"]
    """
    win = open_window(tmp_path)
    dialog = SettingDialog(parent=win)  # 본 창의 onSetting과 같은 부모 — exec_ 대신 show
    dialog.show()
    qtbot.waitExposed(dialog)
    assert _plain_qt_combos(dialog) == []
    assert sorted(_choice_combos(dialog)) == ["afterDownload", "language"]


def test_card_edit_dialog_has_no_plain_qt_combo(qtbot: QtBot, tmp_path) -> None:
    """대기 카드의 편집 창에 Qt 콤보박스가 없고 해상도 칸이 ChoiceCombo여야 한다.

    대기 카드 편집 창 -> Qt 콤보박스 [], ChoiceCombo ["cardEditResolution"]
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    win.onCardEdit(item)
    _pump()
    dialog = win._editDialog
    assert dialog is not None and dialog.isVisible(), "전제: 편집 창이 열렸다"
    assert _plain_qt_combos(dialog) == []
    assert _choice_combos(dialog) == ["cardEditResolution"]


@pytest.mark.usefixtures("basis")
def test_section_dialog_has_no_plain_qt_combo(qtbot: QtBot, tmp_path) -> None:
    """대기 카드의 구간 창에 Qt 콤보박스가 없어야 한다.

    구간 창(조회 끝) -> 창 보임, Qt 콤보박스 []
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    assert dialog.isVisible(), "전제: 구간 창이 보인다"
    assert _plain_qt_combos(dialog) == []

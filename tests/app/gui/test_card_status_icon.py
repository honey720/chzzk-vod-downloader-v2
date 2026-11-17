"""카드 3줄 상태는 아이콘만 — 상태 단어를 화면 글에서 빼고 상태 원 아이콘이 말한다 (inbox 177)."""

import itertools

import pytest
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QApplication

import app.theme as theme
from app.widgets import icons
from core.models.download_state import DownloadState
from tests.app.gui.test_card_state_matrix import _make_widget

# 상태 → (상태 원 버블 상태, 접근성 이름, 화면 글에서 빠지는 상태 단어)
STATES = {
    DownloadState.RUNNING: ("running", "Downloading", "Downloading"),
    DownloadState.PAUSED: ("paused", "Paused", "Paused"),
    DownloadState.FINISHED: ("done", "Completed", "Completed"),
    DownloadState.FAILED: ("all_failed", "Failed", "Failed"),
}


@pytest.mark.parametrize("state", list(STATES))
def test_status_icon_is_named_after_the_state_without_a_tip(
    qapp: QApplication, state: DownloadState
) -> None:
    """카드 상태 원 아이콘은 보이고, 접근성 이름이 상태 이름이고, 툴팁이 없어야 한다.

    받는 중 · 일시정지 · 완료 · 실패 카드 -> statusIcon 보임 · 그림 상태 running · paused · done · all_failed,
    accessibleName "Downloading" · "Paused" · "Completed" · "Failed", toolTip ""
    """
    widget = _make_widget(qapp, state, 42)
    bubble, name, _word = STATES[state]

    assert widget.statusIcon.isVisible()
    assert widget.statusIcon.state() == bubble
    assert widget.statusIcon.accessibleName() == name
    assert widget.statusIcon.toolTip() == ""


def test_status_icon_name_reaches_the_accessibility_tree_when_the_status_text_is_empty(
    qapp: QApplication,
) -> None:
    """상태 글이 빈 카드(소요 시간 없는 완료)에서도 상태 아이콘 이름이 Qt 접근성 인터페이스에 나와야 한다.

    완료 · 소요 시간 "" -> 상태 글 "", QAccessible 인터페이스 있음 · 숨지 않음 · Name == "Completed"
    """
    widget = _make_widget(qapp, DownloadState.FINISHED)
    widget.item.download_time = ""
    widget.setData(widget.item, 0)
    QApplication.processEvents()
    face = QAccessible.queryAccessibleInterface(widget.statusIcon)

    assert widget.statusLabel.text() == "", "전제: 상태 글이 비었다"
    assert face is not None and face.isValid()
    assert not face.state().invisible
    assert face.text(QAccessible.Text.Name) == "Completed"


@pytest.mark.parametrize("state", [s for s in STATES if s != DownloadState.FAILED])
def test_status_text_does_not_repeat_the_state_word(
    qapp: QApplication, state: DownloadState
) -> None:
    """받는 중 · 일시정지 · 완료 카드의 3줄 상태 글에는 상태 단어가 없어야 한다 — 아이콘이 말한다.

    받는 중 42% -> 글에 "Downloading" 없음 · "42" 있음, 일시정지 -> "Paused" 없음, 완료 -> "Completed" 없음
    """
    widget = _make_widget(qapp, state, 42)
    _bubble, _name, word = STATES[state]

    assert widget.statusLabel.isVisible()
    assert word not in widget.statusLabel.text()
    if state != DownloadState.FINISHED:
        assert "42" in widget.statusLabel.text(), "진행률은 남는다"


def test_failed_card_keeps_the_reason_text_and_its_tip(qapp: QApplication) -> None:
    """실패 카드는 상태 단어 없이도 실패 사유 글과 사유 툴팁을 그대로 가져야 한다 — 사유를 보는 길은 그대로다.

    실패 사유 "Failed to save file" -> 상태 글에 사유가 있다, 상태 글 툴팁에 사유가 있다
    """
    widget = _make_widget(qapp, DownloadState.FAILED)

    assert "Failed to save file" in widget.statusLabel.text()
    assert "Failed to save file" in widget.statusLabel.toolTip()


def test_status_icons_differ_in_shape_not_only_in_colour(qapp: QApplication) -> None:
    """상태 원 아이콘 넷은 같은 원 색 · 같은 기호 색으로 그려도 서로 다른 화소여야 한다 — 색만 다른 아이콘이 아니다.

    running · paused · done · all_failed, 원 색 stateRunning 하나 · 기호 onAccent, 16px -> 모든 쌍의 화소가 다르다
    """
    tokens = theme.DARK
    images = {
        state: icons.bubble_pixmap(state, tokens["stateRunning"], tokens["onAccent"], 16).toImage()
        for state in ("running", "paused", "done", "all_failed")
    }

    for (a, first), (b, second) in itertools.combinations(images.items(), 2):
        assert first != second, f"{a}와 {b}의 모양이 같다"

"""제목 호버 강조는 클릭(파일명 편집)이 되는 상태에서만 뜬다 (#244 3행 정리 [2]).

제목 편집은 대기에서만 성립한다(진행 중이면 이미 그 이름으로 쓰고 있고, 완료됐으면
이미 저장됐다). 그런데 호버 강조는 모든 상태에서 떠서 "클릭하면 뭔가 된다"고
거짓말을 했다(실기 확인). 게이트: 다섯 상태 각각에서 **호버 강조 유무 == 클릭 가능
여부**. 표시 문자열·기하는 `shown()`을 거쳐 읽는다(숨은 라벨의 낡은 값 금지).

호버는 실제 커서를 옮기지 않고 `QWindow`에 합성 이벤트(`QTest.mouseMove(window, pos)`)로
보낸다 — `QTest.mouseMove(QWidget)`는 실기에서 `QCursor::setPos`로 오너의 마우스를 뺏는다.
"""

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

import main as main_module
import theme
from app.viewmodels.data import ContentItem
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from tests.unit.card_helpers import drop_new_top_levels, hold_style, shown, snapshot_top_levels

STATES = (DownloadState.WAITING, DownloadState.RUNNING, DownloadState.PAUSED,
          DownloadState.FINISHED, DownloadState.FAILED)


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS(호버 규칙 포함)를 태운다 — scope=function 유지(test_widget_theme 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _drop_windows(qapp):
    """이 파일의 테스트가 띄운 창을 테스트 끝에 확실히 파괴한다(card_helpers.drop_new_top_levels)."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("app.widgets.widget._global_download_path", "C:/dl")


def _card_in_window(state: DownloadState) -> tuple[QWidget, ContentItemWidget]:
    """상태별 카드를 보이는 최상위 창 안에 놓는다 — 호버는 창(QWindow)이 받아야 라벨까지 간다."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 3600},
        [["1080", "u1"], ["720", "u2"]], None, "", "C:/dl", "video", None,
    )
    item.downloadState = DownloadState.WAITING
    item.total_size = "595.34 MB"
    widget = ContentItemWidget(item, 0)
    widget.addRepresentationButtons()
    item.downloadState = state
    item.download_progress = 42
    item.download_speed = "8.2 MB/s"
    item.download_remain_time = "00:12:34"
    item.download_size = 624_000_000
    item.stateMessage = "Failed to save file" if state == DownloadState.FAILED else ""
    widget.setData(item, 0)
    window = QWidget()
    column = QVBoxLayout(window)
    column.setContentsMargins(0, 0, 0, 0)
    column.addWidget(widget)
    column.addStretch(1)
    window.resize(900, widget.sizeHint().height() + 40)
    window.show()
    # 실기 QPA에서는 show() 직후 창이 아직 노출되지 않아 합성 마우스 이벤트가
    # 버려진다(Windows 실측 — offscreen은 즉시 노출) — 노출을 기다린다
    QTest.qWaitForWindowExposed(window)
    QApplication.processEvents()
    return window, widget


def _hover(qtbot, window: QWidget, target: QWidget, on: bool) -> None:
    """`target` 위(on) 또는 창의 빈 바닥(off)으로 합성 이동 — 실제 커서는 건드리지 않는다.

    이동을 두 번(한 점 옆 → 목표) 보낸다 — 직전 테스트가 남긴 좌표와 같으면 이동량
    0으로 Enter/Leave가 안 난다(tests/unit/test_dialog.py의 같은 함정). 실기 QPA는
    이벤트가 비동기로 오므로 `underMouse()`가 바뀔 때까지 조건 대기한다.
    """
    if on:
        pos = target.mapTo(window, target.rect().center())
    else:
        pos = QPoint(window.width() - 2, window.height() - 2)
    QTest.mouseMove(window.windowHandle(), pos + QPoint(1, 1))
    QTest.mouseMove(window.windowHandle(), pos)
    qtbot.waitUntil(lambda: target.underMouse() == on, timeout=2000)


def _title_pixels(widget: ContentItemWidget):
    shown(widget.titleLabel)  # 보이는 라벨의 렌더만 의미가 있다
    return widget.titleLabel.grab().toImage()


class TestTitleHoverMatchesClickability:
    @pytest.mark.parametrize("state", STATES, ids=[s.name for s in STATES])
    def test_hover_highlight_appears_iff_the_title_is_clickable(self, qapp, qtbot, state):
        window, widget = _card_in_window(state)
        qtbot.addWidget(window)  # 실패해도 창을 닫는다 — 남은 창이 다음 테스트의 호버를 가로챈다
        clickable = state == DownloadState.WAITING
        _hover(qtbot, window, widget.titleLabel, on=False)
        idle = _title_pixels(widget)
        _hover(qtbot, window, widget.titleLabel, on=True)
        assert widget.titleLabel.underMouse(), "전제: 합성 호버가 제목 라벨에 닿았다"
        hovered = _title_pixels(widget)
        highlighted = hovered != idle
        assert highlighted == clickable, (
            f"{state.name}: 호버 강조 {'있음' if highlighted else '없음'} vs 클릭 가능 {clickable} — "
            "UI가 클릭 가능 여부와 다른 말을 한다"
        )
        _hover(qtbot, window, widget.titleLabel, on=False)
        window.close()

    @pytest.mark.parametrize("state", STATES, ids=[s.name for s in STATES])
    def test_click_opens_the_editor_iff_waiting(self, qapp, qtbot, state):
        window, widget = _card_in_window(state)
        qtbot.addWidget(window)
        shown(widget.titleLabel)
        QTest.mouseClick(widget.titleLabel, Qt.MouseButton.LeftButton)
        QApplication.processEvents()
        assert widget.titleEdit.isVisible() == (state == DownloadState.WAITING), (
            f"{state.name}: 제목 클릭의 편집창 표시가 상태와 맞지 않는다"
        )
        window.close()

    @pytest.mark.parametrize("state", STATES, ids=[s.name for s in STATES])
    def test_cursor_and_editable_property_follow_the_state(self, qapp, qtbot, state):
        window, widget = _card_in_window(state)
        qtbot.addWidget(window)
        clickable = state == DownloadState.WAITING
        assert widget.titleLabel.property("editable") == clickable
        expected = Qt.CursorShape.IBeamCursor if clickable else Qt.CursorShape.ArrowCursor
        assert widget.titleLabel.cursor().shape() == expected, f"{state.name}: 커서가 클릭 가능 여부와 다르다"
        window.close()

    def test_state_change_after_creation_updates_the_hint(self, qapp, qtbot):
        """대기로 만들어진 카드가 진행으로 바뀌면(실제 경로: setData) 힌트도 따라 꺼진다."""
        window, widget = _card_in_window(DownloadState.WAITING)
        qtbot.addWidget(window)
        assert widget.titleLabel.property("editable") is True
        widget.item.downloadState = DownloadState.RUNNING
        widget.setData(widget.item, 0)
        QApplication.processEvents()
        assert widget.titleLabel.property("editable") is False
        _hover(qtbot, window, widget.titleLabel, on=False)
        idle = _title_pixels(widget)
        _hover(qtbot, window, widget.titleLabel, on=True)
        assert _title_pixels(widget) == idle, "진행으로 바뀐 뒤에도 호버 강조가 남아 있다"
        window.close()

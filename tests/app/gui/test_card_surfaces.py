"""카드가 가장 좁을 때 1줄의 배치가 겹치거나 카드 밖으로 나가지 않는지 잰다 (#244 P-5 → #550).

#244 P-5의 「상태 × 조작 표면」 감사(제목 호버 · 커서 · 그 자리 편집, 경로 클릭 · 저장 위치 버튼,
구간 편집 버튼, 편집 중 상태 전이, 빈 제목)는 #550에서 그 표면이 모두 카드에서 빠져 지웠다 — 제목 ·
해상도 · 저장 위치 · 구간 바꾸기는 카드 편집 창(`app/views/card_edit_dialog.py`)이 하고, 그 창의
테스트가 잰다. 여기에는 표면과 무관하게 남는 기하 계약만 둔다.

#550 배치에서 카드 버튼은 1줄 오른쪽 끝에 겹쳐 놓는 상자(`hoverBox`)이고 호버 · 키보드 초점 때만
보인다. 버튼 밑의 1줄 글은 오버레이(`hoverShade`)가 가리는 것이 설계이므로, 버튼과 채널명이 겹치지
않는 것이 아니라 **버튼끼리 겹치지 않고 1줄 안에 있는 것**을 잰다. 호버는 `setHovered(True)`로
켠다(목록의 호버 다시 맞추기와 같은 길).
"""

from collections.abc import Iterator

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

import CVDv2 as main_module
import app.theme as theme
from app.viewmodels.data import ContentItem
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels
from typing import Never

STATES = (
    DownloadState.WAITING,
    DownloadState.RUNNING,
    DownloadState.PAUSED,
    DownloadState.FINISHED,
    DownloadState.FAILED,
)
IDS = [s.name for s in STATES]

OTHER_PATH = "D:/vod/archive/2026/summer/finals/T1-vs-GEN-full-set-highlights-and-interviews"


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp) -> None:
    """실제 전역 QSS(호버 바탕 규칙 포함)를 태운다 — scope=function 유지(test_widget_theme 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _drop_windows(qapp) -> Iterator[None]:
    """이 파일의 테스트가 띄운 창을 테스트 끝에 확실히 파괴한다(card_helpers.drop_new_top_levels)."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture(autouse=True)
def no_network(monkeypatch) -> None:
    class _FailingSession:
        def head(self, *a, **k) -> Never:
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k) -> Never:
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("app.widgets.widget._global_download_path", "C:/dl")


def _pump() -> None:
    for _ in range(3):
        QApplication.processEvents()


def make_card(
    state: DownloadState, path: str = OTHER_PATH, width: int = 900
) -> tuple[QWidget, ContentItemWidget]:
    """상태별 카드를 보이는 최상위 창 안에 놓는다.

    경로는 전역과 다르게 둔다 — 3줄 저장 위치(아이콘 · 마지막 폴더)가 보이게.
    """
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {
            "title": "제목",
            "category": "",
            "channelName": "채널",
            "createdDate": "2026-08-03 12:00:00",  # 1줄 날짜 글이 생기게
            "duration": 3600,
        },
        [["1080", "u1"], ["720", "u2"]],
        None,
        "",
        path,
        "video",
        None,
    )
    item.downloadState = DownloadState.WAITING
    item.total_size = "595.34 MB"
    widget = ContentItemWidget(item, 0)
    widget.loadRepresentations()
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
    window.resize(width, widget.sizeHint().height() + 40)
    window.show()
    QTest.qWaitForWindowExposed(window)
    _pump()
    return window, widget


def resize_window(window, widget, width) -> None:
    window.resize(width, widget.sizeHint().height() + 40)
    _pump()
    assert widget.width() == width, f"요청 폭 {width}px인데 실제 {widget.width()}px — 클램프"


def _span(widget: ContentItemWidget, part: QWidget) -> tuple[int, int]:
    """`part`의 (왼쪽 끝, 오른쪽 끝 바로 뒤) x — 카드 좌표."""
    left = part.mapTo(widget, part.rect().topLeft()).x()
    return left, left + part.width()


class TestRowOneAtTheNarrowestCard:
    @pytest.mark.parametrize("state", STATES, ids=IDS)
    def test_card_buttons_stay_apart_inside_row_one(self, qapp, qtbot, state) -> None:
        """가장 좁은 카드를 호버하면 카드 버튼이 서로 겹치지 않고 1줄 오른쪽 끝 안에 있어야 한다.

        상태마다 카드를 그 카드의 최소 폭으로 줄이고 호버
        -> 보이는 버튼이 왼쪽부터 겹치지 않고, 첫 버튼 왼쪽 끝 >= 1줄 왼쪽 끝,
           마지막 버튼 오른쪽 끝 == 1줄 오른쪽 끝 == 카드 폭 − (테두리 1 + cardPadding),
           버튼 위끝 == 썸네일 위끝, 오버레이는 1줄 안
        """
        window, widget = make_card(state)
        qtbot.addWidget(window)
        resize_window(window, widget, widget.minimumSizeHint().width())
        widget.setHovered(True)
        _pump()
        buttons = [getattr(widget, name) for name in widget.visibleCardButtons()]
        assert len(buttons) == 2 and all(b.isVisible() for b in buttons), (
            f"{state.name}: 전제 — 호버한 카드에 버튼 둘이 보인다"
        )

        spans = [_span(widget, b) for b in buttons]
        row_left, row_right = _span(widget, widget.topRow)
        for (_left, right), (next_left, _next_right) in zip(spans, spans[1:]):
            assert right <= next_left, f"{state.name}: 버튼끼리 겹친다 {spans}"
        assert spans[0][0] >= row_left, f"{state.name}: 버튼이 1줄 왼쪽 밖이다 {spans}"
        inset = 1 + theme.METRICS["cardPadding"]  # 테두리 1 + 안쪽 여백
        assert spans[-1][1] == row_right == widget.width() - inset, (
            f"{state.name}: 마지막 버튼 {spans[-1]}가 1줄 오른쪽 끝 {row_right}에 붙지 않았다"
        )
        top = widget.thumbnailLabel.mapTo(widget, widget.thumbnailLabel.rect().topLeft()).y()
        assert all(b.mapTo(widget, b.rect().topLeft()).y() == top for b in buttons)
        shade_left, shade_right = _span(widget, widget.hoverShade)
        assert row_left <= shade_left and shade_right == row_right, (
            f"{state.name}: 오버레이 {shade_left}..{shade_right}가 1줄 {row_left}..{row_right} 밖이다"
        )

    @pytest.mark.parametrize("state", STATES, ids=IDS)
    def test_channel_name_and_date_stay_apart_inside_row_one(self, qapp, qtbot, state) -> None:
        """가장 좁은 카드에서 1줄의 채널 그림 · 채널명 · 날짜가 왼쪽부터 겹치지 않고 1줄 안에 있어야 한다.

        상태마다 카드를 그 카드의 최소 폭으로 줄임(호버 아님)
        -> 채널 그림 < 채널명 < 날짜 순으로 겹치지 않고, 셋 다 1줄 안, 채널명 폭 > 0
        """
        window, widget = make_card(state)
        qtbot.addWidget(window)
        resize_window(window, widget, widget.minimumSizeHint().width())
        parts = [widget.channelImageLabel, widget.channelNameLabel, widget.dateLabel]
        assert all(p.isVisible() for p in parts), "전제: 셋 다 보인다"

        spans = [_span(widget, p) for p in parts]
        row_left, row_right = _span(widget, widget.topRow)
        for (_left, right), (next_left, _next_right) in zip(spans, spans[1:]):
            assert right <= next_left, f"{state.name}: 1줄에서 겹친다 {spans}"
        assert spans[0][0] >= row_left and spans[-1][1] <= row_right, (
            f"{state.name}: 1줄 밖으로 나갔다 {spans} (1줄 {row_left}..{row_right})"
        )
        assert spans[1][1] - spans[1][0] > 0

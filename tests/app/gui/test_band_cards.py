"""띠 카드 목록 · 바 경계 · 바 버튼 · 설정 톱니바퀴 · 진행 막대만 — 설계 §13 화면 모양 정리 (#611).

기대값은 테스트가 지시 값으로 따로 둔다(제품 토큰을 읽지 않는다). 화소는 창을 그려서(`grab`) 읽는다.
"""

from collections.abc import Callable, Iterator

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

import app.theme as theme
import CVDv2 as main_module
from app.viewmodels.data import ContentItem
from app.views.mainWindow import VodDownloader
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels

OUTER = 12  # 창 바깥 좌우 여백 = 행 안쪽 좌우 여백(px) — 지시 값
ROW = 80  # 행 높이(px) — 지시 값
SEPARATOR = 1  # 행 사이 구분선(px) — 지시 값
CELL = ROW + SEPARATOR  # 한 칸(px) — 81
BAR = 4  # 진행 막대 높이(px)
SLACK = 24  # 색이 같다고 보는 한도 — 세 채널 차의 합
MANY = 12  # 창 높이 600에서 스크롤바가 보이는 카드 수


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp: QApplication) -> Iterator[None]:
    """실제 전역 QSS · 다크 테마로 잰다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


def _item(i: int, state: DownloadState = DownloadState.WAITING) -> ContentItem:
    item = ContentItem(
        f"https://chzzk.naver.com/video/{i}",
        {
            "title": f"{i:02d} 제목",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 3600,
        },
        [["1080", "u1"], ["720", "u2"]],
        None,
        "",
        "",
        "video",
        None,
    )
    item.downloadState = state
    return item


def _pump() -> None:
    for _ in range(4):
        QApplication.processEvents()


@pytest.fixture
def window(qapp: QApplication) -> Callable[..., VodDownloader]:
    """창을 만드는 함수 — 카드 상태 목록을 받아 창 폭 900 · 높이 600으로 띄운다. 창은 픽스처가 붙잡는다."""
    made: list[VodDownloader] = []

    def build(*states: DownloadState) -> VodDownloader:
        win = VodDownloader()
        made.append(win)
        win.monitorTimer.stop()
        win.show()
        _pump()
        for i, state in enumerate(states):
            item = _item(i, state)
            if state in (DownloadState.RUNNING, DownloadState.PAUSED):
                item.download_progress = 50
            win.contentManager.model.addItem(item)
        win.resize(900, 600)
        _pump()
        return win

    yield build
    for win in made:
        win.hide()


def _card(win: VodDownloader, row: int) -> ContentItemWidget:
    return win.listView.widgetFor(win.contentManager.model.itemAt(row))


def _rect(win: QWidget, widget: QWidget) -> QRect:
    return QRect(widget.mapTo(win, QPoint(0, 0)), widget.size())


def _pixel(image: QImage, x: int, y: int) -> QColor:
    ratio = image.devicePixelRatio()
    return image.pixelColor(int(x * ratio), int(y * ratio))


def _is(colour: QColor, token: str) -> bool:
    want = QColor(theme.current_tokens()[token])
    gap = (
        abs(colour.red() - want.red())
        + abs(colour.green() - want.green())
        + abs(colour.blue() - want.blue())
    )
    return gap <= SLACK


class TestBandRows:
    """카드 목록은 띠 — 바깥 여백 · 카드 사이 0, 행 사이 1px 구분선, 한 칸 81(설계 §13)."""

    def test_rows_are_separated_by_one_border_line(self, window) -> None:
        """이웃한 두 행 사이에는 1px `border` 선 하나만 있고 그 위아래는 행 바탕이어야 한다.

        대기 카드 둘, 창 폭 900 -> 첫 행 아래 끝 줄 ≈ border, 그 위 줄 ≈ cardBg, 둘째 행 첫 줄 ≈ cardBg,
        둘째 행 윗선 = 첫 행 아래 끝 + 1
        """
        win = window(DownloadState.WAITING, DownloadState.WAITING)
        first, second = _card(win, 0), _card(win, 1)
        top, below = _rect(win, first), _rect(win, second)
        assert below.top() == top.bottom() + 1
        image = win.grab().toImage()
        x = top.left() + top.width() * 3 // 4  # 글 · 버튼 밖 바탕 자리
        assert _is(_pixel(image, x, top.bottom()), "border")
        assert _is(_pixel(image, x, top.bottom() - 1), "cardBg")
        assert _is(_pixel(image, x, below.top()), "cardBg")

    def test_row_is_80_with_a_1px_separator_and_one_cell_is_81(self, window) -> None:
        """행은 80 + 구분선 1 = 81이고 목록의 한 칸(휠 · 화살표 걸음)이 81이어야 한다.

        대기 카드 12장(스크롤바 보임) -> 카드 높이 81, 스크롤바 singleStep 81
        """
        win = window(*[DownloadState.WAITING] * MANY)
        assert win.listView.verticalScrollBar().isVisible(), "전제: 스크롤바가 보인다"
        assert _card(win, 0).height() == CELL
        assert win.listView.verticalScrollBar().singleStep() == CELL

    @pytest.mark.parametrize("cards", [2, MANY], ids=["스크롤바 없음", "스크롤바 보임"])
    def test_thumbnail_and_row_text_share_the_bar_lines(self, window, cards: int) -> None:
        """썸네일 왼쪽 끝 = URL 칸 왼쪽 끝 = 12이고 3줄 오른쪽 끝 = 등록 버튼 오른쪽 끝 = 창 폭 − 12여야 한다.

        창 폭 900, 카드 2장 · 12장 -> 썸네일 x 12 == URL 칸 x, 3줄 오른쪽 끝 888 == 등록 버튼 오른쪽 끝,
        행 왼쪽 끝 0
        """
        win = window(*[DownloadState.WAITING] * cards)
        card = _card(win, 0)
        thumb, row = _rect(win, card.thumbnailLabel), _rect(win, card.statusRow)
        url, fetch = _rect(win, win.urlInput), _rect(win, win.fetchButton)
        assert thumb.left() == url.left() == OUTER
        assert row.right() == fetch.right() == win.width() - OUTER - 1
        assert _rect(win, card).left() == 0

    @pytest.mark.parametrize("state", [DownloadState.RUNNING, DownloadState.PAUSED])
    def test_progress_bar_lies_on_the_separator_without_a_track(self, window, state) -> None:
        """진행 막대는 행 아래끝(구분선 자리)에 겹치고 트랙을 그리지 않아야 한다.

        받는 중 · 일시정지 50% 카드 -> 막대 아래 끝 = 행 아래 끝 · 막대 폭 = 행 폭, 행 아래 끝 줄의 25% 자리 ≈
        상태 색(진행분), 75% 자리 ≈ border(트랙 없음 — 구분선)
        """
        win = window(state)
        card = _card(win, 0)
        bar = card.progressBar
        assert bar.isVisible(), "전제: 막대가 보인다"
        frame = card.contentFrame
        assert bar.geometry().getRect() == (0, frame.height() - BAR, frame.width(), BAR)
        rect = _rect(win, frame)
        image = win.grab().toImage()
        token = "stateRunning" if state == DownloadState.RUNNING else "statePaused"
        assert _is(_pixel(image, rect.left() + rect.width() // 4, rect.bottom()), token)
        assert _is(_pixel(image, rect.left() + rect.width() * 3 // 4, rect.bottom()), "border")

    @pytest.mark.parametrize(
        ("state", "token"),
        [(DownloadState.FINISHED, "cardTintFinished"), (DownloadState.FAILED, "cardTintFailed")],
    )
    def test_tint_covers_the_whole_row_width(self, window, state, token: str) -> None:
        """완료 · 실패 색조는 행 왼쪽 끝부터 오른쪽 끝까지(구분선 위)여야 한다.

        완료 · 실패 카드 -> 행 왼쪽 끝 화소 · 오른쪽 끝 화소(세로 가운데) ≈ 그 색조
        """
        win = window(state)
        rect = _rect(win, _card(win, 0))
        image = win.grab().toImage()
        middle = rect.top() + ROW // 2
        assert _is(_pixel(image, rect.left(), middle), token)
        assert _is(_pixel(image, rect.right(), middle), token)

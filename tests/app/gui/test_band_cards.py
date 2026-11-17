"""띠 카드 목록 · 바 경계 · 바 버튼 · 설정 톱니바퀴 · 진행 막대만 — 설계 §13 화면 모양 정리 (#611).

기대값은 테스트가 지시 값으로 따로 둔다(제품 토큰을 읽지 않는다). 화소는 창을 그려서(`grab`) 읽는다.
"""

from collections.abc import Callable, Iterator

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFontMetrics, QImage
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

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
        assert win.listView.overlayScrollBar().isVisible(), "전제: 스크롤바가 있다"
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

    @pytest.mark.parametrize("cards", [2, MANY], ids=["스크롤바 없음", "스크롤바 보임"])
    def test_rows_span_the_list_at_the_minimum_window_width(self, window, cards: int) -> None:
        """창이 최소폭이어도 행은 스크롤바 유무와 상관없이 창 왼쪽 끝부터 오른쪽 끝까지 가야 한다.

        받는 중 카드 2장 · 12장, 창 폭 = 최소폭 -> 행 왼쪽 끝 0, 행 폭 = 창 폭, 3줄 오른쪽 끝 = 창 폭 − 12 − 1,
        가로 스크롤 없음
        """
        win = window(*[DownloadState.RUNNING] * cards)
        win.resize(win.minimumWidth(), 600)
        _pump()
        assert win.width() == win.minimumWidth(), "전제: 창이 최소폭이다"
        bar = win.listView.overlayScrollBar()
        assert bar.isVisible() == (cards == MANY), "전제: 스크롤바 보임 여부"
        for row in range(cards):
            card = _card(win, row)
            rect = _rect(win, card)
            assert rect.left() == 0
            assert rect.width() == win.width()
            assert _rect(win, card.statusRow).right() == win.width() - OUTER - 1
        assert win.windowScrollArea.horizontalScrollBar().maximum() == 0

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


class TestBarLines:
    """바 ↔ 목록 경계는 헤더바 아래 · 하단바 위 선 한 줄씩, 메뉴바 ↔ 헤더바 사이는 선 없음(설계 §13)."""

    def test_header_bottom_and_footer_top_are_lines_and_the_menu_bar_has_none(self, window) -> None:
        """헤더바 맨 아래 줄 · 하단바 맨 위 줄은 border, 메뉴바 맨 아래 줄은 바 면이어야 한다.

        대기 카드 둘, 창 폭 900 -> 헤더바 아래 줄 ≈ border, 하단바 위 줄 ≈ border, 메뉴바 아래 줄 ≈ barBg,
        첫 행 윗선 = 헤더바 아래 끝 + 1(목록 위 여백 0)
        """
        win = window(DownloadState.WAITING, DownloadState.WAITING)
        image = win.grab().toImage()
        x = win.width() // 2
        header, footer, menu = (
            _rect(win, win.headerFrame),
            _rect(win, win.infoFrame),
            _rect(win, win.appMenuBar),
        )
        assert _is(_pixel(image, x, header.bottom()), "border")
        assert _is(_pixel(image, x, footer.top()), "border")
        assert _is(_pixel(image, x, menu.bottom()), "barBg")
        assert _rect(win, _card(win, 0)).top() == header.bottom() + 1


BAR_BUTTONS = ["settingsButton", "fetchButton", "selectButton", "stopButton"]  # 동작 버튼(설계 §13)
DROP_FIELDS = ["saveLocationButton", "sortButton"]  # 넓은 폭의 값 고르는 칸(#611)
FIELD_PAD = (
    10  # 펼침 칸 오른쪽 안쪽 여백(px) — ⌄ 오른쪽 끝 ↔ 칸 오른쪽 안쪽 끝(테두리 안쪽 첫 화소)
)
URL_PAD = 10  # URL 칸 안쪽 좌우 여백(px) — 펼침 칸과 같은 값(#611 지시 값)
URL_BORDER = 1  # 입력 칸 테두리(px)


class TestBarButtons:
    """동작 버튼은 한 규칙 — 평소 면 · 테두리 없음, 호버 때 면, 테두리는 입력 칸만(설계 §13)."""

    @pytest.mark.parametrize("name", BAR_BUTTONS)
    def test_bar_button_has_no_face_or_border_at_rest(self, window, name: str) -> None:
        """평소의 동작 버튼은 위 가장자리 · 왼쪽 안쪽 화소가 바 면 그대로여야 한다(테두리 · 면 없음).

        대기 카드 하나(■ 켬 — 받는 중 아님이라 꺼진 모양도 같은 규칙), 창 폭 900 -> 버튼 위 가장자리 가운데 ·
        왼쪽 안쪽 3px 자리 ≈ barBg
        """
        win = window(DownloadState.WAITING)
        button = getattr(win, name)
        assert button.isVisible(), "전제: 버튼이 보인다"
        rect = _rect(win, button)
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.top()), "barBg")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "barBg")

    @pytest.mark.parametrize("name", BAR_BUTTONS[:3])
    def test_bar_button_shows_the_hover_face(self, window, name: str) -> None:
        """포인터가 동작 버튼 위에 오면 버튼 면이 barHover여야 한다(테두리는 없음).

        창에 합성 포인터 이동(버튼 가운데) -> 버튼 왼쪽 안쪽 3px 자리 ≈ barHover, 위 가장자리 안쪽 2px 가운데 ≈
        barHover(가장자리 줄은 둥근 면의 안티에일리어싱이 섞인다) (176k)
        """
        from PySide6.QtTest import QTest

        win = window(DownloadState.WAITING)
        win.urlInput.setText(
            "https://chzzk.naver.com/video/1"
        )  # 등록 버튼이 켜진다(빈 칸이면 꺼짐)
        _pump()
        button = getattr(win, name)
        assert button.isEnabled(), "전제: 버튼이 켜져 있다"
        rect = _rect(win, button)
        QTest.mouseMove(win.windowHandle(), rect.center())
        _pump()
        assert button.underMouse(), "전제: 포인터가 버튼 위다"
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "barHover")
        assert _is(_pixel(image, rect.center().x(), rect.top() + 2), "barHover")

    @pytest.mark.parametrize(
        ("name", "edge"),
        [
            ("urlInput", "fieldEdge"),  # 네 변 같은 테두리(#611 176h)
            ("saveLocationButton", "fieldEdge"),
            ("sortButton", "fieldEdge"),
            ("settingsButton", "barBg"),
            ("fetchButton", "barBg"),
            ("selectButton", "barBg"),
            ("stopButton", "barBg"),
        ],
    )
    def test_only_input_fields_have_a_border(self, window, name: str, edge: str) -> None:
        """평소 테두리가 보이는 것은 입력 칸(URL · 펼침 칸)뿐이고 동작 버튼은 바 면이어야 한다.

        창 폭 900 -> 위 가장자리 가운데 화소: urlInput · saveLocationButton · sortButton ≈ fieldEdge,
        settingsButton · fetchButton · selectButton · stopButton ≈ barBg
        """
        win = window(DownloadState.WAITING)
        widget = getattr(win, name)
        assert widget.isVisible(), "전제: 보인다"
        rect = _rect(win, widget)
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.top()), edge)

    @pytest.mark.parametrize(
        ("name", "edge"),
        [
            ("downloadButton", "surfaceAlt"),  # ▶ 꺼짐 — 면만(#611 오너 실기 2026-11-15)
            ("stopButton", "barBg"),  # ■ 꺼짐 — 면 · 테두리 없음
            (
                "sortButton",
                "border",
            ),  # 드롭다운 꺼짐 — 입력 칸이라 테두리는 남되 옅은 border(#611 176f)
        ],
    )
    def test_only_input_fields_have_a_border_when_disabled(
        self, window: Callable[..., VodDownloader], name: str, edge: str
    ) -> None:
        """꺼진 상태에서도 테두리는 입력 칸에만 있고 꺼진 ▶은 면 surfaceAlt · ■은 바 면이어야 한다.

        창 폭 900, 위젯 setEnabled(False) -> 위 가장자리 가운데 화소: downloadButton ≈ surfaceAlt,
        stopButton ≈ barBg, sortButton ≈ border
        """
        win = window(DownloadState.WAITING)
        widget = getattr(win, name)
        widget.setEnabled(False)
        _pump()
        assert widget.isVisible() and not widget.isEnabled(), "전제: 보이고 꺼져 있다"
        rect = _rect(win, widget)
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.top()), edge)


class TestPrimaryButton:
    """주 동작(▶ · 카드 편집 창 저장 role=primary) — 켜면 강조색 채움, 꺼지면 면 surfaceAlt · 글 textDisabled ·
    테두리 없음(#611 — 테두리는 입력 칸만)."""

    def test_disabled_download_button_has_no_border(
        self, window: Callable[..., VodDownloader]
    ) -> None:
        """꺼진 ▶은 왼쪽 가장자리 · 안쪽 화소가 모두 surfaceAlt이고 border 색 화소가 없어야 한다.

        카드 없음(▶ 꺼짐) -> 왼쪽 가장자리 가운데 ≈ surfaceAlt, 왼쪽 안쪽 3px ≈ surfaceAlt, 위 가장자리 가운데 ≈
        surfaceAlt
        """
        win = window()
        button = win.downloadButton
        assert button.isVisible() and not button.isEnabled(), "전제: 카드가 없어 꺼져 있다"
        rect = _rect(win, button)
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.left(), rect.center().y()), "surfaceAlt")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "surfaceAlt")
        assert _is(_pixel(image, rect.center().x(), rect.top()), "surfaceAlt")

    def test_disabled_download_glyph_is_text_disabled(
        self, window: Callable[..., VodDownloader]
    ) -> None:
        """꺼진 ▶의 도형은 textDisabled로 그려져야 한다.

        카드 없음(▶ 꺼짐) -> 버튼 안 화소 가운데 textDisabled에 가까운 화소가 있다, onAccent에 가까운 화소는 없다
        """
        win = window()
        button = win.downloadButton
        assert not button.isEnabled(), "전제: 꺼져 있다"
        image = button.grab().toImage()
        ink = [_pixel(image, x, y) for x in range(button.width()) for y in range(button.height())]
        assert any(_is(colour, "textDisabled") for colour in ink)
        assert not any(_is(colour, "onAccent") for colour in ink)

    def test_enabled_download_button_keeps_the_accent_fill(
        self, window: Callable[..., VodDownloader]
    ) -> None:
        """켠 ▶은 가장자리 · 안쪽이 그대로 강조색이어야 한다.

        대기 카드 하나(▶ 켬) -> 왼쪽 가장자리 가운데 ≈ accent, 왼쪽 안쪽 3px ≈ accent
        """
        win = window(DownloadState.WAITING)
        button = win.downloadButton
        assert button.isEnabled(), "전제: 켜져 있다"
        rect = _rect(win, button)
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.left(), rect.center().y()), "accent")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "accent")

    @pytest.mark.parametrize("enabled", [False, True], ids=["꺼짐", "켬"])
    def test_primary_role_button_follows_the_same_rule(
        self, window: Callable[..., VodDownloader], enabled: bool
    ) -> None:
        """role=primary 글 버튼(카드 편집 창 저장)도 꺼지면 테두리 없는 surfaceAlt 면 · textDisabled 글, 켜면
        강조색이어야 한다.

        창 안의 QPushButton("Save", role=primary): 꺼짐 -> 왼쪽 가장자리 가운데 ≈ surfaceAlt, 글 화소 가운데
        textDisabled에 가까운 것이 있다 / 켬 -> 왼쪽 가장자리 가운데 ≈ accent
        """
        from PySide6.QtWidgets import QPushButton

        win = window()
        button = QPushButton("Save", win.contentColumn)
        button.setProperty("role", "primary")
        button.setEnabled(enabled)
        button.setGeometry(20, 20, 120, 32)
        button.show()
        _pump()
        image = button.grab().toImage()
        edge = _pixel(image, 0, button.height() // 2)
        if enabled:
            assert _is(edge, "accent")
            return
        assert _is(edge, "surfaceAlt")
        ink = [_pixel(image, x, y) for x in range(button.width()) for y in range(button.height())]
        assert any(_is(colour, "textDisabled") for colour in ink)


class TestUrlField:
    """URL 입력창 — 평소 테두리 fieldEdge · 호버 fieldEdgeHover(초점 accent가 앞섬), 안쪽 좌우 여백 10(드롭다운 칸과 같음, #611)."""

    def test_url_field_hover_shows_the_strong_border(
        self, window: Callable[..., VodDownloader]
    ) -> None:
        """포인터가 URL 칸 위에 오면 네 변 테두리가 fieldEdgeHover, 면은 fieldBg 그대로여야 한다.

        창에 합성 포인터 이동(URL 칸 가운데) -> 위 · 아래 가장자리 가운데 ≈ fieldEdgeHover, 왼쪽 안쪽 3px ≈ fieldBg
        """
        from PySide6.QtTest import QTest

        win = window(DownloadState.WAITING)
        field = win.urlInput
        rect = _rect(win, field)
        QTest.mouseMove(
            win.windowHandle(), QPoint(1, 1)
        )  # 앞 테스트의 포인터 자리와 다르게 — 들어감이 생긴다
        QTest.mouseMove(win.windowHandle(), rect.center())
        _pump()
        assert field.underMouse() and not field.hasFocus(), "전제: 포인터만 칸 위다"
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.top()), "fieldEdgeHover")
        assert _is(_pixel(image, rect.center().x(), rect.bottom()), "fieldEdgeHover")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "fieldBg")

    def test_focus_beats_hover_on_the_url_field(
        self, window: Callable[..., VodDownloader], qtbot
    ) -> None:
        """초점이 있는 URL 칸은 포인터가 올라 있어도 테두리가 accent여야 한다.

        창 활성 · URL 칸 초점 · 합성 포인터 이동(칸 가운데) -> 위 · 아래 가장자리 가운데 ≈ accent,
        아래 바로 위 줄 ≈ fieldBg(1px)
        """
        from PySide6.QtTest import QTest

        win = window(DownloadState.WAITING)
        win.activateWindow()
        qtbot.waitUntil(win.isActiveWindow, timeout=3000)
        field = win.urlInput
        field.setFocus()
        qtbot.waitUntil(field.hasFocus, timeout=3000)
        rect = _rect(win, field)
        QTest.mouseMove(
            win.windowHandle(), QPoint(1, 1)
        )  # 앞 테스트의 포인터 자리와 다르게 — 들어감이 생긴다
        QTest.mouseMove(win.windowHandle(), rect.center())
        _pump()
        assert field.underMouse(), "전제: 포인터가 칸 위다"
        image = win.grab().toImage()
        for y in (rect.top(), rect.bottom()):
            assert _is(_pixel(image, rect.center().x(), y), "accent"), y
        assert _is(_pixel(image, rect.center().x(), rect.bottom() - 1), "fieldBg")

    def test_url_field_inner_side_padding_is_10(self, window: Callable[..., VodDownloader]) -> None:
        """URL 칸의 글 자리는 테두리 1px + 안쪽 여백 10 = 칸 왼쪽 · 오른쪽 끝에서 11 안이어야 한다.

        창 폭 900 -> SE_LineEditContents 사각형 왼쪽 == 11, 칸 폭 − 오른쪽 끝(끝 포함 안 함) == 11
        """
        from PySide6.QtWidgets import QStyle, QStyleOptionFrame

        win = window(DownloadState.WAITING)
        field = win.urlInput
        option = QStyleOptionFrame()
        field.initStyleOption(option)
        contents = field.style().subElementRect(
            QStyle.SubElement.SE_LineEditContents, option, field
        )
        assert contents.left() == URL_BORDER + URL_PAD
        assert field.width() - (contents.right() + 1) == URL_BORDER + URL_PAD


def _corner(image: QImage, rect: QRect) -> list[QColor]:
    """왼쪽 위 모서리 3×3 화소 — 모서리 반경이 같으면 같은 바 면 위에서 같은 화소가 나온다."""
    return [_pixel(image, rect.left() + dx, rect.top() + dy) for dy in range(3) for dx in range(3)]


def _same(a: QColor, b: QColor) -> bool:
    gap = abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue())
    return gap <= SLACK


class TestDropFields:
    """값을 고르는 칸(펼침 버튼)은 URL 입력칸과 같은 입력 칸 모양 — 테두리 · 면 · 모서리 · 높이(#611)."""

    @pytest.mark.parametrize("name", DROP_FIELDS)
    def test_drop_field_has_the_url_border_and_face_at_rest(self, window, name: str) -> None:
        """평소의 펼침 칸은 위 · 아래 가장자리 · 왼쪽 안쪽 화소가 URL 칸과 같아야 한다.

        창 폭 900 -> 펼침 칸 아래 가장자리 가운데 ≈ URL 칸 아래 가장자리 가운데 ≈ fieldEdge, 위 가장자리 가운데
        ≈ URL 칸 위 가장자리 가운데 ≈ fieldEdge, 왼쪽 안쪽 3px 자리 ≈ URL 칸 왼쪽 안쪽 3px 자리 ≈ fieldBg
        """
        win = window(DownloadState.WAITING)
        image = win.grab().toImage()
        url, field = _rect(win, win.urlInput), _rect(win, getattr(win, name))
        top = _pixel(image, field.center().x(), field.top())
        bottom = _pixel(image, field.center().x(), field.bottom())
        face = _pixel(image, field.left() + 3, field.center().y())
        assert _is(bottom, "fieldEdge")
        assert _same(bottom, _pixel(image, url.center().x(), url.bottom()))
        assert _is(top, "fieldEdge")
        assert _same(top, _pixel(image, url.center().x(), url.top()))
        assert _is(face, "fieldBg")
        assert _same(face, _pixel(image, url.left() + 3, url.center().y()))

    @pytest.mark.parametrize("name", DROP_FIELDS)
    def test_drop_field_has_the_url_height_and_corner(self, window, name: str) -> None:
        """펼침 칸은 URL 칸과 높이가 같고 왼쪽 위 모서리 3×3 화소가 같아야 한다(모서리 반경이 같다).

        창 폭 900 -> 높이 == urlInput 높이, 모서리 3×3 화소 각각 ≈ URL 칸의 같은 자리
        """
        win = window(DownloadState.WAITING)
        image = win.grab().toImage()
        url, field = _rect(win, win.urlInput), _rect(win, getattr(win, name))
        assert field.height() == url.height()
        for got, want in zip(_corner(image, field), _corner(image, url), strict=True):
            assert _same(got, want), (got.name(), want.name())

    @pytest.mark.parametrize("name", DROP_FIELDS)
    def test_caret_sits_at_the_right_inner_end(self, window, name: str) -> None:
        """⌄ 오른쪽 끝은 칸 오른쪽 안쪽 끝에서 FIELD_PAD 안이고, 그 자리에 ⌄가 그려지고, 글과 떨어져야 한다.

        창 폭 900, 글 "A"(칸 폭보다 훨씬 짧은 값) -> (칸 폭 − 2) − caretRect.right() == 10, caretRect 안에
        fieldBg 아닌 화소(⌄)가 있다, caretRect 왼쪽 끝 > 그린 글 오른쪽 끝
        """
        win = window(DownloadState.WAITING)
        button = getattr(win, name)
        button.setFullText("A")  # 글 바로 뒤 자리와 칸 오른쪽 안쪽 끝 자리가 갈린다
        _pump()
        caret = button.caretRect()
        assert (button.width() - 2) - caret.right() == FIELD_PAD
        image = button.grab().toImage()
        ink = [
            _pixel(image, x, y)
            for x in range(caret.left(), caret.right() + 1)
            for y in range(caret.top(), caret.bottom() + 1)
        ]
        assert any(not _is(colour, "fieldBg") for colour in ink), "⌄가 그 칸에 그려진다"
        text_right = button._textRect().left() + button.fontMetrics().horizontalAdvance(
            button.shownText()
        )
        assert caret.left() > text_right

    def test_drop_field_hover_shows_the_strong_border(self, window) -> None:
        """포인터가 펼침 칸 위에 오면 네 변 테두리가 fieldEdgeHover, 면은 fieldBg 그대로여야 한다.

        창에 합성 포인터 이동(정렬 칸 가운데) -> 위 · 아래 가장자리 가운데 ≈ fieldEdgeHover, 왼쪽 안쪽 3px ≈ fieldBg
        """
        from PySide6.QtTest import QTest

        win = window(DownloadState.WAITING)
        button = win.sortButton
        rect = _rect(win, button)
        QTest.mouseMove(win.windowHandle(), rect.center())
        _pump()
        assert button.underMouse(), "전제: 포인터가 칸 위다"
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.top()), "fieldEdgeHover")
        assert _is(_pixel(image, rect.center().x(), rect.bottom()), "fieldEdgeHover")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "fieldBg")

    def test_pressed_drop_field_shows_the_accent_border(self, window) -> None:
        """눌린 펼침 칸(펼친 동안과 같은 규칙)은 테두리가 accent, 면은 fieldBg여야 한다.

        정렬 칸 setDown(True) -> 위 · 아래 가장자리 가운데 ≈ accent, 아래 바로 위 줄 ≈ fieldBg(1px),
        왼쪽 안쪽 3px ≈ fieldBg
        """
        win = window(DownloadState.WAITING)
        button = win.sortButton
        button.setDown(True)
        _pump()
        assert button.isDown(), "전제: 눌린 상태다"
        rect = _rect(win, button)
        image = win.grab().toImage()
        for y in (rect.top(), rect.bottom()):
            assert _is(_pixel(image, rect.center().x(), y), "accent"), y
        assert _is(_pixel(image, rect.center().x(), rect.bottom() - 1), "fieldBg")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "fieldBg")

    def test_disabled_drop_field_has_the_faint_border(self, window) -> None:
        """꺼진 펼침 칸은 테두리가 옅은 border이고 면 fieldBg는 남아야 한다(#611 176f).

        정렬 칸 setEnabled(False) -> 위 · 아래 가장자리 가운데 ≈ border, 왼쪽 안쪽 3px ≈ fieldBg
        """
        win = window(DownloadState.WAITING)
        button = win.sortButton
        button.setEnabled(False)
        _pump()
        assert button.isVisible() and not button.isEnabled(), "전제: 보이고 꺼져 있다"
        rect = _rect(win, button)
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.top()), "border")
        assert _is(_pixel(image, rect.center().x(), rect.bottom()), "border")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "fieldBg")

    @pytest.mark.parametrize("width", ["first", "min"])
    def test_narrow_status_dropdown_is_a_bordered_field(self, window, width: str) -> None:
        """칩이 상태 드롭다운으로 바뀐 좁은 폭에서 그 드롭다운도 테두리 있는 입력 칸이어야 한다.

        창 폭 = 칩이 드롭다운으로 바뀌는 첫 폭(900에서 2px씩 줄여 찾음) · 최소폭 -> filterDropButton 보임,
        아래 가장자리 가운데 ≈ fieldEdge · 왼쪽 안쪽 3px ≈ fieldBg
        """
        win = window(DownloadState.WAITING)
        if width == "min":
            target = win.minimumWidth()
            win.resize(target, 600)
            _pump()
        else:
            target = 900
            while not win.filterChipBar.isCompact() and target > win.minimumWidth():
                target -= 2
                win.resize(target, 600)
                _pump()
        assert win.width() == target, "전제: 요청한 폭이다"
        drop = win.filterChipBar.dropButton
        assert drop.isVisible(), "전제: 상태 드롭다운이 보인다"
        rect = _rect(win, drop)
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.bottom()), "fieldEdge")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "fieldBg")

    @pytest.mark.parametrize("name", DROP_FIELDS)
    def test_icon_drop_field_is_a_bordered_square(self, window, name: str) -> None:
        """아이콘 모양 저장 위치 · 정렬 칸도 테두리 있는 정사각 입력 칸이어야 한다.

        창 폭 900, setCompact(True) -> 폭 == 높이, 아래 가장자리 가운데 ≈ fieldEdge, 왼쪽 안쪽 3px ≈ fieldBg
        """
        win = window(DownloadState.WAITING)
        button = getattr(win, name)
        button.setCompact(True)
        _pump()
        assert button.isCompact(), "전제: 아이콘 모양이다"
        rect = _rect(win, button)
        assert rect.width() == rect.height()
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.bottom()), "fieldEdge")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "fieldBg")


FOLDER_LEFT = 11  # 넓은 폭 📁 그림 칸 왼쪽 끝(px) — 테두리 1 + 안쪽 여백 10
FOLDER_CELL = 16  # 넓은 폭 📁 그림 칸(px) — 글 옆 그림은 글 크기(176k 지시 값)
FOLDER_GAP = 6  # 📁 ↔ 글(px)
NARROW_CELL = 20  # 아이콘만인 좁은 폭 📁 그림 칸(px) — 옆 ＋ · 선택과 같다(176k 지시 값)


def _ink_columns(image: QImage, width: int, height: int) -> list[int]:
    """바탕(왼쪽 안쪽 3px 자리 색)과 다른 화소가 있는 열 — 가운데 위아래 10px 띠만 본다(모서리 테두리 제외)."""
    face = _pixel(image, 3, height // 2)
    columns = []
    for x in range(3, width - 3):
        for y in range(height // 2 - 10, height // 2 + 10):
            c = _pixel(image, x, y)
            gap = (
                abs(c.red() - face.red())
                + abs(c.green() - face.green())
                + abs(c.blue() - face.blue())
            )
            if gap > SLACK:
                columns.append(x)
                break
    return columns


def _first_run(columns: list[int]) -> tuple[int, int]:
    """처음 이어진 잉크 열 묶음의 (첫 열, 끝 열) — 빈 열 셋 이상에서 끊는다."""
    start = end = columns[0]
    for x in columns[1:]:
        if x - end > 3:
            break
        end = x
    return start, end


class TestFolderGlyphSize:
    """저장 위치 드롭다운 📁 — 넓은 폭(그림 + 글)은 글 크기 16, 좁은 폭(그림만)은 바 그림 칸 20(176k)."""

    def test_wide_folder_glyph_fits_the_16_cell_and_text_follows_it(self, window) -> None:
        """넓은 폭 📁 잉크가 그림 칸 16 안에 있고 글이 그 칸 + 6 뒤에서 시작해야 한다.

        창 폭 900, 저장 위치 칸 넓은 폭(그림 + 글) -> 📁 잉크 열 ⊂ [11, 27), 글 첫 잉크 열 ∈ [33, 36)
        (글 왼쪽 여백 0 ~ 2px — 그림 칸이 20이면 37부터)
        """
        win = window(DownloadState.WAITING)
        button = win.saveLocationButton
        assert button.isVisible() and not button.isCompact(), "전제: 넓은 폭이다"
        image = button.grab().toImage()
        columns = _ink_columns(image, button.width(), button.height())
        start, end = _first_run(columns)
        assert FOLDER_LEFT <= start and end < FOLDER_LEFT + FOLDER_CELL
        text_start = min(x for x in columns if x > end)
        text_left = FOLDER_LEFT + FOLDER_CELL + FOLDER_GAP
        assert text_left <= text_start < text_left + 3

    def test_narrow_folder_glyph_is_larger_than_the_wide_one(self, window) -> None:
        """아이콘만인 📁 잉크 폭이 넓은 폭 📁보다 그림 칸 비(20 / 16)만큼 커야 한다.

        창 폭 900, 넓은 폭 잉크 폭 w16 · setCompact(True) 뒤 잉크 폭 w20 -> w20 ≥ w16 × 20 / 16 − 1,
        w20 ≤ 20
        """
        win = window(DownloadState.WAITING)
        button = win.saveLocationButton
        image = button.grab().toImage()
        start, end = _first_run(_ink_columns(image, button.width(), button.height()))
        wide = end - start + 1
        button.setCompact(True)
        _pump()
        assert button.isCompact(), "전제: 아이콘 모양이다"
        image = button.grab().toImage()
        start, end = _first_run(_ink_columns(image, button.width(), button.height()))
        narrow = end - start + 1
        assert wide * NARROW_CELL / FOLDER_CELL - 1 <= narrow <= NARROW_CELL


class TestSettingsGear:
    """설정은 메뉴바 오른쪽 끝 톱니바퀴 · Ctrl+,로 여는 모달 창 — 메뉴바 탭은 작업 화면만(설계 §4 · §13)."""

    @pytest.fixture
    def opened(self, monkeypatch: pytest.MonkeyPatch) -> list:
        """설정 창 `exec_`를 가로채 연 창을 모은다(실제 모달을 띄우지 않는다)."""
        import app.views.mainWindow as main_window_module

        seen: list = []
        monkeypatch.setattr(
            main_window_module.SettingDialog, "exec_", lambda self: seen.append(self) or 0
        )
        return seen

    def test_gear_sits_at_the_right_end_of_the_menu_bar(self, window) -> None:
        """톱니바퀴는 메뉴바 오른쪽 끝(창 폭 − 12)의 32 정사각이고, 메뉴바 탭은 「VOD 다운」 하나여야 한다.

        창 폭 900 -> 톱니바퀴 오른쪽 끝 888 · 크기 32×32 · 글 없음 · 툴팁 「Settings」로 시작, 메뉴바 글 버튼은
        menuDownloadButton 하나
        """
        from PySide6.QtWidgets import QPushButton

        win = window(DownloadState.WAITING)
        gear = win.settingsButton
        rect = _rect(win, gear)
        assert rect.right() == win.width() - OUTER - 1
        assert (gear.width(), gear.height()) == (32, 32)
        assert gear.text() == "" and gear.toolTip().startswith("Settings")
        texts = [b.objectName() for b in win.appMenuBar.findChildren(QPushButton) if b.text()]
        assert texts == ["menuDownloadButton"]

    def test_gear_opens_the_settings_dialog(self, window, opened: list) -> None:
        """톱니바퀴를 누르면 설정 창이 한 번 열려야 한다.

        톱니바퀴 클릭 -> 설정 창 exec_ 1번, 고른 메뉴는 「VOD 다운」 그대로
        """
        win = window(DownloadState.WAITING)
        win.settingsButton.click()
        _pump()
        assert len(opened) == 1
        assert win.menuDownloadButton.isChecked()

    def test_main_window_module_holds_no_key_sequence_object(self) -> None:
        """메인 창 모듈의 모듈 수준 이름 가운데 QKeySequence 객체가 없어야 한다(앱 객체 전에 import된다, #611).

        app.views.mainWindow의 모듈 전역 값 -> QKeySequence 인스턴스 0개, 단축키 글 SETTINGS_KEYS == "Ctrl+,"
        """
        from PySide6.QtGui import QKeySequence

        import app.views.mainWindow as main_window_module

        made = [
            name
            for name, value in vars(main_window_module).items()
            if isinstance(value, QKeySequence)
        ]
        assert made == []
        assert main_window_module.SETTINGS_KEYS == "Ctrl+,"

    def test_ctrl_comma_opens_the_settings_dialog(self, window, opened: list, qtbot) -> None:
        """Ctrl+,(macOS는 Cmd+,)를 누르면 설정 창이 한 번 열려야 한다.

        창 활성 · 목록에 초점, Ctrl+, 키 -> 설정 창 exec_ 1번
        """
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        win = window(DownloadState.WAITING)
        win.activateWindow()
        qtbot.waitUntil(win.isActiveWindow, timeout=3000)
        QTest.keyClick(win.listView, Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
        _pump()
        assert len(opened) == 1


class TestBarOnlyProgress:
    """진행 표시는 막대 하나 — 배경 채움 방식과 그 설정 키는 없다(설계 §13)."""

    def test_a_leftover_fill_row_still_shows_the_bar(self, window) -> None:
        """설정 DB에 옛 `cardProgress` = "fill" 행이 남아 있어도 받는 중 카드는 막대를 보이고 면을 칠하지 않아야 한다.

        DB 행 cardProgress = "fill"(JSON) · 카드 화면 설정 다시 읽음, 받는 중 50% 카드 -> 막대 보임 · 막대 값 5000,
        카드 면 25% 자리(막대 위 글 밖) ≈ cardBg, 그 행은 지워지지 않고 남음(읽을 때 버린다)
        """
        import app.viewmodels.card_display as card_display
        from tests.support.settings_db import read_rows, write_rows

        write_rows({"cardProgress": '"fill"'})
        card_display.use(None)
        try:
            win = window(DownloadState.RUNNING)
            card = _card(win, 0)
            assert card.progressBar.isVisible()
            assert card.progressBar.value() == 5000
            frame = card.contentFrame
            rect = _rect(win, frame)
            image = win.grab().toImage()
            assert _is(_pixel(image, rect.left() + rect.width() // 4, rect.top() + 4), "cardBg")
            assert read_rows().get("cardProgress") == '"fill"'
        finally:
            card_display.use(None)


UNDERLINE = 2  # 고른 탭 밑줄 굵기(px) — 지시 값(#611 176e)


def _underline_columns(win: VodDownloader, tab: QWidget) -> list[int]:
    """탭 아래끝 줄에서 accent인 화소의 창 x — 왼쪽부터."""
    rect = _rect(win, tab)
    image = win.grab().toImage()
    return [
        x
        for x in range(rect.left(), rect.right() + 1)
        if _is(_pixel(image, x, rect.bottom()), "accent")
    ]


def _tab_strip(win: VodDownloader) -> tuple[QWidget, QWidget]:
    """메뉴바에 고르지 않은 탭 하나를 더한다 — 탭이 「VOD 다운」 하나뿐이라 고르지 않은 탭을 잴 자리가 없다."""
    from app.widgets.menu_button import MenuButton

    other = MenuButton(win.appMenuBar)
    other.setProperty("role", "menu")
    other.setCheckable(True)
    other.setText("Tab")
    other.setFixedHeight(win.menuDownloadButton.height())
    win.appMenuGroup.addButton(other)
    win.appMenuLayout.insertWidget(1, other)
    _pump()
    return win.menuDownloadButton, other


class TestMenuTabs:
    """메뉴바 탭은 밑줄 탭 — 고른 탭만 아래 2px 강조색 밑줄(글 진행 폭), 면 없음, 초점은 글 둘레 상자(#611)."""

    def test_selected_tab_has_a_two_pixel_accent_underline(
        self, window: Callable[..., VodDownloader]
    ) -> None:
        """고른 탭의 맨 아래 두 줄은 강조색이고 그 위 줄은 바 면이어야 한다.

        창 폭 900 -> 「VOD 다운」 아래끝 줄 · 그 위 줄 가운데 ≈ accent, 아래끝 − 2 줄 가운데 ≈ barBg
        """
        win = window(DownloadState.WAITING)
        tab = win.menuDownloadButton
        assert tab.isChecked(), "전제: 고른 탭이다"
        rect = _rect(win, tab)
        image = win.grab().toImage()
        x = rect.center().x()
        for y in range(rect.bottom() - UNDERLINE + 1, rect.bottom() + 1):
            assert _is(_pixel(image, x, y), "accent"), y
        assert _is(_pixel(image, x, rect.bottom() - UNDERLINE), "barBg")

    def test_underline_spans_the_text_advance(self, window: Callable[..., VodDownloader]) -> None:
        """고른 탭 밑줄은 글 왼쪽 끝 ~ 글 진행 폭 끝이어야 한다(#611 176g).

        창 폭 900, 버블 없음 -> 탭 아래끝 줄의 accent 화소 열이 끊김 없이 이어지고, 맨 왼쪽 == 탭 x + 1 + menuTabPadX
        + (내용 자리 폭 − 진행 폭) // 2, 열 수 == QFontMetrics.horizontalAdvance(탭 글)
        """
        win = window(DownloadState.WAITING)
        tab = win.menuDownloadButton
        assert tab.isVisible() and tab.isChecked(), "전제: 보이는 고른 탭"
        cols = _underline_columns(win, tab)
        advance = QFontMetrics(tab.font()).horizontalAdvance(tab.text())
        pad = theme.METRICS["menuTabPadX"]
        room = (
            tab.width() - 1 - pad - 1 - theme.MENU_BUTTON_PAD_RIGHT
        )  # 테두리 1 · 왼쪽 여백 · 오른쪽 여백
        left = _rect(win, tab).left() + 1 + pad + max(0, (room - advance) // 2)
        assert cols == list(range(cols[0], cols[0] + len(cols))), "끊김 없이 이어진다"
        assert cols[0] == left
        assert len(cols) == advance

    def test_underline_does_not_run_under_the_bubble(
        self, window: Callable[..., VodDownloader]
    ) -> None:
        """버블이 보여도 밑줄은 글 진행 폭 끝에서 멈추고 버블 아래에 밑줄 화소가 없어야 한다.

        setBubble("done") -> 탭 아래 두 줄에서 버블 화소 왼쪽 끝 ~ 탭 오른쪽 끝 사이에 accent 없음,
        아래끝 줄 accent 열 수 == 글 진행 폭(버블 없을 때와 같다)
        """
        win = window(DownloadState.WAITING)
        tab = win.menuDownloadButton
        before = _underline_columns(win, tab)
        tab.setBubble("done")
        _pump()
        assert tab.bubble.isVisible(), "전제: 버블이 보인다"
        bubble = _rect(win, tab.bubble)
        rect = _rect(win, tab)
        image = win.grab().toImage()
        under = [
            x
            for x in range(bubble.left(), rect.right() + 1)
            for y in range(rect.bottom() - UNDERLINE + 1, rect.bottom() + 1)
            if _is(_pixel(image, x, y), "accent")
        ]
        assert under == []
        assert _underline_columns(win, tab) == before

    def test_selected_tab_has_no_face(self, window: Callable[..., VodDownloader]) -> None:
        """고른 탭은 면 없이 바 면 위 굵은 글이어야 한다.

        창 폭 900 -> 「VOD 다운」 굵은 글꼴, 왼쪽 안쪽 3px 가운데 · 위 가장자리 가운데 ≈ barBg
        """
        win = window(DownloadState.WAITING)
        tab = win.menuDownloadButton
        rect = _rect(win, tab)
        image = win.grab().toImage()
        assert tab.font().bold()
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "barBg")
        assert _is(_pixel(image, rect.center().x(), rect.top()), "barBg")

    def test_unselected_tab_has_no_underline(self, window: Callable[..., VodDownloader]) -> None:
        """고르지 않은 탭은 아래 줄에 강조색이 없고 면도 없어야 한다.

        탭 「Tab」을 더함(고르지 않음) -> 그 탭 아래 두 줄 어디에도 accent 화소가 없다, 왼쪽 안쪽 3px ≈ barBg
        """
        win = window(DownloadState.WAITING)
        _, other = _tab_strip(win)
        assert other.isVisible() and not other.isChecked(), "전제: 보이는 고르지 않은 탭"
        rect = _rect(win, other)
        image = win.grab().toImage()
        bottom = [
            _pixel(image, x, y)
            for x in range(rect.left(), rect.right() + 1)
            for y in range(rect.bottom() - UNDERLINE + 1, rect.bottom() + 1)
        ]
        assert not any(_is(colour, "accent") for colour in bottom)
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "barBg")

    def test_hovered_unselected_tab_has_no_face(self, window: Callable[..., VodDownloader]) -> None:
        """포인터가 올라간 고르지 않은 탭도 면 · 밑줄 없이 바 면이어야 한다.

        창에 합성 포인터 이동(「Tab」 가운데) -> underMouse, 왼쪽 안쪽 3px ≈ barBg, 아래끝 줄 가운데 ≈ barBg
        """
        from PySide6.QtTest import QTest

        win = window(DownloadState.WAITING)
        _, other = _tab_strip(win)
        rect = _rect(win, other)
        QTest.mouseMove(win.windowHandle(), QPoint(1, 1))  # 앞 테스트의 포인터 자리와 다르게
        QTest.mouseMove(win.windowHandle(), rect.center())
        _pump()
        assert other.underMouse(), "전제: 포인터가 탭 위다"
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "barBg")
        assert _is(_pixel(image, rect.center().x(), rect.bottom()), "barBg")

    @pytest.mark.parametrize("which", ["selected", "unselected"])
    def test_keyboard_focus_draws_a_box_around_the_text_above_the_underline(
        self, window: Callable[..., VodDownloader], qtbot, which: str
    ) -> None:
        """키보드 초점을 받은 탭은 글을 둘러싼 강조색 상자가 있고 상자와 밑줄 자리 사이에 바 면 줄이 있어야 한다.

        탭에 Tab 초점 -> 탭 세로 가운데 줄의 accent 화소 맨 왼쪽 < 글 화소 맨 왼쪽, 맨 오른쪽 > 글 화소 맨 오른쪽,
        상자 가로 가운데 열: 세로 가운데 ~ 밑줄 위 두 줄 사이에 accent(상자 아래 변)가 있고 밑줄 바로 위 두 줄
        (아래끝 − 2 · − 3) ≈ barBg, 초점 전에는 세로 가운데 줄에 accent 화소가 없다
        """
        win = window(DownloadState.WAITING)
        selected, other = _tab_strip(win)
        tab = selected if which == "selected" else other
        rect = _rect(win, tab)
        middle = rect.center().y()

        def accent_columns(image: QImage) -> list[int]:
            return [
                x
                for x in range(rect.left(), rect.right() + 1)
                if _is(_pixel(image, x, middle), "accent")
            ]

        assert not accent_columns(win.grab().toImage()), "전제: 초점 전에는 상자가 없다"
        win.activateWindow()
        qtbot.waitUntil(win.isActiveWindow, timeout=3000)
        tab.setFocus(Qt.FocusReason.TabFocusReason)
        qtbot.waitUntil(tab.hasFocus, timeout=3000)
        _pump()
        image = win.grab().toImage()
        box = accent_columns(image)
        assert box, "초점 상자가 그려진다"
        ink = [
            x
            for x in range(rect.left(), rect.right() + 1)
            # 초점 상자의 둥근 모서리(위아래 끝에서 6줄 안) 밖 — 모서리 안티에일리어싱 화소를 글로 세지 않는다
            for y in range(rect.top() + 8, rect.bottom() - 8)
            if not _is(_pixel(image, x, y), "barBg") and not _is(_pixel(image, x, y), "accent")
        ]
        assert ink, "전제: 글 화소가 있다"
        assert min(box) < min(ink) and max(box) > max(ink)
        x = (min(box) + max(box)) // 2  # 상자 가로 가운데
        gap = (rect.bottom() - UNDERLINE, rect.bottom() - UNDERLINE - 1)  # 밑줄 바로 위 두 줄
        assert any(_is(_pixel(image, x, y), "accent") for y in range(middle, min(gap))), (
            "상자 아래 변이 밑줄 자리 위에 있다"
        )
        for y in gap:
            assert _is(_pixel(image, x, y), "barBg"), y


class TestFieldBorderToken:
    """입력 칸 테두리는 fieldEdge, 구분선은 border 그대로 — 두 토큰이 따로 칠한다(#611)."""

    def test_url_field_uses_field_border_and_the_header_line_uses_border(
        self, window: Callable[..., VodDownloader]
    ) -> None:
        """URL 칸 아래 가장자리는 fieldEdge, 헤더바 아래 선은 border여야 한다.

        대기 카드 하나, 창 폭 900 -> URL 칸 아래 가장자리 가운데 ≈ fieldEdge · ≉ border,
        헤더바 아래 줄(URL 칸 가운데 x) ≈ border · ≉ fieldEdge
        """
        win = window(DownloadState.WAITING)
        url, header = _rect(win, win.urlInput), _rect(win, win.headerFrame)
        image = win.grab().toImage()
        edge = _pixel(image, url.center().x(), url.bottom())
        line = _pixel(image, url.center().x(), header.bottom())
        assert _is(edge, "fieldEdge") and not _is(edge, "border")
        assert _is(line, "border") and not _is(line, "fieldEdge")


class TestCardIconHover:
    """카드 행 아이콘 버튼(role=icon) 호버는 면만 — 테두리 투명(바 버튼 규칙과 같다, #611)."""

    def test_hovered_card_icon_button_has_a_face_and_no_border(
        self, window: Callable[..., VodDownloader]
    ) -> None:
        """포인터가 카드의 ✎ 버튼 위에 오면 면은 surfaceHover이고 위 · 왼쪽 가장자리가 borderStrong이 아니어야 한다.

        대기 카드 하나, 창에 합성 포인터 이동(카드 가운데 -> ✎ 가운데) -> underMouse, 왼쪽 안쪽 3px ≈ surfaceHover,
        위 가장자리 안쪽 2px 가운데 ≈ surfaceHover, 위 가장자리 가운데 · 왼쪽 가장자리 가운데 ≉ borderStrong
        """
        from PySide6.QtTest import QTest

        win = window(DownloadState.WAITING)
        card = _card(win, 0)
        QTest.mouseMove(win.windowHandle(), QPoint(1, 1))  # 앞 테스트의 포인터 자리와 다르게
        QTest.mouseMove(win.windowHandle(), _rect(win, card).center())
        _pump()
        button = card.editButton
        assert button.isVisible(), "전제: 카드 버튼이 보인다"
        rect = _rect(win, button)
        QTest.mouseMove(win.windowHandle(), rect.center())
        _pump()
        assert button.underMouse(), "전제: 포인터가 버튼 위다"
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "surfaceHover")
        assert _is(_pixel(image, rect.center().x(), rect.top() + 2), "surfaceHover")
        assert not _is(_pixel(image, rect.center().x(), rect.top()), "borderStrong")
        assert not _is(_pixel(image, rect.left(), rect.center().y()), "borderStrong")


FIELDS = ["urlInput", "sortButton", "combo"]  # URL 칸 · 펼침 칸 · 콤보(설정 창 등의 공용 선택 칸)


@pytest.fixture
def field(window: Callable[..., VodDownloader]) -> Callable[[str], tuple[QWidget, QWidget]]:
    """이름으로 입력 칸과 그 칸을 그릴 창을 돌려주는 함수 — 「combo」는 전역 QSS 아래 콤보 하나를 띄운다."""
    from PySide6.QtWidgets import QComboBox, QVBoxLayout

    held: list[QWidget] = []

    def build(name: str) -> tuple[QWidget, QWidget]:
        if name != "combo":
            win = window(DownloadState.WAITING)
            return win, getattr(win, name)
        from PySide6.QtWidgets import QPushButton

        holder = QWidget()
        held.append(holder)
        layout = QVBoxLayout(holder)
        # 창이 처음 받는 초점을 콤보 앞 버튼이 갖는다 — 콤보가 초점(아래 변 강조색)을 받지 않은 평소 모양을 잰다
        layout.addWidget(QPushButton("x", holder))
        combo = QComboBox(holder)
        combo.addItems(["A", "B"])
        layout.addWidget(combo)
        holder.resize(300, 120)
        holder.show()
        _pump()
        return holder, combo

    yield build
    for holder in held:
        holder.hide()


class TestDisabledFields:
    """꺼진 입력 칸 테두리는 네 변 옅은 border, 켜진 칸은 fieldEdge 그대로(#611 176f · 176h)."""

    @pytest.mark.parametrize("name", FIELDS)
    def test_enabled_field_keeps_the_field_border(
        self, field: Callable[[str], tuple[QWidget, QWidget]], name: str
    ) -> None:
        """켜진 입력 칸의 아래 가장자리는 fieldEdge이고 border가 아니어야 한다.

        URL 칸 · 정렬 칸 · 콤보, 켜짐 -> 아래 가장자리 가운데 ≈ fieldEdge · ≉ border
        """
        host, widget = field(name)
        assert widget.isVisible() and widget.isEnabled(), "전제: 보이고 켜져 있다"
        rect = _rect(host, widget)
        edge = _pixel(host.grab().toImage(), rect.center().x(), rect.bottom())
        assert _is(edge, "fieldEdge")
        assert not _is(edge, "border")

    @pytest.mark.parametrize("name", FIELDS)
    def test_disabled_field_has_the_faint_border(
        self, field: Callable[[str], tuple[QWidget, QWidget]], name: str
    ) -> None:
        """꺼진 입력 칸의 위 · 아래 가장자리는 border이고 fieldEdge가 아니어야 한다.

        URL 칸 · 정렬 칸 · 콤보, setEnabled(False) -> 위 · 아래 가장자리 가운데 ≈ border · ≉ fieldEdge
        """
        host, widget = field(name)
        widget.setEnabled(False)
        _pump()
        assert widget.isVisible() and not widget.isEnabled(), "전제: 보이고 꺼져 있다"
        rect = _rect(host, widget)
        image = host.grab().toImage()
        for y in (rect.top(), rect.bottom()):
            edge = _pixel(image, rect.center().x(), y)
            assert _is(edge, "border"), y
            assert not _is(edge, "fieldEdge"), y

    @pytest.mark.parametrize("enabled", [True, False])
    def test_combo_text_is_dimmed_only_when_disabled(
        self, field: Callable[[str], tuple[QWidget, QWidget]], enabled: bool
    ) -> None:
        """콤보 글은 켜졌을 때 text, 꺼졌을 때 textDisabled 색이어야 한다(#611 176g).

        콤보 글 「█」(글꼴 크기 28px — 획 가운데 화소가 바탕과 섞이지 않게), 켬 · setEnabled(False)
        -> 칸 안쪽(테두리 · 화살표 칸 밖)에서 fieldBg와 가장 먼 화소 ≈ text(켬) · textDisabled(끔)
        """
        _, combo = field("combo")
        font = combo.font()
        font.setPixelSize(28)
        combo.setFont(font)
        combo.setItemText(0, "█")
        combo.setFixedHeight(48)
        combo.setEnabled(enabled)
        _pump()
        assert combo.isVisible() and combo.isEnabled() == enabled, "전제: 보이고 켜짐 · 꺼짐이 맞다"
        image = combo.grab().toImage()
        ground = QColor(theme.current_tokens()["fieldBg"])

        def gap(colour: QColor) -> int:
            return (
                abs(colour.red() - ground.red())
                + abs(colour.green() - ground.green())
                + abs(colour.blue() - ground.blue())
            )

        inside = [
            _pixel(image, x, y)
            for x in range(4, combo.width() - 24)  # 왼쪽 테두리 · 오른쪽 화살표 칸(20) 밖
            for y in range(4, combo.height() - 4)
        ]
        ink = max(inside, key=gap)
        assert _is(ink, "text" if enabled else "textDisabled"), ink.name()


FOCUS_PAD = 4  # 초점 상자 ↔ 탭 내용(글 · 버블) 좌우 간격(px) — 지시 값(#611 176f)
BUBBLE_STATE = "done"  # 버블 색(stateFinished)이 강조색과 달라 상자 화소와 섞이지 않는 상태


def _focus(win: VodDownloader, tab: QWidget, qtbot) -> None:
    win.activateWindow()
    qtbot.waitUntil(win.isActiveWindow, timeout=3000)
    tab.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(tab.hasFocus, timeout=3000)
    _pump()


def _box_and_ink(
    win: VodDownloader, tab: QWidget
) -> tuple[list[int], list[int], list[tuple[int, int]]]:
    """탭 세로 가운데 줄의 강조색 열 · 버블 밖 글 화소 열 · 버블 화소 (x, y)를 창 좌표로 센다."""
    image = win.grab().toImage()
    rect = _rect(win, tab)
    bubble = _rect(win, tab.bubble) if tab.bubble.isVisible() else QRect()
    face = QColor(theme.current_tokens()["barBg"])
    box = [
        x
        for x in range(rect.left(), rect.right() + 1)
        if _is(_pixel(image, x, rect.center().y()), "accent")
    ]
    text = [
        x
        for x in range(rect.left(), rect.right() + 1)
        # 초점 상자의 둥근 모서리(위아래 끝에서 6줄 안) 밖 — 모서리 안티에일리어싱 화소를 글로 세지 않는다
        for y in range(rect.top() + 8, rect.bottom() - 8)
        if not bubble.contains(x, y)
        and not _is(_pixel(image, x, y), "barBg")
        and not _is(_pixel(image, x, y), "accent")
    ]
    dots = [
        (x, y)
        for x in range(bubble.left(), bubble.right() + 1)
        for y in range(bubble.top(), bubble.bottom() + 1)
        if _pixel(image, x, y) != face  # 원 가장자리의 옅은 안티에일리어싱 화소까지 버블로 센다
    ]
    return box, text, dots


def _box_rows(win: VodDownloader, tab: QWidget, x: int) -> list[int]:
    """열 x에서 밑줄 자리 위의 강조색 줄(상자 위 · 아래 변)을 창 좌표로 센다."""
    image = win.grab().toImage()
    rect = _rect(win, tab)
    return [
        y
        for y in range(rect.top(), rect.bottom() - UNDERLINE + 1)
        if _is(_pixel(image, x, y), "accent")
    ]


class TestMenuTabFocusBox:
    """탭 키보드 초점 상자는 탭 내용(글 + 버블)을 둘러싼다 — 오른쪽 변은 버블이 있으면 버블, 없으면 글 + 4(#611 176f)."""

    def test_focus_box_ends_four_past_the_text_without_a_bubble(
        self, window: Callable[..., VodDownloader], qtbot
    ) -> None:
        """버블이 없으면 초점 상자 좌우 변 사이가 글 진행 폭 + 4 + 4이고 글 화소를 둘러싸야 한다.

        버블 없음, 「VOD 다운」에 Tab 초점 -> 세로 가운데 줄 accent 맨 오른쪽 − 맨 왼쪽 + 1
        == QFontMetrics.horizontalAdvance(탭 글) + 8, accent 맨 왼쪽 < 글 화소 맨 왼쪽, 맨 오른쪽 > 글 화소 맨 오른쪽
        """
        from PySide6.QtGui import QFontMetrics

        win = window(DownloadState.WAITING)
        tab = win.menuDownloadButton
        assert tab.bubble.state() == "" and not tab.bubble.isVisible(), "전제: 버블이 없다"
        _focus(win, tab, qtbot)
        box, text, _ = _box_and_ink(win, tab)
        assert box and text, "전제: 상자 · 글 화소가 있다"
        # 글 화소 끝은 글꼴마다 진행 폭 끝과 다르다(빈 글꼴 폴더에서 4px 차) — 폭은 진행 폭으로 잰다
        advance = QFontMetrics(tab.font()).horizontalAdvance(tab.text())
        assert max(box) - min(box) + 1 == advance + 2 * FOCUS_PAD
        assert min(box) < min(text) and max(box) > max(text)

    def test_focus_box_wraps_the_bubble(self, window: Callable[..., VodDownloader], qtbot) -> None:
        """버블이 보이면 초점 상자 오른쪽 변이 버블 화소 오른쪽 끝 + 4이고 버블 화소가 모두 상자 안이어야 한다.

        setBubble("done"), 「VOD 다운」에 Tab 초점 -> 세로 가운데 줄 accent 맨 오른쪽 == 버블 화소 맨 오른쪽 + 4,
        accent 맨 왼쪽 < 글 화소 맨 왼쪽, 버블 가운데 열의 상자 위 변 < 버블 화소 맨 위 · 아래 변 > 맨 아래,
        상자 좌우 변 사이에 버블 화소 열이 모두 있다
        """
        win = window(DownloadState.WAITING)
        tab = win.menuDownloadButton
        tab.setBubble(BUBBLE_STATE)
        _pump()
        assert tab.bubble.isVisible(), "전제: 버블이 보인다"
        _focus(win, tab, qtbot)
        box, text, dots = _box_and_ink(win, tab)
        assert box and text and dots, "전제: 상자 · 글 · 버블 화소가 있다"
        xs = [x for x, _ in dots]
        ys = [y for _, y in dots]
        assert max(box) == max(xs) + FOCUS_PAD
        assert min(box) < min(text) and min(box) < min(xs)
        rows = _box_rows(win, tab, (min(xs) + max(xs)) // 2)
        assert rows, "전제: 버블 가운데 열에 상자 위 · 아래 변이 있다"
        assert min(rows) < min(ys) and max(rows) > max(ys)

    def test_focus_box_follows_the_bubble_coming_and_going(
        self, window: Callable[..., VodDownloader], qtbot
    ) -> None:
        """초점이 있는 채 버블이 나타나면 상자가 버블까지 늘고 사라지면 처음 폭으로 돌아와야 한다.

        「VOD 다운」에 Tab 초점(버블 없음) -> setBubble("done") -> setBubble(""):
        accent 맨 오른쪽이 처음 < 버블 화소 맨 오른쪽 + 4 == 버블 있을 때, 버블을 지운 뒤 == 처음
        """
        win = window(DownloadState.WAITING)
        tab = win.menuDownloadButton
        _focus(win, tab, qtbot)
        before, _, _ = _box_and_ink(win, tab)
        tab.setBubble(BUBBLE_STATE)
        _pump()
        assert tab.bubble.isVisible() and tab.hasFocus(), "전제: 버블이 보이고 초점이 남아 있다"
        shown, _, dots = _box_and_ink(win, tab)
        tab.setBubble("")
        _pump()
        assert not tab.bubble.isVisible() and tab.hasFocus(), "전제: 버블이 숨고 초점이 남아 있다"
        after, _, _ = _box_and_ink(win, tab)
        assert before and shown and after and dots, "전제: 상자 · 버블 화소가 있다"
        assert max(shown) == max(x for x, _ in dots) + FOCUS_PAD
        assert max(before) < max(shown)
        assert max(after) == max(before)


BARS = ["appMenuBar", "headerFrame", "infoFrame"]  # 메뉴바 · 헤더바 · 하단바


def _empty_spot(win: VodDownloader, bar: QWidget) -> QPoint:
    """바 세로 가운데 줄에서 바 자신이 누름을 받는 자리(그 위에 자식 위젯이 없는 곳)의 창 좌표."""
    assert bar.isVisible(), "전제: 바가 보인다"
    y = bar.height() // 2
    for x in range(bar.width() - 1, -1, -1):
        if bar.childAt(QPoint(x, y)) is None:
            return bar.mapTo(win, QPoint(x, y))
    raise AssertionError("전제: 바에 빈 곳이 있다")


def _press_window(win: VodDownloader, spot: QPoint) -> None:
    """창(`QWindow`)에 왼쪽 버튼 누름 · 뗌 합성 이벤트를 보낸다 — 실제 커서는 옮기지 않는다."""
    from PySide6.QtTest import QTest

    QTest.mouseClick(win.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier(0), spot)
    _pump()


class TestBarEmptyPressFocusesList:
    """바의 빈 곳을 누르면 입력 칸 등의 초점이 풀리고 카드 목록이 초점을 받는다(#611 176g)."""

    @pytest.mark.parametrize("name", BARS)
    def test_pressing_an_empty_spot_moves_focus_from_the_url_field_to_the_list(
        self, window: Callable[..., VodDownloader], qtbot, name: str
    ) -> None:
        """URL 칸에 초점이 있을 때 바의 빈 곳을 누르면 카드 목록이 초점을 받아야 한다.

        대기 카드 둘, URL 칸에 초점 -> 메뉴바 · 헤더바 · 하단바의 빈 곳(자식 위젯 없는 자리)을 누름
        -> 초점 위젯 == 카드 목록, URL 칸 초점 없음
        """
        win = window(DownloadState.WAITING, DownloadState.WAITING)
        _focus(win, win.urlInput, qtbot)
        _press_window(win, _empty_spot(win, getattr(win, name)))
        qtbot.waitUntil(win.listView.hasFocus, timeout=3000)
        assert not win.urlInput.hasFocus()

    def test_pressing_the_url_field_keeps_its_focus(
        self, window: Callable[..., VodDownloader], qtbot
    ) -> None:
        """초점을 받는 위젯(URL 칸)을 누르면 초점이 그 위젯에 가야 한다.

        대기 카드 하나, 목록에 초점 -> URL 칸 가운데를 누름 -> 초점 위젯 == URL 칸
        """
        win = window(DownloadState.WAITING)
        _focus(win, win.listView, qtbot)
        _press_window(win, _rect(win, win.urlInput).center())
        qtbot.waitUntil(win.urlInput.hasFocus, timeout=3000)

    def test_picks_and_the_focused_card_stay_after_pressing_an_empty_spot(
        self, window: Callable[..., VodDownloader], qtbot
    ) -> None:
        """선택 모드에서 고른 카드 · 초점 카드가 있을 때 바의 빈 곳을 누르면 둘 다 그대로여야 한다.

        대기 카드 셋, 선택 모드 · 첫째 · 셋째 고름 · 카드에 Tab 초점 -> 헤더바 빈 곳 누름
        -> 고른 카드 = 첫째 · 셋째, 초점 위젯 == 누르기 전 초점 카드
        """
        win = window(DownloadState.WAITING, DownloadState.WAITING, DownloadState.WAITING)
        view = win.listView
        view.setSelecting(True)
        for row in (0, 2):
            view.cardPicked(_card(win, row), Qt.KeyboardModifier.NoModifier)
        picked = view.selectedItems()
        assert len(picked) == 2, "전제: 둘을 골랐다"
        second = _card(win, 1)

        def focused() -> bool:
            if not win.isActiveWindow():
                win.activateWindow()
                return False
            second.setFocus(Qt.FocusReason.TabFocusReason)
            return isinstance(QApplication.focusWidget(), ContentItemWidget)

        qtbot.waitUntil(focused, timeout=3000)
        card = QApplication.focusWidget()
        _press_window(win, _empty_spot(win, win.headerFrame))
        assert view.selectedItems() == picked
        assert QApplication.focusWidget() is card


def _edges(host: QWidget, widget: QWidget) -> dict[str, QColor]:
    """입력 칸 네 변의 가운데 화소(모서리 둥글림 밖) — 위 · 왼쪽 · 오른쪽 · 아래 · 아래 안쪽 한 줄."""
    rect = _rect(host, widget)
    image = host.grab().toImage()
    x, y = rect.center().x(), rect.center().y()
    return {
        "top": _pixel(image, x, rect.top()),
        "left": _pixel(image, rect.left(), y),
        "right": _pixel(image, rect.right(), y),
        "bottom": _pixel(image, x, rect.bottom()),
        "above_bottom": _pixel(image, x, rect.bottom() - 1),
    }


def _activate_and_focus(host: QWidget, widget: QWidget, qtbot) -> None:
    def focused() -> bool:
        if not host.isActiveWindow():
            host.activateWindow()
            return False
        widget.setFocus(Qt.FocusReason.TabFocusReason)
        return widget.hasFocus()

    qtbot.waitUntil(focused, timeout=3000)
    _pump()


def _text_place(widget: QWidget) -> tuple[int, QRect]:
    """칸 높이와 글이 놓이는 사각형 — QLineEdit은 글 자리(SE_LineEditContents), 콤보는 편집 칸, 펼침 칸은 글 칸."""
    from PySide6.QtWidgets import (
        QComboBox,
        QLineEdit,
        QStyle,
        QStyleOptionComboBox,
        QStyleOptionFrame,
    )

    if isinstance(widget, QLineEdit):
        option = QStyleOptionFrame()
        widget.initStyleOption(option)
        place = widget.style().subElementRect(QStyle.SubElement.SE_LineEditContents, option, widget)
    elif isinstance(widget, QComboBox):
        option = QStyleOptionComboBox()
        widget.initStyleOption(option)
        place = widget.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            widget,
        )
    else:
        place = widget._textRect()
    return widget.height(), place


class TestFieldEdges:
    """입력 칸 테두리는 네 변 같은 1px — 평소 fieldEdge, 초점 · 펼친 동안 네 변 강조색 1px(#611 176h)."""

    @pytest.mark.parametrize("name", FIELDS)
    def test_all_four_edges_are_the_field_edge_at_rest(
        self, field: Callable[[str], tuple[QWidget, QWidget]], name: str
    ) -> None:
        """평소 입력 칸은 위 · 왼쪽 · 오른쪽 · 아래 변이 모두 fieldEdge여야 한다.

        URL 칸 · 정렬 칸 · 콤보, 켜짐 · 초점 없음 -> 네 변 가운데 ≈ fieldEdge, 아래 바로 위 줄 ≈ fieldBg
        """
        host, widget = field(name)
        assert widget.isVisible() and not widget.hasFocus(), "전제: 보이고 초점이 없다"
        edges = _edges(host, widget)
        for side in ("top", "left", "right", "bottom"):
            assert _is(edges[side], "fieldEdge"), (side, edges[side].name())
        assert _is(edges["above_bottom"], "fieldBg")

    @pytest.mark.parametrize("name", FIELDS)
    def test_focus_draws_a_one_pixel_accent_on_all_four_edges(
        self, field: Callable[[str], tuple[QWidget, QWidget]], qtbot, name: str
    ) -> None:
        """초점을 받은 입력 칸은 네 변이 accent 1px여야 한다 — 아래 2px 막대는 없다.

        URL 칸 · 정렬 칸 · 콤보에 Tab 초점 -> 네 변 가운데 ≈ accent, 아래 바로 위 줄 ≈ fieldBg
        """
        host, widget = field(name)
        _activate_and_focus(host, widget, qtbot)
        edges = _edges(host, widget)
        for side in ("top", "left", "right", "bottom"):
            assert _is(edges[side], "accent"), (side, edges[side].name())
        assert _is(edges["above_bottom"], "fieldBg")

    @pytest.mark.parametrize("name", FIELDS)
    def test_focus_keeps_the_height_and_the_text_place(
        self, field: Callable[[str], tuple[QWidget, QWidget]], qtbot, name: str
    ) -> None:
        """초점을 받아도 칸 높이와 글이 놓이는 자리가 초점 전과 같아야 한다.

        URL 칸 · 정렬 칸 · 콤보, 초점 전 (높이, 글 자리 사각형) == Tab 초점 뒤 (높이, 글 자리 사각형)
        """
        host, widget = field(name)
        before = _text_place(widget)
        _activate_and_focus(host, widget, qtbot)
        assert widget.hasFocus(), "전제: 초점을 받았다"
        assert _text_place(widget) == before

    def test_open_combo_has_the_focus_edges(
        self, field: Callable[[str], tuple[QWidget, QWidget]], qtbot
    ) -> None:
        """펼친 콤보는 초점과 같게 네 변이 accent 1px여야 한다.

        콤보 showPopup() -> 위 · 아래 가운데 ≈ accent, 아래 바로 위 줄 ≈ fieldBg
        """
        host, combo = field("combo")
        _activate_and_focus(host, combo, qtbot)
        combo.showPopup()
        qtbot.waitUntil(lambda: combo.view().isVisible(), timeout=3000)
        try:
            edges = _edges(host, combo)
            assert _is(edges["bottom"], "accent")
            assert _is(edges["top"], "accent")
            assert _is(edges["above_bottom"], "fieldBg")
        finally:
            combo.hidePopup()
            _pump()


@pytest.fixture(params=["dark", "light"])
def scheme(request, qapp: QApplication) -> Iterator[str]:
    """그 색 체계의 전역 QSS · 팔레트를 세우고 끝나면 다크로 되돌린다."""

    def apply(name: str) -> None:
        theme.set_color_scheme(name)
        qapp.setPalette(theme.build_palette())
        qapp.setStyleSheet(
            theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH))
        )

    apply(request.param)
    yield request.param
    apply("dark")


BAR_FRAMES = ["appMenuBar", "headerFrame", "infoFrame"]  # 메뉴바 · 헤더바 · 하단바


class TestBarFaces:
    """바 면은 카드 면과 한 단계 다른 barBg, 입력 칸 면은 fieldBg(라이트 흰색)다(#611 176i)."""

    @pytest.mark.parametrize("name", BAR_FRAMES)
    def test_bar_face_is_bar_bg_and_not_the_card_face(self, window, scheme: str, name: str) -> None:
        """바의 빈 자리는 barBg이고 같은 창 카드 면(cardBg)과 달라야 한다.

        대기 카드 하나, 창 폭 900 -> 바 왼쪽 끝 2px · 세로 가운데 ≈ barBg, 카드 왼쪽 안쪽 가운데 ≈ cardBg
        (채널마다 1 이내), 바 화소 ≉ cardBg
        """
        win = window(DownloadState.WAITING)
        image = win.grab().toImage()
        bar = _rect(win, getattr(win, name))
        card = _rect(win, _card(win, 0))
        face = _pixel(image, bar.left() + 2, bar.center().y())
        tokens = theme.current_tokens()
        card_face = _pixel(image, card.left() + 2, card.center().y())
        assert _near(card_face, tokens["cardBg"]), f"전제: 카드 면 {card_face.name()}"
        assert _near(face, tokens["barBg"]), f"{scheme} {name}: {face.name()}"
        # 다크 바 면 ↔ 카드 면은 채널 차 합 14(#611 176r)라 SLACK(24)로는 가릴 수 없다
        assert not _near(face, tokens["cardBg"]), f"{scheme} {name}: {face.name()}"

    @pytest.mark.parametrize("name", ["urlInput", "sortButton"])
    def test_field_face_is_field_bg(self, window, scheme: str, name: str) -> None:
        """바 위 입력 칸의 면은 fieldBg여야 한다 — 라이트는 흰색이라 바 면 · surfaceAlt와 다르다.

        창 폭 900 -> 칸 왼쪽 안쪽 3px · 세로 가운데 ≈ fieldBg, 라이트에서는 surfaceAlt · barBg가 아니다
        """
        win = window(DownloadState.WAITING)
        image = win.grab().toImage()
        field = _rect(win, getattr(win, name))
        face = _pixel(image, field.left() + 3, field.center().y())
        assert _is(face, "fieldBg"), f"{scheme} {name}: {face.name()}"
        if scheme == "light":
            assert not _is(face, "surfaceAlt"), face.name()
            assert not _is(face, "barBg"), face.name()

    def test_chosen_chip_face_is_field_bg(self, window, scheme: str) -> None:
        """고른 칩(「전체」)의 면은 fieldBg여야 한다 — 라이트는 흰색이라 바 면과 다르다.

        창 폭 = 최소폭 + 900(칩 모양) -> 고른 칩 왼쪽 안쪽 3px · 세로 가운데 ≈ fieldBg, 라이트에서는 barBg ·
        surfaceAlt가 아니다 (176j)
        """
        win = window(DownloadState.WAITING)
        win.resize(win.minimumWidth() + 900, 600)
        _pump()
        chip = win.filterChips["all"]
        assert chip.isVisible() and chip.isChecked(), "전제: 칩 모양 · 고른 칩"
        image = win.grab().toImage()
        rect = _rect(win, chip)
        face = _pixel(image, rect.left() + 3, rect.center().y())
        assert _is(face, "fieldBg"), f"{scheme}: {face.name()}"
        if scheme == "light":
            assert not _is(face, "barBg"), face.name()
            assert not _is(face, "surfaceAlt"), face.name()

    def test_select_button_on_face_is_field_bg(self, window, scheme: str) -> None:
        """선택 모드에서 켠 선택 버튼의 면은 fieldBg여야 한다 — 라이트는 흰색이라 바 면과 다르다.

        대기 카드 하나, 선택 버튼 누름 -> 버튼 왼쪽 안쪽 3px · 세로 가운데 ≈ fieldBg, 라이트에서는 barBg ·
        surfaceAlt가 아니다 (176j)
        """
        win = window(DownloadState.WAITING)
        win.selectButton.click()
        _pump()
        button = win.selectButton
        assert button.isVisible() and button.property("on") is True, "전제: 선택 모드"
        image = win.grab().toImage()
        rect = _rect(win, button)
        face = _pixel(image, rect.left() + 3, rect.center().y())
        assert _is(face, "fieldBg"), f"{scheme}: {face.name()}"
        if scheme == "light":
            assert not _is(face, "barBg"), face.name()
            assert not _is(face, "surfaceAlt"), face.name()


def _hover(win: VodDownloader, widget: QWidget, fresh: bool = True) -> QRect:
    """창에 합성 포인터 이동으로 위젯 가운데에 올리고 그 자리(창 좌표)를 돌려준다.

    fresh면 먼저 창 왼쪽 위 (1, 1)로 옮긴다(앞 테스트의 포인터 자리와 다르게). 카드 위에서 카드 버튼으로 옮길
    때는 False — 카드를 벗어나면 버튼이 숨는다.
    """
    from PySide6.QtTest import QTest

    if fresh:
        QTest.mouseMove(win.windowHandle(), QPoint(1, 1))
    rect = _rect(win, widget)
    QTest.mouseMove(win.windowHandle(), rect.center())
    _pump()
    assert widget.underMouse(), "전제: 포인터가 위젯 위다"
    return rect


def _ink_is(win: VodDownloader, rect: QRect, token: str) -> bool:
    """칩 자리(창 좌표)에서 면과 가장 먼 화소(글 잉크 한가운데)가 text · stateFailed 가운데 token에 더 가까운지 본다.

    면은 왼쪽 안쪽 3px · 세로 가운데 화소다. 테두리 2px는 뺀다.
    """
    image = win.grab().toImage()
    face = _pixel(image, rect.left() + 3, rect.center().y())

    def gap(a: QColor, b: QColor) -> int:
        return abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue())

    ink = max(
        (
            _pixel(image, x, y)
            for x in range(rect.left() + 2, rect.right() - 1)
            for y in range(rect.top() + 2, rect.bottom() - 1)
        ),
        key=lambda colour: gap(colour, face),
    )
    tokens = theme.current_tokens()
    other = "stateFailed" if token == "text" else "text"
    return gap(ink, QColor(tokens[token])) < gap(ink, QColor(tokens[other]))


class TestBarHoverFaces:
    """바 위 동작 버튼의 호버 · 눌림 면은 바 전용 barHover · barPressed, 카드 버튼은 surfaceHover 그대로(176k)."""

    @pytest.mark.parametrize("name", ["settingsButton", "fetchButton", "selectButton"])
    def test_bar_button_hover_face_is_bar_hover(self, window, scheme: str, name: str) -> None:
        """바 동작 버튼 호버 면은 barHover이고 surfaceHover가 아니어야 한다.

        대기 카드 하나, URL 칸에 글(「+」 켬), 합성 포인터 이동(버튼 가운데) -> 왼쪽 안쪽 3px · 세로 가운데 ≈
        barHover, ≉ surfaceHover
        """
        win = window(DownloadState.WAITING)
        win.urlInput.setText("https://chzzk.naver.com/video/1")
        _pump()
        rect = _hover(win, getattr(win, name))
        face = _pixel(win.grab().toImage(), rect.left() + 3, rect.center().y())
        assert _is(face, "barHover"), f"{scheme} {name}: {face.name()}"
        assert not _is(face, "surfaceHover"), f"{scheme} {name}: {face.name()}"

    def test_unchosen_chip_hover_face_is_bar_hover(self, window, scheme: str) -> None:
        """안 고른 칩(「대기」) 호버 면은 barHover이고 surfaceHover가 아니어야 한다.

        창 폭 = 최소폭 + 900(칩 모양), 합성 포인터 이동(「대기」 가운데) -> 왼쪽 안쪽 3px · 세로 가운데 ≈ barHover,
        ≉ surfaceHover
        """
        win = window(DownloadState.WAITING)
        win.resize(win.minimumWidth() + 900, 600)
        _pump()
        chip = win.filterChips["waiting"]
        assert chip.isVisible() and not chip.isChecked(), "전제: 칩 모양 · 안 고른 칩"
        rect = _hover(win, chip)
        face = _pixel(win.grab().toImage(), rect.left() + 3, rect.center().y())
        assert _is(face, "barHover"), f"{scheme}: {face.name()}"
        assert not _is(face, "surfaceHover"), f"{scheme}: {face.name()}"

    def test_pressed_bar_button_face_is_bar_pressed(self, window, scheme: str) -> None:
        """누르고 있는 바 동작 버튼(선택) 면은 barPressed이고 surfacePressed가 아니어야 한다.

        대기 카드 하나, 합성 포인터 이동 · 누름(선택 버튼 가운데) -> 왼쪽 안쪽 3px · 세로 가운데 ≈ barPressed,
        ≉ surfacePressed, 끝나면 뗀다
        """
        from PySide6.QtTest import QTest

        win = window(DownloadState.WAITING)
        button = win.selectButton
        rect = _hover(win, button)
        QTest.mousePress(
            win.windowHandle(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            rect.center(),
        )
        _pump()
        try:
            assert button.isDown(), "전제: 눌린 채다"
            face = _pixel(win.grab().toImage(), rect.left() + 3, rect.center().y())
            assert _is(face, "barPressed"), f"{scheme}: {face.name()}"
            assert not _is(face, "surfacePressed"), f"{scheme}: {face.name()}"
        finally:
            QTest.mouseRelease(
                win.windowHandle(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                rect.center(),
            )
            _pump()

    def test_failed_chip_text_is_text_while_hovered_and_failed_after(
        self, window, scheme: str
    ) -> None:
        """실패 칩 글은 호버 · 눌림 동안 text, 포인터를 뗀 뒤 stateFailed여야 한다.

        실패 카드 하나(칩 alert), 창 폭 = 최소폭 + 900(칩 모양), 합성 포인터 이동 · 누름 · 뗌 · 칩 밖으로 이동 ->
        칩 안에서 면과 가장 먼 화소가 호버 · 눌림 때 stateFailed보다 text에 가깝고, 칩 밖으로 옮긴 뒤 text보다
        stateFailed에 가깝다
        """
        from PySide6.QtTest import QTest

        win = window(DownloadState.FAILED)
        win.resize(win.minimumWidth() + 900, 600)
        _pump()
        chip = win.filterChips["failed"]
        assert chip.isVisible() and chip.property("alert") is True, "전제: 칩 모양 · 실패 칩 강조"
        chip.blockSignals(True)  # 누름이 목록 거르기를 바꾸지 않게
        try:
            rect = _hover(win, chip)
            assert _ink_is(win, rect, "text"), f"{scheme} 호버"
            QTest.mousePress(
                win.windowHandle(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                rect.center(),
            )
            _pump()
            assert chip.isDown(), "전제: 눌린 채다"
            assert _ink_is(win, rect, "text"), f"{scheme} 눌림"
            QTest.mouseRelease(
                win.windowHandle(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                rect.center(),
            )
            QTest.mouseMove(win.windowHandle(), QPoint(1, 1))
            _pump()
            assert not chip.underMouse(), "전제: 포인터가 칩 밖이다"
            assert _ink_is(win, rect, "stateFailed"), f"{scheme} 뗀 뒤"
        finally:
            chip.blockSignals(False)

    def test_card_button_hover_face_stays_surface_hover(self, window, scheme: str) -> None:
        """카드 ✎ 버튼 호버 면은 surfaceHover 그대로이고 barHover가 아니어야 한다.

        대기 카드 하나, 합성 포인터 이동(카드 가운데 -> ✎ 가운데) -> 왼쪽 안쪽 3px · 세로 가운데 ≈ surfaceHover,
        ≉ barHover
        """
        win = window(DownloadState.WAITING)
        card = _card(win, 0)
        _hover(win, card)
        button = card.editButton
        assert button.isVisible(), "전제: 카드 버튼이 보인다"
        rect = _hover(win, button, fresh=False)
        face = _pixel(win.grab().toImage(), rect.left() + 3, rect.center().y())
        assert _is(face, "surfaceHover"), f"{scheme}: {face.name()}"
        assert not _is(face, "barHover"), f"{scheme}: {face.name()}"


class TestLightLayers:
    """라이트 층 순서 — 바디 = 바 면, 그 위 흰 카드, 고른 카드는 바 면보다 한 단계 어둡게(#611 176m · 176q)."""

    @pytest.mark.parametrize("scheme", ["light"], indirect=True)
    def test_body_is_the_bar_face_and_the_card_is_white(self, window, scheme: str) -> None:
        """라이트 목록의 빈 자리는 바 면, 카드 면은 흰색이어야 한다.

        대기 카드 하나, 창 폭 900 · 높이 600 -> 목록 아래 빈 자리 가운데 ≈ 헤더바 빈 자리 화소 · barBg,
        카드 왼쪽 안쪽 2px · 세로 가운데 ≈ 흰색
        """
        win = window(DownloadState.WAITING)
        image = win.grab().toImage()
        listing = _rect(win, win.listView)
        card = _rect(win, _card(win, 0))
        bar = _rect(win, win.headerFrame)
        assert card.bottom() < listing.bottom() - 20, "전제: 카드 아래 빈 자리가 있다"
        body = _pixel(image, listing.center().x(), listing.bottom() - 10)
        bar_face = _pixel(image, bar.left() + 2, bar.center().y())
        assert _is(bar_face, "barBg"), f"전제: 바 면 {bar_face.name()}"
        assert body == bar_face, f"바디 {body.name()} · 바 {bar_face.name()}"
        face = _pixel(image, card.left() + 2, card.center().y())
        assert face == QColor(Qt.GlobalColor.white), face.name()

    @pytest.mark.parametrize("scheme", ["light"], indirect=True)
    def test_picked_card_is_one_step_darker_than_the_bar(self, window, scheme: str) -> None:
        """라이트 고른 카드 면은 cardSelectedBg이고 바 면과 ΔE00 3 이상 떨어져야 한다(#611 176q).

        대기 카드 하나, 선택 모드, 합성 클릭으로 고름 -> 포인터 (1, 1) -> 카드 왼쪽 안쪽 6px · 세로 가운데 ≈
        cardSelectedBg, ΔE00(그 화소, 헤더바 빈 자리 화소) >= 3, 그 화소가 바 화소보다 어둡다
        """
        from PySide6.QtTest import QTest

        win = window(DownloadState.WAITING)
        rect = _pick_first(win)
        QTest.mouseMove(win.windowHandle(), QPoint(1, 1))
        _pump()
        image = win.grab().toImage()
        bar = _rect(win, win.headerFrame)
        bar_face = _pixel(image, bar.left() + 2, bar.center().y())
        face = _pixel(image, rect.left() + BAND_CLEAR, rect.center().y())
        assert _is(face, "cardSelectedBg"), face.name()
        step = theme.delta_e00(face.name(), bar_face.name())
        assert step >= 3.0, f"고름 {face.name()} · 바 {bar_face.name()} = {step:.2f}"
        assert face.lightnessF() < bar_face.lightnessF()


LIGHT_BODY = (
    "#f2f3f6"  # 라이트 바디 · 바 면 — border 색상각 · 절반 채도, #f3f3f3 밝기 안(#611 176p 지시 값)
)
LIGHT_PICKED = "#e4e4e7"  # 라이트 고른 카드 면 — 176p 호버 면 값 그대로(#611 176q)
TINT_SLACK = 1  # 색조 화소를 지시 값과 같다고 보는 한 채널 차 — 16진 반올림
BLUE_OVER_RED = 3  # 푸른 회색이라고 보는 파랑 − 빨강 채널 차 하한 — 무채색이면 0


def _near(colour: QColor, value: str) -> bool:
    want = QColor(value)
    return all(
        abs(a - b) <= TINT_SLACK
        for a, b in (
            (colour.red(), want.red()),
            (colour.green(), want.green()),
            (colour.blue(), want.blue()),
        )
    )


class TestLightTint:
    """라이트 바디 · 바 · 고른 카드 면은 무채색이 아니라 border 색상각의 푸른 회색이다(#611 176o · 176q)."""

    @pytest.mark.parametrize("scheme", ["light"], indirect=True)
    def test_body_and_bar_are_the_blue_grey(self, window, scheme: str) -> None:
        """라이트 목록 빈 자리 · 헤더바 빈 자리 화소가 #f2f3f6이고 파랑이 빨강보다 3 이상 커야 한다.

        대기 카드 하나, 창 폭 900 · 높이 600 -> 목록 아래 빈 자리 가운데 · 헤더바 왼쪽 안쪽 2px ≈ #f2f3f6
        (채널마다 1 이내), 두 화소 모두 blue − red >= 3
        """
        win = window(DownloadState.WAITING)
        image = win.grab().toImage()
        listing = _rect(win, win.listView)
        bar = _rect(win, win.headerFrame)
        body = _pixel(image, listing.center().x(), listing.bottom() - 10)
        bar_face = _pixel(image, bar.left() + 2, bar.center().y())
        for name, colour in (("바디", body), ("바", bar_face)):
            assert _near(colour, LIGHT_BODY), f"{name} {colour.name()}"
            assert colour.blue() - colour.red() >= BLUE_OVER_RED, f"{name} {colour.name()}"

    @pytest.mark.parametrize("scheme", ["light"], indirect=True)
    def test_picked_card_is_the_blue_grey(self, window, scheme: str) -> None:
        """라이트 고른 카드 면 화소가 #e4e4e7이고 파랑이 빨강보다 3 이상 커야 한다(#611 176q).

        대기 카드 하나, 선택 모드, 합성 클릭으로 고름(포인터는 카드 위) -> 카드 왼쪽 안쪽 6px · 세로 가운데 ≈
        #e4e4e7(채널마다 1 이내), blue − red >= 3
        """
        win = window(DownloadState.WAITING)
        rect = _pick_first(win)
        face = _face_at(win, rect)
        assert _near(face, LIGHT_PICKED), face.name()
        assert face.blue() - face.red() >= BLUE_OVER_RED, face.name()


# 카드 면 — 호버 반 단계 · 고름 한 단계 중립 면(#611 176q 오너 결정: 선택 면 색 넣음, 강조색 없음)
PICKED = {
    DownloadState.WAITING: ("cardBg", "cardHoverBg", "cardSelectedBg"),
    DownloadState.FINISHED: (
        "cardTintFinished",
        "cardTintFinishedHover",
        "cardTintFinishedSelected",
    ),
    DownloadState.FAILED: ("cardTintFailed", "cardTintFailedHover", "cardTintFailedSelected"),
}  # 카드 상태 -> (평소 면 토큰, 호버 면 토큰, 선택 면 토큰)
PICK_STATES = [DownloadState.WAITING, DownloadState.FINISHED, DownloadState.FAILED]
DARK_FACES = {
    DownloadState.WAITING: ("#26282e", "#2d2f35", "#35363c"),
    DownloadState.FINISHED: ("#1f2c24", "#26332b", "#2d3a32"),
    DownloadState.FAILED: ("#362321", "#3d2a28", "#45312f"),
}  # 다크 카드 상태 -> (평소, 호버, 선택) 면 — 평소 면을 반 단계 내린 값(#611 176r)
BAND_CLEAR = 6  # 고른 카드 왼쪽 강조색 띠(4px) 밖에서 면을 재는 안쪽 거리(px)


def _pick_first(win: VodDownloader) -> QRect:
    """선택 모드를 켜고 첫 카드를 창에 합성 클릭으로 골라 그 카드 자리(창 좌표)를 돌려준다."""
    from PySide6.QtTest import QTest

    win.selectButton.click()
    _pump()
    assert win.listView.isSelecting(), "전제: 선택 모드다"
    card = _card(win, 0)
    rect = _rect(win, card)
    QTest.mouseClick(
        win.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center()
    )
    _pump()
    assert card.contentFrame.isSelected(), "전제: 첫 카드를 골랐다"
    return rect


def _face_at(win: VodDownloader, rect: QRect) -> QColor:
    return _pixel(win.grab().toImage(), rect.left() + BAND_CLEAR, rect.center().y())


class TestPickedFace:
    """고른 카드 면은 중립 선택 면(한 단계)이고 호버여도 그대로다 — 왼쪽 띠 · 체크 원도 있다(#611 176q)."""

    @pytest.mark.parametrize("scheme", ["dark", "light"], indirect=True)
    @pytest.mark.parametrize("state", PICK_STATES, ids=lambda s: s.name)
    def test_picked_face_is_the_selected_face(
        self, window, qtbot: QtBot, scheme: str, state: DownloadState
    ) -> None:
        """포인터가 밖에 있는 고른 카드 면 화소가 그 상태의 선택 면이고 띠 · 체크 원이 있어야 한다.

        카드 하나(대기 · 완료 · 실패), 선택 모드, 합성 클릭으로 고름 -> 포인터 (1, 1) -> 카드 왼쪽 안쪽 6px ·
        세로 가운데 ≈ PICKED 선택 면 토큰(채널마다 1 이내), 띠 애니메이션 끝 -> 왼쪽 끝 화소 ≈ accent,
        체크 원은 고른 모양
        """
        from PySide6.QtTest import QTest

        win = window(state)
        rect = _pick_first(win)
        QTest.mouseMove(win.windowHandle(), QPoint(1, 1))
        _pump()
        card = _card(win, 0)
        assert not card.isHovered(), "전제: 호버가 꺼졌다"
        qtbot.waitUntil(lambda: card.contentFrame.band() == 1.0)
        _rest, _hovered, selected = PICKED[state]
        tokens = theme.current_tokens()
        image = win.grab().toImage()
        face = _pixel(image, rect.left() + BAND_CLEAR, rect.center().y())
        assert _near(face, tokens[selected]), f"{scheme} {state.name}: {face.name()}"
        band = _pixel(image, rect.left(), rect.center().y())
        assert _near(band, tokens["accent"]), f"{scheme} {state.name} 띠: {band.name()}"
        assert card.thumbnailLabel.hasPickMark() and card.thumbnailLabel.isMarkPicked()

    @pytest.mark.parametrize("scheme", ["dark", "light"], indirect=True)
    @pytest.mark.parametrize("state", PICK_STATES, ids=lambda s: s.name)
    def test_picked_and_hovered_face_is_the_selected_face(
        self, window, scheme: str, state: DownloadState
    ) -> None:
        """포인터가 위에 있는 고른 카드 면 화소가 그 상태의 선택 면이어야 한다.

        카드 하나(대기 · 완료 · 실패), 선택 모드, 합성 클릭으로 고름 -> 합성 포인터 이동((1, 1) -> 카드 가운데)
        -> 카드 왼쪽 안쪽 6px · 세로 가운데 ≈ PICKED 선택 면 토큰(채널마다 1 이내)
        """
        win = window(state)
        rect = _pick_first(win)
        _hover(win, _card(win, 0))
        assert _card(win, 0).isHovered(), "전제: 호버가 켜졌다"
        _rest, _hovered, selected = PICKED[state]
        face = _face_at(win, rect)
        want = theme.current_tokens()[selected]
        assert _near(face, want), f"{scheme} {state.name}: {face.name()}"

    @pytest.mark.parametrize("scheme", ["dark", "light"], indirect=True)
    @pytest.mark.parametrize("state", PICK_STATES, ids=lambda s: s.name)
    def test_four_faces_of_one_card(self, window, scheme: str, state: DownloadState) -> None:
        """같은 카드의 평소 · 호버 · 선택 · 선택+호버 화소는 평소 ≠ 호버 ≠ 선택, 선택+호버 = 선택이어야 한다.

        카드 하나(대기 · 완료 · 실패) -> 포인터 (1, 1): 평소, 카드 가운데: 호버, 선택 모드에서 고르고
        (1, 1): 선택, 다시 카드 가운데: 선택+호버 — 카드 왼쪽 안쪽 6px · 세로 가운데 화소.
        각 화소 ≈ PICKED 토큰(채널마다 1 이내), 평소 ≉ 호버, 호버 ≉ 선택, 선택+호버 ≈ 선택
        """
        from PySide6.QtTest import QTest

        win = window(state)
        card = _card(win, 0)
        QTest.mouseMove(win.windowHandle(), QPoint(1, 1))
        _pump()
        rect = _rect(win, card)
        rest = _face_at(win, rect)
        _hover(win, card)
        assert card.isHovered(), "전제: 호버가 켜졌다"
        hovered = _face_at(win, rect)
        assert _pick_first(win) == rect, "전제: 선택 모드에서 카드 자리가 그대로다"
        QTest.mouseMove(win.windowHandle(), QPoint(1, 1))
        _pump()
        assert not card.isHovered(), "전제: 호버가 꺼졌다"
        selected = _face_at(win, rect)
        _hover(win, card)
        assert card.isHovered(), "전제: 호버가 다시 켜졌다"
        both = _face_at(win, rect)
        seen = (
            f"{scheme} {state.name}: {rest.name()} {hovered.name()} {selected.name()} {both.name()}"
        )
        tokens = theme.current_tokens()
        for colour, token in zip((rest, hovered, selected), PICKED[state], strict=True):
            assert _near(colour, tokens[token]), f"{token} {seen}"
        assert not _near(rest, hovered.name()), seen
        assert not _near(hovered, selected.name()), seen
        assert _near(both, selected.name()), seen

    @pytest.mark.parametrize("scheme", ["dark"], indirect=True)
    @pytest.mark.parametrize("state", PICK_STATES, ids=lambda s: s.name)
    def test_dark_faces_are_the_176r_values(
        self, window, scheme: str, state: DownloadState
    ) -> None:
        """다크 카드의 평소 · 호버 · 선택 화소는 DARK_FACES 값이어야 한다.

        다크, 카드 하나(대기 · 완료 · 실패) -> 포인터 (1, 1): 평소, 카드 가운데: 호버, 선택 모드에서 고르고
        (1, 1): 선택 — 카드 왼쪽 안쪽 6px · 세로 가운데 화소 ≈ DARK_FACES(채널마다 1 이내)
        """
        from PySide6.QtTest import QTest

        win = window(state)
        card = _card(win, 0)
        QTest.mouseMove(win.windowHandle(), QPoint(1, 1))
        _pump()
        rect = _rect(win, card)
        rest = _face_at(win, rect)
        _hover(win, card)
        assert card.isHovered(), "전제: 호버가 켜졌다"
        hovered = _face_at(win, rect)
        assert _pick_first(win) == rect, "전제: 선택 모드에서 카드 자리가 그대로다"
        QTest.mouseMove(win.windowHandle(), QPoint(1, 1))
        _pump()
        assert not card.isHovered(), "전제: 호버가 꺼졌다"
        selected = _face_at(win, rect)
        seen = f"{state.name}: {rest.name()} {hovered.name()} {selected.name()}"
        for colour, want in zip((rest, hovered, selected), DARK_FACES[state], strict=True):
            assert _near(colour, want), f"{want} {seen}"


class TestFieldHoverStep:
    """입력 칸 · 드롭다운 · 콤보 호버 테두리는 평소 테두리에서 두 단계(ΔE00 약 13.4) 떨어진 fieldEdgeHover다(#611 176s)."""

    @pytest.mark.parametrize("name", FIELDS)
    def test_hover_edge_is_two_steps_from_the_rest_edge(
        self, scheme: str, field: Callable[[str], tuple[QWidget, QWidget]], name: str
    ) -> None:
        """포인터가 칸 위에 오면 위 가장자리가 fieldEdgeHover이고 평소 위 가장자리와 ΔE00 12 이상이어야 한다.

        URL 칸 · 정렬 칸 · 콤보, 다크 · 라이트, 합성 포인터 (1, 1) -> 위 가장자리 가운데(평소), 칸 가운데로 이동
        -> 위 가장자리 가운데(호버) ≈ fieldEdgeHover, ΔE00(평소, 호버) >= 12
        """
        from PySide6.QtTest import QTest

        from tests.app.gui.test_theme_contrast import _de00

        host, widget = field(name)
        rect = _rect(host, widget)
        QTest.mouseMove(host.windowHandle(), QPoint(1, 1))
        _pump()
        assert not widget.underMouse(), "전제: 포인터가 칸 밖이다"
        rest = _pixel(host.grab().toImage(), rect.center().x(), rect.top())
        QTest.mouseMove(host.windowHandle(), rect.center())
        _pump()
        assert widget.underMouse() and not widget.hasFocus(), "전제: 포인터만 칸 위다"
        hover = _pixel(host.grab().toImage(), rect.center().x(), rect.top())
        assert _is(hover, "fieldEdgeHover"), f"{scheme} {name}: {hover.name()}"
        step = _de00(rest.name(), hover.name())
        assert step >= 12.0, f"{scheme} {name}: {rest.name()} -> {hover.name()} = {step:.2f}"

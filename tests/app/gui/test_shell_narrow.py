"""셸 좁은 폭 — 헤더바 2줄 칩 → 드롭다운 · 정렬 → 아이콘, 하단바 칸 빼기, 창 최소폭 (#529 — 셸 S3-2).

판정은 필요 폭(지금 보이는 모양과 무관하게 셈) vs 쓸 수 있는 폭이고, 줄인 모양에서 되돌아갈 때는 +8px다
(설계 §2 「좁은 폭」). 경계 폭은 박지 않는다 — 테스트가 위젯의 고정 폭 · 크기 힌트 · 레이아웃 여백을 독립
합산해 구한다(제품의 `headerRow2Needs` · `footerNeeds`를 부르지 않는다). 창 폭은 `resize()` 뒤 실제 폭을
단언하고 잰다. 모양은 실제 창 크기 바꿈(resizeEvent) · 1초 갱신 타이머 신호 · 카드 추가로 바꾼다.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QWidget

import app.theme as theme
import CVDv2 as main_module
from app.download.logger import DownloadLogger
from app.viewmodels.data import ContentItem
from app.viewmodels.footer_monitor import TransferReading
from app.views import mainWindow as mw_mod
from app.views.mainWindow import VodDownloader
from app.widgets.narrow_steps import RETURN_SLACK, step_level
from core.media.representations import StreamEntry
from core.models.download_state import DownloadState
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels
from typing import Never

MIB = 1024 * 1024
GIB = 1024 * MIB
WIDE_EXTRA = 900  # 「넓은 창」 = 칩 모양 필요 폭 + 이만큼(px) — 창 최소폭에서 유도한 폭에 더한다


@pytest.fixture(autouse=True)
def _environment(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """실제 QSS · 네트워크 · 안내 창 · 로그 파일 차단. 테스트가 띄운 창은 끝에 파괴한다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))

    def no_network() -> Never:
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", no_network)
    monkeypatch.setattr(DownloadLogger, "_setup_logging", lambda self: None)
    monkeypatch.setattr(mw_mod.QMessageBox, "information", lambda *a, **k: None)
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture
def window(monkeypatch: pytest.MonkeyPatch) -> VodDownloader:
    """실제 메인 창 — 1초 갱신 타이머는 멈추고 여유 공간 측정은 고정 값(테스트가 신호로 갱신한다)."""
    win = VodDownloader()
    win.monitorTimer.stop()
    monkeypatch.setattr(win.freeSpace, "request", lambda path: None)
    monkeypatch.setattr(win.freeSpace, "value", lambda path: 50 * GIB)
    return win


def _pump() -> None:
    for _ in range(3):
        QApplication.processEvents()


def _at_width(win: VodDownloader, width: int) -> None:
    """창을 `width`로 놓고 실제 폭이 요청과 같은지 단언한다 — resize()는 요청이지 결과가 아니다."""
    win.resize(width, 700)
    win.show()
    _pump()
    assert win.width() == width, (
        f"요청 폭 {width}px인데 실제 {win.width()}px — 최소폭({win.minimumWidth()})에 클램프됐다"
    )


def _card(n: int, state: DownloadState) -> ContentItem:
    item = ContentItem(
        f"https://chzzk.naver.com/video/{n}",
        {
            "title": f"제목 {n}",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 60,
        },
        [StreamEntry(1080, f"u{n}", frame_rate=60.0)],
        None,
        "",
        "",
        "video",
        None,
    )
    item.downloadState = state
    return item


def _add(win: VodDownloader, state: DownloadState, n: int = 1) -> ContentItem:
    item = _card(n, state)
    win.contentManager.model.addItem(item)
    _pump()
    return item


# ---- 독립 합산 — 제품의 필요 폭 계산을 부르지 않는다 ----


def _header_chrome(win: VodDownloader) -> int:
    """창 폭 − 헤더바 안쪽 폭: 줄 좌우 여백 + 프레임 테두리 둘 + 프레임 안쪽 여백."""
    row, inner = win.headerRow.contentsMargins(), win.headerFrameLayout.contentsMargins()
    return (
        row.left() + row.right() + 2 * win.headerFrame.frameWidth() + inner.left() + inner.right()
    )


def _footer_chrome(win: VodDownloader) -> int:
    """창 폭 − 하단바 안쪽 폭."""
    row, inner = win.infoRow.contentsMargins(), win.infoLayout.contentsMargins()
    return row.left() + row.right() + 2 * win.infoFrame.frameWidth() + inner.left() + inner.right()


def _sort_text_width(win: VodDownloader) -> int:
    """정렬 버튼 글 모양의 폭 — 넓은 창에서 버튼에 걸린 고정 폭을 읽는다."""
    assert not win.sortButton.isCompact(), "전제: 글 모양에서 읽는다"
    return win.sortButton.minimumWidth()


def _chip_shape_need(win: VodDownloader, sort_width: int) -> int:
    """헤더바 2줄 칩 모양(단계 0)이 바라는 창 폭."""
    chips = list(win.filterChips.values())
    gap = win.filterRowLayout.spacing()
    chip_row = sum(c.minimumWidth() for c in chips)
    chip_row += win.filterChipBar.layout().spacing() * (len(chips) - 1)
    clear = win.clearFinishedButton.sizeHint().width()
    return _header_chrome(win) + chip_row + gap + sort_width + gap + clear


def _drop_shape_need(win: VodDownloader, sort_width: int, failed: bool) -> int:
    """헤더바 2줄 드롭다운 모양(단계 1 · 2)이 바라는 창 폭 — `sort_width`가 정렬 모양을 정한다."""
    gap = win.filterRowLayout.spacing()
    filters = win.filterChipBar.dropButton.minimumWidth()
    if failed:
        filters += win.filterChipBar.layout().spacing() + win.filterChips["failed"].minimumWidth()
    clear = win.clearFinishedButton.sizeHint().width()
    return _header_chrome(win) + filters + gap + sort_width + gap + clear


def _url_row_text_need(win: VodDownloader) -> int:
    """헤더바 1줄 저장 위치 글 모양(단계 0)이 바라는 창 폭.

    URL 칸 최소폭(칸에 건 값) + 저장 위치 글 모양 폭 + VOD 추가 + 간격 둘 + 헤더 여백 · 테두리
    """
    gap = win.urlRowLayout.spacing()
    save = win.saveLocationButton.textWidth()
    line = win.urlInput.minimumWidth() + gap + save + gap + win.fetchButton.minimumWidth()
    return _header_chrome(win) + line


class _ShowCounter(QObject):
    """위젯이 보이고 숨는 횟수를 센다."""

    def __init__(self, target: QWidget) -> None:
        super().__init__()
        self.count = 0
        target.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            self.count += 1
        return False


# ---------------------------------------------------------------------------
# ① 판정 함수 — 순수 계산
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("room", "level", "expected"),
    [
        (299, 0, 1),  # 단계 0 필요 300 − 1 → 줄인다
        (300, 0, 0),  # 필요 폭과 같으면 그대로
        (300 + RETURN_SLACK - 1, 1, 1),  # 되돌아가기 +8 − 1 → 그대로
        (300 + RETURN_SLACK, 1, 0),  # 되돌아가기 +8 → 넓은 모양
        (99, 0, 2),  # 마지막 단계에서 멈춘다
        (5000, 2, 0),  # 한 번에 끝까지 되돌아간다
    ],
)
def test_step_level_picks_the_level_by_need_room_and_return_slack(
    room: int, level: int, expected: int
) -> None:
    """step_level은 필요 폭 [300, 200, 100]에서 지금 단계 · 쓸 수 있는 폭에 맞는 단계를 한 번에 돌려줘야 한다.

    needs=[300, 200, 100], RETURN_SLACK=8
    """
    assert step_level([300, 200, 100], room, level) == expected


# ---------------------------------------------------------------------------
# ② 헤더바 2줄 — 칩 → 드롭다운 경계
# ---------------------------------------------------------------------------


def test_chips_turn_into_the_dropdown_one_pixel_below_the_chip_need(window: VodDownloader) -> None:
    """창 폭이 칩 모양 필요 폭 T면 칩, T − 1이면 드롭다운이어야 한다.

    T = 헤더 여백 · 테두리 + 칩 여섯 + 간격 + 정렬 글 모양 + 완료 지우기
    """
    _at_width(window, window.minimumWidth() + WIDE_EXTRA)
    need = _chip_shape_need(window, _sort_text_width(window))
    _at_width(window, need)
    assert not window.filterChipBar.isCompact()
    assert window.filterChips["all"].isVisible()
    _at_width(window, need - 1)
    assert window.filterChipBar.isCompact()
    assert window.filterChipBar.dropButton.isVisible()
    assert not window.filterChips["all"].isVisible()


def test_the_dropdown_returns_to_chips_only_eight_pixels_past_the_need(
    window: VodDownloader,
) -> None:
    """드롭다운에서 칩으로는 T + 7에서 그대로, T + 8에서 되돌아가야 한다.

    T − 1(드롭다운) -> T + 7 드롭다운 -> T + 8 칩
    """
    _at_width(window, window.minimumWidth() + WIDE_EXTRA)
    need = _chip_shape_need(window, _sort_text_width(window))
    _at_width(window, need - 1)
    assert window.filterChipBar.isCompact(), "전제: 드롭다운 모양에서 시작한다"
    _at_width(window, need + RETURN_SLACK - 1)
    assert window.filterChipBar.isCompact()
    _at_width(window, need + RETURN_SLACK)
    assert not window.filterChipBar.isCompact()


def test_a_window_first_shown_in_the_dropdown_shape_has_no_horizontal_scrollbar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """드롭다운 모양 폭으로 처음 띄우면 이벤트 한 차례 뒤 가로 스크롤바가 없고 콘텐츠 열이 창 폭이어야 한다.

    여유 공간 모름(하단 칸이 하나도 안 보인다) · 창 폭 = 칩 모양 필요 폭 − 1(창이 보이기 전에 드롭다운
    모양으로 바뀐다) -> show + processEvents 1번
    """
    from app.system.free_space import FreeSpaceProbe

    monkeypatch.setattr(FreeSpaceProbe, "request", lambda self, path: None)
    monkeypatch.setattr(FreeSpaceProbe, "value", lambda self, path: None)
    win = VodDownloader()
    win.monitorTimer.stop()
    need = _chip_shape_need(win, win.sortButton.textWidth())
    win.resize(need - 1, 700)
    win.show()
    QApplication.processEvents()
    assert win.filterChipBar.isCompact(), "전제: 처음부터 드롭다운 모양"
    assert win.footerMonitor.shownKeys() == [], "전제: 하단 칸이 하나도 안 보인다"
    assert not win.windowScrollArea.horizontalScrollBar().isVisible()
    assert win.contentColumn.width() == win.width()


def test_crossing_the_threshold_settles_before_returning_to_the_event_loop(
    window: VodDownloader,
) -> None:
    """넓은 창에서 칩 모양 필요 폭 − 1로 줄이면 이벤트를 돌리기 전에 콘텐츠 열이 창 폭이어야 한다.

    넓은 창(칩) -> resize(T − 1) 직후, processEvents 없이 잰다
    """
    _at_width(window, window.minimumWidth() + WIDE_EXTRA)
    need = _chip_shape_need(window, _sort_text_width(window))
    window.resize(need - 1, 700)
    assert window.filterChipBar.isCompact(), "전제: 드롭다운 모양으로 바뀌었다"
    assert window.contentColumn.width() == window.width()


def test_a_shape_change_does_not_touch_card_widgets(
    window: VodDownloader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """모양이 바뀔 때 캐시 비우기는 바 위젯과 목록 하나만 건드리고 목록 안(카드 위젯 · 그 자손)은 건드리지 않아야 한다.

    카드 20장 · 넓은 창 -> 칩 모양 필요 폭 − 1(헤더 모양이 바뀜). 캐시 비우기(`_flushBarCaches`)가 도는
    동안 updateGeometry를 부른 위젯을 센다 — 그 뒤 레이아웃 처리에서 카드가 창 폭을 따라 스스로 부르는
    것은 세지 않는다 (#530)
    """
    for n in range(20):
        _add(window, DownloadState.FINISHED, n)
    _at_width(window, window.minimumWidth() + WIDE_EXTRA)
    need = _chip_shape_need(window, _sort_text_width(window))
    touched: list[QWidget] = []
    flushing = {"on": False}
    original_update = QWidget.updateGeometry
    original_flush = VodDownloader._flushBarCaches

    def spy(widget: QWidget) -> None:
        if flushing["on"]:
            touched.append(widget)
        original_update(widget)

    def flush(win: VodDownloader) -> None:
        flushing["on"] = True
        try:
            original_flush(win)
        finally:
            flushing["on"] = False

    before = window.headerShapeChanges
    # 스파이만 되돌린다 — window 픽스처의 여유 공간 대역은 끝까지 남는다(#530 리뷰)
    with monkeypatch.context() as patch:
        patch.setattr(QWidget, "updateGeometry", spy)
        patch.setattr(VodDownloader, "_flushBarCaches", flush)
        window.resize(need - 1, 700)
    assert window.freeSpace.value("") == 50 * GIB, "전제: 픽스처의 여유 공간 대역이 남아 있다"
    assert window.headerShapeChanges - before == 1, "전제: 모양이 바뀌었다"
    assert any(window.headerFrame.isAncestorOf(w) for w in touched), "전제: 바 위젯은 비웠다"
    list_view = window.listView
    inside = [w for w in touched if w is not list_view and list_view.isAncestorOf(w)]
    assert inside == []


def test_one_resize_changes_the_header_shape_once(window: VodDownloader) -> None:
    """넓은 창에서 창 최소폭으로 한 번 줄이면 헤더 모양이 한 번만 바뀌고 드롭다운은 한 번만 나타나야 한다.

    실패 카드 1장, 넓은 창 -> 창 최소폭(헤더 단계 0 → 2) -> 헤더 모양 바뀜 1번, 드롭다운 보임 이벤트 1번
    """
    _add(window, DownloadState.FAILED)
    _at_width(window, window.minimumWidth() + WIDE_EXTRA)
    counter = _ShowCounter(window.filterChipBar.dropButton)
    before = window.headerShapeChanges
    _at_width(window, window.minimumWidth())
    assert window.headerShapeChanges - before == 1
    assert counter.count == 1
    assert window.sortButton.isCompact()


# ---------------------------------------------------------------------------
# ②′ 헤더바 1줄 — 저장 위치 글 → 아이콘(프로토 11v)
# ---------------------------------------------------------------------------


def test_the_url_input_minimum_is_160(window: VodDownloader) -> None:
    """URL 칸의 최소폭은 160px이어야 한다(프로토 11v URL_MIN)."""
    assert window.urlInput.minimumWidth() == 160


def _unfloored(win: VodDownloader) -> None:
    """창 최소폭 바닥을 걷고 넓은 창에 놓는다.

    헤더 1줄 경계(T)는 글꼴에 따라 창 최소폭(헤더 2줄이 정할 수 있다)보다 좁다 — 바닥을 걷어야 그 경계에
    창을 놓을 수 있다. 판정은 창 폭으로 하므로 바닥과 무관하다.
    """
    wide = win.minimumWidth() + WIDE_EXTRA
    win.setMinimumWidth(0)
    _at_width(win, wide)


def test_save_location_turns_into_an_icon_one_pixel_below_the_url_row_need(
    window: VodDownloader,
) -> None:
    """창 폭이 헤더 1줄 글 모양 필요 폭 T면 저장 위치는 글, T − 1이면 아이콘(정사각)이어야 한다.

    T = 헤더 여백 · 테두리 + URL 칸 최소폭 + 저장 위치 글 모양 + VOD 추가 + 간격 둘(창 최소폭 바닥 걷음)
    """
    _unfloored(window)
    need = _url_row_text_need(window)
    _at_width(window, need)
    save = window.saveLocationButton
    assert not save.isCompact()
    _at_width(window, need - 1)
    assert save.isCompact()
    assert save.width() == save.height()


def test_save_location_returns_to_text_only_eight_pixels_past_the_need(
    window: VodDownloader,
) -> None:
    """저장 위치 아이콘에서 글로는 T + 7에서 그대로, T + 8에서 되돌아가야 한다(창 최소폭 바닥 걷음)."""
    _unfloored(window)
    need = _url_row_text_need(window)
    _at_width(window, need - 1)
    save = window.saveLocationButton
    assert save.isCompact(), "전제: 아이콘 모양에서 시작한다"
    _at_width(window, need + RETURN_SLACK - 1)
    assert save.isCompact()
    _at_width(window, need + RETURN_SLACK)
    assert not save.isCompact()


def test_the_url_input_never_goes_below_its_minimum_while_shrinking(window: VodDownloader) -> None:
    """창을 넓은 폭에서 창 최소폭까지 1px씩 줄이는 동안 URL 칸은 늘 최소폭 이상이어야 한다."""
    _at_width(window, window.minimumWidth() + WIDE_EXTRA)
    for width in range(window.width(), window.minimumWidth() - 1, -1):
        _at_width(window, width)
        assert window.urlInput.width() >= window.urlInput.minimumWidth(), f"{width}px"


def test_the_save_location_icon_shows_name_and_path_and_the_same_menu(
    window: VodDownloader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """저장 위치 아이콘의 툴팁은 「이름」 · 「전체 경로」 두 줄이고, 메뉴에서 고르면 위치와 툴팁이 따라야 한다.

    메뉴 「폴더 찾기…」 -> 폴더 선택 창 대역이 「다른 폴더」를 돌려준다
    """
    from PySide6.QtWidgets import QFileDialog

    other = tmp_path / "다른 폴더"
    other.mkdir()
    _unfloored(window)
    _at_width(window, _url_row_text_need(window) - 1)
    save = window.saveLocationButton
    assert save.isCompact(), "전제: 아이콘 모양"
    current = window.saveLocations.current()
    assert save.toolTip() == f"{save.fullText()}\n{current}"
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(other))
    )
    other = str(other)
    save.menuWidget.aboutToShow.emit()
    find = next(a for a in save.menuWidget.actions() if a.objectName() == "findFolderAction")
    find.trigger()
    _pump()
    assert window.saveLocations.current() == other
    assert save.toolTip() == f"다른 폴더\n{other}"


def test_one_resize_changes_the_url_row_shape_once(window: VodDownloader) -> None:
    """넓은 창에서 헤더 1줄 글 모양 필요 폭 − 1로 한 번 줄이면 1줄 모양이 한 번만 바뀌어야 한다(바닥 걷음)."""
    _unfloored(window)
    before = window.urlRowShapeChanges
    _at_width(window, _url_row_text_need(window) - 1)
    assert window.urlRowShapeChanges - before == 1
    assert window.saveLocationButton.isCompact()


# ---------------------------------------------------------------------------
# ③ 정렬 → 아이콘
# ---------------------------------------------------------------------------


def test_sort_turns_into_an_icon_one_pixel_below_the_dropdown_need(window: VodDownloader) -> None:
    """드롭다운 + 실패 칩 + 정렬 글 모양의 필요 폭 T면 정렬은 글, T − 1이면 아이콘(정사각)이어야 한다.

    실패 카드 1장 -> 실패 칩이 드롭다운 옆에 남는 모양
    """
    _add(window, DownloadState.FAILED)
    _at_width(window, window.minimumWidth() + WIDE_EXTRA)
    need = _drop_shape_need(window, _sort_text_width(window), failed=True)
    _at_width(window, need)
    assert window.filterChipBar.isCompact() and not window.sortButton.isCompact()
    _at_width(window, need - 1)
    assert window.sortButton.isCompact()
    assert window.sortButton.width() == window.sortButton.height()


def test_the_sort_icon_has_a_tooltip_and_the_same_menu(window: VodDownloader) -> None:
    """정렬 아이콘의 툴팁은 「Sort: Added order」이고 메뉴에서 고르면 정렬이 바뀌고 툴팁도 따라야 한다."""
    _add(window, DownloadState.FAILED)
    _at_width(window, window.minimumWidth())
    button = window.sortButton
    assert button.isCompact(), "전제: 창 최소폭에서는 아이콘 모양"
    assert button.toolTip() == "Sort: Added order"
    button.menuWidget.aboutToShow.emit()
    title = next(a for a in button.menuWidget.choices() if a.data() == ("by", "title"))
    title.trigger()
    _pump()
    assert window.contentManager.order.sortBy() == "title"
    assert button.toolTip() == "Sort: Title"


def _dot_color(button: QWidget) -> QColor:
    """정렬 아이콘 상자 오른쪽 위 안쪽 점 자리의 색 — grab 이미지는 장치 화소라 배율을 곱해 읽는다."""
    image = button.grab().toImage()
    ratio = image.devicePixelRatio()
    inset = theme.METRICS["sortDotInset"]
    point = QPoint(int((button.width() - inset) * ratio), int(inset * ratio))
    return image.pixelColor(point)


def _distance(a: QColor, b: QColor) -> int:
    return abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue())


def test_the_sort_icon_shows_an_accent_dot_only_for_a_non_default_sort(
    window: VodDownloader,
) -> None:
    """기본 정렬이면 점 자리가 강조색이 아니고, 내림차순으로 바꾸면 그 자리가 강조색이어야 한다."""
    _add(window, DownloadState.FAILED)
    _at_width(window, window.minimumWidth())
    button = window.sortButton
    assert button.isCompact(), "전제: 창 최소폭에서는 아이콘 모양"
    accent = QColor(theme.current_tokens()["accent"])
    assert _distance(_dot_color(button), accent) > 60
    window.contentManager.order.setSort("added", True)
    _pump()
    assert _distance(_dot_color(button), accent) <= 60


# ---------------------------------------------------------------------------
# ④ 드롭다운 · 실패 칩 · 고른 필터
# ---------------------------------------------------------------------------


def test_the_dropdown_shows_the_chosen_filter_and_picking_it_again_keeps_it(
    window: VodDownloader,
) -> None:
    """드롭다운 글은 고른 필터 「이름 N」이고, 메뉴에서 고른 필터를 다시 골라도 그대로여야 한다.

    대기 카드 2장 -> 「All 2」 -> 메뉴에서 대기 -> 「Waiting 2」 -> 다시 대기 -> 대기 그대로
    """
    _add(window, DownloadState.WAITING, 1)
    _add(window, DownloadState.WAITING, 2)
    _at_width(window, window.minimumWidth())
    drop = window.filterChipBar.dropButton
    assert drop.fullText() == "All 2"
    for _ in range(2):
        window.filterChipBar.dropMenu.aboutToShow.emit()
        action = next(a for a in window.filterChipBar.dropMenu.choices() if a.data() == "waiting")
        action.trigger()
        _pump()
        assert window.contentManager.order.filterKey() == "waiting"
    assert drop.fullText() == "Waiting 2"


def test_the_failed_chip_stays_outside_the_dropdown_only_while_cards_failed(
    window: VodDownloader,
) -> None:
    """드롭다운 모양에서 실패 칩은 실패 카드가 없으면 숨고, 생기면 보여야 한다."""
    _at_width(window, window.minimumWidth())
    failed = window.filterChips["failed"]
    assert window.filterChipBar.isCompact(), "전제: 드롭다운 모양"
    assert not failed.isVisible()
    _add(window, DownloadState.FAILED)
    assert failed.isVisible()


def test_pressing_the_failed_chip_again_returns_to_all(window: VodDownloader) -> None:
    """드롭다운 옆 실패 칩을 누르면 실패, 다시 누르면 전체로 돌아가야 한다."""
    _add(window, DownloadState.FAILED)
    _at_width(window, window.minimumWidth())
    failed = window.filterChips["failed"]
    assert failed.isVisible(), "전제: 실패 칩이 드롭다운 옆에 있다"
    failed.click()
    _pump()
    assert window.contentManager.order.filterKey() == "failed"
    assert window.filterChipBar.dropButton.fullText() == "Failed 1"
    failed.click()
    _pump()
    assert window.contentManager.order.filterKey() == "all"


def test_the_chosen_filter_survives_shape_changes(window: VodDownloader) -> None:
    """칩 모양에서 고른 필터는 드롭다운 모양으로 갔다가 돌아와도 그대로이고 그 칩이 눌려 있어야 한다."""
    _add(window, DownloadState.FINISHED)
    wide = window.minimumWidth() + WIDE_EXTRA
    _at_width(window, wide)
    window.filterChips["finished"].click()
    _pump()
    _at_width(window, window.minimumWidth())
    assert window.filterChipBar.isCompact(), "전제: 드롭다운 모양"
    assert window.filterChipBar.dropButton.fullText() == "Finished 1"
    _at_width(window, wide)
    assert window.contentManager.order.filterKey() == "finished"
    assert window.filterChips["finished"].isChecked()


def test_a_failed_card_appearing_at_the_threshold_turns_sort_into_an_icon(
    window: VodDownloader,
) -> None:
    """실패 칩 몫이 없을 때 정렬 글이 들어가는 폭에서 실패 카드가 생기면 정렬이 아이콘이 되어야 한다.

    창 폭 = 드롭다운 + 실패 칩 + 정렬 글 모양 필요 폭 − 1(실패 카드 없음 -> 정렬 글) -> 실패 카드 추가 -> 아이콘
    """
    _at_width(window, window.minimumWidth() + WIDE_EXTRA)
    need = _drop_shape_need(window, _sort_text_width(window), failed=True)
    _at_width(window, need - 1)
    assert not window.sortButton.isCompact(), "전제: 실패 칩 몫이 없으면 정렬 글이 들어간다"
    _add(window, DownloadState.FAILED)
    assert window.sortButton.isCompact()


def test_tab_order_in_the_narrow_shape_goes_through_the_dropdown(window: VodDownloader) -> None:
    """좁은 폭의 탭 순서는 VOD 추가 → 드롭다운 → 실패 칩 → 정렬 아이콘 → 완료 지우기여야 한다."""
    _add(window, DownloadState.FAILED)
    _at_width(window, window.minimumWidth())

    def next_focusable(widget: QWidget) -> QWidget | None:
        cursor = widget.nextInFocusChain()
        while cursor is not None and cursor is not widget:
            if cursor.focusPolicy() != Qt.FocusPolicy.NoFocus and cursor.isVisibleTo(window):
                return cursor
            cursor = cursor.nextInFocusChain()
        return None

    chain = [
        window.fetchButton,
        window.filterChipBar.dropButton,
        window.filterChips["failed"],
        window.sortButton,
        window.clearFinishedButton,
    ]
    assert [next_focusable(w) for w in chain[:-1]] == chain[1:]


# ---------------------------------------------------------------------------
# ⑤ 하단바 — 칸 빼는 차례
# ---------------------------------------------------------------------------


def _receiving(win: VodDownloader, monkeypatch: pytest.MonkeyPatch) -> None:
    """받는 중 값(속도 · 남은 시간 · 연결 · 메모리 · 여유)을 1초 갱신 신호로 넣는다.

    값은 칸 글이 길게 나오는 쪽으로 둔다 — 창 최소폭까지 줄였을 때 여러 칸이 빠지게.
    """
    reading = TransferReading(speed=512.5 * MIB, connections=128, remaining_bytes=9000 * GIB)
    monkeypatch.setattr(win.downloadViewModel, "transferReadings", lambda: [reading])
    monkeypatch.setattr(mw_mod, "resident_bytes", lambda: 98765 * MIB)
    win.monitorTimer.timeout.emit()
    _pump()


def test_footer_drops_cells_from_the_front_of_the_drop_order_while_shrinking(
    window: VodDownloader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """창을 1px씩 줄이면 하단 칸은 연결부터 차례로 하나씩 빠지고 다시 나타나지 않으며 속도는 남아야 한다.

    받는 중 값(다섯 칸) · 넓은 창 -> 창 최소폭까지 1px씩 -> 빠진 칸 = DROP_ORDER의 앞부분(두 칸 이상)
    """
    _at_width(window, window.minimumWidth() + WIDE_EXTRA)
    _receiving(window, monkeypatch)
    assert window.footerMonitor.shownKeys() == ["speed", "remain", "conn", "mem", "free"]
    gone: list[str] = []
    for width in range(window.width(), window.minimumWidth() - 1, -1):
        _at_width(window, width)
        shown = window.footerMonitor.shownKeys()
        assert all(key not in shown for key in gone), f"{width}px에서 빠진 칸이 다시 나타났다"
        gone += [key for key in mw_mod.DROP_ORDER if key not in shown and key not in gone]
        assert "speed" in shown
    assert len(gone) >= 2, "전제: 창 최소폭까지 줄이면 두 칸 이상 빠진다"
    assert gone == list(mw_mod.DROP_ORDER[: len(gone)])


@pytest.mark.parametrize("dropped", range(5))
def test_footer_monitor_hides_the_first_cells_of_the_drop_order(
    qapp: QApplication, dropped: int
) -> None:
    """하단 모니터링에서 n칸을 빼면 연결 · 메모리 · 남은 시간 · 여유 공간 앞에서부터 n칸이 숨어야 한다.

    받는 중 값(다섯 칸 모두 있음), 뺀 칸 n = 0 ~ 4
    """
    from app.viewmodels.footer_monitor import MonitorSnapshot
    from app.widgets.footer_bar import FooterMonitor

    monitor = FooterMonitor()
    snapshot = MonitorSnapshot(True, 12.5 * MIB, 72.0, 8, 321 * MIB, 50 * GIB)
    monitor.setSnapshot(snapshot)
    monitor.setDropped(dropped)
    expected = ["speed", "remain", "conn", "mem", "free"]
    for key in ("conn", "mem", "remain", "free")[:dropped]:
        expected.remove(key)
    assert monitor.shownKeys() == expected


def test_footer_labels_are_never_squeezed_while_shrinking(
    window: VodDownloader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """창을 1px씩 줄이는 동안 보이는 하단 칸은 늘 글 폭(크기 힌트) 이상이어야 한다."""
    _at_width(window, window.minimumWidth() + WIDE_EXTRA)
    _receiving(window, monkeypatch)
    for width in range(window.width(), window.minimumWidth() - 1, -1):
        _at_width(window, width)
        for key in window.footerMonitor.shownKeys():
            label = window.footerMonitor.labels[key]
            assert label.width() >= label.sizeHint().width(), f"{width}px에서 {key} 칸이 눌렸다"


# ---------------------------------------------------------------------------
# ⑥ 창 최소폭 = 끝까지 줄인 모양
# ---------------------------------------------------------------------------


def test_the_minimum_width_shows_the_fully_narrowed_shape(window: VodDownloader) -> None:
    """창 최소폭에서는 헤더바 2줄이 드롭다운 + 정렬 아이콘이고 가로 스크롤바가 없어야 한다."""
    _add(window, DownloadState.FAILED)
    _at_width(window, window.minimumWidth())
    assert window.filterChipBar.isCompact()
    assert window.sortButton.isCompact()
    assert not window.windowScrollArea.horizontalScrollBar().isVisible()


def _inside(win: VodDownloader, widget: QWidget, frame: QWidget) -> bool:
    rect = QRect(widget.mapTo(win, QPoint(0, 0)), widget.size())
    bounds = QRect(frame.mapTo(win, QPoint(0, 0)), frame.size())
    return bounds.contains(rect)


def test_no_bar_button_is_cut_at_the_minimum_width(window: VodDownloader) -> None:
    """창 최소폭에서 메뉴바 · 헤더바 · 하단바의 버튼은 모두 그 바 안에 있고 바라는 폭 이상이어야 한다.

    실패 카드 1장(실패 칩 보임) · 실행 결과 없음
    """
    _add(window, DownloadState.FAILED)
    _at_width(window, window.minimumWidth())
    bars = {
        window.appMenuBar: (window.menuDownloadButton, window.menuSettingsButton),
        window.headerFrame: (
            window.urlInput,
            window.saveLocationButton,
            window.fetchButton,
            window.filterChipBar.dropButton,
            window.filterChips["failed"],
            window.sortButton,
            window.clearFinishedButton,
        ),
        window.infoFrame: (window.downloadButton, window.stopButton),
    }
    for frame, widgets in bars.items():
        for widget in widgets:
            assert widget.isVisible(), f"전제: {widget.objectName()}가 보인다"
            assert _inside(window, widget, frame), f"{widget.objectName()}가 바 밖으로 나갔다"
            # 고정 폭 위젯은 건 폭, 아니면 최소 크기 힌트 이상이어야 눌리지 않은 것이다
            wanted = widget.minimumWidth() or widget.minimumSizeHint().width()
            assert widget.width() >= wanted, f"{widget.objectName()} 눌림"


def test_cards_do_not_change_the_minimum_width(window: VodDownloader) -> None:
    """실패 · 대기 · 완료 카드를 넣어도 창 최소폭 계산값은 창에 걸린 값과 같아야 한다."""
    hung = window.minimumWidth()
    for n, state in enumerate(
        (DownloadState.FAILED, DownloadState.WAITING, DownloadState.FINISHED)
    ):
        _add(window, state, n + 1)
    assert window._contentMinimumWidth() == hung


# ---------------------------------------------------------------------------
# ⑦ 메뉴바 버튼 모양
# ---------------------------------------------------------------------------


def test_menu_buttons_are_bar_controls_inside_the_bar_padding(window: VodDownloader) -> None:
    """메뉴 버튼 둘은 높이가 바 컨트롤 높이이고 첫 버튼은 창 왼쪽 · 위에서 바 안쪽 여백만큼 떨어져야 한다."""
    _at_width(window, window.minimumWidth() + WIDE_EXTRA)
    control = theme.METRICS["barControl"]
    for button in (window.menuDownloadButton, window.menuSettingsButton):
        assert button.height() == control
    corner = window.menuDownloadButton.mapTo(window, QPoint(0, 0))
    assert corner == QPoint(theme.METRICS["barPadX"], theme.METRICS["barPadY"])

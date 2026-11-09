"""카드 C3 — 3줄 접는 단계 · 실패 사유 나눔 · 날짜 연도 · 진행 막대 · 색조 · 배경 채움 · 중지 모션 ·
썸네일 두 모양 · 창 최소폭 · 채널 기본 그림 (#554).

폭은 절대 px로 박지 않는다 — 카드를 1px씩 좁혀 모양이 바뀌는 폭을 찾고, 그 폭이 보이는 위젯의 글 폭 ·
아이콘 폭 · 간격의 독립 합산과 같은지 잰다. 색은 theme 토큰과 화소를 견준다. 시간은 테스트가 정한다
(`QVariantAnimation.setCurrentTime`) — 실제 시간에 기대지 않는다.
"""

import colorsys
import math
from collections.abc import Iterator
from typing import Never

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QLabel, QWidget

import CVDv2 as main_module
import app.theme as theme
import app.viewmodels.card_display as card_display
from app.viewmodels import card_text
from app.viewmodels.data import ContentItem
from app.viewmodels.model import ContentListModel
from app.views.mainWindow import VodDownloader
from app.widgets.view import ContentListView
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from core.models.plan import TimeRange
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels
from tests.app.gui.test_shell_layout import WORST_SAMPLES

DEFAULT_DIR = "C:/dl"  # 전역 저장 위치 — 카드 저장 위치가 이것이면 3줄에 보이지 않는다
OTHER_DIR = "D:/vod/lck-finals"  # 전역과 다른 저장 위치 — 3줄에 보인다
MB = 1024 * 1024
TWO_SECTIONS = (TimeRange(600.0, 1200.0), TimeRange(1800.0, 2400.0))  # 합 20분
RETURN_SLACK = 8  # 되돌아갈 때 더 남아야 하는 폭(px) — 설계 §2 「+8」


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp) -> Iterator[None]:
    """실제 전역 QSS · 다크 테마로 잰다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))
    yield


@pytest.fixture(autouse=True)
def _drop_windows(qapp) -> Iterator[None]:
    """테스트가 띄운 창을 테스트 끝에 파괴한다."""
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
    monkeypatch.setattr("app.widgets.widget._global_download_path", DEFAULT_DIR)


def _display(**fields: object) -> None:
    """카드 화면 설정을 정한다 — 카드를 만들기 전에 부른다."""
    card_display.use(card_display.CardDisplay(**fields))


def _item(
    state: DownloadState = DownloadState.WAITING,
    path: str = DEFAULT_DIR,
    channel: str = "채널",
    created: str = "2026-10-26 10:00:00",
) -> ContentItem:
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {
            "title": "제목",
            "category": "",
            "channelName": channel,
            "createdDate": created,
            "duration": 3600,
        },
        [[1080, "u1080"], [720, "u720"]],
        None,
        "",
        path,
        "video",
        None,
    )
    item.downloadState = state
    item.total_size = "595.34 MB"
    return item


def _card(item: ContentItem, width: int = 900) -> ContentItemWidget:
    """아이템으로 카드를 띄운다 — 해상도를 고른 뒤 상태를 건다(대기에서만 고른다)."""
    state = item.downloadState
    item.downloadState = DownloadState.WAITING
    card = ContentItemWidget(item, 0)
    card.loadRepresentations()
    item.downloadState = state
    card.setData(item, 0)
    card.resize(width, card.sizeHint().height())
    card.show()
    QApplication.processEvents()
    # 카드만 띄운 창은 offscreen 포인터 자리에 따라 호버로 잡힐 수 있다 — 호버는 테스트가 정한다
    card.setHovered(False)
    QApplication.processEvents()
    return card


def _at(card: ContentItemWidget, width: int) -> None:
    """카드를 `width`로 놓는다 — 실제 폭이 요청과 같은지 단언한다(클램프면 아무것도 안 잰다)."""
    card.resize(width, card.sizeHint().height())
    QApplication.processEvents()
    assert card.width() == width, f"요청 {width}px · 실제 {card.width()}px"


def _sections_running() -> ContentItem:
    """3줄에 접을 것이 다 있는 받는 중 카드 — 저장 위치 · 구간 길이 · 「/ 전체」 · 속도 · 남은 시간."""
    item = _item(DownloadState.RUNNING, path=OTHER_DIR)
    item.selections = TWO_SECTIONS
    item.section_bytes = 200 * MB
    item.transfer_bytes = 200 * MB
    item.download_size = 50 * MB
    item.download_progress = 37
    item.download_speed = "18.4 MB/s"
    item.download_remain_time = "00:06:00"
    return item


def _signature(card: ContentItemWidget) -> tuple:
    """3줄 모양 — (상태 글, 정보 글, 폴더 아이콘 보임, 폴더 이름 보임)."""
    info = card.fileSizeLabel.text() if card.fileSizeLabel.isVisible() else ""
    return (
        card.statusLabel.text(),
        info,
        card.pathIcon.isVisible(),
        card.directoryLabel.isVisible(),
    )


def _row3_need(card: ContentItemWidget) -> int:
    """지금 3줄 모양이 바라는 폭 — 보이는 위젯의 글 폭(+4) · 아이콘 폭 · 사이 간격을 독립 합산한다.

    늘어나는 빈칸과 숨은 위젯은 간격을 받지 않는다(QBoxLayout).
    """
    layout = card.resolutionLayout
    widths = []
    for index in range(layout.count()):
        entry = layout.itemAt(index)
        widget = entry.widget()
        if widget is None or entry.isEmpty():
            continue
        if isinstance(widget, QLabel) and widget.text():
            widths.append(widget.fontMetrics().horizontalAdvance(widget.text()) + 4)
        else:
            widths.append(widget.width())
    return sum(widths) + layout.spacing() * (len(widths) - 1)


def _fold_steps(card: ContentItemWidget, start: int, stop: int) -> list[tuple[int, tuple, tuple]]:
    """카드를 `start`에서 `stop`까지 1px씩 좁히며 모양이 바뀐 곳마다 (T, 앞 모양, 뒤 모양)을 모은다.

    T는 앞 모양이 마지막으로 들어간 폭이다. 각 T에서 독립 합산이 T의 3줄 폭과 같은지 보고, T+7에서는
    접힌 채, T+8에서는 앞 모양으로 돌아오는지 본 뒤 다시 좁혀 이어 간다.
    """
    _at(card, start)
    chrome = card.width() - card.statusRow.width()  # 카드 폭 − 3줄 폭(썸네일 · 여백 · 테두리)
    steps = []
    previous = _signature(card)
    need = _row3_need(card)
    for width in range(start - 1, stop - 1, -1):
        _at(card, width)
        current = _signature(card)
        if current == previous:
            need = _row3_need(card)
            continue
        threshold = width + 1
        assert need + chrome == threshold, (
            f"{previous} → {current}: 바뀐 폭 T−1={width}, 독립 합산 {need} + {chrome} ≠ T"
        )
        _at(card, threshold + RETURN_SLACK - 1)
        assert _signature(card) == current, f"T+7({threshold + 7})에서 되돌아갔다: {previous}"
        _at(card, threshold + RETURN_SLACK)
        assert _signature(card) == previous, f"T+8({threshold + 8})에서 되돌아가지 않았다"
        _at(card, width)
        assert _signature(card) == current
        steps.append((threshold, previous, current))
        previous, need = current, _row3_need(card)
    return steps


class TestFoldOrder:
    """3줄 접는 차례 — 저장 위치 이름 → 저장 위치 → 길이 → 「/ 전체」 → 속도 → 남은 시간 (프로토 가)."""

    def test_running_card_folds_in_the_proto_order_with_the_return_slack(self, qapp) -> None:
        """받는 중 구간 카드면 경계마다 독립 합산 폭에서 접히고 그 차례가 프로토 가와 같아야 한다.

        저장 위치 다름 · 구간 둘(20:00) · 50 MB / 200 MB · 18.4 MB/s · 6:00 남음, 900px → 260px 1px씩
        -> 바뀐 차례: 폴더 이름 숨음 → 폴더 아이콘 숨음 → 「20:00」 빠짐 → 「/ 200.00 MB」 빠짐 →
        「18.4 MB/s」 빠짐 → 「6:00 left」 빠짐, 경계마다 T−1 접힘 · T 들어감 · T+7 접힌 채 · T+8 되돌아감
        """
        card = _card(_sections_running())
        steps = _fold_steps(card, 900, 260)
        changes = []
        for _, (status, info, icon, name), (status2, info2, icon2, name2) in steps:
            if name and not name2:
                changes.append("path name")
            elif icon and not icon2:
                changes.append("path")
            elif "20:00" in info and "20:00" not in info2:
                changes.append("length")
            elif "/" in info and "/" not in info2:
                changes.append("total")
            elif "18.4 MB/s" in status and "18.4 MB/s" not in status2:
                changes.append("speed")
            elif "left" in status and "left" not in status2:
                changes.append("remain")
            else:
                changes.append(f"other {status!r}/{info!r} → {status2!r}/{info2!r}")
        assert changes == ["path name", "path", "length", "total", "speed", "remain"]

    def test_failed_card_drops_the_reason_tail_first(self, qapp) -> None:
        """실패 카드면 첫 경계에서 사유의 뒤(조치 안내)가 빠지고 앞은 남아야 한다.

        사유 "Failed to save the file · Check the free space" · 저장 위치 다름, 900px → 260px
        -> 첫 바뀜: 상태 글 "Failed to save the file · Check …" → "Failed to save the file"
        """
        item = _item(DownloadState.FAILED, path=OTHER_DIR)
        item.stateMessage = "Failed to save the file · Check the free space"
        card = _card(item)
        steps = _fold_steps(card, 900, 260)
        first_before, first_after = steps[0][1][0], steps[0][2][0]
        assert first_before == "Failed to save the file · Check the free space"
        assert first_after == "Failed to save the file"


class TestReasonSplit:
    """실패 사유 — 첫 「 · 」에서 앞 · 뒤로 나누고 전문은 툴팁에 둔다."""

    @pytest.mark.parametrize(
        "reason,expected",
        [
            ("Save failed · Check the disk", ("Save failed", "Check the disk")),
            ("A · B · C", ("A", "B · C")),  # 첫 「 · 」에서만 나눈다
            ("No joiner here", ("No joiner here", "")),
        ],
    )
    def test_split_reason_cuts_at_the_first_joiner(self, reason, expected) -> None:
        """사유에 「 · 」가 있으면 첫 것에서 나뉘고, 없으면 (전체, "")여야 한다."""
        assert card_text.split_reason(reason) == expected

    def test_tail_goes_at_level_one_and_the_tip_keeps_the_whole_reason(self) -> None:
        """실패 카드의 상태 글이 단계 0이면 사유 첫 줄 전체, 단계 1부터 앞만, 툴팁은 늘 전문이어야 한다.

        stateMessage "Save failed · Check the disk\\nDetails" -> 0: "Save failed · Check the disk",
        1 · 7: "Save failed", 툴팁 전문
        """
        item = _item(DownloadState.FAILED)
        item.stateMessage = "Save failed · Check the disk\nDetails"
        assert card_text.slot_texts(item, 0).status == "Save failed · Check the disk"
        for level in (1, card_text.LEVEL_MAX):
            texts = card_text.slot_texts(item, level)
            assert texts.status == "Save failed"
            assert texts.status_tip == "Save failed · Check the disk\nDetails"

    @pytest.mark.parametrize("level", (0, 1, 7))
    def test_unknown_error_keeps_its_whole_text(self, level) -> None:
        """사유가 비면 어느 단계에서도 알 수 없는 오류 안내 전체가 상태 글이어야 한다."""
        item = _item(DownloadState.FAILED)
        item.stateMessage = ""
        assert card_text.slot_texts(item, level).status == "Unknown error - check the log"


def _list(widths_cards: list[ContentItem], width: int = 900) -> tuple[ContentListView, list]:
    view = ContentListView()
    model = ContentListModel()
    view.setModel(model)
    for item in widths_cards:
        model.addItem(item)
    view.resize(width, 500)
    view.show()
    for _ in range(3):
        QApplication.processEvents()
    return view, [view.widgetFor(item) for item in widths_cards]


def _list_at(view: ContentListView, width: int) -> None:
    view.resize(width, 500)
    for _ in range(3):
        QApplication.processEvents()
    assert view.width() == width


class TestListWideYear:
    """날짜 연도 — 목록 전체가 함께 뺀다. 판정은 창 폭 · 카드 추가 때만이다."""

    def _cards(self) -> tuple[ContentListView, list]:
        long_name = _item(channel="a channel name that is very long indeed")
        short_name = _item(channel="LCK")
        return _list([long_name, short_name])

    def _drop_width(self, view: ContentListView, cards: list) -> int:
        """1px씩 좁혀 연도가 처음 빠진 목록 폭."""
        width = view.width()
        while cards[0].dateLabel.text() == "2026. 10. 26.":
            width -= 1
            assert width > 200, "연도가 빠지지 않았다"
            _list_at(view, width)
        return width

    def test_year_goes_from_every_card_at_once(self, qapp) -> None:
        """긴 채널명 카드의 1줄이 연도와 안 들어가면 짧은 이름 카드도 함께 연도를 빼야 한다.

        채널명 긴 카드 · 「LCK」 카드, 목록을 1px씩 좁힘
        -> 바뀐 폭에서 두 카드 다 "10. 26.", 한 px 넓으면 두 카드 다 "2026. 10. 26." · 긴 채널명은 넓은
        목록(900)에서와 같은 글(상한 150px까지 — 연도가 채널명을 줄이지 않는다)
        """
        view, cards = self._cards()
        wide_name = QLabel.text(cards[0].channelNameLabel)
        drop = self._drop_width(view, cards)
        assert [card.dateLabel.text() for card in cards] == ["10. 26.", "10. 26."]
        _list_at(view, drop + RETURN_SLACK + 1)
        _list_at(view, drop + 1)
        assert [card.dateLabel.text() for card in cards] == ["2026. 10. 26."] * 2
        assert QLabel.text(cards[0].channelNameLabel) == wide_name

    def test_year_returns_only_with_eight_more_pixels(self, qapp) -> None:
        """연도를 뺀 뒤에는 뺀 폭 + 8에서도 빠진 채, + 9에서 돌아와야 한다.

        연도가 빠진 폭 D(들어가던 마지막 폭 D+1) -> D+8 "10. 26.", D+9 "2026. 10. 26."
        """
        view, cards = self._cards()
        drop = self._drop_width(view, cards)
        _list_at(view, drop + RETURN_SLACK)
        assert cards[0].dateLabel.text() == "10. 26."
        _list_at(view, drop + RETURN_SLACK + 1)
        assert cards[0].dateLabel.text() == "2026. 10. 26."

    def test_a_new_card_takes_the_list_decision(self, qapp) -> None:
        """연도를 뺀 목록에 카드가 들어오면 그 카드도 연도 없이 보여야 한다."""
        view, cards = self._cards()
        self._drop_width(view, cards)
        item = _item(channel="LCK")
        view._model.addItem(item)
        for _ in range(3):
            QApplication.processEvents()
        assert view.widgetFor(item).dateLabel.text() == "10. 26."

    def test_state_change_does_not_refit_the_list(self, qapp, monkeypatch) -> None:
        """카드 상태가 바뀌면 목록 전체 연도 판정이 돌지 않아야 한다.

        카드 둘, 한 카드를 받는 중으로 바꿔 알림 -> `_fitDates` · `_fitNewDates` 부름 0번
        """
        view, cards = self._cards()
        calls = []
        monkeypatch.setattr(view, "_fitDates", lambda: calls.append("all"))
        monkeypatch.setattr(view, "_fitNewDates", lambda: calls.append("new"))
        item = cards[1].item
        item.downloadState = DownloadState.RUNNING
        item.download_progress = 10
        view._model.notifyChanged(item)
        for _ in range(3):
            QApplication.processEvents()
        assert calls == []


def _pixel(widget: QWidget, x: int, y: int) -> QColor:
    """위젯을 grab한 그림에서 논리 좌표 (x, y)의 색 — 장치 화소 배율을 곱해 읽는다."""
    image = widget.grab().toImage()
    ratio = image.devicePixelRatio()
    return image.pixelColor(QPoint(int(x * ratio), int(y * ratio)))


def _token(name: str) -> QColor:
    return QColor(theme.current_tokens()[name])


def _close(a: QColor, b: QColor, slack: int = 3) -> bool:
    return (
        max(abs(a.red() - b.red()), abs(a.green() - b.green()), abs(a.blue() - b.blue())) <= slack
    )


def _empty_y(card: ContentItemWidget) -> int:
    """카드 면에서 글 · 썸네일 · 막대가 없는 높이 — 막대 바로 위 안쪽 여백."""
    return card.progressBar.geometry().top() - 2


class TestProgressBar:
    """막대 — 트랙은 카드 테두리 색, 받는 중 · 일시정지는 진행만큼, 완료 · 실패는 끝까지(상태 색)."""

    @pytest.mark.parametrize(
        "state,token",
        [(DownloadState.FINISHED, "stateFinished"), (DownloadState.FAILED, "stateFailed")],
    )
    def test_finished_and_failed_bars_are_full_in_the_state_colour(
        self, qapp, state, token
    ) -> None:
        """완료 · 실패 카드면 막대가 끝까지 차고 가운데 화소가 그 상태 색(상태 원과 같은 토큰)이어야 한다."""
        card = _card(_item(state))
        bar = card.progressBar
        assert bar.isVisible()
        assert bar.value() == bar.maximum() == 100
        assert _close(_pixel(bar, bar.width() // 2, bar.height() // 2), _token(token))

    def test_running_bar_shows_the_progress_over_the_border_coloured_track(self, qapp) -> None:
        """받는 중 30% 카드면 막대 10% 자리가 진행 색, 90% 자리가 카드 테두리 색이어야 한다."""
        item = _item(DownloadState.RUNNING)
        item.download_progress = 30
        card = _card(item)
        bar = card.progressBar
        y = bar.height() // 2
        assert _close(_pixel(bar, bar.width() // 10, y), _token("stateRunning"))
        assert _close(_pixel(bar, bar.width() * 9 // 10, y), _token("border"))

    def test_paused_bar_keeps_the_value_in_the_paused_colour(self, qapp) -> None:
        """일시정지 54% 카드면 막대 값 54 · 10% 자리가 일시정지 색이어야 한다."""
        item = _item(DownloadState.PAUSED)
        item.download_progress = 54
        card = _card(item)
        bar = card.progressBar
        assert bar.value() == 54
        assert _close(_pixel(bar, bar.width() // 10, bar.height() // 2), _token("statePaused"))

    @pytest.mark.parametrize("cut", (False, True))
    def test_preparing_bar_has_no_value(self, qapp, cut) -> None:
        """준비 중 · 자르기 준비 중 카드면 막대 범위가 0~0(값 없이 움직임)이어야 한다."""
        item = _item(DownloadState.RUNNING)
        if cut:
            item.selections = TWO_SECTIONS
            item.post_process = True
            item.cut_preparing = True
        else:
            item.preparing = True
        card = _card(item)
        assert card.progressBar.isVisible()
        assert card.progressBar.maximum() == 0

    def test_paused_while_preparing_shows_the_word_without_a_percentage(self, qapp) -> None:
        """준비 중에 일시정지한 카드면 상태 글이 「Paused」뿐이어야 한다."""
        item = _item(DownloadState.PAUSED)
        item.prepare_pending = True
        card = _card(item)
        assert card.statusLabel.text() == "Paused"

    def test_full_bar_is_clipped_to_the_card_corner(self, qapp) -> None:
        """완료 카드의 꽉 찬 막대도 카드 면의 둥근 아래 모서리 밖 화소를 칠하지 않아야 한다.

        카드 면 왼쪽 아래 끝 화소(테두리 안쪽 1, 1) -> 완료 색이 아니다
        """
        card = _card(_item(DownloadState.FINISHED))
        frame = card.contentFrame
        corner = _pixel(frame, 1, frame.height() - 2)
        assert not _close(corner, _token("stateFinished"), slack=20)


def _srgb_to_lab(color: QColor) -> tuple[float, float, float]:
    """sRGB → CIELAB(D65) — 테스트가 따로 셈한다."""

    def linear(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(v / 255) for v in (color.red(), color.green(), color.blue()))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def _de00(a: QColor, b: QColor) -> float:
    """CIEDE2000 색차 — 테스트가 따로 셈한다(Sharma 2005 식)."""
    l1, a1, b1 = _srgb_to_lab(a)
    l2, a2, b2 = _srgb_to_lab(b)
    c_bar = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(c_bar**7 / (c_bar**7 + 25**7)))
    a1p, a2p = a1 * (1 + g), a2 * (1 + g)
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360
    dl, dc = l2 - l1, c2p - c1p
    dh = h2p - h1p
    if c1p * c2p == 0:
        dh = 0
    elif dh > 180:
        dh -= 360
    elif dh < -180:
        dh += 360
    d_h = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dh / 2))
    l_bar, cp_bar = (l1 + l2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        h_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        h_bar = (h1p + h2p) / 2
    else:
        h_bar = (h1p + h2p + 360) / 2 if h1p + h2p < 360 else (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(h_bar - 30))
        + 0.24 * math.cos(math.radians(2 * h_bar))
        + 0.32 * math.cos(math.radians(3 * h_bar + 6))
        - 0.20 * math.cos(math.radians(4 * h_bar - 63))
    )
    s_l = 1 + 0.015 * (l_bar - 50) ** 2 / math.sqrt(20 + (l_bar - 50) ** 2)
    s_c = 1 + 0.045 * cp_bar
    s_h = 1 + 0.015 * cp_bar * t
    r_t = (
        -2
        * math.sqrt(cp_bar**7 / (cp_bar**7 + 25**7))
        * math.sin(math.radians(60 * math.exp(-(((h_bar - 275) / 25) ** 2))))
    )
    return math.sqrt(
        (dl / s_l) ** 2 + (dc / s_c) ** 2 + (d_h / s_h) ** 2 + r_t * (dc / s_c) * (d_h / s_h)
    )


def _hue(color: QColor) -> float:
    return colorsys.rgb_to_hls(color.redF(), color.greenF(), color.blueF())[0] * 360


def _hue_gap(a: float, b: float) -> float:
    return min(abs(a - b), 360 - abs(a - b))


def _contrast(fore: QColor, back: QColor) -> float:
    """WCAG 대비 — 테스트가 따로 셈한다."""

    def luminance(color: QColor) -> float:
        def channel(v: int) -> float:
            c = v / 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        return (
            0.2126 * channel(color.red())
            + 0.7152 * channel(color.green())
            + 0.0722 * channel(color.blue())
        )

    high, low = sorted((luminance(fore), luminance(back)), reverse=True)
    return (high + 0.05) / (low + 0.05)


TINT_TARGET = {"dark": 12, "light": 10}  # 색조 세기 목표(ΔE00) — 설계 §8의 6


class TestTint:
    """완료 · 실패 카드 전체 색조 — 상태 색상 · ΔE00 세기 · 그 위 글 대비."""

    @pytest.mark.parametrize("scheme", ("dark", "light"))
    @pytest.mark.parametrize(
        "state,state_token",
        [(DownloadState.FINISHED, "stateFinished"), (DownloadState.FAILED, "stateFailed")],
    )
    def test_tint_pixel_has_the_state_hue_and_the_target_strength(
        self, qapp, scheme, state, state_token
    ) -> None:
        """완료 · 실패 카드 면의 빈 자리 화소가 상태 색상(±25°)이고 카드 바탕과 ΔE00 목표 −1.5 ~ +0.5여야 한다.

        다크 목표 12 · 라이트 목표 10, 화소 = 막대 바로 위 안쪽 여백
        """
        theme.set_color_scheme(scheme)
        qapp.setPalette(theme.build_palette())
        qapp.setStyleSheet(
            theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH))
        )
        try:
            card = _card(_item(state))
            frame = card.contentFrame
            pixel = _pixel(frame, frame.width() // 2, _empty_y(card))
            base = _token("cardBg")
            assert _hue_gap(_hue(pixel), _hue(_token(state_token))) <= 25
            strength = _de00(pixel, base)
            target = TINT_TARGET[scheme]
            assert target - 1.5 <= strength <= target + 0.5, f"ΔE00 {strength:.2f}"
        finally:
            theme.set_color_scheme("dark")

    @pytest.mark.parametrize("scheme", ("dark", "light"))
    @pytest.mark.parametrize("hovered", (False, True))
    @pytest.mark.parametrize("name", ("Finished", "Failed"))
    def test_text_keeps_contrast_on_the_tint(self, scheme, hovered, name) -> None:
        """색조(호버 포함) 위의 본문 · 보조 글 · 상태 글 대비가 4.5 이상이어야 한다."""
        tokens = theme.DARK if scheme == "dark" else theme.LIGHT
        ground = QColor(tokens[f"cardTint{name}" + ("Hover" if hovered else "")])
        for text in ("text", "textMuted", f"state{name}OnTint"):
            assert _contrast(QColor(tokens[text]), ground) >= 4.5, text

    def test_running_card_has_no_tint(self, qapp) -> None:
        """받는 중 카드 면의 빈 자리 화소가 카드 바탕색이어야 한다."""
        item = _item(DownloadState.RUNNING)
        item.download_progress = 30
        card = _card(item)
        frame = card.contentFrame
        assert _close(_pixel(frame, frame.width() // 2, _empty_y(card)), _token("cardBg"))


class TestFill:
    """배경 채움(설정 키 cardProgress = fill) — 받는 중은 진행만큼, 일시정지는 회색."""

    @pytest.mark.parametrize(
        "state,token",
        [(DownloadState.RUNNING, "cardFillRunning"), (DownloadState.PAUSED, "cardFillPaused")],
    )
    def test_fill_paints_the_progress_from_the_left(self, qapp, state, token) -> None:
        """채움 방식에서 40% 카드면 면의 20% 자리가 채움 색, 80% 자리가 카드 바탕, 막대는 숨어야 한다."""
        _display(progress="fill", animations=False)
        item = _item(state)
        item.download_progress = 40
        card = _card(item)
        frame = card.contentFrame
        y = _empty_y(card)
        assert not card.progressBar.isVisible()
        assert _close(_pixel(frame, frame.width() // 5, y), _token(token))
        assert _close(_pixel(frame, frame.width() * 4 // 5, y), _token("cardBg"))

    def test_bar_mode_does_not_fill(self, qapp) -> None:
        """막대 방식(기본)에서 40% 받는 중 카드면 면의 20% 자리가 카드 바탕이어야 한다."""
        item = _item(DownloadState.RUNNING)
        item.download_progress = 40
        card = _card(item)
        frame = card.contentFrame
        assert _close(_pixel(frame, frame.width() // 5, _empty_y(card)), _token("cardBg"))

    def test_fill_moves_between_ticks_when_animations_are_on(self, qapp) -> None:
        """애니메이션 켬이면 40% → 60% 틱에서 채움이 이어 움직이고, 반쯤 시간에 40과 60 사이여야 한다."""
        _display(progress="fill", animations=True)
        item = _item(DownloadState.RUNNING)
        item.download_progress = 40
        card = _card(item)
        item.download_progress = 60
        card.setData(item, 0)
        motion = card.motion()
        assert motion is not None
        motion.setCurrentTime(motion.duration() // 2)
        fraction, _ = card.contentFrame.fill()
        assert 0.4 < fraction < 0.6


class TestStopMotion:
    """중지 모션 — 대기로 돌아가는 카드의 막대 · 채움이 지금 색으로 왼쪽 끝까지 줄어든다."""

    def _stopped(self, progress: int = 60) -> ContentItemWidget:
        item = _item(DownloadState.RUNNING)
        item.download_progress = progress
        card = _card(item)
        item.downloadState = DownloadState.WAITING
        card.setData(item, 0)
        return card

    def test_status_switches_at_once_and_the_bar_shrinks_fast_then_slow(self, qapp) -> None:
        """중지 확인 뒤 대기 카드면 상태 글은 바로 대기, 막대는 반쯤 시간에 이미 1/4 아래 · 받는 중 색이어야 한다.

        받는 중 60% → 대기, 모션 시간 150/300ms -> 상태 글 "Download waiting", 막대 값 < 15, state "running"
        -> 300ms -> 막대 숨음, 모션 없음
        """
        card = self._stopped()
        motion = card.motion()
        assert motion is not None and motion.duration() == 300
        assert card.statusLabel.text() == "Download waiting"
        assert card.statusLabel.property("state") == "waiting"
        motion.setCurrentTime(150)
        bar = card.progressBar
        assert bar.isVisible() and 0 < bar.value() < 15
        assert bar.property("state") == "running"
        motion.setCurrentTime(300)
        QApplication.processEvents()
        assert card.motion() is None
        assert not bar.isVisible()

    def test_without_animations_the_bar_goes_at_once(self, qapp) -> None:
        """애니메이션 끔이면 중지 확인 뒤 대기 카드의 막대가 바로 숨고 모션이 없어야 한다."""
        _display(animations=False)
        card = self._stopped()
        assert card.motion() is None
        assert not card.progressBar.isVisible()

    def test_restart_during_the_motion_stops_it(self, qapp) -> None:
        """모션 도중 다시 받는 중(10%)이 되면 모션이 멈추고 막대가 10이어야 한다."""
        card = self._stopped()
        card.motion().setCurrentTime(100)
        item = card.item
        item.downloadState = DownloadState.RUNNING
        item.download_progress = 10
        card.setData(item, 0)
        assert card.motion() is None
        assert card.progressBar.value() == 10


class TestThumbnailShapes:
    """썸네일 두 모양 — 기본 110×62(세 줄 옆), 줄이기 68×38(위 두 줄 옆, 3줄은 썸네일 밑까지)."""

    def test_default_thumbnail_sits_beside_all_three_rows(self, qapp) -> None:
        """기본이면 썸네일이 110×62이고 3줄이 썸네일 오른쪽에서 시작해야 한다."""
        card = _card(_item())
        thumb = card.thumbnailLabel
        assert (thumb.width(), thumb.height()) == (110, 62)
        row_x = card.statusRow.mapTo(card.contentFrame, QPoint(0, 0)).x()
        assert row_x > thumb.geometry().right()

    def test_compact_thumbnail_sits_beside_the_top_two_rows(self, qapp) -> None:
        """줄이기면 썸네일이 68×38이고 1 · 2줄은 그 오른쪽, 3줄은 썸네일 왼쪽 끝에서 시작해야 한다."""
        _display(compact_thumbnail=True)
        card = _card(_item())
        thumb = card.thumbnailLabel
        assert (thumb.width(), thumb.height()) == (68, 38)
        frame = card.contentFrame
        assert card.statusRow.mapTo(frame, QPoint(0, 0)).x() == thumb.geometry().left()
        assert card.titleLabel.mapTo(frame, QPoint(0, 0)).x() > thumb.geometry().right()
        assert card.statusRow.mapTo(frame, QPoint(0, 0)).y() > thumb.geometry().bottom()

    def test_every_card_in_the_list_has_the_same_shape(self, qapp) -> None:
        """줄이기 설정에서 목록 카드 셋 모두 썸네일이 68×38이어야 한다(상태가 달라도)."""
        _display(compact_thumbnail=True)
        items = [
            _item(state)
            for state in (DownloadState.WAITING, DownloadState.FINISHED, DownloadState.FAILED)
        ]
        _, cards = _list(items)
        assert {(c.thumbnailLabel.width(), c.thumbnailLabel.height()) for c in cards} == {(68, 38)}

    def test_card_height_is_the_same_for_both_shapes(self, qapp) -> None:
        """두 썸네일 모양의 카드 높이가 같아야 한다(목록이 들썩이지 않는다)."""
        regular = _card(_item()).sizeHint().height()
        _display(compact_thumbnail=True)
        compact = _card(_item()).sizeHint().height()
        assert regular == compact


def _window_with(samples: tuple) -> tuple[VodDownloader, list[ContentItemWidget]]:
    win = VodDownloader()
    win.monitorTimer.stop()
    items = []
    for state, fields in samples:
        item = _item(state, path=OTHER_DIR, channel="a channel name longer than the cap")
        item.download_progress = 100
        item.download_size = 1023 * 1024 * 1024 + 1000 * 1024  # 「1023.98 MB」
        item.download_remain_time = "99:59:59"
        item.download_time = "99:59:59"
        item.download_speed = "999.9 MB/s"
        for name, value in fields.items():
            setattr(item, name, value)
        win.contentManager.model.addItem(item)
        items.append(item)
    QApplication.processEvents()
    return win, [win.listView.widgetFor(item) for item in items]


class TestWindowMinimum:
    """창 최소폭 — 가장 나쁜 카드를 다 접은 폭. 카드 수와 무관하고, 그 폭에서 어떤 카드도 넘치지 않는다."""

    @pytest.mark.parametrize("compact", (False, True))
    def test_minimum_is_the_same_for_zero_one_and_fifty_cards(self, qapp, compact) -> None:
        """대기 카드 0 · 1 · 50장인 창의 최소폭이 같아야 한다(썸네일 두 모양 각각)."""
        _display(compact_thumbnail=compact)
        widths = []
        for count in (0, 1, 50):
            win, _ = _window_with(((DownloadState.WAITING, {}),) * count)
            widths.append(win.minimumWidth())
        assert len(set(widths)) == 1, widths

    @pytest.mark.parametrize("compact", (False, True))
    def test_worst_cards_fit_at_the_minimum_width(self, qapp, compact) -> None:
        """창 최소폭에서 가장 나쁜 견본 카드마다 넘침이 없고 진행률 · 상태 단어 · 사유 앞이 남아야 한다.

        test_shell_layout의 견본(상태마다 가장 긴 모양) + 속도 999.9 MB/s, 창 = 최소폭
        -> 카드 오른쪽 끝 < 목록 뷰포트 폭, 3줄 위젯 오른쪽 끝 ≤ 3줄 폭, 받는 중 · 일시정지 · 완료 · 대기의
        상태 글은 잘리지 않음, 실패 상태 글은 앞 세 글자가 남음
        """
        _display(compact_thumbnail=compact)
        win, cards = _window_with(WORST_SAMPLES)
        win.resize(win.minimumWidth(), 900)
        win.show()
        for _ in range(4):
            QApplication.processEvents()
        assert win.width() == win.minimumWidth()
        viewport = win.listView.viewport()
        for card in cards:
            state = card.item.downloadState
            assert card.geometry().right() < viewport.width(), state
            for label in (card.statusLabel, card.fileSizeLabel):
                if label.isVisible():
                    assert label.geometry().right() <= card.statusRow.width(), (state, label.text())
            status = QLabel.text(card.statusLabel)
            if state == DownloadState.FAILED:
                assert status.startswith(card.statusLabel.text()[:3]), status
            else:
                assert status == card.statusLabel.text(), f"{state}: {status!r}"
            if state in (DownloadState.RUNNING, DownloadState.PAUSED):
                assert "100%" in status


class TestChannelDefaultImage:
    """채널 기본 그림 — 그림이 없거나 받기에 실패하면 테마 토큰으로 그린 사람 모양."""

    def _assert_default(self, label: QWidget) -> None:
        side = min(label.width(), label.height())
        assert not label.hasPicture()
        assert _close(_pixel(label, side // 2, int(side * 0.36)), _token("textMuted"), slack=12)
        assert _close(_pixel(label, side // 2, int(side * 0.08)), _token("thumbBg"), slack=12)

    def test_card_without_a_channel_image_draws_the_default(self, qapp) -> None:
        """채널 그림 주소가 없으면 머리 자리는 보조 글 색, 원 위쪽은 썸네일 바탕색이어야 한다."""
        card = _card(_item())
        self._assert_default(card.channelImageLabel)

    def test_failed_fetch_draws_the_default(self, qapp, monkeypatch) -> None:
        """채널 그림 받기가 실패하면(세션 예외) 기본 그림이어야 한다 — 받기 스레드는 그 자리에서 돈다."""

        class _Inline:
            def __init__(self, target, args=(), daemon=None) -> None:
                self._run = lambda: target(*args)

            def start(self) -> None:
                self._run()

        monkeypatch.setattr("app.widgets.widget.threading", type("T", (), {"Thread": _Inline}))
        item = _item()
        item.channel_image_url = "https://images.invalid/channel.png"
        card = _card(item)
        QApplication.processEvents()
        self._assert_default(card.channelImageLabel)

    def test_default_follows_the_light_theme(self, qapp) -> None:
        """라이트 테마에서도 기본 그림이 라이트 토큰 색이어야 한다."""
        theme.set_color_scheme("light")
        qapp.setPalette(theme.build_palette())
        qapp.setStyleSheet(
            theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH))
        )
        try:
            card = _card(_item())
            self._assert_default(card.channelImageLabel)
        finally:
            theme.set_color_scheme("dark")

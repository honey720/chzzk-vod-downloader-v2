"""카드 C3 — 3줄 접는 단계 · 실패 사유 나눔 · 날짜 연도 · 진행 막대 · 색조 · 배경 채움 · 중지 모션 ·
썸네일 두 모양 · 창 최소폭 · 채널 기본 그림 (#554).

폭은 절대 px로 박지 않는다 — 카드를 1px씩 좁혀 모양이 바뀌는 폭을 찾고, 그 폭이 보이는 위젯의 글 폭 ·
아이콘 폭 · 간격의 독립 합산과 같은지 잰다. 색은 theme 토큰과 화소를 견준다. 시간은 테스트가 정한다
(`QVariantAnimation.setCurrentTime`) — 실제 시간에 기대지 않는다.
"""

import colorsys
import json
import math
import time
from collections.abc import Callable, Iterator
from typing import Never

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QVariantAnimation
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

import CVDv2 as main_module
import app.network as network
import app.theme as theme
import app.viewmodels.card_display as card_display
from app.download.logger import DownloadLogger
from app.network import NetworkManager
from app.viewmodels import card_text
from app.viewmodels.card_order import CardOrder
from app.viewmodels.data import ContentItem
from app.viewmodels.model import ContentListModel
from app.views.card_edit_dialog import CardEditDialog
from app.views.mainWindow import VodDownloader
from app.widgets.view import ContentListView
from app.widgets.widget import STILL_STEP_MS, STOP_MOTION_MS, ContentItemWidget
from core.engine.base import PROGRESS_SECONDS
from core.models.download_state import DownloadState
from core.models.plan import TimeRange
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels
from tests.app.gui.card_helpers import row3_need as _row3_need
from tests.app.gui.test_header_layout import _font_whose_rounded_width_elides
from tests.app.gui.test_shell_layout import WORST_SAMPLES
from tests.mocks.mock_http import MockResponse

DEFAULT_DIR = "C:/dl"  # 전역 저장 위치 — 카드 저장 위치가 이것이면 3줄에 보이지 않는다
OTHER_DIR = "D:/vod/lck-finals"  # 전역과 다른 저장 위치 — 3줄에 보인다
MB = 1024 * 1024
TWO_SECTIONS = (TimeRange(600.0, 1200.0), TimeRange(1800.0, 2400.0))  # 합 20분
RETURN_SLACK = 8  # 되돌아갈 때 더 남아야 하는 폭(px) — 설계 §2 「+8」
WIDE = 2400  # 대체 글꼴(빈 글꼴 폴더)에서도 3줄이 접히지 않는 카드 폭(px)
GEOMETRY_WAIT_S = 2.0  # 크기 변경 반영을 기다리는 최대 시간(초) — xvfb 왕복은 이보다 훨씬 짧다
GEOMETRY_POLL_MS = 5  # 반영을 다시 확인하는 간격(ms)
HOST_SIZE = (WIDE + 200, 900)  # 카드 · 목록을 넣는 빈 창 크기(px) — 재는 가장 넓은 폭보다 크게


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


def _host(widget: QWidget) -> None:
    """`widget`을 보이는 빈 최상위 창의 자식으로 넣는다 — 자식이라 `resize()`가 바로 그 폭이 된다.

    최상위 창의 크기는 창 시스템이 정한다. xcb(xvfb)는 그 결과를 이벤트로 늦게 돌려주고, Qt는
    `resize()` 때 위젯 폭을 먼저 바꾸므로 앞 요청의 결과가 뒤늦게 와 폭을 되돌릴 수 있다(1px씩 좁힐 때
    요청 + 1). 자식 위젯의 크기는 창 시스템을 거치지 않는다. 창은 크기를 정한 뒤 띄우고 다시 바꾸지
    않는다. 창은 `widget`이 붙들고 `_drop_windows`가 정리한다 (#569).
    """
    host = QWidget()
    host.resize(HOST_SIZE[0], HOST_SIZE[1])
    widget.setParent(host)
    widget._test_host = host  # 파이썬 쪽 참조가 없으면 창이 지워지며 자식도 함께 지워진다
    host.show()


def _card(item: ContentItem, width: int = 900, hosted: bool = False) -> ContentItemWidget:
    """아이템으로 카드를 띄운다 — 해상도를 고른 뒤 상태를 건다(대기에서만 고른다).

    `hosted`면 카드를 최상위 창이 아니라 빈 창의 자식으로 띄운다(`_host`) — 폭을 여러 번 바꾸며 재는
    테스트가 쓴다.
    """
    state = item.downloadState
    item.downloadState = DownloadState.WAITING
    card = ContentItemWidget(item, 0)
    card.loadRepresentations()
    item.downloadState = state
    card.setData(item, 0)
    card.resize(width, card.sizeHint().height())
    if hosted:
        _host(card)
    card.show()
    QApplication.processEvents()
    # 카드만 띄운 창은 offscreen 포인터 자리에 따라 호버로 잡힐 수 있다 — 호버는 테스트가 정한다
    card.setHovered(False)
    QApplication.processEvents()
    return card


def _settle_width(widget: QWidget, width: int) -> None:
    """방금 요청한 크기 변경이 반영돼 `widget` 폭이 `width`가 될 때까지 기다린다(최대 2초).

    최상위 창이면 앞 요청의 결과가 뒤늦게 와 폭을 되돌릴 수 있다(`_host`). `QGuiApplication.sync()`
    (창 시스템과 한 번 왕복한 뒤 쌓인 이벤트를 처리)를 폭이 요청과 같아질 때까지 되풀이한다. 같아진
    뒤에 늦은 이벤트가 또 올 수 있어 최상위 창에서는 완전하지 않다 — 폭을 여러 번 바꾸는 테스트는
    `_host`를 쓴다. 자식 위젯이면 바로 끝난다. 기한이 지나도 기다리기만 끝낸다 — 단언은 부른 쪽이
    한다(클램프 확인) (#569).
    """
    deadline = time.monotonic() + GEOMETRY_WAIT_S
    QGuiApplication.sync()
    while widget.width() != width and time.monotonic() < deadline:
        QTest.qWait(GEOMETRY_POLL_MS)
        QGuiApplication.sync()


def _at(card: ContentItemWidget, width: int) -> None:
    """카드를 `width`로 놓는다 — 실제 폭이 요청과 같은지 단언한다(클램프면 아무것도 안 잰다)."""
    card.resize(width, card.sizeHint().height())
    _settle_width(card, width)
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


def _fold_steps(
    card: ContentItemWidget, start: int, stop: int, count: int
) -> list[tuple[int, tuple, tuple]]:
    """카드를 `start`에서 `stop`까지 1px씩 좁히며 모양이 바뀐 곳마다 (T, 앞 모양, 뒤 모양)을 모은다.

    T는 앞 모양이 마지막으로 들어간 폭이다. 각 T에서 독립 합산이 T의 3줄 폭과 같은지 보고, T+7에서는
    접힌 채, T+8에서는 앞 모양으로 돌아오는지 본 뒤 다시 좁혀 이어 간다. 바뀜을 `count`번 모으면
    멈추고, 카드 최소폭에 닿아도 멈춘다. 카드는 빈 창의 자식이어야 한다(`_host`) — 최상위 창이면
    늦은 크기 이벤트가 폭을 되돌린다.
    """
    assert not card.isWindow(), "전제: 카드가 빈 창의 자식이다(_card(..., hosted=True))"
    _at(card, start)
    chrome = card.width() - card.statusRow.width()  # 카드 폭 − 3줄 폭(썸네일 · 여백 · 테두리)
    steps = []
    previous = _signature(card)
    need = _row3_need(card)
    for width in range(start - 1, stop - 1, -1):
        if width < card.minimumSizeHint().width():
            break  # 카드 최소폭 아래는 클램프된다 — 거기서는 아무것도 안 잰다
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
        if len(steps) == count:
            break
        previous, need = current, _row3_need(card)
    return steps


class TestFoldOrder:
    """3줄 접는 차례 — 저장 위치 이름 → 저장 위치 → 길이 → 「/ 전체」 → 속도 → 남은 시간 (프로토 가)."""

    def test_running_card_folds_in_the_proto_order_with_the_return_slack(self, qapp) -> None:
        """받는 중 구간 카드면 경계마다 독립 합산 폭에서 접히고 그 차례가 프로토 가와 같아야 한다.

        저장 위치 다름 · 구간 둘(20:00) · 50 MB / 200 MB · 18.4 MB/s · 6:00 남음, 2400px → 280px 1px씩
        -> 바뀐 차례: 폴더 이름 숨음 → 폴더 아이콘 숨음 → 「20:00」 빠짐 → 「/ 200.00 MB」 빠짐 →
        「18.4 MB/s」 빠짐 → 「6:00 left」 빠짐, 경계마다 T−1 접힘 · T 들어감 · T+7 접힌 채 · T+8 되돌아감
        """
        card = _card(_sections_running(), hosted=True)
        steps = _fold_steps(card, WIDE, 280, count=7)  # 7번째가 없으면 끝까지 줄인 뒤 6개로 끝난다
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

        사유 "Failed to save the file · Check the free space" · 저장 위치 다름, 2400px부터 1px씩(첫 바뀜까지)
        -> 첫 바뀜: 상태 글 "Failed to save the file · Check …" → "Failed to save the file"
        """
        item = _item(DownloadState.FAILED, path=OTHER_DIR)
        item.stateMessage = "Failed to save the file · Check the free space"
        card = _card(item, hosted=True)
        steps = _fold_steps(card, WIDE, 280, count=1)
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
    _host(view)  # 목록 폭을 1px씩 바꾸며 잰다 — 최상위 창이면 늦은 크기 이벤트가 폭을 되돌린다
    view.show()
    for _ in range(3):
        QApplication.processEvents()
    return view, [view.widgetFor(item) for item in widths_cards]


def _list_at(view: ContentListView, width: int) -> None:
    assert not view.isWindow(), "전제: 목록이 빈 창의 자식이다(_list)"
    view.resize(width, 500)
    _settle_width(view, width)
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

    def test_fractional_date_width_does_not_shrink_the_channel_name(self, qapp) -> None:
        """글자 폭이 소수인 글꼴에서도 연도가 돌아온 폭의 긴 채널명 라벨 폭이 넓은 목록과 같아야 한다.

        앱 글꼴에 소수 글자 간격(정수 글 폭 칸에서 "2026. 10. 26."이 말줄임되는 값), 채널명 긴 카드 ·
        「LCK」 카드 -> 연도가 빠진 폭 + 1에서 두 카드 "2026. 10. 26." · 긴 채널명 라벨 폭 == 900에서의 폭
        """
        old = qapp.font()
        qapp.setFont(_font_whose_rounded_width_elides(old, "2026. 10. 26."))
        try:
            view, cards = self._cards()
            name = cards[0].channelNameLabel
            wide_width = name.width()
            drop = self._drop_width(view, cards)
            _list_at(view, drop + RETURN_SLACK + 1)
            _list_at(view, drop + 1)
            assert [card.dateLabel.text() for card in cards] == ["2026. 10. 26."] * 2
            assert name.isVisible() and name.width() == wide_width
        finally:
            qapp.setFont(old)

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

    def test_year_returns_when_the_blocking_card_is_removed(self, qapp) -> None:
        """연도를 막던 긴 채널명 카드를 지우면 남은 카드가 창 폭 그대로 연도를 되찾아야 한다.

        긴 채널명 · 「LCK」 카드, 연도가 빠진 폭에서 긴 채널명 카드를 지움(남은 카드는 +8이 남음)
        -> 「LCK」 카드 "2026. 10. 26."
        """
        view, cards = self._cards()
        self._drop_width(view, cards)
        short = cards[1]
        assert short.row1Need(True) + RETURN_SLACK <= short.row1Room(), (
            "전제: 남은 카드에 +8이 남는다"
        )
        view._model.removeRows(view._model.getRow(cards[0].item))
        for _ in range(3):
            QApplication.processEvents()
        assert short.dateLabel.text() == "2026. 10. 26."

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

    def test_card_without_a_date_does_not_take_the_year_away(self, qapp, monkeypatch) -> None:
        """날짜가 빈 카드의 1줄이 넘쳐도 날짜 있는 카드가 연도와 들어가면 연도가 남아야 한다.

        날짜 빈 카드 · 「LCK」 날짜 카드, 날짜 빈 카드의 1줄 폭 = 1줄 자리 + 1, 목록 폭을 1px 바꿈
        -> 날짜 카드는 연도와 들어가고 "2026. 10. 26."
        """
        view, cards = _list([_item(created=""), _item(channel="LCK")])
        blank, dated = cards
        assert blank.dateLabel.text() == ""
        # 넘침은 글꼴마다 닿는 폭이 달라(대체 글꼴은 카드 최소폭이 먼저 막는다) 폭을 직접 준다
        monkeypatch.setattr(blank, "row1Need", lambda with_year: blank.row1Room() + 1)
        _list_at(view, view.width() - 1)
        assert dated.row1Need(True) <= dated.row1Room(), "전제: 날짜 카드는 연도와 들어간다"
        assert dated.dateLabel.text() == "2026. 10. 26."


class _PaintCounter(QObject):
    """위젯 하나의 그리기 이벤트를 센다 — 그 위젯에만 거는 필터(앱 전체 필터가 아님)."""

    def __init__(self, widget: QWidget) -> None:
        super().__init__(widget)
        self.count = 0
        widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Paint:
            self.count += 1
        return False


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
        assert bar.value() == bar.maximum() == 10000
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
        assert bar.value() == 5400
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

    @pytest.mark.parametrize("cut", (False, True), ids=["preparing", "preparing-to-cut"])
    def test_preparing_card_shows_a_bar_without_a_value_in_fill_mode(self, qapp, cut) -> None:
        """채움 방식에서도 준비 중 · 자르기 준비 중 카드는 값 없는 막대가 보이고 면은 채우지 않아야 한다.

        배경 채움 방식, 받는 중 + 준비 중(또는 구간 자르기 준비 중) -> 막대 보임 · 범위 0~0 · 채움 0
        """
        _display(progress="fill", animations=False)
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
        assert card.contentFrame.fill()[0] == 0.0

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
        assert bar.isVisible() and 0 < bar.value() < 1500
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

    def test_finished_fill_motions_do_not_pile_up_on_the_card(self, qapp) -> None:
        """채움 방식에서 틱 다섯 번의 모션이 끝나면 카드에 남은 모션 객체가 0개여야 한다.

        받는 중 10% → 20 · 30 · 40 · 50 · 60%, 틱마다 모션을 끝까지 돌림 -> QVariantAnimation 자식 0개
        """
        _display(progress="fill", animations=True)
        item = _item(DownloadState.RUNNING)
        item.download_progress = 10
        card = _card(item)
        for progress in (20, 30, 40, 50, 60):
            item.download_progress = progress
            card.setData(item, 0)
            motion = card.motion()
            assert motion is not None
            motion.setCurrentTime(motion.duration())
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert card.findChildren(QVariantAnimation) == []

    def test_restart_during_the_motion_stops_it(self, qapp) -> None:
        """모션 도중 다시 받는 중(10%)이 되면 모션이 멈추고 막대 값이 1000(10%, 0.01% 단위)이어야 한다."""
        card = self._stopped()
        card.motion().setCurrentTime(100)
        item = card.item
        item.downloadState = DownloadState.RUNNING
        item.download_progress = 10
        card.setData(item, 0)
        assert card.motion() is None
        assert card.progressBar.value() == 1000

    def test_late_waiting_update_keeps_the_motion(self, qapp) -> None:
        """중지 모션 도중 대기 카드에 갱신이 한 번 더 와도 모션이 이어지고 끝에서 막대가 사라져야 한다.

        받는 중 60% → 대기(모션 시작), 100ms에 대기 갱신 -> 같은 모션 · 막대 보임 · 150ms 값 < 100ms 값,
        300ms -> 막대 숨음 · 모션 없음
        """
        card = self._stopped()
        motion = card.motion()
        motion.setCurrentTime(100)
        bar = card.progressBar
        before = bar.value()
        card.setData(card.item, 0)
        assert card.motion() is motion
        assert bar.isVisible() and bar.value() == before
        motion.setCurrentTime(150)
        assert 0 < bar.value() < before
        motion.setCurrentTime(STOP_MOTION_MS)
        assert card.motion() is None
        assert not bar.isVisible()


class TestBarTickMotion:
    """막대 방식(기본)도 받는 중 진행이 틱 사이를 이어 움직인다 — 배경 채움과 같은 방식 · 같은 시간."""

    def _ticked(self) -> ContentItemWidget:
        """받는 중 40% 카드에 60% 틱을 준다."""
        item = _item(DownloadState.RUNNING)
        item.download_progress = 40
        card = _card(item)
        item.download_progress = 60
        card.setData(item, 0)
        return card

    def test_bar_moves_between_ticks_when_animations_are_on(self, qapp) -> None:
        """애니메이션 켬이면 40% → 60% 틱에서 막대가 반쯤 시간에 40%와 60% 사이, 끝에 60%여야 한다.

        막대 방식 · 애니메이션 켬, 모션 시간 = 채움과 같은 시간의 절반 -> 4000 < 값 < 6000(0.01% 단위),
        끝 -> 6000 · 모션 없음
        """
        _display(progress="fill", animations=True)
        fill_duration = self._ticked().motion().duration()
        _display(animations=True)
        card = self._ticked()
        motion = card.motion()
        assert motion is not None
        assert motion.duration() == fill_duration
        bar = card.progressBar
        motion.setCurrentTime(motion.duration() // 2)
        assert bar.isVisible() and 4000 < bar.value() < 6000
        motion.setCurrentTime(motion.duration())
        assert bar.value() == 6000
        assert card.motion() is None

    def test_without_animations_the_bar_jumps_to_the_tick(self, qapp) -> None:
        """애니메이션 끔이면 40% → 60% 틱에서 막대 값이 바로 6000(60%)이고 모션이 없어야 한다."""
        _display(animations=False)
        card = self._ticked()
        assert card.motion() is None
        assert card.progressBar.value() == 6000

    def test_without_animations_the_bar_changes_once_per_step(
        self, qapp, qtbot, monkeypatch
    ) -> None:
        """애니메이션 끔이면 1초보다 잦은 진행 통지에도 막대가 `STILL_STEP_MS`에 한 번, 마지막 값으로 바뀌어야 한다.

        받는 중 40%, 시계 0ms에 50% · 300ms에 60% · 600ms에 70% 통지
        -> 50%는 바로(값 5000), 60% · 70% 통지 뒤에는 5000 그대로, 간격이 차면 7000으로 한 번만 바뀜 · 모션 없음
        """
        _display(animations=False)
        clock = [0.0]
        monkeypatch.setattr("app.widgets.widget._now_ms", lambda: clock[0])
        item = _item(DownloadState.RUNNING)
        item.download_progress = 40
        card = _card(item)
        bar = card.progressBar
        item.download_progress = 50
        card.setData(item, 0)
        assert bar.value() == 5000
        values: list[int] = []
        bar.valueChanged.connect(values.append)
        for at, progress in ((300.0, 60), (600.0, 70)):
            clock[0] = at
            item.download_progress = progress
            card.setData(item, 0)
            assert bar.value() == 5000, f"{at}ms"
        clock[0] = STILL_STEP_MS
        qtbot.waitUntil(lambda: bar.value() == 7000, timeout=2 * STILL_STEP_MS)
        assert values == [7000]
        assert card.motion() is None

    def test_pause_during_a_tick_motion_takes_the_paused_colour_at_once(self, qapp) -> None:
        """틱 모션 도중 같은 진행률로 일시정지가 오면 막대가 바로 일시정지 색 · 그 진행률이고 모션이 없어야 한다.

        받는 중 40% → 60% 틱, 모션 반쯤에 일시정지(60%) -> 막대 state "paused" · 값 6000 · 모션 없음
        """
        card = self._ticked()
        motion = card.motion()
        motion.setCurrentTime(motion.duration() // 2)
        card.item.downloadState = DownloadState.PAUSED
        card.setData(card.item, 0)
        bar = card.progressBar
        assert card.motion() is None
        assert bar.property("state") == "paused"
        assert bar.value() == 6000

    def test_finished_bar_motions_do_not_pile_up_on_the_card(self, qapp) -> None:
        """막대 방식에서 틱 다섯 번의 모션이 끝나면 카드에 남은 모션 객체가 0개여야 한다.

        받는 중 10% → 20 · 30 · 40 · 50 · 60%, 틱마다 모션을 끝까지 돌림 -> QVariantAnimation 자식 0개
        """
        _display(animations=True)
        item = _item(DownloadState.RUNNING)
        item.download_progress = 10
        card = _card(item)
        for progress in (20, 30, 40, 50, 60):
            item.download_progress = progress
            card.setData(item, 0)
            motion = card.motion()
            assert motion is not None
            motion.setCurrentTime(motion.duration())
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert card.findChildren(QVariantAnimation) == []

    def test_motion_lasts_the_whole_tick_and_keeps_moving(self, qapp) -> None:
        """이어 움직임 시간이 엔진 틱 간격과 같고, 틱 안 50% · 90% 시점에도 막대가 계속 차올라야 한다.

        40% → 60% 틱 -> 모션 시간 == PROGRESS_SECONDS × 1000ms(엔진의 진행 통지 간격), 값(50%) < 값(90%) < 6000
        """
        card = self._ticked()
        motion = card.motion()
        assert motion.duration() == round(PROGRESS_SECONDS * 1000)
        bar = card.progressBar
        motion.setCurrentTime(motion.duration() // 2)
        half = bar.value()
        motion.setCurrentTime(motion.duration() * 9 // 10)
        assert 4000 < half < bar.value() < 6000

    def test_early_tick_continues_from_the_drawn_value(self, qapp) -> None:
        """모션이 끝나기 전에 다음 틱이 오면 새 모션이 지금 그려진 값에서 시작하고 막대가 뒤로 가지 않아야 한다.

        40% → 60% 틱, 반쯤 시간에 80% 틱 -> 새 모션 시작 값이 그때 그린 막대와 같은 화소, 막대 값 줄지 않음
        """
        card = self._ticked()
        first = card.motion()
        first.setCurrentTime(first.duration() // 2)
        bar = card.progressBar
        drawn = bar.value()
        card.item.download_progress = 80
        card.setData(card.item, 0)
        second = card.motion()
        assert second is not None and second is not first
        width = bar.width()
        assert round(float(second.startValue()) * width) == round(drawn * width / 10000)
        assert bar.value() >= drawn
        second.setCurrentTime(1)
        assert bar.value() >= drawn

    def _slow_tick(self) -> ContentItemWidget:
        """받는 중 37% 카드에 38% 틱을 준다(카드 폭 900 — 1%가 막대 몇 px)."""
        item = _item(DownloadState.RUNNING)
        item.download_progress = 37
        card = _card(item)
        item.download_progress = 38
        card.setData(item, 0)
        return card

    def test_bar_draws_a_value_between_whole_percents(self, qapp) -> None:
        """37% → 38% 틱의 40% 시점이면 막대가 37.4% 화소 자리까지 칠해져야 한다.

        카드 폭 900, 모션 시간의 40% -> 값 == 37.4%를 막대 폭 1px로 묶은 값(0.01% 단위), 막대 폭의
        37.4% − 2px 자리 진행 색 · 37.4% + 2px 자리 테두리 색(37%와 38% 경계 사이)
        """
        card = self._slow_tick()
        motion = card.motion()
        motion.setCurrentTime(motion.duration() * 4 // 10)
        bar = card.progressBar
        assert bar.value() == round(round(0.374 * bar.width()) / bar.width() * 10000)
        edge = bar.width() * 0.374
        assert bar.width() * 0.37 < edge - 2 and edge + 2 < bar.width() * 0.38, (
            "전제: 1%가 4px보다 넓다"
        )
        y = bar.height() // 2
        assert _close(_pixel(bar, int(edge) - 2, y), _token("stateRunning"))
        assert _close(_pixel(bar, int(edge) + 2, y), _token("border"))

    def test_motion_frames_redraw_only_when_a_pixel_changes(self, qapp) -> None:
        """37% → 38% 모션을 1ms씩 돌리면 막대 값이 1px씩만 바뀌고 다시 그리기가 화소 수 남짓이어야 한다.

        카드 폭 900, 모션 시간 1000ms를 1ms씩(1001 프레임), 프레임마다 이벤트 처리
        -> 값이 바뀐 횟수 ≤ 38% · 37% 화소 차 + 2(첫 프레임 · 모션 끝은 같은 화소 안에서 정확한 값과
        묶은 값이 오감), 바뀔 때마다 그린 화소가 0 또는 1 늘어남(뒤로 가지 않음), 막대 다시 그리기 ≤
        화소 차 + 2
        """
        card = self._slow_tick()
        bar = card.progressBar
        width = bar.width()
        span = round(0.38 * width) - round(0.37 * width)
        assert span >= 4, "전제: 1%가 막대 4px 이상"
        values: list[int] = []
        bar.valueChanged.connect(values.append)
        motion = card.motion()
        motion.pause()  # 실제 시간으로 흐르지 않게 — 시간은 아래에서 테스트가 정한다
        paints = _PaintCounter(bar)
        QApplication.processEvents()
        paints.count = 0
        for ms in range(motion.duration() + 1):
            motion.setCurrentTime(ms)
            QApplication.processEvents()
        assert len(values) <= span + 2, values
        pixels = [round(value * width / 10000) for value in values]
        assert all(b - a in (0, 1) for a, b in zip(pixels, pixels[1:])), pixels
        assert paints.count <= span + 2, f"다시 그리기 {paints.count}번 · 화소 차 {span}"

    def test_width_change_snaps_to_the_new_width(self, qapp) -> None:
        """모션 도중 카드 폭이 바뀌면 다음 프레임의 막대 값이 새 막대 폭의 1px 단위여야 한다.

        37% → 38% 모션 30%, 카드 900 → 600, 모션 60% -> 값 == 그때 비율을 새 폭 1px로 묶은 값
        """
        card = self._slow_tick()
        motion = card.motion()
        motion.pause()  # 폭을 바꾸는 동안(이벤트 처리) 실제 시간으로 흐르지 않게
        motion.setCurrentTime(motion.duration() * 3 // 10)
        _at(card, 600)
        motion.setCurrentTime(motion.duration() * 6 // 10)
        bar = card.progressBar
        width = bar.width()
        fraction = float(motion.currentValue())
        assert bar.value() == round(round(fraction * width) / width * 10000)


class TestLastFoldOfTheCuttingCard:
    """마지막 접는 단계(⑥) — 자르는 카드는 「구간 N개」를 뺀다. 받는 중 · 일시정지의 받은 크기는 남는다."""

    def _sections(self, state: DownloadState, cutting: bool) -> ContentItem:
        item = _sections_running()
        item.downloadState = state
        item.post_process = cutting
        item.resolution = 1080
        return item

    @pytest.mark.parametrize("preparing", (False, True), ids=["cutting", "preparing-to-cut"])
    def test_cutting_card_drops_the_section_count_only_at_the_last_step(self, preparing) -> None:
        """자르는(준비 중 포함) 구간 둘 카드의 정보 글은 ⑤에서 「Sections 2」가 있고 ⑥에서 없어야 한다."""
        item = self._sections(DownloadState.RUNNING, cutting=True)
        item.cut_preparing = preparing
        sections = "Sections 2"
        assert sections in card_text.info_text(item, level=card_text.LEVEL_REMAIN - 1)
        last = card_text.info_text(item, level=card_text.LEVEL_REMAIN)
        assert sections not in last and last.startswith("1080p")

    @pytest.mark.parametrize("state", (DownloadState.RUNNING, DownloadState.PAUSED))
    def test_transferring_card_keeps_the_received_size_at_the_last_step(self, state) -> None:
        """전송 중(받는 중 · 일시정지) 구간 카드는 ⑥에서도 받은 크기가 남아야 한다."""
        item = self._sections(state, cutting=False)
        last = card_text.info_text(item, level=card_text.LEVEL_REMAIN)
        assert card_text.received_size_text(item, False) in last

    def test_cutting_card_at_the_window_minimum_shows_no_section_count(self, qapp) -> None:
        """창 최소폭에서 자르기 준비 중 견본 카드(구간 99개)의 정보 글에 「Sections」가 없어야 한다."""
        win, cards = _window_with(WORST_SAMPLES)
        win.resize(win.minimumWidth(), 900)
        win.show()
        for _ in range(4):
            QApplication.processEvents()
        assert win.width() == win.minimumWidth()
        cutting = [
            card
            for card in cards
            if card.item.downloadState == DownloadState.RUNNING and card.item.post_process
        ]
        assert cutting, "견본에 자르는 카드가 없다"
        for card in cutting:
            assert card.isVisible()
            assert "Sections" not in card.fileSizeLabel.text()


UPLOAD_DATE = "2026-06-15 09:59:58"  # 픽스처 응답의 publishDate(공개 시각)


@pytest.fixture
def upload_item(monkeypatch, load_mock_response: Callable[[str], str]) -> ContentItem:
    """liveOpenDate 없는 업로드 영상 응답(픽스처에서 키를 지움)을 조회해 만든 아이템."""
    content = json.loads(load_mock_response("video_member_only_13714380.json"))["content"]
    del content["liveOpenDate"]
    body = json.dumps({"content": content})
    monkeypatch.setattr(network._session, "get", lambda url, **kwargs: MockResponse(text=body))
    metadata = NetworkManager.get_video_info("1", {}).metadata
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        metadata,
        [[1080, "u1080"], [720, "u720"]],
        None,
        "",
        DEFAULT_DIR,
        "video",
        None,
    )
    item.downloadState = DownloadState.WAITING
    return item


class TestUploadVideoDate:
    """업로드 영상(liveOpenDate 없음)의 날짜 — publishDate를 쓰는 곳 넷이 함께 바뀐다."""

    def test_card_shows_the_publish_date(self, qapp, upload_item: ContentItem) -> None:
        """카드 1줄 날짜가 "2026. 06. 15."여야 한다."""
        card = _card(upload_item)
        assert card.dateLabel.isVisible()
        assert card.dateLabel.text() == "2026. 06. 15."

    def test_edit_dialog_head_shows_the_publish_date(self, qapp, upload_item: ContentItem) -> None:
        """카드 편집 창 머리 줄에 "2026. 06. 15."가 들어 있어야 한다."""
        dialog = CardEditDialog(
            upload_item, DEFAULT_DIR, lambda item: None, lambda item: None, lambda item: True
        )
        assert "2026. 06. 15." in dialog.metaLabel.text()

    def test_upload_order_puts_it_in_place_not_at_the_end(
        self, qapp, upload_item: ContentItem
    ) -> None:
        """업로드순(최신 먼저)에서 업로드 영상 카드가 날짜 자리에 들어가야 한다(모르는 날짜 뒤가 아님).

        추가 차례: 7월 1일 · 5월 1일 · 날짜 모름 · 업로드 영상(6월 15일)
        -> [7월 1일, 업로드 영상, 5월 1일, 날짜 모름]
        """
        model = ContentListModel()
        order = CardOrder(model)
        july = _item(created="2026-07-01 10:00:00")
        may = _item(created="2026-05-01 10:00:00")
        unknown = _item(created="")
        for item in (july, may, unknown, upload_item):
            model.addItem(item)
        order.setSort("upload", False)
        assert order.ordered() == [july, upload_item, may, unknown]

    def test_download_log_writes_the_publish_date(self, upload_item: ContentItem) -> None:
        """다운로드 시작 로그의 created_date 줄이 publishDate여야 한다."""
        logger = DownloadLogger()
        lines: list[str] = []
        logger.info = lines.append
        logger.log_download_info(upload_item)
        assert f"created_date: {UPLOAD_DATE}" in lines


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
        item.download_progress = 99.9  # 「99.9%」 — 가장 긴 진행률 글(#557)
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
                assert "99.9%" in status


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

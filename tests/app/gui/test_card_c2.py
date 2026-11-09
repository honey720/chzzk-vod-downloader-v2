"""카드 C2 — 줄 배치 · 카드 버튼 · 호버 · 키보드 초점 · 폴더 열기 (#550)."""

import os
import time
from collections.abc import Iterator
from typing import Never

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QEnterEvent, QFontMetrics
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import CVDv2 as main_module
import app.theme as theme
import app.widgets.view as view_module
from app.viewmodels.data import ContentItem
from app.viewmodels.item_state import ItemState
from app.viewmodels.model import ContentListModel
from app.widgets.view import ContentListView
from app.widgets.widget import CARD_BUTTON_NAMES, ContentItemWidget
from core.media.dash import parse_dash_manifest
from core.models.download_state import DownloadState
from core.models.plan import TimeRange
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels

DEFAULT_DIR = "C:/dl"  # 전역 저장 위치 — 카드의 저장 위치가 이것이면 3줄에 보이지 않는다


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
    """이 파일의 테스트가 띄운 창을 테스트 끝에 파괴한다."""
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


def _item(state=DownloadState.WAITING, title: str = "제목", path: str = DEFAULT_DIR) -> ContentItem:
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {
            "title": title,
            "category": "",
            "channelName": "채널",
            "createdDate": "2026-10-26 10:00:00",
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
    return item


def _card(state=DownloadState.WAITING, width: int = 700, **kwargs) -> ContentItemWidget:
    item = _item(state, **kwargs)
    card = ContentItemWidget(item, 0)
    card.loadRepresentations()
    card.setData(item, 0)
    card.resize(width, card.sizeHint().height())
    card.show()
    QApplication.processEvents()
    # 카드만 띄운 창은 창이 활성이 될 때 첫 초점 위젯(카드)이 초점을 받는다 — 초점은 테스트가 정한다
    card.clearFocus()
    QApplication.processEvents()
    return card


def _enter(card: ContentItemWidget) -> None:
    """카드에 들어감 이벤트를 보낸다(포인터는 옮기지 않는다)."""
    center = QPointF(card.rect().center())
    QApplication.sendEvent(card, QEnterEvent(center, center, card.mapToGlobal(center)))
    QApplication.processEvents()


def _leave(card: ContentItemWidget) -> None:
    QApplication.sendEvent(card, QEvent(QEvent.Type.Leave))
    QApplication.processEvents()


STATE_BUTTONS = [
    (DownloadState.WAITING, ["editButton", "deleteButton"]),
    (DownloadState.RUNNING, ["pauseButton", "deleteButton"]),
    (DownloadState.PAUSED, ["pauseButton", "deleteButton"]),
    (DownloadState.FINISHED, ["openDirectoryButton", "deleteButton"]),
    (DownloadState.FAILED, ["retryButton", "deleteButton"]),
    (ItemState.LOADING, ["deleteButton"]),
]


class TestCardButtons:
    """상태마다의 카드 버튼과 보이는 때."""

    @pytest.mark.parametrize(("state", "expected"), STATE_BUTTONS)
    def test_hovered_card_shows_the_buttons_of_its_state(self, qapp, state, expected) -> None:
        """호버한 카드면 그 상태의 버튼만 왼쪽부터 보여야 한다.

        대기 ✎ ✕ · 받는 중 ‖ ✕ · 일시정지 ▶ ✕ · 완료 폴더 ✕ · 실패 다시 시도 ✕ · 조회 중 ✕
        """
        card = _card(state)
        _enter(card)
        shown = [name for name in CARD_BUTTON_NAMES if getattr(card, name).isVisible()]
        assert shown == expected

    def test_buttons_are_hidden_until_hover_and_hidden_again_after_leaving(self, qapp) -> None:
        """카드 버튼은 호버가 아니면 숨고, 호버 동안만 보이고, 나가면 다시 숨어야 한다.

        대기 카드 · 나감 -> 숨음, 들어감 -> 보임, 나감 -> 숨음
        """
        card = _card()
        _leave(card)  # offscreen은 띄울 때 포인터 자리에 따라 들어감을 보낼 수 있다
        assert not card.isHovered() and not card.hoverBox.isVisible()
        _enter(card)
        assert card.hoverBox.isVisible() and card.editButton.isVisible()
        _leave(card)
        assert not card.hoverBox.isVisible()

    def test_keyboard_focus_shows_the_buttons_and_the_focus_border(self, qapp, qtbot) -> None:
        """Tab으로 초점을 받은 카드면 카드 버튼과 초점 테두리(keyFocus)가 보여야 한다.

        setFocus(TabFocusReason) -> hoverBox 보임 · contentFrame keyFocus True
        """
        card = _card()
        card.activateWindow()
        card.setFocus(Qt.FocusReason.TabFocusReason)
        qtbot.waitUntil(card.hasFocus)
        assert card.hoverBox.isVisible()
        assert card.contentFrame.property("keyFocus") is True

    def test_buttons_sit_at_the_right_end_of_row_one_and_take_no_tab(self, qapp) -> None:
        """카드 버튼이 1줄 오른쪽 끝(위끝 = 썸네일 위끝)에 놓이고 Tab 차례에 없어야 한다.

        상자 오른쪽 끝 = 1줄 오른쪽 끝, 상자 위끝 = 썸네일 위끝, 버튼 20×20 · NoFocus
        """
        card = _card()
        _enter(card)
        box = card.hoverBox.geometry()
        assert box.right() == card.topRow.geometry().right()
        assert box.top() == card.thumbnailLabel.geometry().top()
        for name in CARD_BUTTON_NAMES:
            button = getattr(card, name)
            assert (button.width(), button.height()) == (20, 20)
            assert button.focusPolicy() == Qt.FocusPolicy.NoFocus

    def test_the_shade_under_the_buttons_lets_clicks_through(self, qapp) -> None:
        """버튼 밑 오버레이는 1줄 높이로 버튼 상자를 덮고 마우스를 받지 않아야 한다.

        hoverShade 높이 = 버튼 높이, WA_TransparentForMouseEvents
        """
        card = _card()
        _enter(card)
        shade = card.hoverShade
        assert shade.isVisible()
        assert shade.height() == card.hoverBox.height()
        assert shade.geometry().contains(card.hoverBox.geometry())
        assert shade.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


class TestRows:
    """줄 자리 · 카드 높이 · 제목 폭."""

    def _rows(self, card: ContentItemWidget) -> tuple:
        return (
            card.height(),
            card.thumbnailLabel.geometry().getRect(),
            card.topRow.geometry().getRect(),
            card.titleRow.geometry().getRect(),
            card.statusRow.geometry().getRect(),
        )

    def test_card_is_80_high_with_a_110_by_62_thumbnail(self, qapp) -> None:
        """카드 높이가 80, 썸네일이 110×62, 썸네일 ↔ 글이 12여야 한다.

        대기 카드 -> 80, (110, 62), 1줄 왼끝 − 썸네일 오른끝 = 12
        """
        card = _card()
        thumb = card.thumbnailLabel.geometry()
        assert card.height() == 80
        assert (thumb.width(), thumb.height()) == (110, 62)
        assert card.topRow.geometry().left() - (thumb.right() + 1) == 12

    def test_row_positions_are_the_same_in_every_state(self, qapp) -> None:
        """상태가 달라도 카드 높이 · 썸네일 · 세 줄의 자리가 같아야 한다.

        대기 · 받는 중 · 일시정지 · 완료 · 실패 · 조회 중 카드 -> 같은 (높이, 썸네일, 1 · 2 · 3줄 사각형)
        """
        rows = {state: self._rows(_card(state)) for state, _ in STATE_BUTTONS}
        assert len(set(rows.values())) == 1, rows

    def test_row_three_starts_at_the_title_x_with_or_without_an_icon(self, qapp) -> None:
        """3줄 첫 항목(상태 원, 없으면 상태 글)의 왼쪽 x가 어느 상태에서나 제목 왼쪽 x와 같아야 한다.

        대기 · 조회 중(원 없음) -> statusLabel x == titleLabel x, 그 밖 -> statusIcon x == titleLabel x
        """
        for state, _ in STATE_BUTTONS:
            card = _card(state)
            head = card.statusLabel if card.statusIcon.isHidden() else card.statusIcon
            title_x = card.titleLabel.mapTo(card, QPoint(0, 0)).x()
            assert head.mapTo(card, QPoint(0, 0)).x() == title_x, state

    def test_title_label_is_as_wide_as_its_text_and_has_no_tooltip(self, qapp) -> None:
        """자리가 남는 카드의 제목 라벨 폭이 글자 폭이고 툴팁이 없어야 한다.

        제목 "짧은 제목" · 카드 폭 700 -> 라벨 폭 = 글자 폭 + 4, 툴팁 ""
        """
        card = _card(title="짧은 제목")
        label = card.titleLabel
        expected = QFontMetrics(label.font()).horizontalAdvance("짧은 제목") + 4
        assert label.width() == expected
        assert label.toolTip() == ""

    def test_elided_title_carries_the_full_title_in_the_tooltip(self, qapp) -> None:
        """잘린 제목이면 툴팁이 전체 제목이어야 한다.

        제목 = "아주 긴 제목 " × 30 · 카드 폭 400 -> 보이는 글 ≠ 제목, 툴팁 = 제목
        """
        title = "아주 긴 제목 " * 30
        card = _card(title=title, width=400)
        assert card.titleLabel.isElided()
        assert card.titleLabel.toolTip() == title

    def test_path_shows_only_when_it_differs_from_the_default(self, qapp) -> None:
        """저장 위치가 기본 위치면 숨고, 다르면 마지막 폴더 이름과 전체 경로 툴팁으로 보여야 한다.

        "C:/dl" -> 숨음, "D:/vod/lck" -> "lck" · 툴팁 "D:/vod/lck"
        """
        default = _card()
        assert not default.directoryLabel.isVisible() and not default.pathIcon.isVisible()
        other = _card(path="D:/vod/lck")
        assert other.directoryLabel.isVisible() and other.pathIcon.isVisible()
        assert other.directoryLabel.text() == "lck"
        assert other.directoryLabel.toolTip() == "D:/vod/lck"

    def test_path_regains_room_after_failing_at_a_narrow_width(self, qapp) -> None:
        """좁은 폭의 대기 카드가 실패한 뒤 넓혀지면 저장 위치가 다시 폭을 받아야 한다.

        "D:/vod/lck" 대기 · 폭 300 -> 실패 · 폭 900 -> 저장 위치 폭 ≥ "lck" 글 폭
        """
        card = _card(path="D:/vod/lck", width=300)
        card.item.downloadState = DownloadState.FAILED
        card.item.stateMessage = "실패"
        card.setData(card.item, 0)
        card.resize(900, card.height())
        QApplication.processEvents()
        label = card.directoryLabel
        assert label.isVisible()
        assert label.width() >= label.fontMetrics().horizontalAdvance("lck")

    def test_row_one_shows_the_upload_date_with_the_year(self, qapp) -> None:
        """1줄 날짜가 연도까지 적혀야 한다.

        createdDate "2026-10-26 10:00:00" -> "2026. 10. 26."
        """
        assert _card().dateLabel.text() == "2026. 10. 26."


class TestHover:
    """카드 바탕 한 단계 · 목록이 바뀐 뒤 다시 맞추기."""

    def test_hovered_card_paints_the_hover_background(self, qapp) -> None:
        """호버한 카드면 바탕이 cardHoverBg, 아니면 cardBg로 그려져야 한다.

        위 여백 한 점(테두리 안, 글 밖)의 색 -> 평소 cardBg, 호버 cardHoverBg
        """
        card = _card()
        _leave(card)
        point = QPoint(card.contentFrame.width() // 2, 4)
        tokens = theme.current_tokens()
        before = card.contentFrame.grab().toImage().pixelColor(point)
        _enter(card)
        after = card.contentFrame.grab().toImage().pixelColor(point)
        assert before == QColor(tokens["cardBg"])
        assert after == QColor(tokens["cardHoverBg"])

    def _list(self, count: int = 3) -> tuple[ContentListView, ContentListModel, list]:
        view = ContentListView()
        model = ContentListModel()
        view.setModel(model)
        view.resize(800, 500)
        view.show()
        items = []
        for number in range(count):
            item = _item(title=f"제목{number}")
            model.addItem(item)
            items.append(item)
        QApplication.processEvents()
        return view, model, items

    def test_deleting_the_hovered_card_hovers_the_card_that_moves_under_the_pointer(
        self, qapp, qtbot, monkeypatch
    ) -> None:
        """호버한 카드를 지우면 그 자리로 올라온 카드가 호버가 되어야 한다.

        카드 0 호버 · 포인터 밑 = 카드 1 · 카드 0 지움 -> 카드 1 호버, 목록의 호버 카드 = 카드 1
        """
        view, model, items = self._list()
        first, second = view.widgetFor(items[0]), view.widgetFor(items[1])
        _enter(first)
        assert view.hoverCard() is first
        monkeypatch.setattr(view_module, "widget_under_pointer", lambda: second.titleLabel)
        model.removeRows(0, 1)
        qtbot.waitUntil(second.isHovered)
        assert view.hoverCard() is second

    def test_resizing_the_list_moves_the_hover_to_the_card_under_the_pointer(
        self, qapp, qtbot, monkeypatch
    ) -> None:
        """목록 크기가 바뀌어 포인터 밑 카드가 달라지면 호버가 그 카드로 옮겨야 한다.

        카드 0 호버 · 포인터 밑 = 카드 2 · 목록 크기 변경 -> 카드 2 호버, 카드 0 호버 아님
        """
        view, model, items = self._list()
        first, third = view.widgetFor(items[0]), view.widgetFor(items[2])
        _enter(first)
        monkeypatch.setattr(view_module, "widget_under_pointer", lambda: third.contentFrame)
        view.resize(700, 500)
        qtbot.waitUntil(third.isHovered)
        assert not first.isHovered()

    def test_pointer_outside_the_cards_leaves_no_card_hovered(
        self, qapp, qtbot, monkeypatch
    ) -> None:
        """목록이 바뀐 뒤 포인터 밑에 카드가 없으면 호버 카드가 없어야 한다.

        카드 0 호버 · 포인터 밑 = 없음 · 목록 크기 변경 -> 호버 카드 None
        """
        view, model, items = self._list()
        first = view.widgetFor(items[0])
        _enter(first)
        monkeypatch.setattr(view_module, "widget_under_pointer", lambda: None)
        view.resize(700, 500)
        qtbot.waitUntil(lambda: not first.isHovered())
        assert view.hoverCard() is None


class TestStopAndDelete:
    """받는 중 카드의 ✕ — 「멈추고 지우기」."""

    def _view(self, state) -> tuple[ContentListView, ContentItem, list, list]:
        view = ContentListView()
        model = ContentListModel()
        view.setModel(model)
        item = _item(state)
        model.addItem(item)
        deleted: list = []
        stopped: list = []
        view.deleteRequest.connect(deleted.append)
        view.stopAndDeleteRequested.connect(stopped.append)
        return view, item, deleted, stopped

    @pytest.mark.parametrize("state", [DownloadState.RUNNING, DownloadState.PAUSED])
    @pytest.mark.parametrize(("answer", "expected"), [(True, 1), (False, 0)])
    def test_busy_card_asks_before_deleting(
        self, qapp, monkeypatch, state, answer, expected
    ) -> None:
        """받는 중 · 일시정지 카드의 ✕면 확인을 묻고, 고르면 멈추고 지우기만 · 취소면 아무 일도 없어야 한다.

        확인 True -> stopAndDeleteRequested 1번 · deleteRequest 0번, False -> 둘 다 0번
        """
        view, item, deleted, stopped = self._view(state)
        asked: list = []
        monkeypatch.setattr(
            view_module, "confirm_stop_and_delete", lambda parent: asked.append(parent) or answer
        )
        view.widgetFor(item).deleteButton.click()
        assert len(asked) == 1
        assert (len(stopped), len(deleted)) == (expected, 0)

    @pytest.mark.parametrize("state", [DownloadState.WAITING, DownloadState.FINISHED])
    def test_idle_card_deletes_without_asking(self, qapp, monkeypatch, state) -> None:
        """받는 중이 아닌 카드의 ✕면 묻지 않고 바로 지우기를 청해야 한다.

        대기 · 완료 -> 확인 0번, deleteRequest 1번
        """
        view, item, deleted, stopped = self._view(state)
        asked: list = []
        monkeypatch.setattr(view_module, "confirm_stop_and_delete", lambda p: asked.append(p))
        view.widgetFor(item).deleteButton.click()
        assert (asked, deleted, stopped) == ([], [item], [])

    def test_the_question_defaults_to_cancel_and_escape_cancels(self, qapp) -> None:
        """「멈추고 지우기」 확인의 기본 단추 · Esc가 취소이고 Esc면 거짓이어야 한다.

        확인 창 -> 기본 단추 = Cancel, Esc 단추 = Cancel, Esc 누름 -> False
        """
        seen: dict = {}

        def answer() -> None:
            box = QApplication.activeModalWidget()
            seen["box"] = isinstance(box, QMessageBox)
            if isinstance(box, QMessageBox):
                cancel = box.button(QMessageBox.StandardButton.Cancel)
                seen["default"] = box.defaultButton() is cancel
                seen["escape"] = box.escapeButton() is cancel
                QTest.keyClick(box, Qt.Key.Key_Escape)

        QTimer.singleShot(0, answer)
        result = view_module.confirm_stop_and_delete(None)
        assert seen == {"box": True, "default": True, "escape": True}
        assert result is False


class TestActivate:
    """더블 클릭 · Enter · 폴더 열기."""

    def _record_open(self, monkeypatch) -> list:
        opened: list = []
        monkeypatch.setattr(
            "app.widgets.widget.QProcess.startDetached",
            lambda program, args: opened.append(args[-1]) or True,
        )
        monkeypatch.setattr(
            "app.widgets.widget.QDesktopServices.openUrl",
            lambda url: opened.append(url.toLocalFile()) or True,
        )
        return opened

    def _record_warning(self, monkeypatch) -> list:
        warned: list = []
        monkeypatch.setattr(
            "app.widgets.widget.QMessageBox.warning",
            lambda parent, title, text: warned.append(text),
        )
        return warned

    def test_double_clicking_a_waiting_card_asks_for_the_editor(self, qapp, qtbot) -> None:
        """대기 카드를 더블 클릭하면 편집 창을 청해야 한다.

        대기 카드 · 1줄 빈자리 더블 클릭 -> editRequest 1번
        """
        card = _card()
        with qtbot.waitSignal(card.editRequest, timeout=1000):
            qtbot.mouseDClick(card.contentFrame, Qt.MouseButton.LeftButton, pos=QPoint(4, 4))

    def test_double_clicking_a_finished_card_opens_the_file_in_its_folder(
        self, qapp, qtbot, tmp_path, monkeypatch
    ) -> None:
        """완료 카드를 더블 클릭하면 받은 파일을 고른 채 폴더를 열어야 한다.

        완료 · output_path 있는 파일 -> 그 파일 경로로 폴더 열기 1번, 경고 0번
        """
        opened, warned = self._record_open(monkeypatch), self._record_warning(monkeypatch)
        card = _card(DownloadState.FINISHED)
        output = tmp_path / "제목 1080p.mp4"
        output.write_bytes(b"x")
        card.item.output_path = str(output)
        qtbot.mouseDClick(card.contentFrame, Qt.MouseButton.LeftButton, pos=QPoint(4, 4))
        assert [os.path.normcase(os.path.abspath(p)) for p in opened] == [
            os.path.normcase(str(output))
        ]
        assert warned == []

    def test_missing_file_only_warns(self, qapp, tmp_path, monkeypatch) -> None:
        """완료 카드의 받은 파일이 없으면 경고만 하고 폴더를 열지 않아야 한다.

        output_path = 없는 파일 -> 경고 1번, 열기 0번
        """
        opened, warned = self._record_open(monkeypatch), self._record_warning(monkeypatch)
        card = _card(DownloadState.FINISHED)
        card.item.output_path = str(tmp_path / "없음.mp4")
        card.activate()
        assert (len(warned), opened) == (1, [])

    def test_sections_all_missing_only_warns(self, qapp, tmp_path, monkeypatch) -> None:
        """구간 카드의 구간 파일이 하나도 없으면 경고만 해야 한다.

        구간 2개 · section_paths 둘 다 없음 -> 경고 1번, 열기 0번
        """
        opened, warned = self._record_open(monkeypatch), self._record_warning(monkeypatch)
        card = _card(DownloadState.FINISHED)
        card.item.selections = (TimeRange(0, 60), TimeRange(120, 180))
        card.item.section_paths = [str(tmp_path / "a_1.mp4"), str(tmp_path / "a_2.mp4")]
        card.activate()
        assert (len(warned), opened) == (1, [])

    def test_sections_partly_missing_opens_a_remaining_file(
        self, qapp, tmp_path, monkeypatch
    ) -> None:
        """구간 카드의 구간 파일이 일부만 남았으면 남은 파일을 열어야 한다.

        section_paths = [없음, 있음] -> 있는 파일로 열기 1번, 경고 0번
        """
        opened, warned = self._record_open(monkeypatch), self._record_warning(monkeypatch)
        card = _card(DownloadState.FINISHED)
        remaining = tmp_path / "a_2.mp4"
        remaining.write_bytes(b"x")
        card.item.selections = (TimeRange(0, 60), TimeRange(120, 180))
        card.item.section_paths = [str(tmp_path / "a_1.mp4"), str(remaining)]
        card.activate()
        assert [os.path.normcase(os.path.abspath(p)) for p in opened] == [
            os.path.normcase(str(remaining))
        ]
        assert warned == []

    @pytest.mark.parametrize(
        "state", [DownloadState.RUNNING, DownloadState.PAUSED, DownloadState.FAILED]
    )
    def test_double_click_does_nothing_on_other_states(
        self, qapp, qtbot, monkeypatch, state
    ) -> None:
        """받는 중 · 일시정지 · 실패 카드의 더블 클릭은 아무 일도 하지 않아야 한다.

        -> editRequest 0번, 열기 0번
        """
        opened = self._record_open(monkeypatch)
        card = _card(state)
        asked: list = []
        card.editRequest.connect(lambda: asked.append(1))
        qtbot.mouseDClick(card.contentFrame, Qt.MouseButton.LeftButton, pos=QPoint(4, 4))
        assert (asked, opened) == ([], [])

    def test_enter_on_a_focused_waiting_card_asks_for_the_editor(self, qapp, qtbot) -> None:
        """초점이 있는 대기 카드에서 Enter를 누르면 편집 창을 청해야 한다.

        setFocus(Tab) · Enter -> editRequest 1번
        """
        card = _card()
        card.activateWindow()
        card.setFocus(Qt.FocusReason.TabFocusReason)
        qtbot.waitUntil(card.hasFocus)
        with qtbot.waitSignal(card.editRequest, timeout=1000):
            QTest.keyClick(card, Qt.Key.Key_Return)


class TestTabOrder:
    """카드 하나가 Tab 한 번 — 보이는 차례로."""

    def _list(self) -> tuple[ContentListView, list]:
        view = ContentListView()
        model = ContentListModel()
        view.setModel(model)
        view.resize(800, 500)
        view.show()
        view.activateWindow()
        items = [_item(title=f"제목{number}") for number in range(3)]
        for item in items:
            model.addItem(item)
        QApplication.processEvents()
        return view, [view.widgetFor(item) for item in items]

    def test_tab_moves_to_the_next_card_in_layout_order(self, qapp, qtbot) -> None:
        """Tab이 레이아웃 차례(정렬 뒤)의 다음 카드로 가야 한다 — 만든 차례가 아니다.

        카드 0 · 1 · 2를 만들고 레이아웃을 0 · 2 · 1로 바꿈 · 카드 0에서 Tab -> 카드 2
        """
        view, cards = self._list()
        layout = view._layout
        layout.removeWidget(cards[2])
        layout.insertWidget(1, cards[2])
        cards[0].setFocus(Qt.FocusReason.TabFocusReason)
        qtbot.waitUntil(cards[0].hasFocus)
        QTest.keyClick(cards[0], Qt.Key.Key_Tab)
        qtbot.waitUntil(cards[2].hasFocus)

    def test_tab_skips_hidden_cards_and_shift_tab_goes_back(self, qapp, qtbot) -> None:
        """Tab이 숨은 카드를 건너뛰고 Shift+Tab이 앞 카드로 돌아와야 한다.

        카드 1 숨김 · 카드 0에서 Tab -> 카드 2, 카드 2에서 Shift+Tab -> 카드 0
        """
        view, cards = self._list()
        cards[1].hide()
        cards[0].setFocus(Qt.FocusReason.TabFocusReason)
        qtbot.waitUntil(cards[0].hasFocus)
        QTest.keyClick(cards[0], Qt.Key.Key_Tab)
        qtbot.waitUntil(cards[2].hasFocus)
        QTest.keyClick(cards[2], Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
        qtbot.waitUntil(cards[0].hasFocus)

    def test_card_buttons_are_not_tab_stops(self, qapp, qtbot) -> None:
        """카드에서 Tab을 누르면 그 카드의 버튼이 아니라 다음 카드로 가야 한다.

        카드 0 초점(버튼 보임) · Tab -> 카드 1 초점, 어떤 버튼도 초점 없음
        """
        view, cards = self._list()
        cards[0].setFocus(Qt.FocusReason.TabFocusReason)
        qtbot.waitUntil(cards[0].hasFocus)
        assert cards[0].hoverBox.isVisible()
        QTest.keyClick(cards[0], Qt.Key.Key_Tab)
        qtbot.waitUntil(cards[1].hasFocus)
        assert not cards[0].hoverBox.isVisible()


_DUPLICATE_HEIGHTS = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="a" width="1920" height="1080" bandwidth="5000000"><BaseURL>https://v.invalid/1080_low.mp4</BaseURL></Representation>
    <Representation id="b" width="1920" height="1080" bandwidth="6000000"><BaseURL>https://v.invalid/1080_high.mp4</BaseURL></Representation>
    <Representation id="c" width="1280" height="720" bandwidth="3000000"><BaseURL>https://v.invalid/720.mp4</BaseURL></Representation>
    <Representation id="d" width="1280" height="720" bandwidth="2000000"><BaseURL>https://v.invalid/720_low.mp4</BaseURL></Representation>
  </AdaptationSet></Period>
</MPD>"""


class _SlowResponse:
    def __init__(self, size: int) -> None:
        self.headers = {"content-length": str(size)}

    def raise_for_status(self) -> None:
        pass


class _SlowSession:
    """head()가 delay초 자고 나서 크기를 준다 — 조회가 사용자 조작보다 늦게 끝나는 창을 벌린다."""

    SIZES = {"u1080": 700_000_000, "u720": 400_000_000}  # 667.57 MB · 381.47 MB

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.answered: list[str] = []

    def head(self, url, timeout=None) -> _SlowResponse:
        time.sleep(self.delay)
        self.answered.append(url)
        return _SlowResponse(self.SIZES[url])

    def get(self, *a, **k) -> Never:
        raise RuntimeError("get은 쓰이지 않아야 한다 — content-length가 있다")


class TestResolutionStaysOnTheCard:
    """카드에 해상도 버튼은 없지만 정렬 · 크기 조회 · 기본 선택은 카드가 한다 (#550)."""

    def test_one_entry_per_height_from_a_manifest_with_duplicate_heights(self, qapp) -> None:
        """같은 높이 트랙이 둘인 매니페스트면 높이마다 대역폭 높은 트랙 하나만 남아야 한다.

        1080 둘(5M · 6M) · 720 둘(3M · 2M) -> [(1080, 1080_high), (720, 720)], 기본 선택 0번
        """
        reps, _, _ = parse_dash_manifest(_DUPLICATE_HEIGHTS)
        item = _item()
        item.unique_reps = [list(r) for r in reps]
        card = ContentItemWidget(item, 0)
        card.loadRepresentations()
        assert [(rep[0], rep[1]) for rep in item.unique_reps] == [
            (1080, "https://v.invalid/1080_high.mp4"),
            (720, "https://v.invalid/720.mp4"),
        ]
        assert card.pickedIndex() == 0 and item.base_url == "https://v.invalid/1080_high.mp4"

    def _slow_card(self, monkeypatch) -> tuple[ContentItemWidget, _SlowSession]:
        session = _SlowSession(0.3)
        monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: session)
        return _card(), session

    def test_a_pick_made_before_the_size_arrives_survives(self, qapp, qtbot, monkeypatch) -> None:
        """크기 조회가 끝나기 전에 고른 해상도(편집 창 저장)를 늦게 온 조회가 덮지 않아야 한다.

        720p 고름 · 두 조회가 0.3초 뒤 도착 -> 해상도 720 · 주소 u720, 정보 글에 「381.47 MB」
        """
        card, session = self._slow_card(monkeypatch)
        item = card.item
        assert session.answered == [], "전제: 아직 아무 크기도 도착하지 않았다"
        item.select_rep(item.unique_reps[1])  # 편집 창이 저장하며 쓰는 것과 같다
        card.setData(item, 0)  # 목록의 notifyChanged 길
        qtbot.waitUntil(lambda: len(session.answered) == 2, timeout=5000)
        qtbot.waitUntil(lambda: "381.47 MB" in card.fileSizeLabel.text(), timeout=2000)
        assert (str(item.resolution), item.base_url) == ("720", "u720")
        assert card.pickedIndex() == 1

    def test_without_a_pick_the_default_selection_takes_the_arrived_size(
        self, qapp, qtbot, monkeypatch
    ) -> None:
        """고르지 않았으면 최고 해상도가 기본 선택이고 도착한 크기가 정보 글 · total_size에 들어가야 한다.

        조회 둘 도착 -> 해상도 1080, 정보 글에 「667.57 MB」, total_size = "667.57 MB"
        """
        card, session = self._slow_card(monkeypatch)
        item = card.item
        qtbot.waitUntil(lambda: len(session.answered) == 2, timeout=5000)
        qtbot.waitUntil(lambda: "667.57 MB" in card.fileSizeLabel.text(), timeout=2000)
        assert str(item.resolution) == "1080"
        assert item.total_size == "667.57 MB"

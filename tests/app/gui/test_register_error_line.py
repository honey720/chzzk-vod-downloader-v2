"""헤더바 등록 오류 말풍선 · 등록 버튼 도는 표시 · 완료 지우기 자리 게이트 (#495 — 셸 S1 · #536 — 설계 알림 자리).

- 잘못된 URL · 조회 실패는 대화상자가 아니라 URL 칸 아래 오류 말풍선이다(#536 — 그 전에는 빨간 글 한 줄).
  포인터가 나가도 남고, 입력을 고치거나 Esc · 다른 곳을 누르면 닫힌다
- 헤더바에 오류 줄 자리가 없다 — 말풍선이 떠 있어 오류가 뜨고 사라져도 목록이 움직이지 않는다
- 불러오는 동안 등록 버튼 안에 도는 표시가 있고, 버튼 폭은 그대로다
- 완료 지우기는 헤더바 2줄 오른쪽 끝이다

등록은 실제 배선(URL 칸 Enter · 등록 버튼 클릭)으로 하고, 조회 작업만 대역으로 바꾼다(네트워크 없음).
누름 · 키는 QTest로 보낸다(창의 입력 경로를 탄다).
"""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import app.theme as theme

import app.viewmodels.content_viewmodel as content_viewmodel_module
import app.views.mainWindow as mw_mod
from app.views.mainWindow import VodDownloader


class _ControlledFetchJob(QObject):
    """끝을 테스트가 정하는 조회 작업 — `run()`은 아무것도 하지 않고, 만든 작업을 `made`에 모은다."""

    finished = Signal(object, str)
    error = Signal(str)
    made: list = []

    def __init__(self, vod_url, cookies, path) -> None:
        super().__init__()
        self.vod_url = vod_url
        _ControlledFetchJob.made.append(self)

    def run(self) -> None:
        pass


@pytest.fixture
def window(qapp, monkeypatch, tmp_path) -> Iterator[VodDownloader]:
    _ControlledFetchJob.made = []
    monkeypatch.setattr(content_viewmodel_module, "FetchJob", _ControlledFetchJob)
    popups = []
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(
            mw_mod.QMessageBox,
            name,
            lambda parent, title, text, *a, _n=name, **k: popups.append(text),
        )
    win = VodDownloader()
    assert win.saveLocations.choose(str(tmp_path))
    win.resize(win.minimumWidth() + 200, win.minimumHeight() + 200)
    win.show()
    QApplication.processEvents()
    win.popups = popups
    yield win
    win.hide()
    win.deleteLater()
    QApplication.processEvents()


def _press_enter(win, text: str) -> None:
    win.urlInput.setText(text)
    QTest.keyClick(win.urlInput, Qt.Key.Key_Return)
    QApplication.processEvents()


def _list_top(win) -> int:
    return win.listView.mapTo(win, win.listView.rect().topLeft()).y()


def _global(widget) -> QRect:
    return QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size())


def _click_window_at(win, widget) -> None:
    """그 위젯 가운데를 창(QWindow)에 합성 누름으로 누른다 — 실기 커서를 옮기지 않는다(safety 규칙)."""
    point = widget.mapTo(win, widget.rect().center())
    QTest.mouseClick(
        win.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point
    )
    QApplication.processEvents()


class TestErrorLine:
    def test_an_empty_url_shows_the_error_bubble_without_a_dialog(self, window) -> None:
        """URL 칸이 빈 채 Enter를 누르면 오류 말풍선에 안내가 뜨고 URL 칸이 오류 표시가 되며 대화상자는 없어야 한다."""
        _press_enter(window, "")
        assert window.registerError.isShowing()
        assert window.registerError.text() == "Please enter VOD URL."
        assert window.urlInput.property("invalid") is True
        assert window.popups == []

    def test_the_bubble_hangs_below_the_url_input_without_covering_it(self, window) -> None:
        """오류 말풍선은 URL 칸 아래(꼬리 위)에 tipGap 띄워 뜨고 URL 칸과 겹치지 않으며 면은 오류 색 묶음이어야 한다."""
        _press_enter(window, "")
        bubble = window.registerError.bubble()
        url = _global(window.urlInput)
        assert bubble.side == "below" and bubble.tone == "error"
        assert bubble.globalRect().top() == url.bottom() + 1 + theme.METRICS["tipGap"]
        assert not bubble.globalRect().intersects(url)

    def test_leaving_the_input_keeps_the_bubble(self, window) -> None:
        """오류가 뜬 뒤 포인터가 URL 칸을 나가도 오류 말풍선은 남아야 한다."""
        _press_enter(window, "")
        QApplication.sendEvent(window.urlInput, QEvent(QEvent.Type.Leave))
        QApplication.processEvents()
        assert window.registerError.isShowing()

    def test_escape_closes_the_bubble(self, window) -> None:
        """오류가 뜬 뒤 Esc를 누르면 오류 말풍선이 닫혀야 한다."""
        _press_enter(window, "")
        QTest.keyClick(window.urlInput, Qt.Key.Key_Escape)
        QApplication.processEvents()
        assert not window.registerError.isShowing()

    def test_pressing_elsewhere_closes_the_bubble(self, window) -> None:
        """오류가 뜬 뒤 목록(URL 칸 밖)을 누르면 오류 말풍선이 닫혀야 한다.

        누름 = 창(QWindow)에 보내는 합성 누름 — 실제 입력이 지나가는 자리(위젯에 바로 보내면 창을 건너뛴다)
        """
        _press_enter(window, "")
        _click_window_at(window, window.listView.viewport())
        assert not window.registerError.isShowing()

    def test_pressing_inside_the_input_keeps_the_bubble(self, window) -> None:
        """오류가 뜬 뒤 URL 칸 안을 누르면 오류 말풍선이 남아야 한다(고치러 들어가는 누름)."""
        _press_enter(window, "")
        _click_window_at(window, window.urlInput)
        assert window.registerError.isShowing()

    def test_the_header_has_no_error_row(self, window) -> None:
        """헤더바 높이는 안쪽 여백 위아래 + 두 줄(바 컨트롤) + 줄 사이 간격 + 프레임 위아래 테두리여야 한다 — 오류 줄 자리가 없다.

        barPadY 8 · barControl 32 · 줄 사이 8, 테두리는 놓인 프레임에서 잰다(전역 QSS면 아래 경계선 1 -> 89,
        #536 — 프로토 헤더바와 같다)
        """
        frame = window.headerFrame
        border = frame.height() - frame.contentsRect().height()
        expected = (
            2 * theme.METRICS["barPadY"]
            + 2 * theme.METRICS["barControl"]
            + window.headerFrameLayout.spacing()
            + border
        )
        assert frame.height() == expected

    def test_the_list_does_not_move_when_the_error_appears_or_goes(self, window) -> None:
        """오류가 뜨고 사라져도 목록의 위끝 · 높이가 그대로여야 한다(줄 자리를 늘 잡아 둔다)."""
        before = (_list_top(window), window.listView.height())
        _press_enter(window, "")
        shown = (_list_top(window), window.listView.height())
        window.urlInput.setText("h")
        QApplication.processEvents()
        after = (_list_top(window), window.listView.height())
        assert before == shown == after

    def test_editing_the_input_clears_the_error(self, window) -> None:
        """오류가 뜬 뒤 URL 칸에 글을 넣으면 오류 말풍선이 닫히고 URL 칸 오류 표시가 꺼져야 한다."""
        _press_enter(window, "")
        QTest.keyClicks(window.urlInput, "h")
        QApplication.processEvents()
        assert not window.registerError.isShowing()
        assert window.registerError.text() == ""
        assert not window.urlInput.property("invalid")

    def test_a_fetch_failure_shows_the_reason_and_the_url_in_the_bubble(self, window) -> None:
        """조회가 실패하면 조회 중 카드가 빠지고 오류 말풍선에 「까닭 · 주소」가 뜨며 대화상자는 없어야 한다."""
        url = "https://chzzk.naver.com/video/404"
        _press_enter(window, url)
        assert window.contentManager.model.rowCount() == 1, "전제: 조회 중 카드가 생겨야 한다"
        _ControlledFetchJob.made[-1].error.emit(f"{url}\nVideo not found")
        QApplication.processEvents()
        assert window.contentManager.model.rowCount() == 0
        assert window.registerError.text() == f"Video not found · {url}"
        assert window.popups == []

    def test_a_vanished_location_shows_the_error_bubble(self, window, tmp_path) -> None:
        """고른 저장 위치가 사라진 뒤 등록하면 오류 말풍선에 「Path does not exist.」가 떠야 한다."""
        gone = tmp_path / "분리"
        gone.mkdir()
        assert window.saveLocations.choose(str(gone))
        gone.rmdir()
        _press_enter(window, "https://chzzk.naver.com/video/1")
        assert window.registerError.text() == "Path does not exist."
        assert window.contentManager.model.rowCount() == 0

    def test_a_long_error_wraps_in_the_bubble_without_widening_the_window(self, window) -> None:
        """아주 긴 오류 글이 떠도 창 최소폭 · 콘텐츠 최소폭이 그대로이고, 말풍선은 폭 상한 안에서 전문을 보여야 한다.

        주소 = 빈칸 없는 400자 -> 말풍선 몸통 폭 ≤ tipMaxWidth + 안쪽 여백 · 테두리, 그린 글 = 전문
        """
        base = window.contentColumn.minimumSizeHint().width()
        long_url = "https://chzzk.naver.com/video/" + "9" * 400
        _press_enter(window, long_url)
        _ControlledFetchJob.made[-1].error.emit(f"{long_url}\nVideo not found")
        QApplication.processEvents()
        assert window.contentColumn.minimumSizeHint().width() == base
        bubble = window.registerError.bubble()
        limit = theme.METRICS["tipMaxWidth"] + 2 * theme.METRICS["tipPadX"] + 2
        assert bubble.bodyRect().width() <= limit
        assert bubble.shownText() == f"Video not found · {long_url}"


class TestErrorBubbleInTheWindow:
    def test_the_error_bubble_is_a_child_of_the_main_window(self, window) -> None:
        """오류 말풍선은 따로 뜬 창이 아니라 본 창의 자식 위젯이어야 한다 — 본 창과 함께 뒤로 간다."""
        _press_enter(window, "")
        bubble = window.registerError.bubble()
        assert bubble.isVisible()
        assert not bubble.isWindow()
        assert bubble.window() is window
        assert bubble not in QApplication.topLevelWidgets()

    def test_it_stays_inside_the_window_on_top_of_its_siblings(self, window) -> None:
        """오류 말풍선은 창 안에 있고, 레이아웃에 들지 않으며, 형제 위젯 가운데 맨 위(겹쳐 그림)여야 한다."""
        _press_enter(window, "")
        bubble = window.registerError.bubble()
        assert _global(window).contains(bubble.globalRect())
        assert window.layout().indexOf(bubble) == -1
        siblings = [w for w in bubble.parentWidget().children() if w.isWidgetType()]
        assert siblings[-1] is bubble

    def test_minimizing_hides_it_and_restoring_brings_it_back(self, window) -> None:
        """오류가 뜬 채 본 창을 최소화하면 말풍선이 따로 남지 않고, 다시 띄우면 같은 오류가 다시 보여야 한다.

        최소화는 닫는 조건(입력 고침 · Esc · 다른 곳 누름)이 아니다 — 오류는 그대로 남아 있다
        """
        _press_enter(window, "")
        window.showMinimized()
        QApplication.processEvents()
        assert window.isMinimized(), "전제: 창이 최소화됐다"
        visible = [w for w in QApplication.topLevelWidgets() if w.isVisible() and w is not window]
        assert window.registerError.bubble() not in visible
        assert window.registerError.bubble().window() is window
        window.showNormal()
        QApplication.processEvents()
        assert window.registerError.isShowing()
        assert window.registerError.text() == "Please enter VOD URL."


class TestBusyIndicator:
    def test_the_add_button_spins_while_loading_and_keeps_its_width(self, window) -> None:
        """등록한 조회가 끝나기 전에는 등록 버튼이 도는 상태이고, 끝나면 멈추며 폭은 내내 같아야 한다."""
        width = window.fetchButton.width()
        assert not window.fetchButton.isBusy()
        _press_enter(window, "https://chzzk.naver.com/video/1")
        assert window.fetchButton.isBusy()
        assert window.fetchButton.width() == width
        _ControlledFetchJob.made[-1].error.emit("https://chzzk.naver.com/video/1\nVideo not found")
        QApplication.processEvents()
        assert not window.fetchButton.isBusy()
        assert window.fetchButton.width() == width

    def test_it_spins_until_the_last_of_several_fetches_ends(self, window) -> None:
        """조회 둘을 등록하면 첫 조회가 끝나도 돌고, 둘째까지 끝나야 멈춰야 한다."""
        _press_enter(window, "https://chzzk.naver.com/video/1")
        _press_enter(window, "https://chzzk.naver.com/video/2")
        first, second = _ControlledFetchJob.made[-2:]
        first.error.emit("u1\nVideo not found")
        QApplication.processEvents()
        assert window.fetchButton.isBusy()
        second.error.emit("u2\nVideo not found")
        QApplication.processEvents()
        assert not window.fetchButton.isBusy()


class TestSelectButtonPlace:
    def test_select_button_is_the_last_item_of_the_second_line(self, window) -> None:
        """선택 버튼은 헤더바 2줄 레이아웃의 마지막 항목이고 하단바에는 없어야 한다(#561)."""
        line = window.filterRowLayout
        assert line.itemAt(line.count() - 1).widget() is window.selectButton
        assert not window.infoFrame.isAncestorOf(window.selectButton)

    def test_its_right_edge_lines_up_with_the_add_button(self, window) -> None:
        """선택 버튼의 오른쪽 끝은 1줄 VOD 추가의 오른쪽 끝과 같아야 한다(헤더바 안쪽 오른쪽 끝)."""

        def right(widget) -> int:
            return widget.mapTo(window, widget.rect().topRight()).x()

        assert window.selectButton.isVisible()
        assert right(window.selectButton) == right(window.fetchButton)

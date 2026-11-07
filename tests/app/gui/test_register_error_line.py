"""헤더바 등록 오류 줄 · 등록 버튼 도는 표시 · 완료 지우기 자리 게이트 (#495 — 셸 S1, 설계 알림 자리).

- 잘못된 URL · 조회 실패는 대화상자가 아니라 URL 칸 바로 아래 빨간 글 한 줄이다. 입력을 고치면 사라진다
- 그 줄 자리는 늘 잡혀 있다 — 오류가 뜨고 사라져도 목록이 움직이지 않는다
- 불러오는 동안 등록 버튼 안에 도는 표시가 있고, 버튼 폭은 그대로다
- 완료 지우기는 헤더바 2줄 오른쪽 끝이다

등록은 실제 배선(URL 칸 Enter · 등록 버튼 클릭)으로 하고, 조회 작업만 대역으로 바꾼다(네트워크 없음).
"""

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

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
def window(qapp, monkeypatch, tmp_path):
    _ControlledFetchJob.made = []
    monkeypatch.setattr(content_viewmodel_module, "FetchJob", _ControlledFetchJob)
    popups = []
    for name in ("warning", "critical", "information"):
        monkeypatch.setattr(
            mw_mod.QMessageBox, name, lambda parent, title, text, *a, _n=name, **k: popups.append(text)
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


class TestErrorLine:
    def test_an_empty_url_shows_the_error_line_without_a_dialog(self, window) -> None:
        """URL 칸이 빈 채 Enter를 누르면 오류 줄에 안내가 뜨고 URL 칸이 오류 표시가 되며 대화상자는 없어야 한다."""
        _press_enter(window, "")
        assert window.registerErrorLabel.isVisible()
        assert window.registerErrorLabel.text() == "Please enter VOD URL."
        assert window.urlInput.property("invalid") is True
        assert window.popups == []

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
        """오류가 뜬 뒤 URL 칸에 글을 넣으면 오류 줄이 비고 URL 칸 오류 표시가 꺼져야 한다."""
        _press_enter(window, "")
        QTest.keyClicks(window.urlInput, "h")
        QApplication.processEvents()
        assert window.registerErrorLabel.text() == ""
        assert not window.urlInput.property("invalid")

    def test_a_fetch_failure_shows_the_reason_and_the_url_on_the_line(self, window) -> None:
        """조회가 실패하면 조회 중 카드가 빠지고 오류 줄에 「까닭 · 주소」가 뜨며 대화상자는 없어야 한다."""
        url = "https://chzzk.naver.com/video/404"
        _press_enter(window, url)
        assert window.contentManager.model.rowCount() == 1, "전제: 조회 중 카드가 생겨야 한다"
        _ControlledFetchJob.made[-1].error.emit(f"{url}\nVideo not found")
        QApplication.processEvents()
        assert window.contentManager.model.rowCount() == 0
        assert window.registerErrorLabel.text() == f"Video not found · {url}"
        assert window.popups == []

    def test_a_vanished_location_shows_the_error_line(self, window, tmp_path) -> None:
        """고른 저장 위치가 사라진 뒤 등록하면 오류 줄에 「Path does not exist.」가 떠야 한다."""
        gone = tmp_path / "분리"
        gone.mkdir()
        assert window.saveLocations.choose(str(gone))
        gone.rmdir()
        _press_enter(window, "https://chzzk.naver.com/video/1")
        assert window.registerErrorLabel.text() == "Path does not exist."
        assert window.contentManager.model.rowCount() == 0

    def test_a_long_error_is_elided_without_widening_the_window(self, window) -> None:
        """아주 긴 오류 글이 떠도 창 최소폭 · 콘텐츠 최소폭이 그대로이고 전문은 툴팁에 있어야 한다."""
        base = window.contentColumn.minimumSizeHint().width()
        long_url = "https://chzzk.naver.com/video/" + "9" * 400
        _press_enter(window, long_url)
        _ControlledFetchJob.made[-1].error.emit(f"{long_url}\nVideo not found")
        QApplication.processEvents()
        assert window.contentColumn.minimumSizeHint().width() == base
        assert window.registerErrorLabel.toolTip().endswith(long_url[-20:])


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


class TestClearFinishedPlace:
    def test_clear_finished_is_the_last_item_of_the_second_line(self, window) -> None:
        """완료 지우기는 헤더바 2줄 레이아웃의 마지막 항목이고 하단바에는 없어야 한다."""
        line = window.filterRowLayout
        assert line.itemAt(line.count() - 1).widget() is window.clearFinishedButton
        assert not window.infoFrame.isAncestorOf(window.clearFinishedButton)

    def test_its_right_edge_lines_up_with_the_add_button(self, window) -> None:
        """완료 지우기의 오른쪽 끝은 1줄 VOD 추가의 오른쪽 끝과 같아야 한다(헤더바 안쪽 오른쪽 끝)."""

        def right(widget) -> int:
            return widget.mapTo(window, widget.rect().topRight()).x()

        assert window.clearFinishedButton.isVisible()
        assert right(window.clearFinishedButton) == right(window.fetchButton)

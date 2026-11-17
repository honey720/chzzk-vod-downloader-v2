"""꺼지거나 숨은 위젯에서 저절로 넘어온 초점은 그 전 초점의 종류를 따라야 한다 (#611 176n).

받는 중 카드를 하단 ■ · 카드 ✕ · 카드 ‖로 멈춘다. 확인 대화상자는 실제 `QMessageBox`이고 마우스 누름 · Enter로
닫는다. 누름 · 키는 창(`QWindow`)에 합성 이벤트로 보낸다(실제 포인터는 옮기지 않는다). 엔진 자리는 서비스 대역이
맡는다 — 상태 머신은 제품의 것 그대로다.
"""

from collections.abc import Callable, Iterator
from typing import Self

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QWidget

import app.theme as theme
import CVDv2 as main_module
from app.download.logger import DownloadLogger
from app.viewmodels.data import ContentItem
from app.views.mainWindow import VodDownloader
from app.widgets.widget import ContentItemWidget
from core.media.representations import StreamEntry
from core.models.download_state import DownloadState
from tests.app.gui.card_helpers import drop_new_top_levels, enter, hold_style, snapshot_top_levels

ANSWER_TRIES = 150  # 확인 대화상자가 뜨기를 기다리는 횟수 — 20ms 간격, 3초
ANSWER_STEP_MS = 20  # 확인 대화상자를 찾는 간격(ms)


class _Engine:
    """DownloadService 대역 — 제출을 적어 둔다."""

    def __init__(self) -> None:
        self.submissions: list[dict] = []

    def submit(self, content: object, **kwargs: object) -> Self:
        self.submissions.append({"content": content, **kwargs})
        return self

    def elapsed_seconds(self) -> float:
        return 1.0

    def wait(self, timeout: float | None = None) -> bool:
        return True

    def abandon(self, handle: object) -> None:
        pass


@pytest.fixture(autouse=True)
def _environment(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """실제 QSS · 네트워크 차단 · 로그 파일 차단. 테스트가 띄운 창은 끝에 파괴한다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))

    def no_network() -> None:
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", no_network)
    monkeypatch.setattr(DownloadLogger, "_setup_logging", lambda self: None)
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


def _pump() -> None:
    for _ in range(6):
        QApplication.processEvents()


def _item(i: int, folder: str) -> ContentItem:
    item = ContentItem(
        f"https://chzzk.naver.com/video/{i}",
        {
            "title": f"{i} 제목",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 60,
        },
        [StreamEntry(1080, "u1", frame_rate=60.0)],
        None,
        "",
        folder,
        "video",
        None,
    )
    item.total_size = "10.00 MB"
    return item


def _click(win: QWidget, widget: QWidget) -> None:
    """창에 위젯 가운데 누름 · 뗌 합성 이벤트를 보낸다."""
    spot = widget.mapTo(win, widget.rect().center())
    QTest.mouseClick(win.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier(0), spot)


def _answer_yes(how: str) -> None:
    """곧 뜰 확인 대화상자에서 Yes · 「멈추고 지우기」를 고른다 — 마우스 누름 또는 그 단추에 초점을 두고 Enter."""
    tries = [0]

    def tick() -> None:
        box = QApplication.activeModalWidget()
        if not isinstance(box, QMessageBox) or not box.isVisible():
            tries[0] += 1
            if tries[0] < ANSWER_TRIES:
                QTimer.singleShot(ANSWER_STEP_MS, tick)
            return
        yes_roles = (QMessageBox.ButtonRole.YesRole, QMessageBox.ButtonRole.DestructiveRole)
        target = next(b for b in box.buttons() if box.buttonRole(b) in yes_roles)
        assert isinstance(target, QPushButton)
        if how == "mouse":
            spot = target.mapTo(box, target.rect().center())
            QTest.mouseClick(
                box.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier(0), spot
            )
        else:
            target.setFocus(Qt.FocusReason.TabFocusReason)
            QTest.keyClick(box.windowHandle(), Qt.Key.Key_Return)

    QTimer.singleShot(ANSWER_STEP_MS, tick)


@pytest.fixture
def downloading(
    qtbot, tmp_path
) -> Iterator[Callable[[int], tuple[VodDownloader, ContentItemWidget]]]:
    """받는 중 카드가 있는 창을 만드는 함수 — 카드 수를 받는다. ▶를 마우스로 눌러 첫 카드를 받기 시작한다."""
    made: list[VodDownloader] = []

    def build(cards: int) -> tuple[VodDownloader, ContentItemWidget]:
        win = VodDownloader()
        made.append(win)
        win.monitorTimer.stop()
        win.resize(1000, 700)
        win.show()
        QTest.qWaitForWindowExposed(win)
        items = [_item(i, str(tmp_path)) for i in range(cards)]
        for item in items:
            win.contentManager.model.addItem(item)
        _pump()
        engine = _Engine()
        win.downloadViewModel._service = engine
        _activate(win, qtbot)
        _click(win, win.downloadButton)
        _pump()
        assert engine.submissions, "전제: 다운로드가 제출됐다"
        assert win.downloadButton.hasFocus(), "전제: ▶가 마우스로 초점을 받았다"
        running = items[0]
        running.downloadState = DownloadState.RUNNING  # 엔진 대역은 진행을 알리지 않는다
        win.contentManager.model.notifyChanged(running)
        _pump()
        return win, win.listView.widgetFor(running)

    yield build
    for win in made:
        if win.downloadViewModel.isDownloading():
            win.stopDownload()  # 닫을 때 「받는 중」 확인이 뜨지 않게
        win.hide()


def _activate(win: VodDownloader, qtbot) -> None:
    def active() -> bool:
        if not win.isActiveWindow():
            win.activateWindow()
            return False
        return True

    qtbot.waitUntil(active, timeout=3000)


def _settle(win: VodDownloader, qtbot) -> None:
    """확인 대화상자가 닫히고 창이 다시 활성이 될 때까지 기다린다."""
    _pump()
    qtbot.waitUntil(lambda: QApplication.activeModalWidget() is None, timeout=3000)
    _activate(win, qtbot)
    _pump()


def _ring_on_card(win: VodDownloader) -> bool:
    ring = win.listView.focusRing()
    return ring.isVisible() and isinstance(ring.target(), ContentItemWidget)


def _tab_to(win: VodDownloader, widget: QWidget, qtbot) -> None:
    widget.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(widget.hasFocus, timeout=3000)
    _pump()


class TestMouseStopHasNoRing:
    """마우스로 시작한 중지 뒤에는 어느 카드에도 focus ring이 없어야 한다."""

    @pytest.mark.parametrize("close", ["mouse", "enter"])
    @pytest.mark.parametrize("cards", [1, 2])
    def test_stop_button(self, downloading, qtbot, close: str, cards: int) -> None:
        """하단 ■을 마우스로 누르고 확인 대화상자를 닫으면 카드에 focus ring이 없어야 한다.

        카드 1 · 2장, ▶ 마우스로 받기 시작 -> ■ 마우스 누름 -> 확인 대화상자 Yes를 마우스 · Enter
        -> ■ 꺼짐, 초점 위젯 = 카드, focus ring 숨음
        """
        win, _card = downloading(cards)
        _answer_yes(close)
        _click(win, win.stopButton)
        _settle(win, qtbot)
        assert not win.stopButton.isEnabled(), "전제: 다운로드가 멈췄다"
        assert isinstance(QApplication.focusWidget(), ContentItemWidget), (
            "전제: 초점이 카드로 넘어갔다"
        )
        assert not _ring_on_card(win)

    @pytest.mark.parametrize("close", ["mouse", "enter"])
    @pytest.mark.parametrize("cards", [1, 2])
    def test_card_delete(self, downloading, qtbot, close: str, cards: int) -> None:
        """받는 중 카드의 ✕를 마우스로 누르고 확인 대화상자를 닫으면 카드에 focus ring이 없어야 한다.

        카드 1 · 2장, ▶ 마우스로 받기 시작 -> 받는 중 카드 호버 · ✕ 마우스 누름 -> 「멈추고 지우기」를 마우스 · Enter
        -> 그 카드 지워짐, focus ring 숨음
        """
        win, card = downloading(cards)
        enter(card)
        assert card.deleteButton.isVisible(), "전제: 호버로 ✕가 보인다"
        _answer_yes(close)
        _click(win, card.deleteButton)
        _settle(win, qtbot)
        assert win.contentManager.model.rowCount() == cards - 1, "전제: 카드가 지워졌다"
        assert not _ring_on_card(win)

    @pytest.mark.parametrize("cards", [1, 2])
    def test_card_pause_then_stop_button(self, downloading, qtbot, cards: int) -> None:
        """카드 ‖로 일시정지한 뒤 ■을 마우스로 눌러 멈추면 카드에 focus ring이 없어야 한다.

        카드 1 · 2장, ▶ 마우스로 받기 시작 -> 카드 호버 · ‖ 마우스 누름(일시정지) -> ■ 마우스 누름 -> Yes 마우스
        -> 일시정지 뒤 focus ring 숨음, 멈춘 뒤 focus ring 숨음
        """
        win, card = downloading(cards)
        enter(card)
        _click(win, card.pauseButton)
        _pump()
        assert win.downloadViewModel.isPaused(), "전제: 일시정지됐다"
        assert not _ring_on_card(win)
        _answer_yes("mouse")
        _click(win, win.stopButton)
        _settle(win, qtbot)
        assert not win.stopButton.isEnabled(), "전제: 다운로드가 멈췄다"
        assert not _ring_on_card(win)

    def test_stop_without_a_dialog(self, downloading, qtbot) -> None:
        """마우스 초점이 있는 ■이 창이 활성인 채 꺼지면 넘겨받은 카드에 focus ring이 없어야 한다.

        카드 1장, ■에 마우스 초점 -> stopDownload(■ 꺼짐 — 대화상자 없음)
        -> 초점 위젯 = 카드, focus ring 숨음
        """
        win, _card = downloading(1)
        win.stopButton.setFocus(Qt.FocusReason.MouseFocusReason)
        qtbot.waitUntil(win.stopButton.hasFocus, timeout=3000)
        win.stopDownload()
        _pump()
        assert isinstance(QApplication.focusWidget(), ContentItemWidget), (
            "전제: 초점이 카드로 넘어갔다"
        )
        assert not _ring_on_card(win)


class TestKeyboardStopKeepsRing:
    """키보드로 시작한 조작이면 넘겨받은 카드 · 남은 카드에 focus ring이 있어야 한다."""

    @pytest.mark.parametrize("close", ["mouse", "enter"])
    def test_stop_button_pressed_with_space(self, downloading, qtbot, close: str) -> None:
        """Tab으로 ■에 와서 Space로 누르고 확인 대화상자를 닫으면 넘겨받은 카드에 focus ring이 있어야 한다.

        카드 1장, ▶ 마우스로 받기 시작 -> ■에 Tab 초점 · Space -> Yes를 마우스 · Enter
        -> ■ 꺼짐, 초점 위젯 = 카드, focus ring 보임
        """
        win, _card = downloading(1)
        _tab_to(win, win.stopButton, qtbot)
        _answer_yes(close)
        QTest.keyClick(win.windowHandle(), Qt.Key.Key_Space)
        _settle(win, qtbot)
        assert not win.stopButton.isEnabled(), "전제: 다운로드가 멈췄다"
        assert isinstance(QApplication.focusWidget(), ContentItemWidget), (
            "전제: 초점이 카드로 넘어갔다"
        )
        assert _ring_on_card(win)

    @pytest.mark.parametrize("close", ["mouse", "enter"])
    def test_card_delete(self, downloading, qtbot, close: str) -> None:
        """Tab으로 받는 중 카드에 와서 ✕로 지우면 남은 카드에 focus ring이 있어야 한다.

        카드 2장, ▶ 마우스로 받기 시작 -> 받는 중 카드에 Tab 초점 -> ✕ 마우스 누름 -> 「멈추고 지우기」 마우스 · Enter
        -> 초점 위젯 = 남은 카드, focus ring 보임
        """
        win, card = downloading(2)
        _tab_to(win, card, qtbot)
        _answer_yes(close)
        _click(win, card.deleteButton)
        _settle(win, qtbot)
        assert win.contentManager.model.rowCount() == 1, "전제: 카드가 지워졌다"
        assert _ring_on_card(win)

    def test_card_pause(self, downloading, qtbot) -> None:
        """Tab으로 받는 중 카드에 와서 ‖를 누르면 그 카드에 focus ring이 남아야 한다.

        카드 1장, ▶ 마우스로 받기 시작 -> 카드에 Tab 초점 -> ‖ 마우스 누름 -> 일시정지, focus ring이 그 카드
        """
        win, card = downloading(1)
        _tab_to(win, card, qtbot)
        _click(win, card.pauseButton)
        _pump()
        assert win.downloadViewModel.isPaused(), "전제: 일시정지됐다"
        assert win.listView.focusRing().target() is card
        assert _ring_on_card(win)

    def test_stop_without_a_dialog(self, downloading, qtbot) -> None:
        """Tab 초점이 있는 ■이 창이 활성인 채 꺼지면 넘겨받은 카드에 focus ring이 있어야 한다.

        카드 1장, ■에 Tab 초점 -> stopDownload(■ 꺼짐 — 대화상자 없음) -> 초점 위젯 = 카드, focus ring 보임
        """
        win, _card = downloading(1)
        _tab_to(win, win.stopButton, qtbot)
        win.stopDownload()
        _pump()
        assert isinstance(QApplication.focusWidget(), ContentItemWidget), (
            "전제: 초점이 카드로 넘어갔다"
        )
        assert _ring_on_card(win)


class TestBarControlHandover:
    """바 컨트롤 사이에서 저절로 넘어간 초점도 같은 판단을 따라야 한다(`KeyFocusMarker`)."""

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [(Qt.FocusReason.MouseFocusReason, False), (Qt.FocusReason.TabFocusReason, True)],
    )
    def test_disabled_download_button_hands_its_kind_to_the_stop_button(
        self, downloading, qtbot, reason: Qt.FocusReason, expected: bool
    ) -> None:
        """초점이 있는 ▶가 꺼지면 다음 바 컨트롤(■)의 keyFocus가 ▶가 받은 초점의 종류여야 한다.

        카드 1장(받는 중 — ■ 켜짐), 목록을 거쳐 ▶에 마우스 · Tab 초점 -> ▶ setEnabled(False)
        -> 초점 위젯 = ■, ■ keyFocus = 마우스면 False · Tab이면 True
        """
        win, _card = downloading(1)
        win.listView.setFocus(Qt.FocusReason.MouseFocusReason)  # ▶가 쥔 초점을 먼저 뺀다
        qtbot.waitUntil(win.listView.hasFocus, timeout=3000)
        win.downloadButton.setFocus(reason)
        qtbot.waitUntil(win.downloadButton.hasFocus, timeout=3000)
        assert win.downloadButton.property("keyFocus") is expected, (
            "전제: ▶가 그 종류로 초점을 받았다"
        )
        win.downloadButton.setEnabled(False)
        _pump()
        assert QApplication.focusWidget() is win.stopButton, "전제: 초점이 ■으로 넘어갔다"
        assert win.stopButton.property("keyFocus") is expected


def test_handover_record_ends_after_the_event_loop_turn(qapp: QApplication) -> None:
    """꺼진 위젯이 남긴 넘김 기록은 다음 이벤트 루프 턴에 지워져야 한다.

    위젯 하나를 꺼 둔 채 left(키보드, Tab 까닭) -> 같은 턴의 arrived(Tab) = True
    -> 이벤트를 처리한 뒤 arrived(Tab) = None
    """
    from app.widgets.focus_ring import FocusHandover

    handover = FocusHandover()
    gone = QWidget()
    gone.setEnabled(False)
    taker = QWidget()
    handover.left(gone, True, Qt.FocusReason.TabFocusReason)
    assert handover.arrived(taker, Qt.FocusReason.TabFocusReason) is True
    QApplication.processEvents()
    assert handover.arrived(taker, Qt.FocusReason.TabFocusReason) is None

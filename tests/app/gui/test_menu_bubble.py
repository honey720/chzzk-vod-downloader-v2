"""메뉴바 상태 버블 · 결과 강조 깜빡임 · 다운로드 끝 OS 알림 · 닫기 전 창 앞으로 (#523 — 셸 S3-1).

실제 메인 창에서 버튼을 누른다. 엔진 자리는 하단바 테스트의 서비스 대역이 맡는다. 창이 앞에 있는지는
실제 활성 · 최소화로 만든다(offscreen도 창 활성을 바꾼다). 트레이는 대역이다 — offscreen에는 트레이가 없다.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QCloseEvent, QGuiApplication, QIcon, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

import app.theme as theme
import app.widgets.os_notice as os_notice
import CVDv2 as main_module
from app.viewmodels.data import ContentItem
from app.views import mainWindow as mw_mod
from app.views.dialog import SettingDialog
from app.views.mainWindow import VodDownloader
from app.widgets.icons import BUBBLE_FILL_TOKENS, BUBBLE_STATES, bubble_pixmap
from core.models.download_state import DownloadState
from tests.app.gui import test_footer_bar as footer
from tests.app.gui.test_footer_bar import _environment, answers  # noqa: F401 — 같은 환경 · 확인 창 대역

#: 창 활성이 자리를 잡을 때까지 기다리는 시간(ms) — 실제 화면(Windows)에서 활성 이벤트가 늦게 온다.
ACTIVATION_SETTLE_MS = 300

W, R, P, F, X = (
    DownloadState.WAITING,
    DownloadState.RUNNING,
    DownloadState.PAUSED,
    DownloadState.FINISHED,
    DownloadState.FAILED,
)


class _Tray(QObject):
    """QSystemTrayIcon 대역 — 띄운 알림을 적어 둔다. 트레이가 있고 알림을 받는 환경처럼 군다."""

    messageClicked = Signal()
    activated = Signal(object)
    messages: list[tuple[str, str]] = []
    message_icons: list[QIcon] = []

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._visible = False

    @staticmethod
    def isSystemTrayAvailable() -> bool:
        return True

    @staticmethod
    def supportsMessages() -> bool:
        return True

    def setIcon(self, icon: QIcon) -> None:
        self.icon = icon

    def setToolTip(self, text: str) -> None:
        self.tip = text

    def show(self) -> None:
        self._visible = True

    def hide(self) -> None:
        self._visible = False

    def isVisible(self) -> bool:
        return self._visible

    def showMessage(self, title: str, text: str, icon: QIcon, msecs: int) -> None:
        _Tray.messages.append((title, text))
        _Tray.message_icons.append(icon)


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """OS 알림 대역 — 작업 표시줄 깜빡임 횟수와 트레이 알림 글을 적는다."""
    alerts: list[QWidget] = []
    _Tray.messages = []
    _Tray.message_icons = []
    monkeypatch.setattr(
        os_notice.QApplication, "alert", lambda widget, msec=0: alerts.append(widget)
    )
    monkeypatch.setattr(os_notice, "QSystemTrayIcon", _Tray)
    return {"alerts": alerts, "messages": _Tray.messages, "icons": _Tray.message_icons}


@pytest.fixture
def app_logo(qapp: QApplication) -> Iterator[QIcon]:
    """진입점처럼 앱 전체 창 아이콘을 로고로 정한다 — 끝나면 원래 아이콘으로 되돌린다."""
    before = qapp.windowIcon()
    logo = QIcon(main_module.resource_path("resources/icon.png"))
    qapp.setWindowIcon(logo)
    yield logo
    qapp.setWindowIcon(before)


def _image(icon: QIcon, side: int) -> QImage:
    """아이콘의 그 크기 그림 — 같은 그림인지 비교한다."""
    return icon.pixmap(side, side).toImage()


def _window_with(
    tmp_path: Path, states: list[DownloadState]
) -> tuple[VodDownloader, list[ContentItem]]:
    """카드 상태를 정해 둔 창 — 다운로드 없이 상태만 넣는다."""
    win, _items, _engine = footer._window(tmp_path, cards=0)
    items = []
    for n, state in enumerate(states, start=1):
        item = footer._card(str(tmp_path), n)
        item.downloadState = state
        win.contentManager.model.addItem(item)
        items.append(item)
    footer._pump()
    return win, items


def _bubble(win: VodDownloader) -> str:
    """메뉴 버블이 그리는 상태 — 숨었으면 빈 글. 보이는지와 같이 본다."""
    bubble = win.menuDownloadButton.bubble
    assert bubble.isVisible() == bool(bubble.state()), "버블은 상태가 있을 때만 보인다"
    return bubble.state()


def _send_behind(win: VodDownloader) -> None:
    """메인 창을 최소화해 앞에 없게 한다 — 실제 화면에서도 결정적이다.

    다른 창을 띄워 앞으로 보내는 방법은 실제 화면(Windows)에서 활성을 빼앗지 못한다 — Qt가 잠깐 다른 창을
    활성으로 셌다가 OS가 메인 창에 돌려준다. 그래서 「다른 창이 앞」은 offscreen 전용 테스트 하나로만 본다.
    """
    win.showMinimized()
    QTest.qWait(ACTIVATION_SETTLE_MS)
    assert win.isMinimized() and not win._inFront(), "전제: 메인 창이 앞에 없다"


def _activate(win: VodDownloader) -> None:
    """메인 창을 앞으로 — 최소화를 풀고 포커스를 준다."""
    if win.isMinimized():
        win.showNormal()
    win.activateWindow()
    QTest.qWait(ACTIVATION_SETTLE_MS)
    if not win.isActiveWindow() and QGuiApplication.platformName() != "offscreen":
        # 실제 화면 병렬 실행에서는 다른 프로세스가 앞에 있으면 OS가 활성을 거절한다 — 전제를 만들 수 없다
        pytest.skip("OS가 창 활성을 거절했다(다른 프로세스가 앞에 있음)")
    assert win.isActiveWindow(), "전제: 메인 창이 앞에 있다"


def _run_to_end(win: VodDownloader, engine: footer._Engine, outcomes: list[Callable]) -> None:
    """다운로드를 눌러 대기 카드를 차례로 끝낸다 — outcomes는 카드마다 footer._finish · footer._fail."""
    win.downloadButton.click()
    footer._pump()
    for outcome in outcomes:
        outcome(engine)


# ---------------------------------------------------------------------------
# ① 버블 상태 — 우선순위 · 결과 버블의 조건
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("states", "bubble"),
    [
        ([W], ""),
        ([W, F], ""),
        ([R, P, X, F], "running"),
        ([R, W], "running"),
        ([P, X, F], "paused"),
        ([P, F], "paused"),
        ([X, X], "all_failed"),
        ([F, X], "some_failed"),
        ([F, F], "done"),
    ],
    ids=[
        "대기만",
        "대기 · 완료",
        "받는 중이 먼저",
        "받는 중 · 대기",
        "일시정지가 결과보다 먼저",
        "일시정지 · 완료",
        "전부 실패",
        "일부 실패",
        "완료",
    ],
)
def test_bubble_follows_the_priority_after_a_run_ends(
    tmp_path: Path, states: list[DownloadState], bubble: str
) -> None:
    """작업이 끝난 뒤 버블은 받는 중 > 일시정지 > 전부 실패 > 일부 실패 > 완료 > 없음 차례여야 한다.

    카드 상태 조합 → 실행 결과 끝(RunResult.finish) -> 그 조합의 버블(대기가 있으면 결과 버블 없음)
    """
    win, _items = _window_with(tmp_path, states)
    win.runResult.finish()
    footer._pump()
    assert _bubble(win) == bubble


@pytest.mark.parametrize(("states", "bubble"), [([R], "running"), ([P], "paused"), ([F, X], "")])
def test_bubble_without_a_finished_run_shows_only_current_states(
    tmp_path: Path, states: list[DownloadState], bubble: str
) -> None:
    """작업이 끝나지 않았으면 버블은 받는 중 · 일시정지만 보이고 결과 버블은 없어야 한다.

    받는 중 -> running · 일시정지 -> paused · 완료 · 실패만(작업 끝 없음) -> 없음
    """
    win, _items = _window_with(tmp_path, states)
    assert _bubble(win) == bubble


@pytest.mark.usefixtures("answers")
def test_result_bubble_appears_when_the_run_ends_with_the_footer_result(tmp_path: Path) -> None:
    """다운로드 작업이 끝나면 완료 버블과 하단바 실행 결과가 함께 나타나야 한다.

    대기 1장 → 다운로드(받는 중 버블) → 완료 -> 버블 done · 하단바 결과 보임
    """
    win, _items, engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    assert _bubble(win) == "running"
    assert not win.runResultLabel.isVisible(), "전제: 받는 중에는 결과가 없다"
    footer._finish(engine)
    assert (_bubble(win), win.runResultLabel.isVisible()) == ("done", True)


@pytest.mark.usefixtures("answers")
def test_result_bubble_shows_some_failed_with_the_footer_result(tmp_path: Path) -> None:
    """끝난 작업에 완료와 실패가 섞였으면 일부 실패 버블과 하단바 결과가 함께 보여야 한다.

    대기 2장 → 완료 · 실패 -> 버블 some_failed · 하단바 "Completed 1 · Failed 1"
    """
    win, _items, engine = footer._window(tmp_path, cards=2)
    _run_to_end(win, engine, [footer._finish, footer._fail])
    assert _bubble(win) == "some_failed"
    assert win.runResultLabel.text() == "Completed 1 · Failed 1"


def test_deleting_cards_alone_shows_no_result_bubble(tmp_path: Path) -> None:
    """다운로드 없이 카드를 지워 완료 카드만 남으면 결과 버블 · 하단바 결과 둘 다 없어야 한다.

    완료 1장 · 대기 1장 → 대기 카드 지움 -> 버블 없음 · 하단바 결과 없음
    """
    win, items = _window_with(tmp_path, [F, W])
    win.contentManager.removeItem(items[1])
    footer._pump()
    assert (_bubble(win), win.runResultLabel.isVisible()) == ("", False)


@pytest.mark.usefixtures("answers")
def test_a_new_waiting_card_removes_the_result_bubble_with_the_footer_result(
    tmp_path: Path,
) -> None:
    """결과 버블이 보이는 동안 대기 카드가 생기면 버블과 하단바 결과가 함께 사라져야 한다.

    완료 버블 → 대기 카드 추가 -> 버블 없음 · 하단바 결과 없음
    """
    win, _items, engine = footer._window(tmp_path)
    _run_to_end(win, engine, [footer._finish])
    assert _bubble(win) == "done", "전제: 결과 버블이 보인다"
    win.contentManager.model.addItem(footer._card(str(tmp_path), 9))
    footer._pump()
    assert (_bubble(win), win.runResultLabel.isVisible()) == ("", False)


@pytest.mark.usefixtures("answers")
def test_result_bubble_follows_deleting_a_card(tmp_path: Path) -> None:
    """결과 버블이 보이는 동안 카드를 지우면 지금 목록의 결과로 바뀌고, 목록이 비면 하단바 결과와 함께 사라져야 한다.

    완료 · 실패 → 실패 카드 지움 -> done → 완료 카드 지움 -> 버블 없음 · 하단바 결과 없음
    """
    win, items, engine = footer._window(tmp_path, cards=2)
    _run_to_end(win, engine, [footer._finish, footer._fail])
    win.contentManager.removeItem(items[1])
    footer._pump()
    assert (_bubble(win), win.runResultLabel.isVisible()) == ("done", True)
    win.contentManager.removeItem(items[0])
    footer._pump()
    assert (_bubble(win), win.runResultLabel.isVisible()) == ("", False)


@pytest.mark.usefixtures("answers")
def test_bubble_tooltip_has_the_numbers(tmp_path: Path) -> None:
    """버블 툴팁은 상태 이름 · 완료 수 / 전체 · 실패 수여야 한다.

    대기 2장 → 완료 · 실패 -> 툴팁 "Some failed · Completed 1/2 · Failed 1"
    """
    win, _items, engine = footer._window(tmp_path, cards=2)
    _run_to_end(win, engine, [footer._finish, footer._fail])
    assert win.menuDownloadButton.toolTip() == "Some failed · Completed 1/2 · Failed 1"


def test_hidden_bubble_keeps_the_menu_bar_in_place(tmp_path: Path) -> None:
    """버블이 숨어도 「VOD 다운」 버튼 폭과 「설정」 버튼 자리는 버블이 보일 때와 같아야 한다.

    카드 없음(버블 없음) · 받는 중 카드(버블 보임) -> VOD 다운 폭 · 설정 x 같음
    """
    win, _items = _window_with(tmp_path, [])
    assert _bubble(win) == "", "전제: 버블이 없다"
    win._contentMinimumWidth()  # 레이아웃을 다시 셈한다
    hidden = (win.menuDownloadButton.width(), win.menuSettingsButton.x())
    item = footer._card(str(tmp_path), 1)
    item.downloadState = R
    win.contentManager.model.addItem(item)
    footer._pump()
    assert _bubble(win) == "running", "전제: 버블이 보인다"
    win._contentMinimumWidth()
    shown = (win.menuDownloadButton.width(), win.menuSettingsButton.x())
    assert shown == hidden


def test_bubble_sits_inside_the_button_right_padding(tmp_path: Path) -> None:
    """버블은 「VOD 다운」 버튼 오른쪽 안쪽 여백 자리 안에 있어야 한다.

    받는 중 카드 -> 버블이 [버튼 폭 − (기본 여백 + 글 ↔ 버블 + 버블), 버튼 폭 − 기본 여백) 안 · 세로로 버튼 안
    """
    win, _items = _window_with(tmp_path, [R])
    button = win.menuDownloadButton
    bubble = button.bubble
    assert bubble.isVisible(), "전제: 버블이 보인다"
    metrics = mw_mod.theme.METRICS
    room_left = button.width() - (
        metrics["buttonPadX"] + metrics["menuBubbleGap"] + metrics["menuBubble"]
    )
    room_right = button.width() - metrics["buttonPadX"]
    geometry = bubble.geometry()
    assert room_left <= geometry.left() and geometry.right() < room_right
    assert geometry.top() >= 0 and geometry.bottom() < button.height()


def test_each_bubble_state_draws_its_own_picture() -> None:
    """버블 다섯 상태는 서로 다른 그림이어야 한다 — 같은 색이어도 기호가 다르다.

    다섯 상태를 같은 원 색 · 기호 색으로 그림 -> 픽셀이 모두 다름
    """
    from app.widgets.icons import bubble_pixmap

    images = {
        bytes(bubble_pixmap(state, "#808080", "#ffffff", 16).toImage().constBits())
        for state in BUBBLE_STATES
    }
    assert len(images) == len(BUBBLE_STATES)


# ---------------------------------------------------------------------------
# ② 결과 버블 강조 — 메뉴 테두리 깜빡임
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("answers")
def test_blink_starts_when_the_run_ends_behind_other_windows(
    tmp_path: Path, notices: dict[str, list]
) -> None:
    """창이 뒤에 있을 때 작업이 끝나 결과 버블이 나타나면 메뉴 테두리가 깜빡여야 한다.

    다운로드 → 다른 창이 앞 → 완료 -> 깜빡임 켜짐
    """
    win, _items, engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    _send_behind(win)
    footer._finish(engine)
    assert win.menuDownloadButton.isBlinking()


@pytest.mark.usefixtures("answers")
def test_no_blink_when_the_vod_screen_is_in_front(tmp_path: Path) -> None:
    """VOD 다운 화면을 보고 있고 창이 앞이면 작업이 끝나도 깜빡이지 않아야 한다.

    창 앞 · VOD 다운 화면 → 완료 -> 깜빡임 없음 · 버블 done
    """
    win, _items, engine = footer._window(tmp_path)
    _activate(win)
    _run_to_end(win, engine, [footer._finish])
    assert _bubble(win) == "done", "전제: 결과 버블이 보인다"
    assert not win.menuDownloadButton.isBlinking()


@pytest.mark.usefixtures("answers")
def test_blink_stops_when_the_window_gets_focus_on_the_vod_screen(
    tmp_path: Path, notices: dict[str, list]
) -> None:
    """VOD 다운 화면에서 창이 포커스를 받으면 깜빡임이 멈춰야 한다.

    창 뒤에서 완료(깜빡임) → 메인 창 활성 -> 깜빡임 꺼짐 · 결과 버블은 그대로
    """
    win, _items, engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    _send_behind(win)
    footer._finish(engine)
    assert win.menuDownloadButton.isBlinking(), "전제: 깜빡인다"
    _activate(win)
    assert not win.menuDownloadButton.isBlinking()
    assert _bubble(win) == "done"


@pytest.mark.usefixtures("answers")
def test_blink_on_the_settings_screen_waits_for_the_vod_menu(tmp_path: Path) -> None:
    """설정 화면이 떠 있으면 작업이 끝날 때 깜빡이고, 창이 포커스를 받아도 VOD 다운을 누를 때까지 계속해야 한다.

    설정 창(모달 없이) 띄움 → 완료(깜빡임) → 메인 창 활성 -> 계속 → 설정 창 닫고 VOD 다운 누름 -> 멈춤
    """
    win, _items, engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    win.dialog = SettingDialog(parent=win)
    win.dialog.show()
    QTest.qWaitForWindowExposed(win.dialog)
    footer._finish(engine)
    assert win.menuDownloadButton.isBlinking(), "전제: 설정 화면이면 깜빡인다"
    _activate(win)
    assert win.menuDownloadButton.isBlinking()
    win.dialog.hide()
    win.menuDownloadButton.click()
    footer._pump()
    assert not win.menuDownloadButton.isBlinking()


@pytest.mark.usefixtures("answers")
def test_blink_stops_when_the_result_bubble_goes(tmp_path: Path, notices: dict[str, list]) -> None:
    """깜빡이는 동안 결과 버블이 사라지면(대기 카드가 생김) 깜빡임도 멈춰야 한다.

    창 뒤에서 완료(깜빡임) → 대기 카드 추가 -> 버블 없음 · 깜빡임 꺼짐
    """
    win, _items, engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    _send_behind(win)
    footer._finish(engine)
    assert win.menuDownloadButton.isBlinking(), "전제: 깜빡인다"
    win.contentManager.model.addItem(footer._card(str(tmp_path), 9))
    footer._pump()
    assert (_bubble(win), win.menuDownloadButton.isBlinking()) == ("", False)


def test_blink_period_is_slower_than_three_flashes_a_second() -> None:
    """깜빡임 한 주기는 1.5 ~ 2초여야 한다 — 초당 3번 넘게 번쩍이지 않는다.

    BLINK_MS -> 1500 ≤ 값 ≤ 2000
    """
    from app.widgets.menu_button import BLINK_MS

    assert 1500 <= BLINK_MS <= 2000


# ---------------------------------------------------------------------------
# ③ 다운로드 끝 OS 알림
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcomes", "title", "text"),
    [
        ([footer._finish, footer._finish], "Download finished", "Completed 2"),
        ([footer._finish, footer._fail], "Download finished", "Completed 1 · Failed 1"),
        ([footer._fail, footer._fail], "Download failed", "Failed 2"),
    ],
    ids=["모두 완료", "일부 실패", "전부 실패"],
)
@pytest.mark.usefixtures("answers")
def test_os_notice_text_when_the_window_is_behind(
    tmp_path: Path, notices: dict[str, list], outcomes: list[Callable], title: str, text: str
) -> None:
    """창이 뒤에 있을 때 작업이 끝나면 작업 표시줄을 깜빡이고 결과 제목 · 글로 트레이 알림을 띄워야 한다.

    대기 2장 → 창 최소화 → 두 장 끝 -> 깜빡임 1번(메인 창) · 트레이 알림 (그 제목, 그 글) · 제목에 앱 이름 없음
    """
    win, _items, engine = footer._window(tmp_path, cards=2)
    win.downloadButton.click()
    footer._pump()
    _send_behind(win)
    for outcome in outcomes:
        outcome(engine)
    assert notices["alerts"] == [win]
    assert notices["messages"] == [(title, text)]
    assert mw_mod.APP_DISPLAY_NAME not in notices["messages"][0][0]


@pytest.mark.usefixtures("answers")
def test_os_notice_when_the_window_is_minimized(tmp_path: Path, notices: dict[str, list]) -> None:
    """창이 최소화돼 있을 때 작업이 끝나도 OS 알림을 띄워야 한다.

    최소화 → 완료 -> 트레이 알림 1건
    """
    win, _items, engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    win.showMinimized()
    footer._pump()
    assert win.isMinimized(), "전제: 최소화"
    footer._finish(engine)
    assert len(notices["messages"]) == 1


@pytest.mark.usefixtures("answers")
def test_no_os_notice_when_the_window_is_in_front(tmp_path: Path, notices: dict[str, list]) -> None:
    """창이 앞에 있으면 작업이 끝나도 작업 표시줄 깜빡임 · 트레이 알림이 없어야 한다.

    창 앞 → 완료 -> 깜빡임 0 · 트레이 알림 0
    """
    win, _items, engine = footer._window(tmp_path)
    _activate(win)
    _run_to_end(win, engine, [footer._finish])
    assert (notices["alerts"], notices["messages"]) == ([], [])


@pytest.mark.usefixtures("answers")
def test_os_notice_when_another_window_is_in_front(
    tmp_path: Path, notices: dict[str, list]
) -> None:
    """다른 창이 앞에 있으면(메인 창은 최소화 아님) 작업이 끝날 때 OS 알림을 띄워야 한다.

    다른 창 활성 → 완료 -> 트레이 알림 1건 · 깜빡임 켜짐
    """
    if QGuiApplication.platformName() == "windows":
        pytest.skip("실제 화면(Windows)에서는 다른 창이 활성을 빼앗지 못한다(OS가 돌려준다)")
    win, _items, engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    other = QWidget()
    other.show()
    QTest.qWaitForWindowExposed(other)
    other.activateWindow()
    footer._pump()
    assert not win.isMinimized() and not win.isActiveWindow(), "전제: 다른 창이 앞"
    footer._finish(engine)
    assert (len(notices["messages"]), win.menuDownloadButton.isBlinking()) == (1, True)


@pytest.mark.usefixtures("answers")
def test_os_notice_only_flashes_without_a_tray(
    tmp_path: Path, notices: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """트레이 알림을 쓸 수 없는 환경이면 작업 표시줄 깜빡임만 해야 한다.

    트레이 없음 · 창 뒤 → 완료 -> 깜빡임 1번 · 트레이 알림 0
    """
    monkeypatch.setattr(_Tray, "isSystemTrayAvailable", staticmethod(lambda: False))
    win, _items, engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    _send_behind(win)
    footer._finish(engine)
    assert (notices["alerts"], notices["messages"]) == ([win], [])


def test_notice_icon_has_sizes_from_16_to_64() -> None:
    """알림 아이콘은 16 · 32 · 48 · 64px 그림을 모두 담아야 한다 — 16px 한 장이면 Windows가 띄우지 않는다.

    완료 아이콘 -> availableSizes 16 · 32 · 48 · 64
    """
    sides = sorted(size.width() for size in os_notice.notice_icon("done").availableSizes())
    assert sides == [16, 32, 48, 64]


@pytest.mark.usefixtures("answers")
def test_tray_icon_is_the_app_logo_and_the_notice_icon_is_the_bubble(
    tmp_path: Path, notices: dict[str, list], app_logo: QIcon
) -> None:
    """트레이 아이콘은 앱 로고, 알림 본문 아이콘은 결과 상태 버블이어야 한다 — 두 아이콘이 달라야 한다.

    앱 창 아이콘 = 로고 · 창 뒤 → 완료 -> 트레이 아이콘 그림 = 로고(16 ~ 64px) · 알림 아이콘 그림 = 완료 버블
    """
    win, _items, engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    _send_behind(win)
    footer._finish(engine)
    tray = win.downloadEndNotice._tray
    tokens = theme.current_tokens()
    for side in os_notice.NOTICE_ICON_SIDES:
        bubble = bubble_pixmap(
            "done", tokens[BUBBLE_FILL_TOKENS["done"]], tokens["onAccent"], side
        ).toImage()
        assert _image(tray.icon, side) == _image(app_logo, side), side
        assert _image(notices["icons"][0], side) == bubble, side
        assert _image(tray.icon, side) != bubble, side


@pytest.mark.usefixtures("answers")
def test_tray_icon_tooltip_is_the_app_display_name(
    tmp_path: Path, notices: dict[str, list]
) -> None:
    """트레이 아이콘 툴팁은 앱 표시 이름이어야 한다.

    창 뒤 → 완료 -> 트레이 툴팁 APP_DISPLAY_NAME
    """
    win, _items, engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    _send_behind(win)
    footer._finish(engine)
    assert win.downloadEndNotice._tray.tip == mw_mod.APP_DISPLAY_NAME


@pytest.mark.usefixtures("answers")
def test_clicking_the_notice_brings_the_window_back(
    tmp_path: Path, notices: dict[str, list]
) -> None:
    """알림을 누르면 최소화된 창이 앞으로 오고 트레이 아이콘이 내려가야 한다.

    최소화 → 완료(트레이 아이콘 뜸) → 알림 누름 -> 최소화 풀림 · (offscreen) 창 활성 · 트레이 아이콘 내림
    """
    win, _items, engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    win.showMinimized()
    footer._pump()
    footer._finish(engine)
    assert win.downloadEndNotice.trayShown(), "전제: 트레이 아이콘이 떠 있다"
    win.downloadEndNotice._tray.messageClicked.emit()
    footer._pump()
    # 활성은 offscreen에서만 본다 — 실제 화면은 다른 프로세스가 앞이면 OS가 활성을 거절할 수 있다
    active = win.isActiveWindow() or QGuiApplication.platformName() != "offscreen"
    assert (win.isMinimized(), active, win.downloadEndNotice.trayShown()) == (False, True, False)


# ---------------------------------------------------------------------------
# ④ 창 닫기 확인 창 전에 본 창을 앞으로
# ---------------------------------------------------------------------------


def _close(win: VodDownloader) -> QCloseEvent:
    """창에 닫기 이벤트를 보낸다 — 창을 실제로 닫지 않는다(크기 기억이 다음 창에 남지 않게)."""
    event = QCloseEvent()
    QApplication.sendEvent(win, event)
    return event


@pytest.mark.usefixtures("answers")
def test_close_confirmation_restores_a_minimized_window_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """받는 중에 최소화된 창을 닫으면 확인 창을 띄우기 전에 최소화가 풀리고 창이 앞에 와야 한다.

    받는 중 · 최소화 → 닫기 -> 확인 창을 띄우는 순간 최소화 아님 · 창 보임 · (offscreen) 창 활성
    실제 화면(Windows)은 다른 프로세스가 앞에 있으면 활성을 OS가 거절할 수 있어 활성은 offscreen에서만 본다
    """
    win, _items, _engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    win.showMinimized()
    footer._pump()
    seen: list[tuple[bool, bool, bool]] = []
    offscreen = QGuiApplication.platformName() == "offscreen"

    def warning(parent: QWidget, title: str, text: str, *args: object, **kwargs: object) -> object:
        footer._pump()
        seen.append((win.isMinimized(), win.isVisible(), win.isActiveWindow() or not offscreen))
        return mw_mod.QMessageBox.No

    monkeypatch.setattr(mw_mod.QMessageBox, "warning", warning)
    event = _close(win)
    assert not event.isAccepted(), "전제: 아니요 — 닫지 않는다"
    assert seen == [(False, True, True)]


@pytest.mark.usefixtures("answers")
def test_close_confirmation_restores_a_maximized_window_as_maximized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """최대화였다가 최소화된 창을 닫으면 확인 창 전에 최대화로 돌아와야 한다.

    받는 중 · 최대화 → 최소화 → 닫기 -> 확인 창을 띄우는 순간 최대화
    """
    win, _items, _engine = footer._window(tmp_path)
    win.downloadButton.click()
    footer._pump()
    win.showMaximized()
    footer._pump()
    win.setWindowState(win.windowState() | mw_mod.Qt.WindowState.WindowMinimized)
    footer._pump()
    assert win.isMinimized(), "전제: 최소화"
    seen: list[bool] = []

    def warning(parent: QWidget, title: str, text: str, *args: object, **kwargs: object) -> object:
        footer._pump()
        seen.append(win.isMaximized())
        return mw_mod.QMessageBox.No

    monkeypatch.setattr(mw_mod.QMessageBox, "warning", warning)
    _close(win)
    assert seen == [True]


def test_closing_without_a_question_does_not_raise_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """받는 것이 없어 바로 닫히면 창을 앞으로 올리지 않아야 한다.

    받는 것 없음 · 최소화 → 닫기 -> 앞으로 올리기 0번 · 닫기 받아들임
    """
    win, _items, _engine = footer._window(tmp_path)
    win.showMinimized()
    footer._pump()
    raised: list[bool] = []
    monkeypatch.setattr(win, "_bringToFront", lambda: raised.append(True))
    event = _close(win)
    assert (event.isAccepted(), raised) == (True, [])

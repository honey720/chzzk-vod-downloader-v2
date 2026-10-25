"""app/user_activity.py 검증 (#309) — 앱 전체의 입력을 지켜보고 "조작 중"을 답한다.

실제 배선을 탄다: QApplication에 필터를 걸고, 위젯 · 창에 이벤트를 보내 본다. 시계는 테스트가
정한 값을 돌려주는 대역으로 바꾼다. 필터는 테스트마다 걷는다 — 앱에 남기지 않는다.
"""

import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import (
    QExposeEvent,
    QFocusEvent,
    QKeyEvent,
    QMouseEvent,
    QRegion,
    QWheelEvent,
    QWindowStateChangeEvent,
)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

import app.section_basis as section_basis
import app.user_activity as activity
import app.viewmodels.download_viewmodel as download_viewmodel
import app.viewmodels.section_edit_viewmodel as sev
import core.utils.interaction as interaction
from core.api.mp4 import pending_mp4_head
from core.models.plan import TimeRange


class _Clock:
    """테스트가 정하는 시계 — 값을 읽은 횟수를 센다."""

    def __init__(self):
        self.value = 1000.0
        self.read = 0

    def __call__(self) -> float:
        self.read += 1
        return self.value


class _Keys(QWidget):
    """받은 키 입력을 적는 위젯."""

    def __init__(self):
        super().__init__()
        self.pressed: list[int] = []

    def keyPressEvent(self, event) -> None:  # noqa: N802 — Qt의 이름
        self.pressed.append(event.key())


@pytest.fixture(autouse=True)
def rest_at_every_gap(monkeypatch):
    """조작 중이면 틈마다 쉬게 한다(쉼 사이 시간 판정 0) — 이 파일은 쉬는 자리를 잰다 (#408).

    쉼을 일한 시간으로 묶는 동작은 ``tests/unit/core/test_interaction.py``의 가짜 시계 테스트가 잰다.
    """
    monkeypatch.setattr(interaction, "REST_AFTER_SECONDS", 0.0)


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(activity, "_now", clock)
    return clock


@pytest.fixture
def watching(qapp, clock):
    """앱 전체의 입력을 지켜보는 상태 — 끝나면 필터를 걷고 마지막 입력을 잊는다."""
    activity.watch_user_input(qapp)
    yield
    activity.stop_watching_user_input()


class _WindowEvents(QObject):
    """창(QWindow) 하나가 받은 Expose · Resize 이벤트를 센다 — 그 창에만 건다."""

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self.exposes = 0
        self.resizes = 0

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 — Qt의 이름
        if event.type() == QEvent.Type.Expose:
            self.exposes += 1
        elif event.type() == QEvent.Type.Resize:
            self.resizes += 1
        return False


# 창을 띄운 뒤 첫 Expose 이벤트를 기다리는 마감(초) — 리눅스 xcb에서 CPU 부하를 걸고 잰 최댓값의
# 수십 배다 (#379). 넘기면 그 테스트를 실패로 끝낸다
_FIRST_EXPOSE_LIMIT = 1.0


@pytest.fixture
def window(qtbot, watching):
    """보이는 창 하나(안에 자식 위젯 하나) — 띄우며 생긴 이벤트는 잊고 시작한다.

    창 시스템이 띄운 창의 크기 · 위치(Resize · Move)는 그 창의 첫 Expose 이벤트보다 먼저 온다.
    첫 Expose 이벤트가 올 때까지 기다린 뒤 잊는다 (#379) — waitExposed는 창이 드러났다는 표시만
    보고 그 이벤트들이 배달되기 전에 돌아올 수 있다.
    """
    window = _Keys()
    window.child = QWidget(window)
    window.child.setGeometry(10, 10, 50, 50)
    qtbot.addWidget(window)
    window.resize(300, 200)
    window.winId()  # 창(QWindow)을 먼저 만든다 — 첫 Expose 이벤트를 세기 시작한 뒤에 띄운다
    window.events = _WindowEvents(window)
    window.windowHandle().installEventFilter(window.events)
    window.show()
    qtbot.waitExposed(window)
    # 타이머로 기다리지 않는다 — 입력 필터가 이벤트를 삼키는 고장에서는 타이머 이벤트도 삼켜져
    # waitUntil이 끝나지 않는다. 이벤트를 직접 돌리며 단조 시계로 마감을 본다
    deadline = time.monotonic() + _FIRST_EXPOSE_LIMIT
    while window.events.exposes == 0:
        if time.monotonic() > deadline:
            pytest.fail("창이 첫 Expose 이벤트를 받지 못했다")
        QApplication.processEvents()
    QApplication.processEvents()
    activity._last_input = float("-inf")
    assert window.isVisible()
    return window


def _wheel(widget: QWidget) -> None:
    position = QPointF(20, 20)
    event = QWheelEvent(
        position,
        widget.mapToGlobal(position),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


def _resize_window(window: QWidget) -> None:
    """창의 크기를 바꾸고, 창 시스템이 바뀐 크기를 창(QWindow)에 알릴 때까지(최대 3초) 기다린다.

    xcb에서는 그 알림이 창 시스템을 한 번 다녀온 뒤라 processEvents 한 번 안에 오지 않을 수 있다
    (#379). 기다려도 오지 않으면 그대로 돌아온다 — 판정은 부른 쪽의 단언이 한다.
    """
    before = window.events.resizes
    window.resize(window.width() + 40, window.height())
    deadline = time.monotonic() + 3.0
    while window.events.resizes == before and time.monotonic() < deadline:
        QTest.qWait(5)
    QApplication.processEvents()


def _move_window(window: QWidget) -> None:
    window.move(window.x() + 30, window.y() + 30)
    QApplication.processEvents()


INPUTS = {
    "키": lambda w: QTest.keyClick(w, Qt.Key.Key_A),
    "마우스 버튼": lambda w: QTest.mouseClick(w.child, Qt.MouseButton.LeftButton),
    "휠": lambda w: _wheel(w.child),
    "창 크기 바꾸기": _resize_window,
    "창 옮기기": _move_window,
}


def test_user_is_not_interacting_before_any_input(window):
    """입력이 한 번도 없었으면 조작 중이 아니어야 한다.

    필터를 건 뒤 아무 입력도 보내지 않음 -> user_interacting() is False.
    그 뒤 키를 하나 누르면 True(필터가 살아 있음)
    """
    assert activity.user_interacting() is False

    QTest.keyClick(window, Qt.Key.Key_A)

    assert activity.user_interacting() is True


@pytest.mark.parametrize("kind", list(INPUTS))
def test_each_kind_of_input_makes_the_user_interacting(qtbot, window, kind):
    """앱 창에 입력이 들어오면 조작 중이어야 한다.

    키 · 마우스 버튼 · 휠 · 창 크기 바꾸기 · 창 옮기기를 하나씩 보냄
    -> 보내기 전 user_interacting() is False, 보낸 뒤 True가 된다
    """
    assert activity.user_interacting() is False

    INPUTS[kind](window)

    # 창을 옮긴 것은 플랫폼이 알려 줘야 창에 이벤트로 온다 — xcb에서는 창 시스템을 한 번
    # 다녀온 뒤라 processEvents 한 번 안에 오지 않는다. 올 때까지 조건으로 기다린다.
    # 시계는 대역이라 한 번 조작 중이 되면 기다리는 동안 풀리지 않는다
    qtbot.waitUntil(activity.user_interacting, timeout=3000)


def _key(kind: QEvent.Type) -> QEvent:
    return QKeyEvent(kind, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier)


MOVES = (QEvent.Type.MouseMove, QEvent.Type.NonClientAreaMouseMove)


def _mouse(kind: QEvent.Type, held: Qt.MouseButton = Qt.MouseButton.LeftButton) -> QEvent:
    """마우스 이벤트 하나. 이동 이벤트는 누른 버튼(held)만 싣고, 그 밖의 것은 그 버튼의 이벤트다."""
    button = Qt.MouseButton.NoButton if kind in MOVES else held
    return QMouseEvent(
        kind, QPointF(5, 5), QPointF(5, 5), button, held, Qt.KeyboardModifier.NoModifier
    )


SINGLE_EVENTS = {
    "KeyPress": lambda: _key(QEvent.Type.KeyPress),
    "KeyRelease": lambda: _key(QEvent.Type.KeyRelease),
    "MouseButtonPress": lambda: _mouse(QEvent.Type.MouseButtonPress),
    "MouseButtonRelease": lambda: _mouse(QEvent.Type.MouseButtonRelease),
    "MouseButtonDblClick": lambda: _mouse(QEvent.Type.MouseButtonDblClick),
    "NonClientAreaMouseButtonPress": lambda: _mouse(QEvent.Type.NonClientAreaMouseButtonPress),
    "NonClientAreaMouseButtonRelease": lambda: _mouse(QEvent.Type.NonClientAreaMouseButtonRelease),
    "NonClientAreaMouseButtonDblClick": lambda: _mouse(
        QEvent.Type.NonClientAreaMouseButtonDblClick
    ),
}


@pytest.mark.parametrize("kind", list(SINGLE_EVENTS))
def test_each_input_event_alone_makes_the_user_interacting(window, kind):
    """입력 이벤트는 종류마다 하나만 와도 조작 중이어야 한다.

    창 안의 위젯에 KeyPress · KeyRelease · MouseButtonPress · MouseButtonRelease ·
    MouseButtonDblClick · 창 테두리의 같은 세 가지(NonClientArea…) 가운데 하나만 보냄
    -> 보내기 전 False, 보낸 뒤 True
    """
    assert activity.user_interacting() is False

    QApplication.sendEvent(window.child, SINGLE_EVENTS[kind]())

    assert activity.user_interacting() is True


@pytest.mark.parametrize("kind", MOVES, ids=["창 안", "창 테두리 · 제목 줄"])
def test_mouse_move_without_a_button_is_not_interaction(window, kind):
    """버튼을 누르지 않은 마우스 이동은 조작으로 치지 않아야 한다.

    창 안의 위젯에 MouseMove / NonClientAreaMouseMove(누른 버튼 없음)를 20번 보냄
    -> user_interacting() is False. 그 뒤 키를 하나 누르면 True(필터가 살아 있음)
    """
    for _ in range(20):
        QApplication.sendEvent(window.child, _mouse(kind, Qt.MouseButton.NoButton))

    assert activity.user_interacting() is False
    QTest.keyClick(window, Qt.Key.Key_A)
    assert activity.user_interacting() is True


# 창 시스템이 창(QWindow)에 보내는, 사용자의 입력이 아닌 이벤트 (#379)
WINDOW_SYSTEM_EVENTS = {
    "Expose": lambda: QExposeEvent(QRegion(0, 0, 300, 200)),
    "FocusIn": lambda: QFocusEvent(QEvent.Type.FocusIn),
    "WindowStateChange": lambda: QWindowStateChangeEvent(Qt.WindowState.WindowNoState),
}


@pytest.mark.parametrize("kind", list(WINDOW_SYSTEM_EVENTS))
def test_window_system_event_to_the_window_is_not_interaction(window, kind):
    """창 시스템이 창에 보내는 노출 · 포커스 · 상태 이벤트는 조작으로 치지 않아야 한다.

    창(QWindow)에 Expose / FocusIn / WindowStateChange를 하나 보냄
    -> user_interacting() is False. 그 뒤 키를 하나 누르면 True(필터가 살아 있음)
    """
    QApplication.sendEvent(window.windowHandle(), WINDOW_SYSTEM_EVENTS[kind]())

    assert activity.user_interacting() is False
    QTest.keyClick(window, Qt.Key.Key_A)
    assert activity.user_interacting() is True


def test_mouse_move_over_the_window_without_a_button_is_not_interaction(window):
    """창(QWindow)에 온 버튼 없는 마우스 이동도 조작으로 치지 않아야 한다.

    창에 합성 마우스 이동을 보냄(진짜 커서는 그대로다) -> user_interacting() is False.
    그 뒤 창 안의 위젯을 누르면 True
    """
    QTest.mouseMove(window.windowHandle(), QPoint(30, 30))
    QApplication.processEvents()

    assert activity.user_interacting() is False
    QTest.mouseClick(window.child, Qt.MouseButton.LeftButton)
    assert activity.user_interacting() is True


@pytest.mark.parametrize("kind", MOVES, ids=["창 안", "창 테두리 · 제목 줄"])
def test_mouse_move_with_a_button_held_is_interaction(window, kind):
    """버튼을 누른 채 움직이는 마우스는 조작이어야 한다(끌기).

    창 안의 위젯에 MouseMove / NonClientAreaMouseMove(왼쪽 버튼을 누른 채)를 하나 보냄
    -> 보내기 전 False, 보낸 뒤 True
    """
    assert activity.user_interacting() is False

    QApplication.sendEvent(window.child, _mouse(kind, Qt.MouseButton.LeftButton))

    assert activity.user_interacting() is True


def test_interaction_ends_after_the_interaction_time(window, clock):
    """마지막 입력 뒤 INTERACTION_SECONDS(0.2초)가 지나면 조작 중이 아니어야 한다.

    시계 0.0에서 키 입력 -> 시계 0.19: True, 0.2: False
    """
    assert activity.INTERACTION_SECONDS == 0.2
    clock.value = 0.0  # 0에서 재면 지난 시간이 시계의 값 그대로다 — 꼭 0.2초인 때를 잰다
    QTest.keyClick(window, Qt.Key.Key_A)

    clock.value = 0.19
    still = activity.user_interacting()
    clock.value = 0.2
    after = activity.user_interacting()

    assert (still, after) == (True, False)


def test_a_later_input_extends_the_interaction(window, clock):
    """입력이 이어지면 마지막 입력을 기준으로 조작 중이어야 한다.

    시계 1000.0에서 키 입력, 1000.15에서 다시 키 입력 -> 시계 1000.3: True, 1000.35: False
    """
    QTest.keyClick(window, Qt.Key.Key_A)
    clock.value = 1000.15
    QTest.keyClick(window, Qt.Key.Key_B)

    clock.value = 1000.3
    still = activity.user_interacting()
    clock.value = 1000.35
    after = activity.user_interacting()

    assert (still, after) == (True, False)


def test_events_that_are_not_input_do_not_count(window):
    """창 안 위젯의 크기 · 위치 변경과 그 밖의 이벤트는 입력으로 치지 않아야 한다.

    자식 위젯의 크기 · 위치를 바꾸고, 다시 그리고, 사용자 정의 이벤트를 보냄
    -> user_interacting() is False. 그 뒤 창의 크기를 바꾸면 True(필터가 살아 있음)
    """
    window.child.resize(80, 60)
    window.child.move(20, 20)
    window.repaint()
    QApplication.sendEvent(window, QEvent(QEvent.Type.User))
    QApplication.sendEvent(window.child, QEvent(QEvent.Type.LayoutRequest))
    QApplication.processEvents()
    assert activity.user_interacting() is False

    _resize_window(window)

    assert activity.user_interacting() is True


def test_watching_does_not_swallow_the_input(window):
    """지켜보는 필터는 입력을 막지 않아야 한다 — 위젯이 그대로 받는다.

    보이는 창에 키 A를 누름 -> 창이 받은 키 == [Key_A]
    """
    QTest.keyClick(window, Qt.Key.Key_A)

    assert window.pressed == [Qt.Key.Key_A]


def test_watching_twice_installs_one_filter(window, clock, qapp):
    """지켜보기를 두 번 청해도 필터는 하나여야 한다 — 입력 하나에 시계를 한 번만 읽는다.

    watch_user_input을 한 번 더 부른 뒤 KeyPress 하나를 창에 직접 보냄
    -> 시계를 읽은 횟수가 1 늘어난다
    """
    activity.watch_user_input(qapp)
    before = clock.read

    QApplication.sendEvent(
        window, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier)
    )

    assert clock.read - before == 1


def test_stopping_removes_the_filter_and_forgets_the_last_input(window):
    """지켜보기를 그만두면 조작 중이 아니게 되고, 그 뒤의 입력도 세지 않아야 한다.

    키 입력(True 확인) → stop_watching_user_input() -> False. 다시 키 입력 -> 여전히 False
    """
    QTest.keyClick(window, Qt.Key.Key_A)
    assert activity.user_interacting() is True

    activity.stop_watching_user_input()
    stopped = activity.user_interacting()
    QTest.keyClick(window, Qt.Key.Key_B)

    assert (stopped, activity.user_interacting()) == (False, False)


def test_download_viewmodel_hands_the_question_to_the_service(qapp, monkeypatch):
    """다운로드 뷰모델은 서비스를 만들 때 조작 중인지 묻는 값으로 user_interacting을 넘겨야 한다.

    DownloadService를 받은 인자를 적는 대역으로 바꾸고 DownloadViewModel(content)을 만듦
    -> 적힌 user_interacting is app.user_activity.user_interacting
    """
    given: dict = {}

    class Service:
        def __init__(self, **kwargs):
            given.update(kwargs)

    class Content(QObject):
        def update_progress(self, *args): ...
        def pause(self, *args): ...
        def resume(self, *args): ...
        def stop(self, *args): ...
        def finish(self, *args): ...
        def fail(self, *args): ...

    monkeypatch.setattr(download_viewmodel, "DownloadService", Service)

    download_viewmodel.DownloadViewModel(Content())

    assert given["user_interacting"] is activity.user_interacting


# ================================================================ 카드의 조회 · 크기 세기 (#309)


@pytest.fixture
def rests(monkeypatch):
    """쉰 횟수를 담는 리스트 — 쉬는 일을 자지 않고 세기만 하는 대역으로 바꾼다."""
    counted: list[int] = []
    monkeypatch.setattr(interaction, "_rest", lambda: counted.append(1))
    return counted


def _gap(*_args):
    """조회의 대역 — 조각 사이의 틈을 한 번 흉내 낸다."""
    interaction.rest_if_user_interacting()
    return 7, 3


def _size_gap(*_args) -> int:
    """크기 세기의 대역 — 조각 사이의 틈을 한 번 흉내 내고 크기 7을 돌려준다."""
    interaction.rest_if_user_interacting()
    return 7


def _snapshot() -> SimpleNamespace:
    return SimpleNamespace(content_type="video", base_url="", vod_url="", resolution=1080)


def _run_size_job() -> list[tuple]:
    job, done = sev.SectionSizeJob(object(), (), "요청"), []
    job.done.connect(lambda *args: done.append(args))
    job.run()
    return done


def _run_refit_job() -> list[tuple]:
    job, done = sev.SectionRefitJob(_snapshot(), "요청"), []
    job.done.connect(lambda *args: done.append(args))
    job.run()
    return done


def test_section_jobs_ask_the_question_of_user_activity():
    """카드의 조회 · 크기 세기가 조작 중인지 묻는 값은 app.user_activity.user_interacting이어야 한다.

    -> section_edit_viewmodel.user_interacting is app.user_activity.user_interacting
    """
    assert sev.user_interacting is activity.user_interacting


@pytest.mark.parametrize(("answer", "expected"), [(True, 1), (False, 0)], ids=["조작 중", "아님"])
def test_size_job_rests_at_a_gap_only_while_the_user_is_interacting(
    qapp, monkeypatch, rests, answer, expected
):
    """크기 세기는 조작 중이라고 답할 때만 틈에서 쉬어야 한다.

    section_file_bytes_of를 틈을 한 번 흉내 내고 7을 돌려주는 대역으로, user_interacting을
    늘 참 / 늘 거짓으로 바꾸고 SectionSizeJob.run()
    -> 쉰 횟수 == 1 / 0, done == [("요청", True, 7)]
    """
    monkeypatch.setattr(sev, "section_file_bytes_of", _size_gap)
    monkeypatch.setattr(sev, "user_interacting", lambda: answer)

    done = _run_size_job()

    assert len(rests) == expected
    assert done == [("요청", True, 7)]


@pytest.mark.parametrize(("answer", "expected"), [(True, 1), (False, 0)], ids=["조작 중", "아님"])
def test_refit_job_rests_at_a_gap_only_while_the_user_is_interacting(
    qapp, monkeypatch, rests, answer, expected
):
    """해상도 변경 뒤의 조회는 조작 중이라고 답할 때만 틈에서 쉬어야 한다.

    probe_section_basis를 틈을 한 번 흉내 내는 대역으로, user_interacting을 늘 참 / 늘 거짓으로
    바꾸고 SectionRefitJob.run() -> 쉰 횟수 == 1 / 0, done == [("요청", 대역이 준 값)]
    """
    monkeypatch.setattr(section_basis, "probe_section_basis", _gap)
    monkeypatch.setattr(sev, "user_interacting", lambda: answer)

    done = _run_refit_job()

    assert len(rests) == expected
    assert done == [("요청", (7, 3))]


def test_section_jobs_leave_nothing_set_on_the_thread_after_they_run(qapp, monkeypatch, rests):
    """카드의 조회 · 크기 세기가 끝나면 그 스레드에 조작 중인지 묻는 값이 남지 않아야 한다.

    user_interacting = 늘 참. 두 일을 차례로 돌린 뒤(쉰 횟수 2) 같은 스레드에서 틈을 한 번 흉내 냄
    -> 쉰 횟수 == 2 그대로
    """
    monkeypatch.setattr(sev, "section_file_bytes_of", _size_gap)
    monkeypatch.setattr(section_basis, "probe_section_basis", _gap)
    monkeypatch.setattr(sev, "user_interacting", lambda: True)
    _run_size_job()
    _run_refit_job()
    assert len(rests) == 2

    interaction.rest_if_user_interacting()

    assert len(rests) == 2


def test_edit_window_lookup_does_not_rest(qapp, monkeypatch, rests):
    """편집 창의 조회는 조작 중이어도 쉬지 않아야 한다 — 쉬는 것은 카드의 조회 · 크기 세기뿐이다.

    user_interacting = 늘 참, probe_section_basis는 틈을 한 번 흉내 내는 대역. SectionBasisJob.run()
    -> finished 1회, 쉰 횟수 == 0. 같은 대역으로 SectionRefitJob.run()을 돌리면 1(대역이 살아 있음)
    """
    monkeypatch.setattr(section_basis, "probe_section_basis", _gap)
    monkeypatch.setattr(sev, "user_interacting", lambda: True)
    job, finished = sev.SectionBasisJob(_snapshot()), []
    job.finished.connect(finished.append)

    job.run()

    assert finished == [(7, 3)]
    assert rests == []
    _run_refit_job()
    assert len(rests) == 1


def test_size_counted_while_resting_equals_the_size_counted_without_resting(qapp, monkeypatch):
    """조작 중에 쉬어 가며 센 카드의 크기는 쉬지 않고 센 크기와 같아야 한다.

    영상 20,000샘플의 moov(해석하지 않은 것), 구간 10~60초 · 200~320초. 실제로 잔다(1ms).
    user_interacting = 늘 참 / 늘 거짓으로 SectionSizeJob.run()
    -> 두 결과의 (셌는지, 크기)가 같고 셌는지 == True, 크기 > 0.
       늘 참일 때 쉰 횟수 > 0, 늘 거짓일 때 0
    """
    from tests.unit.core.test_mp4_section_sizes import _audio, _head_only, _video

    raw = _head_only(
        [_video(20_000, seed=3, per_chunk=7, b_frames=True), _audio(9_000, seed=4, per_chunk=5)]
    )
    wanted = (TimeRange(10.0, 60.0), TimeRange(200.0, 320.0))
    results, rested = [], []
    for answer in (True, False):
        monkeypatch.setattr(sev, "user_interacting", lambda answer=answer: answer)
        job, done = sev.SectionSizeJob(pending_mp4_head(raw), wanted, "요청"), []
        job.done.connect(lambda *args, done=done: done.append(args))
        before = interaction.rest_totals()[0]
        job.run()
        rested.append(interaction.rest_totals()[0] - before)
        ((_token, counted, size),) = done
        results.append((counted, size))

    assert results[0] == results[1]
    assert results[0][0] is True and results[0][1] > 0
    assert rested[0] > 0 and rested[1] == 0

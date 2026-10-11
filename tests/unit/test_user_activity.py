"""app/user_activity.py 검증 (#309) — 앱 전체의 입력을 지켜보고 "조작 중"을 답한다.

실제 배선을 탄다: QApplication에 필터를 걸고, 위젯 · 창에 이벤트를 보내 본다. 시계는 테스트가
정한 값을 돌려주는 대역으로 바꾼다. 필터는 테스트마다 걷는다 — 앱에 남기지 않는다.
"""

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

import app.user_activity as activity
import app.viewmodels.download_viewmodel as download_viewmodel


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


@pytest.fixture
def window(qtbot, watching):
    """보이는 창 하나(안에 자식 위젯 하나) — 띄우며 생긴 이벤트는 잊고 시작한다."""
    window = _Keys()
    window.child = QWidget(window)
    window.child.setGeometry(10, 10, 50, 50)
    qtbot.addWidget(window)
    window.resize(300, 200)
    window.show()
    qtbot.waitExposed(window)
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
    window.resize(window.width() + 40, window.height())
    QApplication.processEvents()


def _move_window(window: QWidget) -> None:
    window.move(window.x() + 30, window.y() + 30)
    QApplication.processEvents()


INPUTS = {
    "키": lambda w: QTest.keyClick(w, Qt.Key.Key_A),
    "마우스 버튼": lambda w: QTest.mouseClick(w.child, Qt.MouseButton.LeftButton),
    # 창(QWindow)에 합성 이벤트를 보낸다 — 위젯에 보내면 진짜 커서가 움직인다
    "마우스 이동": lambda w: QTest.mouseMove(w.windowHandle(), QPoint(30, 30)),
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
def test_each_kind_of_input_makes_the_user_interacting(window, kind):
    """앱 창에 입력이 들어오면 조작 중이어야 한다.

    키 · 마우스 버튼 · 마우스 이동 · 휠 · 창 크기 바꾸기 · 창 옮기기를 하나씩 보냄
    -> 보내기 전 user_interacting() is False, 보낸 뒤 True
    """
    assert activity.user_interacting() is False

    INPUTS[kind](window)

    assert activity.user_interacting() is True


def _key(kind: QEvent.Type) -> QEvent:
    return QKeyEvent(kind, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier)


def _mouse(kind: QEvent.Type) -> QEvent:
    moves = (QEvent.Type.MouseMove, QEvent.Type.NonClientAreaMouseMove)
    held = Qt.MouseButton.NoButton if kind in moves else Qt.MouseButton.LeftButton
    return QMouseEvent(
        kind, QPointF(5, 5), QPointF(5, 5), held, held, Qt.KeyboardModifier.NoModifier
    )


SINGLE_EVENTS = {
    "KeyPress": lambda: _key(QEvent.Type.KeyPress),
    "KeyRelease": lambda: _key(QEvent.Type.KeyRelease),
    "MouseButtonPress": lambda: _mouse(QEvent.Type.MouseButtonPress),
    "MouseButtonRelease": lambda: _mouse(QEvent.Type.MouseButtonRelease),
    "MouseButtonDblClick": lambda: _mouse(QEvent.Type.MouseButtonDblClick),
    "MouseMove": lambda: _mouse(QEvent.Type.MouseMove),
    "NonClientAreaMouseButtonPress": lambda: _mouse(QEvent.Type.NonClientAreaMouseButtonPress),
    "NonClientAreaMouseButtonRelease": lambda: _mouse(QEvent.Type.NonClientAreaMouseButtonRelease),
    "NonClientAreaMouseButtonDblClick": lambda: _mouse(
        QEvent.Type.NonClientAreaMouseButtonDblClick
    ),
    "NonClientAreaMouseMove": lambda: _mouse(QEvent.Type.NonClientAreaMouseMove),
}


@pytest.mark.parametrize("kind", list(SINGLE_EVENTS))
def test_each_input_event_alone_makes_the_user_interacting(window, kind):
    """입력 이벤트는 종류마다 하나만 와도 조작 중이어야 한다.

    창 안의 위젯에 KeyPress · KeyRelease · MouseButtonPress · MouseButtonRelease ·
    MouseButtonDblClick · MouseMove · 창 테두리의 같은 네 가지(NonClientArea…) 가운데 하나만 보냄
    -> 보내기 전 False, 보낸 뒤 True
    """
    assert activity.user_interacting() is False

    QApplication.sendEvent(window.child, SINGLE_EVENTS[kind]())

    assert activity.user_interacting() is True


def test_interaction_ends_after_the_interaction_time(window, clock):
    """마지막 입력 뒤 INTERACTION_SECONDS(0.2초)가 지나면 조작 중이 아니어야 한다.

    시계 1000.0에서 키 입력 -> 시계 1000.19: True, 1000.2: False
    """
    assert activity.INTERACTION_SECONDS == 0.2
    QTest.keyClick(window, Qt.Key.Key_A)

    clock.value = 1000.19
    still = activity.user_interacting()
    clock.value = 1000.2
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

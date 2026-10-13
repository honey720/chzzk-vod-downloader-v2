"""임시 진단 — 창을 옮긴 뒤 창(QWindow)에 Move 이벤트가 언제 오는지 러너에서 본다 (#309).

`test_user_activity.py`의 "창 옮기기는 조작 중"이 리눅스 러너(xvfb)에서만 실패했다. 원인을
가르려고 플랫폼마다 이벤트가 오는 때를 경고로 찍는다(pytest의 경고 요약에 나온다). 단언은
없다 — 원인을 확인한 뒤 이 파일은 지운다.
"""

import time
import warnings

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QWidget


class _Seen(QObject):
    """앱 전체의 Move · Resize 이벤트를 받은 시각과 받은 객체의 종류로 적는다."""

    def __init__(self):
        super().__init__()
        self.started = time.monotonic()
        self.events: list[tuple[str, str, float]] = []

    def eventFilter(self, watched, event):  # noqa: N802 — Qt의 이름
        kind = event.type()
        if kind in (QEvent.Type.Move, QEvent.Type.Resize):
            who = "window" if watched.isWindowType() else "widget"
            name = "Move" if kind == QEvent.Type.Move else "Resize"
            self.events.append((name, who, round(time.monotonic() - self.started, 3)))
        return False


def _watch(qapp, action, seconds: float) -> list[tuple[str, str, float]]:
    seen = _Seen()
    qapp.installEventFilter(seen)
    try:
        seen.started = time.monotonic()
        action()
        QApplication.processEvents()
        first = list(seen.events)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        return [
            ("--첫 processEvents까지--", "", 0.0),
            *first,
            ("--그 뒤--", "", 0.0),
            *seen.events[len(first) :],
        ]
    finally:
        qapp.removeEventFilter(seen)


def test_tmp_report_when_move_and_resize_events_reach_the_window(qapp, qtbot):
    """(임시 진단) 창을 옮기고 · 크기를 바꾼 뒤 이벤트가 온 때를 경고로 찍는다. 단언 없음."""
    window = QWidget()
    qtbot.addWidget(window)
    window.resize(300, 200)
    window.show()
    qtbot.waitExposed(window)
    for _ in range(20):
        QApplication.processEvents()
        time.sleep(0.01)
    before = (window.x(), window.y(), window.width(), window.height())
    handle = window.windowHandle()
    before_handle = (handle.x(), handle.y())

    moved = _watch(qapp, lambda: window.move(window.x() + 30, window.y() + 30), 1.0)
    after_move = (window.x(), window.y())
    after_move_handle = (handle.x(), handle.y())
    resized = _watch(qapp, lambda: window.resize(window.width() + 40, window.height()), 1.0)
    moved_again = _watch(qapp, lambda: window.move(window.x() + 30, window.y() + 30), 1.0)

    warnings.warn(
        "MOVE-PROBE "
        f"platform={qapp.platformName()} before={before} handle_before={before_handle} "
        f"after_move={after_move} handle_after_move={after_move_handle} "
        f"move={moved} resize={resized} move_again={moved_again}",
        stacklevel=1,
    )

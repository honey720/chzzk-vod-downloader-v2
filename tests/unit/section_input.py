"""구간 편집 창의 시각 입력(시분초 칸 + 프레임 칸)에 키로 값을 넣는 테스트 도우미 (#309).

시각 하나는 칸 둘이다(``app/widgets/timecode_edit.py``의 ``TimePointEdit``). 테스트는 값을
``HH:MM:SS:FF`` 한 줄로 적고, 여기서 두 칸에 나눠 친다 — 끝 두 자리가 프레임 칸, 그 앞이
시분초 칸이다. 콜론은 읽기 좋으라고 적는 것이다(칸은 숫자만 받는다).
"""

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

_FRAME_DIGITS = 2


def _pump() -> None:
    for _ in range(3):
        QApplication.processEvents()


def split_digits(text: str) -> tuple[str, str]:
    """``HH:MM:SS:FF`` 꼴의 글(또는 숫자열)을 (시분초 칸에 칠 숫자, 프레임 칸에 칠 숫자)로 나눈다."""
    digits = "".join(ch for ch in text if ch in "0123456789")
    return digits[:-_FRAME_DIGITS], digits[-_FRAME_DIGITS:]


def type_time(edit, text: str) -> None:
    """시각의 두 칸을 비우고 키로 값을 넣는다 — 칸을 떠나지 않는다(Enter도 누르지 않는다).

    프레임이 00이면 프레임 칸은 비워 두기만 한다 — 치지 않은 프레임 칸의 값이 00이다.
    그래서 마지막으로 친 칸은 시분초 칸이다(프레임이 00이 아니면 프레임 칸).
    """
    clock, frame = split_digits(text)
    QTest.keyClick(edit.clockEdit, Qt.Key.Key_Delete)
    QTest.keyClick(edit.frameEdit, Qt.Key.Key_Delete)
    if clock:
        QTest.keyClicks(edit.clockEdit, clock)
    if frame.strip("0"):
        QTest.keyClicks(edit.frameEdit, frame)
    _pump()


def type_clock(edit, digits: str) -> None:
    """시분초 칸에 숫자를 이어 친다 — 비우지 않는다."""
    QTest.keyClicks(edit.clockEdit, digits)
    _pump()


def type_frame(edit, digits: str) -> None:
    """프레임 칸에 숫자를 이어 친다 — 비우지 않는다."""
    QTest.keyClicks(edit.frameEdit, digits)
    _pump()


def leave_time(edit) -> None:
    """그 시각의 편집을 끝낸다 — 두 칸을 모두 떠날 때 스스로 하는 일(``commit``)이다."""
    edit.commit()
    _pump()


def enter_time(edit, text: str) -> None:
    """시각에 값을 치고 편집을 끝낸다."""
    type_time(edit, text)
    leave_time(edit)

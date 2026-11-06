"""묶이는 PySide6 · Qt 판 가드 — macOS 실행 즉시 충돌을 낸 6.11.0 · 6.11.1로 돌아가지 않는다 (#494).

Qt 6.11.0 · 6.11.1의 `QImage::toCGImage()`는 색 공간을 가진 이미지를 Core Graphics로 넘길 때 이미 놓은 색 공간을
건넨다(Qt QTBUG-147602, qtbase 6.11.2에서 고침). macOS에서 창 아이콘(`setWindowIcon`)을 걸 때 그 길을 타 앱이
시작하자마자 끝났다. 판을 6.11.2로 올렸고, 락이 다시 그 판으로 풀리면 이 시험이 막는다. 실행 중인 판을 그대로 잰다 —
락 글자가 아니라 실제로 불러온 라이브러리다.
"""

import PySide6
from PySide6.QtCore import qVersion

BROKEN = ("6.11.0", "6.11.1")  # macOS 실행 충돌을 내는 판(#494)


def _release(version: str) -> str:
    """판 글에서 앞 세 자리만 — PySide6 판에는 꼬리(6.8.1.1 등)가 붙을 수 있다."""
    return ".".join(version.split(".")[:3])


def test_running_pyside6_is_not_a_crashing_release() -> None:
    """실행 중인 PySide6가 6.11.0 · 6.11.1이면 실패해야 한다.

    PySide6.__version__ -> BROKEN에 없음
    """
    assert _release(PySide6.__version__) not in BROKEN, PySide6.__version__


def test_running_qt_is_not_a_crashing_release() -> None:
    """실행 중인 Qt 라이브러리가 6.11.0 · 6.11.1이면 실패해야 한다.

    qVersion() -> BROKEN에 없음
    """
    assert _release(qVersion()) not in BROKEN, qVersion()

"""테스트가 남긴 마우스 버튼 눌림을 푸는 도우미 · GUI 공용 픽스처 테스트 (#590)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from tests.app.gui.mouse_buttons import double_click, release_held_buttons, release_left_by

ROOT = Path(__file__).resolve().parents[3]  # 저장소 루트 — 하위 pytest가 tests 패키지를 찾는 곳
LEFT = Qt.MouseButton.LeftButton
NO_BUTTON = Qt.MouseButton.NoButton


class _Recorder(QWidget):
    """받은 마우스 이벤트 종류를 차례대로 적는 위젯."""

    def __init__(self) -> None:
        """빈 기록으로 만든다."""
        super().__init__()
        self.seen: list[QEvent.Type] = []

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — Qt 재정의
        """누름을 적는다."""
        self.seen.append(event.type())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — Qt 재정의
        """놓기를 적는다."""
        self.seen.append(event.type())

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — Qt 재정의
        """더블클릭을 적는다."""
        self.seen.append(event.type())


@pytest.fixture
def recorder(qtbot: QtBot) -> _Recorder:
    """띄운 기록 위젯."""
    widget = _Recorder()
    qtbot.addWidget(widget)
    widget.resize(100, 100)
    widget.show()
    qtbot.waitExposed(widget)
    return widget


def test_a_widget_double_click_leaves_the_left_button_pressed(recorder: _Recorder) -> None:
    """위젯 대상 QTest.mouseDClick 뒤에는 앱 버튼 상태에 왼쪽 버튼이 남아야 한다.

    위젯에 QTest.mouseDClick -> 받은 이벤트 [DblClick] · mouseButtons() == LeftButton
    """
    QTest.mouseDClick(recorder, LEFT)
    seen, buttons = list(recorder.seen), QApplication.mouseButtons()
    release_held_buttons()  # 공용 픽스처가 이 테스트를 실패시키지 않게 푼다
    assert (seen, buttons) == ([QEvent.Type.MouseButtonDblClick], LEFT)


def test_release_held_buttons_frees_a_left_button(recorder: _Recorder) -> None:
    """눌린 왼쪽 버튼이 남아 있으면 release_held_buttons가 풀고 그 버튼을 돌려줘야 한다.

    위젯에 QTest.mouseDClick -> release_held_buttons() == [LeftButton] · mouseButtons() == NoButton
    """
    QTest.mouseDClick(recorder, LEFT)
    assert QApplication.mouseButtons() == LEFT, "전제: 왼쪽 버튼이 남았다"
    assert (release_held_buttons(), QApplication.mouseButtons()) == ([LEFT], NO_BUTTON)


def test_release_held_buttons_sends_nothing_to_the_widget(recorder: _Recorder) -> None:
    """release_held_buttons는 테스트가 띄운 위젯에 이벤트를 보내지 않아야 한다.

    위젯에 QTest.mouseDClick 뒤 release_held_buttons -> 위젯이 받은 이벤트 [DblClick] 그대로
    """
    QTest.mouseDClick(recorder, LEFT)
    release_held_buttons()
    assert recorder.seen == [QEvent.Type.MouseButtonDblClick]


def test_release_left_by_returns_none_without_held_buttons(recorder: _Recorder) -> None:
    """눌린 버튼이 없으면 release_left_by는 None이어야 한다.

    mouseButtons() == NoButton -> release_left_by("a.py::t") is None
    """
    assert QApplication.mouseButtons() == NO_BUTTON, "전제: 눌린 버튼이 없다"
    assert release_left_by("a.py::t") is None


def test_double_click_sends_only_a_double_click_to_the_widget(recorder: _Recorder) -> None:
    """double_click은 위젯에 더블클릭 이벤트 하나만 보내야 한다.

    double_click(위젯) -> 받은 이벤트 [DblClick]
    """
    double_click(recorder)
    assert recorder.seen == [QEvent.Type.MouseButtonDblClick]


def test_double_click_leaves_no_button_pressed(recorder: _Recorder) -> None:
    """double_click 뒤에는 앱에 눌린 버튼이 없어야 한다.

    double_click(위젯) -> mouseButtons() == NoButton
    """
    double_click(recorder)
    assert QApplication.mouseButtons() == NO_BUTTON


# ---- 공용 픽스처 배선 — 하위 pytest 프로세스에서 conftest 픽스처를 그대로 쓴다 ----

LEAKING_SUITE = """
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget


def test_leaves_a_button(qtbot):
    widget = QWidget()
    qtbot.addWidget(widget)
    QTest.mouseDClick(widget, Qt.MouseButton.LeftButton)
    assert QApplication.mouseButtons() == Qt.MouseButton.LeftButton


def test_runs_after(qtbot):
    assert QApplication.mouseButtons() == Qt.MouseButton.NoButton
"""  # 버튼을 남기는 테스트 하나 · 그 뒤 「버튼 없음」을 단언하는 테스트 하나


@pytest.fixture(scope="module")
def leaking_run(tmp_path_factory: pytest.TempPathFactory) -> str:
    """GUI conftest 픽스처만 건 하위 pytest로 LEAKING_SUITE를 차례대로 돌린 출력."""
    folder = tmp_path_factory.mktemp("leaking_suite")
    (folder / "conftest.py").write_text(
        "from tests.app.gui.conftest import _release_held_mouse_buttons  # noqa: F401\n",
        encoding="utf-8",
    )
    (folder / "test_leaks.py").write_text(LEAKING_SUITE, encoding="utf-8")
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(ROOT),
        PYTHONIOENCODING="utf-8",
        QT_QPA_PLATFORM="offscreen",
        PYTEST_ADDOPTS="",
    )
    env.pop("PYTEST_XDIST_WORKER", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rA"],
        cwd=folder,
        env=env,
        capture_output=True,
        timeout=120,
    )
    return result.stdout.decode("utf-8", errors="replace")


def test_the_fixture_fails_the_test_that_left_a_button_with_its_name(leaking_run: str) -> None:
    """버튼을 남긴 테스트는 공용 픽스처가 그 테스트 이름을 넣어 teardown 오류로 실패시켜야 한다.

    test_leaves_a_button이 왼쪽 버튼을 남김 -> 「ERROR test_leaks.py::test_leaves_a_button」 줄과
    「눌린 채 남겼다 — test_leaks.py::test_leaves_a_button: LeftButton」 메시지
    """
    assert "ERROR test_leaks.py::test_leaves_a_button" in leaking_run, leaking_run
    assert "눌린 채 남겼다 — test_leaks.py::test_leaves_a_button: LeftButton" in leaking_run


def test_the_fixture_releases_the_button_before_the_next_test(leaking_run: str) -> None:
    """버튼을 남긴 테스트 뒤 테스트는 「버튼 없음」에서 시작해야 한다.

    test_leaves_a_button 다음 test_runs_after -> PASSED
    """
    assert "PASSED test_leaks.py::test_runs_after" in leaking_run, leaking_run

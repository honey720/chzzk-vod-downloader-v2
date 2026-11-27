"""테스트 실행 중에만 Windows 메시지 상자 알림음이 꺼지는지 (#664).

``tests/alert_sound.py``의 규칙, ``conftest.py``가 실제로 건 처리기, 앱 실행 경로에는 처리기가 없다는
것을 잰다.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

from tests import alert_sound

ROOT_DIR = Path(__file__).resolve().parents[2]
# 앱 실행 경로 — 이 파일들 어디에도 처리기를 거는 코드가 없어야 한다
APP_SOURCES = [ROOT_DIR / "CVDv2.py", *sorted((ROOT_DIR / "app").rglob("*.py"))]
APP_SOURCES += sorted((ROOT_DIR / "core").rglob("*.py"))
# 자식 파이썬이 테스트 conftest 없이 QApplication을 만든 뒤 걸린 처리기 주소를 찍는다
_REPORT_HANDLER = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from PySide6.QtWidgets import QApplication\n"
    "qapp = QApplication([])\n"
    "from tests.alert_sound import current_handler_address\n"
    "print(current_handler_address())\n"
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows 플랫폼 플러그인 전용")


def test_install_on_other_platforms_does_nothing() -> None:
    """Windows가 아닌 플랫폼 값으로 부르면 아무것도 걸지 않아야 한다 (#664).

    platform="linux" · "darwin" -> install이 False, installed()는 부르기 전과 같음
    """
    before = alert_sound.installed()

    assert alert_sound.install("linux") is False
    assert alert_sound.install("darwin") is False
    assert alert_sound.installed() is before


def test_conftest_installs_the_handler_on_windows_only() -> None:
    """conftest가 Windows에서만 처리기를 걸어야 한다 (#664).

    sys.platform == "win32" -> installed() True, 그 밖 -> False
    """
    assert alert_sound.installed() is (sys.platform == "win32")


def test_app_sources_do_not_touch_the_update_handler() -> None:
    """앱 실행 경로 소스에 처리기를 거는 코드가 없어야 한다 (#664).

    CVDv2.py · app/ · core/ 의 .py -> "installUpdateHandler" · "alert_sound" 없음
    """
    found = [
        str(path.relative_to(ROOT_DIR))
        for path in APP_SOURCES
        if any(
            word in path.read_text(encoding="utf-8")
            for word in ("installUpdateHandler", "alert_sound")
        )
    ]

    assert len(APP_SOURCES) > 10  # 경로를 잘못 잡아 빈 목록을 재지 않게
    assert found == []


@windows_only
def test_test_process_has_the_handler_installed() -> None:
    """pytest 프로세스에는 Qt에 처리기가 걸려 있어야 한다 (#664).

    current_handler_address() -> 0이 아님
    """
    assert alert_sound.current_handler_address() != 0


@windows_only
def test_process_without_conftest_has_no_handler() -> None:
    """conftest 없이 QApplication만 만든 프로세스에는 처리기가 없어야 한다 (#664).

    자식 파이썬(offscreen, QApplication만 만듦) -> current_handler_address() == 0
    """
    done = subprocess.run(
        [sys.executable, "-c", _REPORT_HANDLER, str(ROOT_DIR)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        check=True,
    )

    assert done.stdout.strip() == "0"


@windows_only
def test_warning_box_show_reaches_the_test_handler(qtbot) -> None:
    """Warning 메시지 상자를 띄우면 접근성 이벤트가 테스트 처리기로 가야 한다 (#664).

    QMessageBox(Warning).show() -> update_count()가 show 전보다 큼
    """
    box = QMessageBox(QMessageBox.Icon.Warning, "t", "m", QMessageBox.StandardButton.Ok)
    qtbot.addWidget(box)
    before = alert_sound.update_count()

    box.show()

    assert alert_sound.update_count() > before

"""공유 화면의 다른 X 클라이언트가 이 워커 창의 초점을 빼앗지 못하는지 잰다 (#592).

Linux CI는 ``xvfb-run``의 X 서버 하나(공유 화면)를 띄우고, xdist 워커는 저마다 자기 Xvfb로 옮긴다
(``tests/qt_platform.py`` ``start_worker_display``). 자식 프로세스를 공유 화면에 붙여 다른 워커처럼
창 활성을 거듭 가져가게 하고, 그 동안 이 워커 창의 초점이 그대로인지 본다. 실제 포인터는 옮기지
않는다. 워커 Xvfb가 없는 실행(Windows · macOS · 로컬 · ``-n`` 없음)에서는 건너뛴다.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget
from pytestqt.qtbot import QtBot

from tests.qt_platform import SHARED_DISPLAY

STEAL_SECONDS = 3.0  # 자식이 공유 화면에서 창 활성을 되풀이해 가져가는 시간
CHILD_TIMEOUT = 60.0  # 초. 자식이 Qt 앱을 띄우고 끝나기까지의 상한
# 자식 — 창 하나를 띄우고 STEAL_SECONDS 동안 activateWindow()를 되풀이한다. 창을 띄운 뒤 "ready"를,
# 끝에 활성 요청 횟수와 자기 창이 한 번이라도 활성이 됐는지를 JSON으로 찍는다
_STEALER = """
import json, sys, time
from PySide6.QtWidgets import QApplication, QLineEdit
app = QApplication([])
win = QLineEdit()
win.setWindowTitle("focus stealer")
win.show()
app.processEvents()
print("ready", flush=True)
requests = 0
ever_active = False
end = time.monotonic() + float(sys.argv[1])
while time.monotonic() < end:
    win.raise_()
    win.activateWindow()
    requests += 1
    app.processEvents()
    ever_active = ever_active or win.isActiveWindow()
    time.sleep(0.02)
print(json.dumps({"requests": requests, "ever_active": ever_active}), flush=True)
"""


def _report_xvfb_run_args() -> None:
    """``xvfb-run``의 기본 인자(XVFBARGS) 줄을 경고로 남긴다 — 워커 Xvfb 인자와 비교하려고 (#592)."""
    found = shutil.which("xvfb-run")
    if not found:
        warnings.warn("xvfb-run 없음", stacklevel=1)
        return
    lines = Path(found).read_text(encoding="utf-8", errors="replace").splitlines()
    args = [line.strip() for line in lines if "XVFBARGS=" in line]
    warnings.warn(f"{found} XVFBARGS: {args}", stacklevel=1)


def _focused_field(qtbot: QtBot) -> tuple[QWidget, QLineEdit]:
    """창 하나를 띄워 활성이 될 때까지 기다린 뒤 입력칸에 초점을 주고 창과 입력칸을 돌려준다.

    부르는 쪽이 창을 붙잡아야 한다 — ``qtbot.addWidget``은 창을 붙잡지 않아, 창이 GC로 지워지면 자식
    입력칸도 함께 지워진다.
    """
    win = QWidget()
    qtbot.addWidget(win)
    field = QLineEdit(win)
    QVBoxLayout(win).addWidget(field)
    win.show()

    def focused() -> bool:
        if not win.isActiveWindow():
            win.activateWindow()
            return False
        field.setFocus()
        return field.hasFocus()

    qtbot.waitUntil(focused)
    return win, field


@pytest.mark.skipif(SHARED_DISPLAY not in os.environ, reason="워커 Xvfb가 있는 Linux CI 워커만")
def test_focus_stays_while_another_client_activates_on_the_shared_display(qtbot: QtBot) -> None:
    """공유 화면의 다른 X 클라이언트가 3초 동안 창 활성을 거듭 가져가도 이 워커 창의 초점은 그대로여야 한다 (#592).

    이 워커: 창을 활성으로 만들고 입력칸에 초점
    자식: DISPLAY=CVDV2_SHARED_DISPLAY, 3초 동안 20ms마다 activateWindow()
    -> 자식 창은 공유 화면에서 활성이 됨, 이 워커의 입력칸은 그 동안 매 확인에서 hasFocus()
    """
    _report_xvfb_run_args()
    win, field = _focused_field(qtbot)
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    env["DISPLAY"] = os.environ[SHARED_DISPLAY]
    child = subprocess.Popen(
        [sys.executable, "-c", _STEALER, str(STEAL_SECONDS)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready", child.stderr.read()
        checks = 0
        lost = 0
        deadline = time.monotonic() + CHILD_TIMEOUT
        while child.poll() is None and time.monotonic() < deadline:
            qtbot.wait(20)
            checks += 1
            lost += not field.hasFocus()
        out, err = child.communicate(timeout=CHILD_TIMEOUT)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()

    assert child.returncode == 0, err
    stolen = json.loads(out.strip().splitlines()[-1])
    assert stolen["requests"] > 0
    assert stolen["ever_active"], "자식 창이 공유 화면에서 한 번도 활성이 되지 않았다"
    assert checks > 0
    assert lost == 0, f"확인 {checks}번 중 {lost}번 초점을 잃었다"

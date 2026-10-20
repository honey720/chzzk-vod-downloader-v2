"""로컬 테스트의 Qt 플랫폼 기본값 (#399).

``tests/qt_platform.py``의 규칙과, ``conftest.py``가 실제로 정한 값을 잰다. conftest의 값은 이 세션이
아니라 환경 변수를 고른 별도 프로세스에서 conftest를 읽혀 잰다 — 이 세션의 값은 이미 정해져 있다.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.qt_platform import CI_FONT_DIRS, NATIVE_SWITCH, defaults

ROOT = Path(__file__).resolve().parents[2]  # 저장소 루트
# 별도 프로세스에서 지울 환경 변수 — 이 세션(CI · 로컬)의 값이 결과에 섞이지 않게
_CLEARED = ("QT_QPA_PLATFORM", "QT_QPA_FONTDIR", "CI", NATIVE_SWITCH)
# conftest를 읽은 뒤의 두 값을 JSON으로 찍는다. 인자가 app이면 Qt 앱을 만들어 쓰인 플랫폼도 찍는다
_READ_CONFTEST = (
    "import json, os, sys\n"
    "sys.path.insert(0, os.getcwd())\n"
    "import tests.conftest\n"
    "out = {k: os.environ.get(k) for k in ('QT_QPA_PLATFORM', 'QT_QPA_FONTDIR')}\n"
    "if sys.argv[1:] == ['app']:\n"
    "    from PySide6.QtGui import QGuiApplication\n"
    "    out['used'] = QGuiApplication([]).platformName()\n"
    "print(json.dumps(out))\n"
)


def _conftest_sets(extra: dict[str, str], *args: str) -> dict:
    """_CLEARED를 지우고 extra를 더한 환경에서 conftest를 읽은 별도 프로세스가 찍은 값."""
    env = {k: v for k, v in os.environ.items() if k not in _CLEARED} | extra
    done = subprocess.run(
        [sys.executable, "-c", _READ_CONFTEST, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        env=env,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_unset_platform_defaults_to_offscreen_with_the_ci_font_folder(platform):
    """플랫폼 값이 없는 로컬 실행이면 offscreen과 CI의 폰트 폴더를 더해야 한다 (#399).

    environ={}, platform -> {"QT_QPA_PLATFORM": "offscreen", "QT_QPA_FONTDIR": CI_FONT_DIRS[platform]}
    """
    assert defaults({}, platform) == {
        "QT_QPA_PLATFORM": "offscreen",
        "QT_QPA_FONTDIR": CI_FONT_DIRS[platform],
    }


def test_unset_platform_on_linux_defaults_to_offscreen_without_a_font_folder():
    """Linux는 CI가 offscreen 폰트 폴더를 주지 않으므로 offscreen만 더해야 한다 (#399).

    environ={}, platform="linux" -> {"QT_QPA_PLATFORM": "offscreen"}
    """
    assert defaults({}, "linux") == {"QT_QPA_PLATFORM": "offscreen"}


def test_font_folder_already_set_is_kept():
    """폰트 폴더를 이미 정했으면 그 값을 덮지 않아야 한다 (#399).

    environ={"QT_QPA_FONTDIR": "D:/fonts"}, platform="win32" -> {"QT_QPA_PLATFORM": "offscreen"}
    """
    assert defaults({"QT_QPA_FONTDIR": "D:/fonts"}, "win32") == {"QT_QPA_PLATFORM": "offscreen"}


@pytest.mark.parametrize(
    "environ",
    [
        {"QT_QPA_PLATFORM": "windows"},  # 이미 정한 값
        {"QT_QPA_PLATFORM": ""},  # 빈 값도 정한 값이다
        {"CI": "true"},  # CI — Linux는 값을 정하지 않고 xvfb로 돈다
        {NATIVE_SWITCH: "1"},  # 실제 플랫폼을 고름
    ],
    ids=["set", "set-empty", "ci", "native"],
)
def test_nothing_is_added_when_the_platform_is_set_or_on_ci_or_native(environ):
    """플랫폼을 이미 정했거나 CI이거나 CVDV2_TEST_NATIVE=1이면 아무것도 더하지 않아야 한다 (#399).

    environ -> {}
    """
    assert defaults(environ, "win32") == {}


def test_native_switch_other_than_1_still_defaults_to_offscreen():
    """CVDV2_TEST_NATIVE가 1이 아니면(0) offscreen을 더해야 한다 (#399).

    environ={"CVDV2_TEST_NATIVE": "0"}, platform="linux" -> {"QT_QPA_PLATFORM": "offscreen"}
    """
    assert defaults({NATIVE_SWITCH: "0"}, "linux") == {"QT_QPA_PLATFORM": "offscreen"}


def test_conftest_makes_a_local_run_use_offscreen_before_qt_makes_its_app():
    """값을 정하지 않은 로컬 실행에서 conftest를 읽은 뒤 만든 Qt 앱은 offscreen이어야 한다 (#399).

    별도 프로세스: QT_QPA_PLATFORM · QT_QPA_FONTDIR · CI · CVDV2_TEST_NATIVE 없음 → conftest를 읽고
    QGuiApplication을 만듦
    -> 쓰인 플랫폼 "offscreen", 폰트 폴더는 이 OS의 CI 값(Linux는 없음)
    """
    seen = _conftest_sets({}, "app")

    assert seen["used"] == "offscreen"
    assert seen["QT_QPA_FONTDIR"] == CI_FONT_DIRS.get(sys.platform)


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"QT_QPA_PLATFORM": "minimal"}, "minimal"),  # 이미 정한 값
        ({"CI": "true"}, None),  # CI
        ({NATIVE_SWITCH: "1"}, None),  # 실제 플랫폼
    ],
    ids=["set", "ci", "native"],
)
def test_conftest_leaves_the_platform_alone_when_set_or_on_ci_or_native(extra, expected):
    """이미 정했거나 CI이거나 CVDV2_TEST_NATIVE=1이면 conftest가 플랫폼 · 폰트 폴더를 바꾸지 않아야 한다 (#399).

    별도 프로세스: 위 넷을 지운 뒤 extra만 둠 → conftest를 읽음
    -> QT_QPA_PLATFORM == expected, QT_QPA_FONTDIR 없음
    """
    seen = _conftest_sets(extra)

    assert seen == {"QT_QPA_PLATFORM": expected, "QT_QPA_FONTDIR": None}

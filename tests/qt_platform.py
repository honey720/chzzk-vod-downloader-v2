"""로컬 테스트가 Qt 창을 실제 화면에 띄우지 않게 플랫폼 기본값을 정하는 도구 (#399).

``QT_QPA_PLATFORM``을 정하지 않고 ``uv run pytest``를 돌리면 Qt가 실제 플랫폼(Windows · cocoa ·
xcb)을 써서, 테스트가 만드는 창이 화면에 떴다 사라진다. CI는 값을 정해 둔다(Windows · macOS는
``offscreen``, Linux는 정하지 않고 ``xvfb``의 가상 화면에서 xcb). ``conftest.py``가 Qt 앱을 만들기
전에 ``apply``를 부른다 — Qt는 ``QGuiApplication``을 만들 때 이 값을 읽는다.

- CI(``CI`` 환경 변수)에서는 아무것도 바꾸지 않는다 — Linux CI는 값을 정하지 않은 채 xvfb로 돈다.
- ``CVDV2_TEST_NATIVE=1``이면 실제 플랫폼을 쓴다(값을 정하지 않는다).
- 이미 정한 플랫폼은 그대로 둔다. 값이 없으면 ``offscreen``으로 둔다.
- 플랫폼이 ``offscreen``이면(직접 정했든 기본값이든) 폰트 폴더(``QT_QPA_FONTDIR``) 변수가 없을 때
  CI와 같은 폴더를 준다 — offscreen은 시스템 폰트를 스스로 찾지 않아 한글이 □로 그려지고(#154),
  대체 폰트로는 창 최소폭이 달라진다(#405). 이미 있는 값은 빈 문자열 · 빈 폴더라도 그대로 둔다.

Linux CI의 xdist 워커는 ``start_worker_display``로 워커마다 따로 Xvfb를 띄운다(#592) — 위 규칙과는
따로 동작하고, 위 두 함수는 바꾸지 않는다.
"""

import atexit
import os
import subprocess
import tempfile
import time
from collections.abc import MutableMapping
from typing import IO

NATIVE_SWITCH = "CVDV2_TEST_NATIVE"  # 1이면 로컬 테스트도 실제 플랫폼을 쓴다
# offscreen일 때 CI가 주는 폰트 폴더 (.github/workflows/ci.yml). Linux CI는 offscreen을 쓰지 않아 값이 없다
CI_FONT_DIRS = {"win32": r"C:\Windows\Fonts", "darwin": "/System/Library/Fonts"}
# 워커가 자기 Xvfb로 옮기기 전의 DISPLAY(xvfb-run이 띄운 공유 화면)를 남겨 두는 환경 변수 (#592)
SHARED_DISPLAY = "CVDV2_SHARED_DISPLAY"
# 워커 Xvfb의 화면 — xvfb-run 기본값(-screen 0 1280x1024x24 -nolisten tcp)과 같게 둔다 (#592)
WORKER_XVFB_ARGS = ("-screen", "0", "1280x1024x24", "-nolisten", "tcp")
WORKER_XVFB_START_TIMEOUT = 30.0  # 초. Xvfb가 화면 번호를 알려 주기까지 기다리는 상한


class WorkerDisplayError(RuntimeError):
    """워커 Xvfb를 띄우지 못했다 — 공유 화면으로 조용히 돌아가지 않게 테스트 실행을 멈춘다."""


def start_worker_display(environ: MutableMapping[str, str], platform: str) -> str | None:
    """Linux CI의 xdist 워커면 그 워커만 쓰는 Xvfb를 띄우고 DISPLAY를 그 화면으로 바꾼다 (#592).

    ``xvfb-run -a`` 하나의 X 서버에 워커가 모두 붙으면 입력 초점 · 포인터가 서버에 하나뿐이라, 한 워커의
    ``activateWindow()``가 다른 워커 창의 초점을 빼앗는다. 워커마다 X 서버를 따로 두면 그 간섭이 없다.

    리눅스 · ``CI`` 있음 · ``PYTEST_XDIST_WORKER`` 있음 셋이 모두 참일 때만 동작하고, 그 밖(Windows ·
    macOS · 로컬 · xdist 컨트롤러 · ``-n`` 없는 실행)에서는 아무것도 하지 않는다. Qt 앱을 만들기 전에
    한 번 부른다. 원래 DISPLAY는 ``CVDV2_SHARED_DISPLAY``에 남긴다. Xvfb는 프로세스가 끝날 때 끈다.

    Returns:
        새 DISPLAY 값(예: ``":5"``). 아무것도 하지 않았으면 None

    Raises:
        WorkerDisplayError: Xvfb가 시작하지 못했거나 화면 번호를 알려 주지 않았을 때
    """
    if not platform.startswith("linux"):
        return None
    if not environ.get("CI"):
        return None
    if not environ.get("PYTEST_XDIST_WORKER"):
        return None
    display = f":{_launch_xvfb()}"
    environ[SHARED_DISPLAY] = environ.get("DISPLAY", "")
    environ["DISPLAY"] = display
    return display


def _launch_xvfb() -> int:
    """``Xvfb -displayfd``로 빈 화면 번호를 Xvfb가 고르게 해 띄우고, 그 번호를 돌려준다.

    Xvfb는 잠금 파일을 원자적으로 만들어 번호를 고르고, 연결을 받을 준비가 끝난 뒤에 번호를 쓴다 —
    워커 여럿이 동시에 띄워도 번호가 겹치지 않고 준비를 따로 기다리지 않는다.
    """
    import select  # Windows에서는 파이프에 쓸 수 없어 Linux 경로에서만 읽는다

    read_fd, write_fd = os.pipe()
    log = tempfile.TemporaryFile()
    try:
        xvfb = subprocess.Popen(
            ["Xvfb", "-displayfd", str(write_fd), *WORKER_XVFB_ARGS],
            pass_fds=(write_fd,),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
    except OSError as e:
        os.close(read_fd)
        os.close(write_fd)
        raise WorkerDisplayError(f"Xvfb를 실행하지 못했다: {e}") from e
    os.close(write_fd)
    atexit.register(_stop_xvfb, xvfb)

    written = b""
    deadline = time.monotonic() + WORKER_XVFB_START_TIMEOUT
    try:
        while not written.endswith(b"\n"):
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([read_fd], [], [], left)[0]:
                raise WorkerDisplayError(_xvfb_failure("화면 번호를 기다리다 시간 초과", log))
            chunk = os.read(read_fd, 64)
            if not chunk:
                raise WorkerDisplayError(
                    _xvfb_failure(f"화면 번호 없이 끝남(종료 코드 {xvfb.poll()})", log)
                )
            written += chunk
    finally:
        os.close(read_fd)
    return int(written.strip())


def _xvfb_failure(what: str, log: IO[bytes]) -> str:
    """Xvfb 실패 문장 — Xvfb가 남긴 출력을 붙인다."""
    log.seek(0)
    output = log.read().decode("utf-8", "replace").strip()
    return f"워커 Xvfb {what}: {output or '(출력 없음)'}"


def _stop_xvfb(xvfb: subprocess.Popen[bytes]) -> None:
    """워커 Xvfb를 끈다 — 프로세스가 끝날 때 부른다."""
    if xvfb.poll() is not None:
        return
    xvfb.terminate()
    try:
        xvfb.wait(timeout=5)
    except subprocess.TimeoutExpired:
        xvfb.kill()


def defaults(environ: MutableMapping[str, str], platform: str) -> dict[str, str]:
    """environ에 더할 Qt 플랫폼 기본값 — 더할 것이 없으면 빈 dict.

    Args:
        environ: 지금의 환경 변수
        platform: ``sys.platform`` 값
    """
    if environ.get("CI") or environ.get(NATIVE_SWITCH) == "1":
        return {}
    added = {} if "QT_QPA_PLATFORM" in environ else {"QT_QPA_PLATFORM": "offscreen"}
    if environ.get("QT_QPA_PLATFORM", "offscreen") != "offscreen":
        return added
    if "QT_QPA_FONTDIR" not in environ and platform in CI_FONT_DIRS:
        added["QT_QPA_FONTDIR"] = CI_FONT_DIRS[platform]
    return added


def apply(environ: MutableMapping[str, str], platform: str) -> dict[str, str]:
    """``defaults``의 값을 environ에 넣고, 넣은 것을 돌려준다."""
    added = defaults(environ, platform)
    environ.update(added)
    return added

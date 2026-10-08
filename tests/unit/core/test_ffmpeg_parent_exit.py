"""앱이 어떻게 끝나든 앱이 띄운 ffmpeg가 남지 않는지 — Windows (#309).

컷 도중 일시정지한 채 앱이 정상 종료가 아닌 길로 끝나면 멈춘 ffmpeg가 멈춘 채 남았다. 앱이
띄우는 ffmpeg를 잡(Job Object)에 넣어, 앱 프로세스가 사라지면 OS가 함께 끝내게 한다
(``core/utils/ffmpeg.py``의 ``_end_with_parent``).

앱 대신 파이썬 프로세스 하나를 띄워 그 안에서 제품의 ``run_ffmpeg``로 10분 도는 ffmpeg를
띄우게 하고, 그 파이썬 프로세스를 강제로 끝낸다. ffmpeg는 그 프로세스가 알려 준 PID로만
확인하고, 남았으면 그 PID로만 끝낸다.
"""

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="잡으로 묶는 것은 Windows만 한다 — 그 밖의 OS는 아직 없다"
)

# 앱 대신 도는 프로세스 — 제품의 run_ffmpeg로 10분 도는 ffmpeg를 띄우고, 제품이 그 프로세스를
# 지켜보기 시작하면(일시정지 확인이 불린다 — 잡에 넣은 뒤다) 그 PID를 한 줄 적는다
_PARENT = """
import subprocess
import sys

import core.utils.ffmpeg as ffmpeg_module
from tests.unit.core.long_ffmpeg import LONG_RUNNING

launched = []
told = []
real = subprocess.Popen


class Recording(real):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        launched.append(self.pid)


ffmpeg_module.subprocess.Popen = Recording
ffmpeg_module.get_ffmpeg_exe()  # 실행 파일을 찾는 확인 실행을 미리 치른다
launched.clear()


def should_pause() -> bool:
    if launched and not told:
        told.append(1)
        print(launched[0], flush=True)
    return sys.argv[1] == "paused" and bool(launched)


ffmpeg_module.run_ffmpeg(LONG_RUNNING, timeout=600, should_pause=should_pause)
"""

_GONE_WITHIN_SECONDS = 10.0  # 부모가 사라진 뒤 ffmpeg가 끝나기를 기다려 주는 시간


def _alive(pid: int) -> bool:
    """그 PID의 프로세스가 살아 있는지 — 프로세스 목록에서 그 PID 하나만 묻는다."""
    listed = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    return f'"{pid}"' in listed


def _end(pid: int) -> None:
    """남은 프로세스를 그 PID로 끝낸다 — 테스트가 실패해도 10분짜리 ffmpeg를 남기지 않는다."""
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)


@pytest.mark.parametrize("mode", ["running", "paused"])
def test_ffmpeg_ends_when_the_process_that_launched_it_is_killed(tmp_path, mode):
    """ffmpeg를 띄운 프로세스가 강제로 끝나면 그 ffmpeg도 끝나야 한다 — 일시정지로 멈춰 있는 것도.

    파이썬 프로세스가 제품의 run_ffmpeg로 10분 도는 ffmpeg를 띄움(paused는 띄운 직후 일시정지).
    그 ffmpeg의 PID를 받은 0.5초 뒤 파이썬 프로세스를 강제로 끝냄(Popen.kill — 정리 코드가 돌지 않는다)
    -> 끝내기 전: 그 PID의 ffmpeg가 살아 있다
    -> 끝낸 뒤 10초 안에: 그 PID의 프로세스가 없다
    """
    script = tmp_path / "parent.py"
    script.write_text(_PARENT, encoding="utf-8")
    repo = os.getcwd()
    env = dict(os.environ, PYTHONPATH=repo, PYTHONDONTWRITEBYTECODE="1")
    parent = subprocess.Popen(
        [sys.executable, str(script), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=repo,
        env=env,
    )
    child = None
    try:
        line = parent.stdout.readline().strip()
        assert line.isdigit(), f"전제: ffmpeg의 PID를 받았다 — {line!r} {parent.poll()}"
        child = int(line)
        time.sleep(0.5)  # paused: 제품이 그 ffmpeg를 멈출 틈
        alive_before = _alive(child)

        parent.kill()
        parent.wait(10)
        deadline = time.monotonic() + _GONE_WITHIN_SECONDS
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.1)
        gone = not _alive(child)

        assert alive_before, "전제: 부모를 끝내기 전에 ffmpeg가 살아 있었다"
        assert gone, "부모가 끝났는데 ffmpeg가 남았다"
    finally:
        if parent.poll() is None:
            parent.kill()
        if child is not None and _alive(child):
            _end(child)

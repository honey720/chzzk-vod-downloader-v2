"""CI windows에서 가끔 멈추던 일시정지 테스트를 두 갈래 30번씩 되풀이한다 — 반복 확인용, 머지하지 않는다 (#533).

#533의 고침(제어 실행 루프를 읽기 스레드 + `process.wait(timeout)`으로) 위에서, 멈춤이 났던
`test_time_spent_paused_does_not_count_towards_the_timeout`을 두 갈래(출력을 모으는 실행 · 진행을
받는 실행) 매개변수로 30번씩 그대로 부른다. windows에서만 돈다. 멈추면 #506의 poll 기록이 감시
실패 메시지에 남는다.
"""

import sys
from pathlib import Path

import pytest

from tests.ffmpeg.process.test_ffmpeg_pause import (  # noqa: F401 — 픽스처를 이 모듈에도 둔다
    launch_hooks,
    launched,
    poll_record,
    test_time_spent_paused_does_not_count_towards_the_timeout as _paused_run,
)

ROUNDS = 30  # 갈래마다 되풀이 횟수
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="windows에서만 되풀이한다(#533)")


@pytest.mark.parametrize("reporting", [False, True], ids=["출력을 모으는 실행", "진행을 받는 실행"])
@pytest.mark.parametrize("round_number", range(1, ROUNDS + 1))
def test_time_spent_paused_again(
    tmp_path: Path, request: pytest.FixtureRequest, round_number: int, reporting: bool
) -> None:
    """일시정지해 둔 시간을 제한 시간에 세지 않는 테스트가 두 갈래 30번 모두 끝나야 한다."""
    fixtures = [
        request.getfixturevalue(name) for name in ("launched", "launch_hooks", "poll_record")
    ]
    _paused_run(tmp_path, *fixtures, reporting)

"""CI windows에서 가끔 멈추는 일시정지 테스트를 30번 되풀이한다 — 재현용, 머지하지 않는다 (#505).

멈춘 실행은 모두 `test_time_spent_paused_does_not_count_towards_the_timeout[출력을 모으는 실행]`
이었다. 그 테스트를 매개변수 30개로 그대로 부른다(새 플러그인 없이). 멈추면 #505의 poll 기록이
감시 실패 메시지에 남는다.
"""

import pytest

from tests.ffmpeg.process.test_ffmpeg_pause import (  # noqa: F401 — 픽스처를 이 모듈에도 둔다
    launch_hooks,
    launched,
    poll_record,
    test_time_spent_paused_does_not_count_towards_the_timeout as _paused_run,
)

ROUNDS = 30  # 되풀이 횟수 — 멈춘 비율(71회 중 6)에서 한 번은 걸릴 만큼


@pytest.mark.parametrize("round_number", range(1, ROUNDS + 1))
def test_time_spent_paused_again(tmp_path, request, round_number):
    """일시정지해 둔 시간을 제한 시간에 세지 않는 테스트(출력을 모으는 실행)가 30번 모두 끝나야 한다."""
    fixtures = [
        request.getfixturevalue(name) for name in ("launched", "launch_hooks", "poll_record")
    ]
    _paused_run(tmp_path, *fixtures, False)

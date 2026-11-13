"""Linux CI의 xdist 워커가 자기 Xvfb를 쓰는지 잰다 (#592).

``start_worker_display``가 조용히 아무것도 하지 않으면 워커가 다시 공유 화면에 붙고, 다른 테스트는
그대로 통과한다. 이 테스트가 그 경우를 실패로 드러낸다. Linux CI 워커 밖에서는 건너뛴다.
"""

import os
import sys

import pytest

from tests.qt_platform import SHARED_DISPLAY

_LINUX_CI_WORKER = (
    sys.platform.startswith("linux")
    and bool(os.environ.get("CI"))
    and bool(os.environ.get("PYTEST_XDIST_WORKER"))
)  # 워커 Xvfb가 켜져야 하는 실행


@pytest.mark.skipif(not _LINUX_CI_WORKER, reason="Linux CI의 xdist 워커에서만 워커 Xvfb를 띄운다")
def test_linux_ci_worker_uses_its_own_display() -> None:
    """Linux CI의 xdist 워커면 DISPLAY가 공유 화면(CVDV2_SHARED_DISPLAY)과 달라야 한다 (#592).

    Linux · CI · PYTEST_XDIST_WORKER 있음
    -> CVDV2_SHARED_DISPLAY가 있고, DISPLAY는 비어 있지 않으며 그 값과 다름
    """
    assert SHARED_DISPLAY in os.environ
    assert os.environ.get("DISPLAY")
    assert os.environ["DISPLAY"] != os.environ[SHARED_DISPLAY]

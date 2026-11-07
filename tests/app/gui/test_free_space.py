"""여유 공간 측정이 경로마다 따로 도는지 (#504 — PR #508 리뷰).

한 경로(무응답 마운트)의 측정이 멈춰 있어도 다른 경로는 재야 하고, 같은 경로는 1초마다 불러도 스레드가
쌓이지 않아야 한다. `shutil.disk_usage` 자리에 경로 A에서만 붙잡히는 대역을 넣는다.
"""

import threading
from collections import namedtuple

import pytest

import app.system.free_space as free_space_module
from app.system.free_space import FreeSpaceProbe

_Usage = namedtuple("_Usage", "total used free")
PATH_A = "A:\\stuck"  # 붙잡히는 경로(무응답 마운트 자리)
PATH_B = "B:\\fine"  # 바로 돌아오는 경로
FREE = {PATH_A: 111, PATH_B: 222}  # 경로마다 돌려줄 여유 공간(바이트)


@pytest.fixture
def held(monkeypatch):
    """경로 A의 측정을 붙잡아 두는 대역 — 돌려준 Event를 set하면 풀린다. 끝날 때 늘 푼다."""
    release = threading.Event()

    def disk_usage(path):
        if path == PATH_A:
            release.wait(20)
        return _Usage(0, 0, FREE[path])

    monkeypatch.setattr(free_space_module.shutil, "disk_usage", disk_usage)
    yield release
    release.set()


def _probe_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "FreeSpaceProbe" and t.is_alive())


def test_a_stuck_path_does_not_block_another_path(qtbot, held):
    """경로 A의 측정이 붙잡혀 있어도 경로 B를 요청하면 B의 값이 들어와야 한다.

    A 요청(붙잡힘) → B 요청 -> 두 요청 모두 True, B 값 222가 들어오고 A는 아직 None
    """
    probe = FreeSpaceProbe()
    assert probe.request(PATH_A) is True
    assert probe.request(PATH_B) is True
    qtbot.waitUntil(lambda: probe.value(PATH_B) == FREE[PATH_B], timeout=5000)
    assert probe.value(PATH_A) is None


def test_asking_a_stuck_path_again_starts_no_thread(qtbot, held):
    """재는 중인 경로를 다시 요청하면 새로 재지 않아 측정 스레드가 늘지 않아야 한다.

    A 요청(붙잡힘) → A 다섯 번 더 요청 -> 다섯 번 모두 False, 측정 스레드 수 그대로
    """
    probe = FreeSpaceProbe()
    assert probe.request(PATH_A) is True
    before = _probe_threads()
    assert [probe.request(PATH_A) for _ in range(5)] == [False] * 5
    assert _probe_threads() == before


def test_a_released_path_reports_its_value_and_can_be_asked_again(qtbot, held):
    """붙잡혔던 경로가 풀리면 그 값이 들어오고, 그 뒤에는 다시 요청할 수 있어야 한다.

    A 요청(붙잡힘) → 풀어 줌 -> A 값 111이 들어옴, 다시 A 요청 True
    """
    probe = FreeSpaceProbe()
    assert probe.request(PATH_A) is True
    held.set()
    qtbot.waitUntil(lambda: probe.value(PATH_A) == FREE[PATH_A], timeout=5000)
    qtbot.waitUntil(lambda: probe.request(PATH_A), timeout=5000)

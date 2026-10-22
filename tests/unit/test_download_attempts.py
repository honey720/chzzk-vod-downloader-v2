"""앞 시도의 통지 (#410) — 중지한 뒤 다시 시작하면 앞 시도의 엔진이 낸 통지는 카드에 닿지 않는다.

배선은 viewmodel 계약 게이트(tests/unit/test_download_viewmodel_contract.py)의 대역을 그대로 쓴다.
같은 카드를 시작 → 중지 → 정리 → 다시 시작한 뒤, 첫 제출의 콜백(앞 시도의 엔진이 통지하는 입구)을
워커 스레드에서 부르고 content 대역의 기록 · 아이템 상태로 잰다.
"""

import threading

import pytest

from core.models.download_state import DownloadState
from core.models.events import ProgressEvent
from tests.unit.test_download_viewmodel_contract import Wired, _make_item
from tests.unit.test_download_viewmodel_contract import (  # noqa: F401 — 픽스처를 이 모듈에 들인다
    _stop_viewmodel_timers,
    log_calls,
)


def _on_worker(call) -> None:
    """엔진처럼 워커 스레드에서 부른다."""
    thread = threading.Thread(target=call)
    thread.start()
    thread.join()


@pytest.fixture
def restarted(qapp, log_calls):  # noqa: F811
    """같은 카드를 시작 → 중지 → 정리 → 다시 시작한 배선. 기록은 다시 시작한 뒤부터다."""
    wired = Wired(qapp, log_calls)
    item = _make_item()
    wired.start(item)
    wired.vm.stop()
    wired.vm.removeThreads()
    wired.start(item)
    wired.pump()
    wired.content.calls.clear()
    earlier, later = wired.service.submissions
    return wired, item, earlier, later


def _event() -> ProgressEvent:
    return ProgressEvent(downloaded_size=1000, total_size=4000, speed=0.0, active_threads=0)


def test_progress_of_the_earlier_attempt_does_not_reach_the_card(restarted):
    """앞 시도의 진행 통지는 content에 닿지 않고, 지금 시도의 통지는 닿아야 한다.

    시작 → 중지 → 다시 시작. 앞 시도의 on_progress(1000/4000) → 지금 시도의 on_progress(1000/4000)
    -> 앞 시도 뒤: content 기록 없음, 지금 시도 뒤: update_progress 1회
    """
    wired, item, earlier, later = restarted

    _on_worker(lambda: earlier["on_progress"](_event()))
    wired.pump()
    after_earlier = list(wired.content.names)
    _on_worker(lambda: later["on_progress"](_event()))
    wired.pump()

    assert after_earlier == []
    assert wired.content.names == ["update_progress"]


def test_finish_of_the_earlier_attempt_does_not_finish_the_current_one(restarted):
    """앞 시도의 완료 통지는 지금 시도를 끝내지 않아야 한다.

    시작 → 중지 → 다시 시작. 앞 시도의 on_finished
    -> content에 finish 없음, viewmodel의 핸들은 지금 시도의 것 그대로, 아이템은 RUNNING
    """
    wired, item, earlier, later = restarted

    _on_worker(earlier["on_finished"])
    wired.pump()

    assert "finish" not in wired.content.names
    assert wired.vm.handle is wired.service.handles[1]
    assert item.downloadState is DownloadState.RUNNING


def test_failure_of_the_earlier_attempt_does_not_fail_the_current_one(restarted):
    """앞 시도의 실패 통지는 지금 시도를 실패로 끝내지 않아야 한다.

    시작 → 중지 → 다시 시작. 앞 시도의 on_failed(RuntimeError("앞 시도"))
    -> content에 fail 없음, viewmodel의 핸들은 지금 시도의 것 그대로, 아이템은 RUNNING
    """
    wired, item, earlier, later = restarted

    _on_worker(lambda: earlier["on_failed"](RuntimeError("앞 시도")))
    wired.pump()

    assert "fail" not in wired.content.names
    assert wired.vm.handle is wired.service.handles[1]
    assert item.downloadState is DownloadState.RUNNING


def test_merge_start_of_the_earlier_attempt_does_not_mark_the_card(restarted):
    """앞 시도의 병합 시작 통지는 카드의 병합 표시를 켜지 않고, 지금 시도의 것은 켜야 한다.

    시작 → 중지 → 다시 시작. 앞 시도의 on_merge_start → 지금 시도의 on_merge_start
    -> 앞 시도 뒤: post_process 거짓, 지금 시도 뒤: 참
    """
    wired, item, earlier, later = restarted

    _on_worker(earlier["on_merge_start"])
    after_earlier = item.post_process
    _on_worker(later["on_merge_start"])

    assert after_earlier is False
    assert item.post_process is True


def test_failure_of_the_earlier_attempt_leaves_the_merge_mark_of_the_current_one(restarted):
    """앞 시도의 실패 통지는 지금 시도가 켠 병합 표시를 끄지 않아야 한다.

    시작 → 중지 → 다시 시작. 지금 시도의 on_merge_start → 앞 시도의 on_failed
    -> post_process 참 그대로
    """
    wired, item, earlier, later = restarted

    _on_worker(later["on_merge_start"])
    _on_worker(lambda: earlier["on_failed"](RuntimeError("앞 시도")))
    wired.pump()

    assert item.post_process is True

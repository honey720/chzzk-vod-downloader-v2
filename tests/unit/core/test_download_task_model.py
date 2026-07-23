"""core/models/download_task.py 상태 머신 단위 테스트 (#60).

검증 범위: 정상 전이 경로, 허용되지 않는 전이, 일시정지/재개 시 Event 동작,
진행률 집계, 상태 변경 콜백.
"""

import threading

import pytest

from core.models.download_state import DownloadState
from core.models.download_task import DownloadTaskModel, InvalidStateTransitionError


def test_initial_state_is_waiting():
    """생성 직후 상태는 WAITING이고 pause_event는 진행 가능(set) 상태다."""
    model = DownloadTaskModel()
    assert model.state is DownloadState.WAITING
    assert model.pause_event.is_set()
    assert not model.is_running()


def test_normal_transition_path():
    """정상 경로: 시작 → 일시정지 → 재개 → 완료."""
    model = DownloadTaskModel()

    assert model.start() is True
    assert model.state is DownloadState.RUNNING
    assert model.is_running()
    assert model.start_time > 0

    assert model.pause() is True
    assert model.state is DownloadState.PAUSED

    assert model.resume() is True
    assert model.state is DownloadState.RUNNING

    assert model.finish() is True
    assert model.state is DownloadState.FINISHED
    assert model.end_time >= model.start_time


def test_stop_from_running_returns_to_waiting():
    """실행 중 취소하면 WAITING으로 복귀한다."""
    model = DownloadTaskModel()
    model.start()
    assert model.stop() is True
    assert model.state is DownloadState.WAITING


def test_stop_from_paused_wakes_waiting_threads():
    """일시정지 상태에서 취소해도 WAITING으로 가고, 대기 중 스레드를 깨운다."""
    model = DownloadTaskModel()
    model.start()
    model.pause()
    assert model.stop() is True
    assert model.state is DownloadState.WAITING
    # 일시정지 대기(wait) 중인 스레드가 종료 경로로 빠져나가야 하므로 set이어야 한다
    assert model.pause_event.is_set()


def test_fail_transition():
    """실행 중 실패 처리하면 FAILED가 되고 종료 시각이 기록된다."""
    model = DownloadTaskModel()
    model.start()
    assert model.fail() is True
    assert model.state is DownloadState.FAILED
    assert model.end_time > 0


@pytest.mark.parametrize(
    ("setup", "invalid"),
    [
        # WAITING에서 pause/resume/finish 불가
        ([], "pause"),
        ([], "resume"),
        ([], "finish"),
        # FINISHED는 종료 상태 — 어떤 전이도 불가
        (["start", "finish"], "start"),
        (["start", "finish"], "pause"),
        (["start", "finish"], "resume"),
        (["start", "finish"], "stop"),
        (["start", "finish"], "fail"),
        # FAILED도 종료 상태 — 어떤 전이도 불가
        (["start", "fail"], "start"),
        (["start", "fail"], "resume"),
        (["start", "fail"], "finish"),
    ],
)
def test_invalid_transitions_raise(setup: list[str], invalid: str):
    """허용되지 않는 전이는 InvalidStateTransitionError를 던진다."""
    model = DownloadTaskModel()
    for name in setup:
        getattr(model, name)()
    with pytest.raises(InvalidStateTransitionError):
        getattr(model, invalid)()


def test_same_state_transition_is_idempotent_noop():
    """이미 목표 상태면 예외 없이 False를 반환하는 멱등 no-op이다."""
    model = DownloadTaskModel()
    model.start()
    assert model.start() is False
    assert model.resume() is False  # 이미 RUNNING
    model.pause()
    assert model.pause() is False
    # WAITING에서 stop도 no-op — 엔진의 중복 stop 호출을 허용한다
    model.stop()
    assert model.stop() is False


def test_pause_resume_event_behavior():
    """pause는 Event를 clear해 스레드를 재우고, resume은 set해 깨운다."""
    model = DownloadTaskModel()
    model.start()
    assert model.pause_event.is_set()

    model.pause()
    assert not model.pause_event.is_set()

    model.resume()
    assert model.pause_event.is_set()


def test_injected_pause_event_is_shared():
    """외부에서 주입한 Event를 그대로 공유한다 (기존 엔진과의 연결용)."""
    shared = threading.Event()
    model = DownloadTaskModel(pause_event=shared)
    assert shared.is_set()  # 생성 시 진행 가능 상태로 초기화
    model.start()
    model.pause()
    assert not shared.is_set()


def test_on_state_change_fires_only_on_real_transition():
    """콜백은 상태가 실제로 바뀔 때만, 새 상태를 인자로 호출된다."""
    changes: list[DownloadState] = []
    model = DownloadTaskModel(on_state_change=changes.append)

    model.start()
    model.pause()
    model.pause()  # 멱등 no-op — 콜백 없음
    model.resume()
    model.finish()

    assert changes == [
        DownloadState.RUNNING,
        DownloadState.PAUSED,
        DownloadState.RUNNING,
        DownloadState.FINISHED,
    ]


def test_progress_aggregation():
    """스레드별 진행 바이트의 합과 백분율 진행률을 집계한다."""
    model = DownloadTaskModel()
    model.total_size = 1000
    model.init_threads_progress(4)
    assert model.downloaded_size == 0
    assert model.progress == 0.0

    model.set_thread_progress(0, 100)
    model.set_thread_progress(1, 200)
    model.set_thread_progress(3, 200)
    assert model.downloaded_size == 500
    assert model.progress == 50.0


def test_progress_is_zero_without_total_size():
    """total_size가 확정되지 않았으면(0) 진행률은 0.0이다."""
    model = DownloadTaskModel()
    model.init_threads_progress(2)
    model.set_thread_progress(0, 100)
    assert model.progress == 0.0


def test_progress_is_capped_at_100():
    """재시도 등으로 total_size보다 많이 받아도 진행률은 100.0으로 캡한다."""
    model = DownloadTaskModel()
    model.total_size = 100
    model.init_threads_progress(1)
    model.set_thread_progress(0, 150)
    assert model.progress == 100.0


def test_state_enum_values_preserved():
    """이주 후에도 상태 값·의미가 기존 download/state.py와 동일해야 한다."""
    assert DownloadState.WAITING.value == 0
    assert DownloadState.RUNNING.value == 1
    assert DownloadState.PAUSED.value == 2
    assert DownloadState.FINISHED.value == 3
    assert DownloadState.FAILED.value == 4

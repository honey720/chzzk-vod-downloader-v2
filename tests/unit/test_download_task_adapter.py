"""download/task.py 어댑터·download/state.py re-export 검증 (#60).

상태 머신을 core/models로 이주한 뒤에도 기존 호출부 인터페이스
(state 속성, lock, start/pause/resume/stop/finish/isRunning)가 유지되고,
ContentItem 상태 반영과 pause_event 공유가 동작하는지 확인한다.
"""

import threading

from core.models.download_state import DownloadState as CoreDownloadState
from download.data import DownloadData
from download.state import DownloadState
from download.task import DownloadTask


class _FakeLogger:
    """DownloadLogger 대역 — 파일 핸들러 생성 없이 호출 기록만 남긴다."""

    def __init__(self):
        self.warnings: list[str] = []
        self.logged_items: list[object] = []

    def warning(self, message: str):
        self.warnings.append(message)

    def log_download_info(self, item):
        self.logged_items.append(item)


class _FakeItem:
    """ContentItem 대역 — setDownloadState 수신 상태만 기록한다."""

    def __init__(self):
        self.downloadState = DownloadState.WAITING

    def setDownloadState(self, state: DownloadState):
        self.downloadState = state


def _make_task() -> tuple[DownloadTask, DownloadData, _FakeItem, _FakeLogger]:
    data = DownloadData("base", "vod", "out.mp4", 1080, "video")
    item = _FakeItem()
    logger = _FakeLogger()
    return DownloadTask(data, item, logger), data, item, logger


def test_state_reexport_is_same_class():
    """download.state의 DownloadState는 core 정의와 동일 객체여야 한다."""
    assert DownloadState is CoreDownloadState


def test_adapter_keeps_engine_interface():
    """엔진(worker/monitor)이 쓰는 state·lock 인터페이스가 유지된다."""
    task, _, _, _ = _make_task()
    assert task.state is DownloadState.WAITING
    assert isinstance(task.lock, type(threading.Lock()))
    assert not task.isRunning()


def test_transitions_update_item_state():
    """전이 시 ContentItem에 상태가 반영된다 (기존 setDownloadState 경로 유지)."""
    task, _, item, logger = _make_task()

    task.start()
    assert task.state is DownloadState.RUNNING
    assert item.downloadState is DownloadState.RUNNING
    assert logger.logged_items  # 시작 시 다운로드 정보 로깅 유지

    task.pause()
    assert item.downloadState is DownloadState.PAUSED

    task.resume()
    assert item.downloadState is DownloadState.RUNNING

    task.finish()
    assert item.downloadState is DownloadState.FINISHED


def test_pause_event_is_shared_with_download_data():
    """어댑터는 DownloadData._pause_event를 core 모델과 공유한다 (엔진 무변경)."""
    task, data, _, _ = _make_task()
    task.start()

    task.pause()
    assert not data._pause_event.is_set()

    task.resume()
    assert data._pause_event.is_set()


def test_invalid_transition_is_absorbed_not_raised():
    """허용되지 않는 전이는 예외로 앱을 죽이지 않고 경고 로그로 흡수한다."""
    task, _, item, logger = _make_task()

    task.resume()  # WAITING에서 resume은 허용되지 않는 전이
    assert task.state is DownloadState.WAITING
    assert item.downloadState is DownloadState.WAITING
    assert logger.warnings

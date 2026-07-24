"""core/models/events.py 콜백 계약 테스트 (#72).

ProgressEvent가 불변(frozen)이고 미확정 필드에 None 기본값을 허용하는지 검증한다.
"""

import dataclasses

import pytest

from core.models.events import ProgressEvent


def test_progress_event_is_frozen():
    """진행 이벤트는 스레드 간에 전달되므로 생성 후 변경할 수 없어야 한다."""
    event = ProgressEvent(downloaded_size=1024)

    with pytest.raises(dataclasses.FrozenInstanceError):
        event.downloaded_size = 2048


def test_progress_event_optional_fields_default_to_none():
    """total_size·speed·active_threads는 아직 알 수 없는 시점이 있어 None을 허용한다."""
    event = ProgressEvent(downloaded_size=0)

    assert event.total_size is None
    assert event.speed is None
    assert event.active_threads is None


def test_progress_event_holds_given_values():
    """모든 필드가 값 그대로 보존돼야 한다."""
    event = ProgressEvent(downloaded_size=10, total_size=100, speed=2.5, active_threads=4)

    assert (event.downloaded_size, event.total_size, event.speed, event.active_threads) == (
        10,
        100,
        2.5,
        4,
    )

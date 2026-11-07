"""초 → 「시:분:초」 글이 24시간을 넘어도 날짜를 버리지 않는지 (#511)."""

from types import SimpleNamespace

import pytest

from app.viewmodels.clock_text import clock_text
from app.viewmodels.download_viewmodel import _file_progress_args, _segment_progress_args
from app.widgets.widget import short_time
from core.models.download_data import DownloadData
from core.models.events import ProgressEvent

MIB = 1024 * 1024


@pytest.mark.parametrize(
    ("seconds", "text", "short"),
    [
        (0, "00:00:00", "0:00"),
        (59, "00:00:59", "0:59"),
        (3599, "00:59:59", "59:59"),
        (3600, "01:00:00", "1:00:00"),
        (86399, "23:59:59", "23:59:59"),
        (86400, "24:00:00", "24:00:00"),  # 하루 — 날짜를 버리면 "00:00:00"
        (108000, "30:00:00", "30:00:00"),  # 30시간 — 날짜를 버리면 "06:00:00"
        (360000, "100:00:00", "100:00:00"),  # 시가 세 자리
        (59.9, "00:00:59", "0:59"),  # 초 아래는 버린다
    ],
)
def test_clock_text_counts_hours_past_a_day(seconds, text, short):
    """초를 "HH:MM:SS"로 적을 때 시는 24로 나누지 않아야 하고, 짧은 표기도 그 시를 그대로 보여야 한다."""
    assert clock_text(seconds) == text
    assert short_time(clock_text(seconds)) == short


def test_file_remaining_time_over_a_day_keeps_the_hours():
    """파일 다운로드의 남은 시간이 30시간이면 카드에 갈 글이 "30:00:00"이어야 한다.

    받을 크기 108000 MiB, 받은 0, 속도 1 MB/s -> 남은 108000초 = "30:00:00"
    """
    event = ProgressEvent(downloaded_size=0, total_size=108000 * MIB, speed=1.0)
    remaining, _size, _speed, _progress = _file_progress_args(event)
    assert remaining == "30:00:00"


def test_segment_remaining_time_over_a_day_keeps_the_hours():
    """세그먼트 다운로드의 남은 시간이 30시간이면 카드에 갈 글이 "30:00:00"이어야 한다.

    세그먼트 108001개 중 1개 받음(1 MiB), 속도 1 MB/s -> 남은 108000개 × 1 MiB ÷ 1 MiB/s = "30:00:00"
    """
    data = DownloadData(
        base_url="https://example.invalid/media.m3u8",
        vod_url="https://chzzk.naver.com/video/1",
        output_path="unused.mp4",
        resolution=144,
        content_type="m3u8",
    )
    data.max_threads, data.completed_threads, data.reused_threads = 108001, 1, 0
    event = ProgressEvent(downloaded_size=MIB, total_size=None, speed=1.0)
    item = SimpleNamespace(post_process=False, content_type="m3u8")
    remaining, _size, _speed, _progress = _segment_progress_args(event, data, item)
    assert remaining == "30:00:00"


@pytest.mark.parametrize("seconds", [-1, -3600, -0.5])
def test_negative_seconds_are_cut_to_zero(seconds):
    """음수 초는 0으로 잘라 "00:00:00"(짧은 표기 "0:00")이어야 한다 — 어느 OS에서도 예외 없이 (#513 리뷰)."""
    assert clock_text(seconds) == "00:00:00"
    assert short_time(clock_text(seconds)) == "0:00"


def test_file_remaining_time_with_more_received_than_expected_is_zero():
    """받은 크기가 받을 크기보다 크면 파일의 남은 시간은 "00:00:00"이어야 한다.

    받을 크기 1 MiB, 받은 2 MiB, 속도 1 MB/s -> 남은 −1초를 0으로
    """
    event = ProgressEvent(downloaded_size=2 * MIB, total_size=MIB, speed=1.0)
    remaining, _size, _speed, _progress = _file_progress_args(event)
    assert remaining == "00:00:00"


def test_file_remaining_time_without_a_known_size_is_zero():
    """받을 크기를 모르는데 속도가 있으면 파일의 남은 시간은 "00:00:00"이어야 한다.

    받을 크기 None(0으로 셈), 받은 1 MiB, 속도 1 MB/s -> 남은 −1초를 0으로
    """
    event = ProgressEvent(downloaded_size=MIB, total_size=None, speed=1.0)
    remaining, _size, _speed, _progress = _file_progress_args(event)
    assert remaining == "00:00:00"


def test_segment_remaining_time_with_more_finished_than_planned_is_zero():
    """세그먼트를 계획보다 많이 끝냈다고 세면 남은 시간은 "00:00:00"이어야 한다.

    전체 2개, 끝낸 3개(다시 쓴 것 0), 받은 3 MiB, 속도 1 MB/s -> 남은 −1개 × 1 MiB를 0으로
    """
    data = DownloadData(
        base_url="https://example.invalid/media.m3u8",
        vod_url="https://chzzk.naver.com/video/1",
        output_path="unused.mp4",
        resolution=144,
        content_type="m3u8",
    )
    data.max_threads, data.completed_threads, data.reused_threads = 2, 3, 0
    event = ProgressEvent(downloaded_size=3 * MIB, total_size=None, speed=1.0)
    item = SimpleNamespace(post_process=False, content_type="m3u8")
    remaining, _size, _speed, _progress = _segment_progress_args(event, data, item)
    assert remaining == "00:00:00"


def test_unknown_time_is_empty_text() -> None:
    """모르는 시간(None)은 빈 글이어야 한다 — 지금 시각을 내지 않는다 (#515)."""
    assert clock_text(None) == ""

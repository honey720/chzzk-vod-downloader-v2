"""세그먼트 방식 카드의 남은 시간 — 받아 둔 세그먼트가 있어도 받은 것만으로 평균을 낸다 (#376)."""

from types import SimpleNamespace

from app.viewmodels.download_viewmodel import _segment_progress_args
from core.models.download_data import DownloadData
from core.models.events import ProgressEvent

MB = 1024 * 1024


def _data(total: int, completed: int, reused: int) -> DownloadData:
    data = DownloadData(
        base_url="https://example.invalid/media.m3u8",
        vod_url="https://chzzk.naver.com/video/1",
        output_path="unused.mp4",
        resolution=144,
        content_type="m3u8",
    )
    data.max_threads = total
    data.completed_threads = completed
    data.reused_threads = reused
    return data


def _args(data: DownloadData, received: int, speed: float) -> tuple[str, str, str, int]:
    event = ProgressEvent(downloaded_size=received, total_size=None, speed=speed)
    item = SimpleNamespace(post_process=False, content_type="m3u8")
    return _segment_progress_args(event, data, item)


def test_remaining_time_averages_only_the_segments_received_when_some_were_stored():
    """받아 둔 세그먼트가 있으면 남은 시간은 받은 세그먼트만의 평균 크기로 내야 한다.

    전체 10개, 끝낸 6개 중 받아 둔 것 4개, 받은 2 MiB, 1 MB/s
    -> 남은 4개 × 1 MiB ÷ 1 MiB/s = "00:00:04", 진행률 60
    """
    remaining, size, _speed, progress = _args(_data(10, 6, 4), 2 * MB, 1.0)

    assert (remaining, size, progress) == ("00:00:04", str(2 * MB), 60)


def test_remaining_time_without_stored_segments_averages_every_finished_segment():
    """받아 둔 세그먼트가 없으면 남은 시간은 끝낸 세그먼트 전부의 평균 크기로 내야 한다.

    전체 10개, 끝낸 2개(받아 둔 것 0), 받은 2 MiB, 1 MB/s
    -> 남은 8개 × 1 MiB ÷ 1 MiB/s = "00:00:08", 진행률 20
    """
    remaining, _size, _speed, progress = _args(_data(10, 2, 0), 2 * MB, 1.0)

    assert (remaining, progress) == ("00:00:08", 20)


def test_remaining_time_is_unknown_while_only_stored_segments_are_finished():
    """끝낸 세그먼트가 모두 받아 둔 것이면 남은 시간은 알 수 없음이어야 한다.

    전체 10개, 끝낸 4개 모두 받아 둔 것, 받은 0, 1 MB/s -> "N/A", 진행률 40
    """
    remaining, _size, _speed, progress = _args(_data(10, 4, 4), 0, 1.0)

    assert (remaining, progress) == ("N/A", 40)


def test_progress_reaches_100_when_every_segment_was_stored():
    """세그먼트 전부를 받아 둔 것으로 끝냈으면 진행률은 100이어야 한다.

    전체 10개, 끝낸 10개 모두 받아 둔 것, 받은 0 -> 진행률 100
    """
    _remaining, _size, _speed, progress = _args(_data(10, 10, 10), 0, 0.0)

    assert progress == 100

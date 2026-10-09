"""틱 속도 측정 (#347) — 받은 양을 실제 경과 시간으로 나눈다.

시계는 엔진의 단조 시계(`_now`)를 가짜로 바꿔 끼운다. 실제 시간은 쓰지 않는다.
"""

import pytest

from core.downloaders.file_downloader import FileDownloader
from core.models.download_data import DownloadData

MB = 1024 * 1024


class QuietLogger:
    """엔진이 부르는 로그 메서드를 모두 받아 버린다."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class ManualClock:
    """엔진의 단조 시계 대체 — 시험이 직접 옮긴다."""

    def __init__(self):
        self.now = 500.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def measured():
    """가짜 시계를 끼우고 방금 한 번 잰 것으로 해 둔 엔진 — (엔진, 데이터, 시계)."""
    data = DownloadData(
        base_url="https://example.invalid/video.mp4",
        vod_url="https://chzzk.naver.com/video/1",
        output_path="unused.part",
        resolution=1080,
        content_type="video",
    )
    scaler = FileDownloader(data, QuietLogger())
    clock = ManualClock()
    scaler._now = clock
    scaler._measured_at = clock.now
    return scaler, data, clock


def test_measure_speed_divides_by_the_time_since_the_last_measurement(measured):
    """속도는 직전 측정 뒤로 받은 양을 그 사이의 실제 시간으로 나눈 값이어야 한다.

    직전 측정 뒤 1.25초 동안 5 MB를 받음 -> 4.0 MB/s, prev_size == 5 MB
    """
    scaler, data, clock = measured
    clock.now += 1.25
    data.total_downloaded_size = 5 * MB

    scaler.measure_speed()

    assert data.speed_mb == pytest.approx(4.0)
    assert data.prev_size == 5 * MB


def test_measure_speed_after_a_pause_is_not_divided_and_restarts_the_clock(measured):
    """재개 직후의 측정은 시간으로 나누지 않고, 다음 측정의 기준 시각을 지금으로 잡아야 한다.

    직전 측정 뒤 300초(일시정지 포함) 동안 5 MB → since_pause 측정 -> 5.0 MB/s
    그 뒤 2초 동안 6 MB를 더 받고 보통 측정 -> 3.0 MB/s
    """
    scaler, data, clock = measured
    clock.now += 300.0
    data.total_downloaded_size = 5 * MB

    scaler.measure_speed(since_pause=True)
    after_pause = data.speed_mb
    clock.now += 2.0
    data.total_downloaded_size = 11 * MB
    scaler.measure_speed()

    assert after_pause == pytest.approx(5.0)
    assert data.speed_mb == pytest.approx(3.0)


def test_measure_speed_is_not_divided_when_no_time_has_passed(measured):
    """직전 측정과 같은 시각에 재면 나누지 않아야 한다(0으로 나누지 않는다).

    직전 측정 뒤 0초, 2 MB를 받음 -> 2.0 MB/s
    """
    scaler, data, _clock = measured
    data.total_downloaded_size = 2 * MB

    scaler.measure_speed()

    assert data.speed_mb == pytest.approx(2.0)

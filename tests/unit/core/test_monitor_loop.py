"""관측 루프의 틱 주기와 일시정지 처리 (#347).

시계와 대기를 바꿔 끼워 루프를 그대로 돌린다 — 대기는 실제로 자지 않고 가짜 시계를 옮긴다.
"""

import pytest

from core.downloaders.file_downloader import FileDownloader
from core.models.download_data import DownloadData

WORK = 0.19  # 틱 하나의 일(속도 측정)에 드는 시간(초)으로 흉내 내는 값


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class SleepingStop:
    """정지 신호 대체 — wait는 자는 대신 시계를 그만큼 옮긴다."""

    def __init__(self, clock: Clock):
        self._clock = clock
        self._stopped = False

    def is_set(self) -> bool:
        return self._stopped

    def set(self) -> None:
        self._stopped = True

    def wait(self, timeout: float) -> bool:
        self._clock.now += timeout
        return self._stopped


class PausedOnce:
    """일시정지 Event 대체 — 한 번 '정지 중'으로 보였다가, wait가 seconds만큼 시계를 옮기고 풀린다."""

    def __init__(self, clock: Clock, seconds: float):
        self._clock = clock
        self._seconds = seconds
        self._paused = True

    def is_set(self) -> bool:
        return not self._paused

    def wait(self) -> bool:
        self._clock.now += self._seconds
        self._paused = False
        return True


class QuietLogger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class Loop:
    """관측 루프를 가짜 시계로 돌리고, 조정이 불린 시각을 적는다."""

    def __init__(self, ticks: int, work=lambda index: WORK):
        data = DownloadData(
            base_url="https://example.invalid/video.mp4",
            vod_url="https://chzzk.naver.com/video/1",
            output_path="unused.part",
            resolution=1080,
            content_type="video",
        )
        data.model.start()
        self.data = data
        self.clock = Clock()
        self.engine = FileDownloader(data, QuietLogger())
        self.engine._now = self.clock
        self.engine._monitor_stop = SleepingStop(self.clock)
        self.measured_at: list[float] = []  # 틱마다, 속도를 재기 시작한 시각
        self.pause_measures = 0
        self._ticks = ticks
        self._work = work
        self.engine.measure_speed = self._measure
        self.engine._adjust_threads = self._adjust
        self.engine.emit_progress = lambda: None

    def _measure(self, since_pause: bool = False) -> None:
        if since_pause:
            self.pause_measures += 1
            return
        self.measured_at.append(self.clock.now)
        self.clock.now += self._work(len(self.measured_at) - 1)

    def _adjust(self) -> None:
        if len(self.measured_at) >= self._ticks:
            self.engine._monitor_stop.set()

    def run(self) -> list[float]:
        self.engine._monitor_loop()
        return self.measured_at

    def gaps(self) -> list[float]:
        return [later - earlier for earlier, later in zip(self.measured_at, self.measured_at[1:])]


def test_ticks_are_one_second_apart_whatever_the_work_takes():
    """틱마다의 일에 시간이 들어도 틱 사이의 간격은 1초여야 한다.

    틱의 일 0.19초, 6틱 -> 간격 다섯 개가 모두 1.0초 (일한 시간만큼 늘어지면 1.19초다)
    """
    loop = Loop(ticks=6)

    loop.run()

    assert loop.gaps() == pytest.approx([1.0] * 5)


def test_each_tick_measures_the_speed_before_adjusting_the_threads():
    """틱마다 속도를 먼저 재고, 그 값으로 스레드를 조정한 뒤, 진행을 알려야 한다.

    2틱 -> 부른 순서: 측정, 조정, 통지, 측정, 조정, 통지
    """
    loop = Loop(ticks=2)
    order: list[str] = []
    measure, adjust = loop.engine.measure_speed, loop.engine._adjust_threads
    loop.engine.measure_speed = lambda **kwargs: (order.append("측정"), measure(**kwargs))[1]
    loop.engine._adjust_threads = lambda: (order.append("조정"), adjust())[1]
    loop.engine.emit_progress = lambda: order.append("통지")

    loop.run()

    assert order == ["측정", "조정", "통지"] * 2


def test_a_tick_that_overruns_is_followed_by_a_full_period_not_a_burst():
    """틱 하나가 주기보다 오래 걸리면, 밀린 틱을 몰아서 돌지 않고 그때부터 1초 뒤에 다음 틱을 돌아야 한다.

    둘째 틱의 일만 3.2초, 나머지는 0.19초, 5틱
    -> 간격: 1.0, 4.2(일 3.2초 + 한 주기), 1.0, 1.0
    """
    loop = Loop(ticks=5, work=lambda index: 3.2 if index == 1 else WORK)

    loop.run()

    assert loop.gaps() == pytest.approx([1.0, 4.2, 1.0, 1.0])


def test_pause_is_measured_once_and_the_next_tick_comes_a_period_after_the_resume():
    """일시정지에서 풀리면 재개 측정을 한 번 하고, 그때부터 1초 뒤에 다음 틱을 돌아야 한다.

    첫 대기 뒤 300초 일시정지 → 재개, 그 뒤 3틱
    -> 재개 측정 1번, 첫 틱은 재개 1초 뒤, 그다음 간격은 1.0초씩
    """
    loop = Loop(ticks=3)
    loop.data.model.pause_event = PausedOnce(loop.clock, 300.0)
    started = loop.clock.now

    loop.run()

    assert loop.pause_measures == 1
    assert loop.measured_at[0] == pytest.approx(started + 1.0 + 300.0 + 1.0)
    assert loop.gaps() == pytest.approx([1.0, 1.0])


def test_paused_seconds_are_taken_out_of_the_thread_adjust_timers():
    """일시정지한 시간은 스레드 조정의 시각 기준에서 빠져야 한다 — 재개 직후 재탐침이 몰리지 않는다.

    조정기를 만든 뒤 300초 일시정지 → 재개
    -> 조정기에 shift(300초)와 skip(재개 시각, 1초)이 한 번씩 불린다
    """
    loop = Loop(ticks=1)
    calls: list[tuple] = []

    class Recorder:
        target = 4

        def shift(self, seconds: float) -> None:
            calls.append(("shift", seconds))

        def skip(self, now: float, seconds: float) -> None:
            calls.append(("skip", now, seconds))

    loop.engine._threads = Recorder()
    loop.data.model.pause_event = PausedOnce(loop.clock, 300.0)
    started = loop.clock.now

    loop.run()

    assert calls == [
        ("shift", pytest.approx(300.0)),
        ("skip", pytest.approx(started + 1.0 + 300.0), pytest.approx(1.0)),
    ]

"""관측 tick 분리 — sampler · 1초 evaluation window · 진행 통지 (#557).

같음 증명: 로컬 대역 서버로 받으며 남긴 sample 열(tests/fixtures/thread_replay — 치지직 데이터가 아님)을
엔진의 조정 경로(`_adjust_threads` → window → 조정기)에 다시 넣는 replay로, 기록한 목표 열과 한 틱도
다르지 않은지 본다. sample 간격을 0.5초로 쪼갠 열도 window를 거치면 같은 결정이고, window가 0.5초면
(묶지 않음) 다르다. 시계는 테스트가 정한다 — 실제 시간 · 네트워크를 쓰지 않는다.
"""

import csv
from collections.abc import Callable
from pathlib import Path

import pytest

import core.engine.base as base
from core.engine.thread_control import EVAL_SECONDS, EvalBinner, Sample, ThreadController
from core.models.download_data import DownloadData
from core.models.events import ProgressEvent
from core.sources.file_downloader import FileDownloader
from tests.core.engine.test_monitor_loop import Loop, PausedOnce

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "thread_replay"
SCENES = ("s2-calibrated", "s3-slowmix", "s4-drop", "s5-rise", "s6-throttle")
START_TARGET = 4  # 픽스처 머리의 시작 목표
SPLIT = 0.3  # 0.5초 두 sample로 쪼갤 때 앞 · 뒤에 얹는 흔들림(± 30%) — 둘의 평균은 원래 속도
MB = 1024 * 1024


class QuietLogger:
    def __getattr__(self, name) -> Callable[..., None]:
        return lambda *args, **kwargs: None


class SpeedLines(QuietLogger):
    """속도 줄(log_thread_debug)만 적는다."""

    def __init__(self) -> None:
        self.lines: list[tuple] = []

    def log_thread_debug(self, active: int, speed: float, average: float) -> None:
        self.lines.append((active, speed, average))


def _rows(scene: str) -> list[tuple[float, float, int]]:
    with open(FIXTURES / f"{scene}.csv", encoding="utf-8") as handle:
        lines = [line for line in handle if not line.startswith("#")]
    return [
        (float(r["now"]), float(r["speed_mb"]), int(r["target"])) for r in csv.DictReader(lines)
    ]


def _engine(logger=None) -> tuple[FileDownloader, DownloadData, list[float]]:
    data = DownloadData(
        base_url="https://example.invalid/video.mp4",
        vod_url="https://chzzk.naver.com/video/1",
        output_path="unused.part",
        resolution=1080,
        content_type="video",
    )
    data.max_threads = 6996  # 상한이 TARGET_CAP(48)이 되게
    data.adjust_threads = START_TARGET
    engine = FileDownloader(data, logger or QuietLogger())
    clock = [0.0]
    engine._now = lambda: clock[0]
    return engine, data, clock


def _feed(
    samples: list[tuple[float, float, float]], marks: set[float], window: float | None = None
) -> list[int]:
    """sample (시각, 속도, 잰 시간)을 엔진의 조정 경로에 넣고, `marks` 시각마다의 목표를 모은다.

    Args:
        window: 주면 그 길이(초)의 window로 묶는다 — 엔진 기본(EVAL_SECONDS) 대신
    """
    engine, data, clock = _engine()
    if window is not None:
        engine._threads = ThreadController(START_TARGET, 48)
        engine._binner = EvalBinner(window)
    targets = []
    for now, speed, seconds in samples:
        clock[0] = now
        data.speed_mb = speed
        engine._sample_seconds = seconds
        engine._adjust_threads()
        if now in marks:
            targets.append(data.adjust_threads)
    return targets


def _whole(rows) -> list[tuple[float, float, float]]:
    previous = rows[0][0] - 1.0
    out = []
    for now, speed, _target in rows:
        out.append((now, speed, now - previous))
        previous = now
    return out


def _halves(rows) -> list[tuple[float, float, float]]:
    """1초 sample 하나를 0.5초 sample 둘(속도 × 1.3 · × 0.7, 잰 시간 반씩)로 쪼갠다 — 묶으면 원래 속도.

    앞 · 뒤 어느 쪽이 빠른지는 sample마다 번갈아 — window 속도를 마지막 sample로 잘못 쓰면 흔들림이 된다.
    """
    previous = rows[0][0] - 1.0
    out = []
    for index, (now, speed, _target) in enumerate(rows):
        half = (now - previous) / 2
        sign = 1 if index % 2 == 0 else -1
        out.append((previous + half, speed * (1 + sign * SPLIT), half))
        out.append((now, speed * (1 - sign * SPLIT), half))
        previous = now
    return out


# ================================================================ 같음 증명(replay)


@pytest.mark.parametrize("scene", SCENES)
def test_recorded_samples_replay_to_the_recorded_targets(scene) -> None:
    """기록한 sample 열을 엔진의 조정 경로에 넣으면 목표 열이 기록과 한 틱도 달라서는 안 된다.

    픽스처(로컬 대역 서버 150초) sample 149개, 간격 1초 -> sample마다의 목표 == 기록한 목표
    """
    rows = _rows(scene)
    assert len({target for *_, target in rows}) > 1, "전제: 목표가 바뀌는 장면"

    targets = _feed(_whole(rows), {now for now, *_ in rows})

    assert targets == [target for *_, target in rows]


@pytest.mark.parametrize("scene", SCENES)
def test_half_second_samples_decide_the_same_through_the_eval_window(scene) -> None:
    """sample 간격 0.5초로 쪼갠 열도 1초 window를 거치면 1초 sample과 같은 목표 열이어야 한다.

    1초 sample을 속도 × 1.3 · × 0.7 반씩으로 쪼갬 -> 1초 시각마다의 목표 == 기록한 목표
    """
    rows = _rows(scene)

    targets = _feed(_halves(rows), {now for now, *_ in rows})

    assert targets == [target for *_, target in rows]


@pytest.mark.parametrize("scene", SCENES)
def test_half_second_samples_without_the_window_decide_differently(scene) -> None:
    """같은 0.5초 열을 묶지 않고(window 0.5초 — window 하나 = sample 하나) 넣으면 목표 열이 달라야 한다.

    window 0.5초로 0.5초 sample을 넣음 -> 1초 시각마다의 목표 != 기록한 목표
    """
    rows = _rows(scene)

    targets = _feed(_halves(rows), {now for now, *_ in rows}, window=0.5)

    assert targets != [target for *_, target in rows]


def test_one_second_ticks_close_a_window_each_even_with_quarter_samples() -> None:
    """sample 간격이 0.25초여도 1초 틱으로 조정을 부르면(재기 전) 부를 때마다 window가 차야 한다.

    SAMPLE_SECONDS 0.25, `measure_speed` 없이 시계를 1초씩 옮기며 `_adjust_threads` 3번
    -> 조정기 step 3번(박제 조정 테스트 · 시뮬레이션이 1초 틱으로 부른다)
    """
    assert base.SAMPLE_SECONDS == 0.25, "전제: 이 판의 sample 간격"
    engine, data, clock = _engine()
    steps: list[float] = []

    class Counting:
        target = START_TARGET

        def step(self, now: float, speed: float) -> None:
            steps.append(now)

    engine._threads = Counting()
    engine._binner = EvalBinner()
    for second in (1.0, 2.0, 3.0):
        clock[0] = second
        data.speed_mb = 5.0
        engine._adjust_threads()

    assert steps == [1.0, 2.0, 3.0]


# ================================================================ 1초 window


def test_window_calls_back_only_when_a_second_is_full() -> None:
    """sample 간격 0.25초면 넷째 sample에서만 판정 sample이 나오고 그 값은 받은 양 ÷ 시간이어야 한다.

    0.25초 sample 속도 4 · 8 · 4 · 8 MB/s(시각 1.25 ~ 2.0) -> 셋째까지 None, 넷째에 (2.0, 6.0, 1.0)
    """
    window = EvalBinner()
    out = [
        window.add(Sample(1.0 + 0.25 * (i + 1), speed, 0.25))
        for i, speed in enumerate((4, 8, 4, 8))
    ]

    assert out[:3] == [None, None, None]
    assert out[3] == Sample(2.0, pytest.approx(6.0), pytest.approx(1.0))


def test_window_fills_when_quarter_samples_run_short_by_a_clock_tick() -> None:
    """sample 넷의 시간 합이 시계 단위만큼 모자라도(0.24초씩 = 0.96초) 넷째에 window가 차야 한다.

    0.24초 sample 넷 -> 셋째까지 None(0.72초), 넷째에 판정 sample
    """
    window = EvalBinner()
    out = [window.add(Sample(0.24 * (i + 1), 5.0, 0.24)) for i in range(4)]

    assert out[:3] == [None, None, None] and out[3] is not None


def test_window_weights_the_speed_by_the_time_each_sample_took() -> None:
    """sample마다 잰 시간이 다르면 window의 처리량은 속도의 시간 가중 평균이어야 한다.

    sample 둘: 10 MB/s · 0.75초, 2 MB/s · 0.25초 -> (10 × 0.75 + 2 × 0.25) ÷ 1.0 = 8.0
    """
    window = EvalBinner()
    window.add(Sample(1.0, 10.0, 0.75))

    assert window.add(Sample(1.25, 2.0, 0.25)).speed == pytest.approx(8.0)


def test_window_passes_a_one_second_sample_through_unchanged() -> None:
    """1초 sample 하나면 window는 받은 sample을 그대로 돌려줘야 한다(반올림 없이 나누기 전과 같은 값).

    (5.0, 0.1 + 0.2, 1.003) -> 같은 객체
    """
    sample = Sample(5.0, 0.1 + 0.2, 1.003)

    assert EvalBinner().add(sample) is sample


def test_window_clear_drops_the_half_filled_window() -> None:
    """채우던 window를 비우면 그 앞의 sample은 다음 window에 섞이지 않아야 한다.

    0.5초 sample 하나 → clear → sample 하나 -> None, 하나 더 -> 뒤 두 sample만의 평균(4.0)
    """
    window = EvalBinner()
    window.add(Sample(1.0, 100.0, 0.5))
    window.clear()

    assert window.add(Sample(1.5, 2.0, 0.5)) is None
    assert window.add(Sample(2.0, 6.0, 0.5)).speed == pytest.approx(4.0)


# ================================================================ 통지 속도 · 속도 줄


def _measure_quarters(engine, data, clock, per_quarter_mb: list[float]) -> None:
    """0.25초마다 받은 양을 늘리며 `measure_speed`를 부른다(관측 루프와 같은 차례)."""
    engine._measured_at = clock[0]
    for amount in per_quarter_mb:
        clock[0] += 0.25
        data.total_downloaded_size += round(amount * MB)
        engine.measure_speed()


def test_progress_speed_is_the_last_second_average_not_the_quarter_sample() -> None:
    """진행 통지의 속도는 최근 1초 평균이어야 한다(0.25초 순간값이 아님).

    0.25초 sample 속도 4 · 8 · 4 · 8 MB/s(받은 양 1 · 2 · 1 · 2MB) -> 마지막 sample 8, 통지 속도 6.0
    """
    engine, data, clock = _engine()
    events: list[ProgressEvent] = []
    engine._on_progress = events.append
    _measure_quarters(engine, data, clock, [1, 2, 1, 2])

    engine.emit_progress()

    assert data.speed_mb == pytest.approx(8.0)
    assert events[-1].speed == pytest.approx(6.0)


def test_progress_speed_keeps_only_the_last_second() -> None:
    """1초보다 앞의 sample은 통지 속도에서 빠져야 한다.

    0.25초 sample 여덟: 앞 넷 40 MB/s(10MB씩) · 뒤 넷 4 MB/s(1MB씩) -> 통지 속도 4.0
    """
    engine, data, clock = _engine()
    events: list[ProgressEvent] = []
    engine._on_progress = events.append
    _measure_quarters(engine, data, clock, [10, 10, 10, 10, 1, 1, 1, 1])

    engine.emit_progress()

    assert events[-1].speed == pytest.approx(4.0)


def test_speed_line_is_logged_once_a_second_not_every_sample() -> None:
    """속도 줄(log_thread_debug)은 sample마다가 아니라 1초에 한 번 남아야 한다.

    0.25초 sample 여덟(2초) -> 속도 줄 2줄(첫 sample 0.25초 · 1초 지난 1.25초)
    """
    logger = SpeedLines()
    engine, data, clock = _engine(logger)
    _measure_quarters(engine, data, clock, [1] * 8)

    assert len(logger.lines) == 2


# ================================================================ 관측 루프


def test_pause_skips_one_eval_window_whatever_the_sample_interval(monkeypatch) -> None:
    """sample 간격이 0.5초여도 재개 뒤에는 판정 sample 하나(1초)를 버리고 채우던 window를 비워야 한다.

    SAMPLE_SECONDS = 0.5, window에 sample 하나가 든 채 300초 일시정지 → 재개
    -> 조정기에 skip(재개 시각, EVAL_SECONDS = 1.0) · window가 비어 다음 0.5초 sample 하나로는 차지 않음
    """
    monkeypatch.setattr(base, "SAMPLE_SECONDS", 0.5)
    loop = Loop(ticks=1)
    calls: list[tuple] = []

    class Recorder:
        target = 4

        def shift(self, seconds: float) -> None:
            calls.append(("shift", seconds))

        def skip(self, now: float, seconds: float) -> None:
            calls.append(("skip", now, seconds))

    loop.engine._threads = Recorder()
    loop.engine._binner = EvalBinner()
    loop.engine._binner.add(Sample(100.0, 9.0, 0.5))
    loop.data.model.pause_event = PausedOnce(loop.clock, 300.0)
    started = loop.clock.now

    loop.run()

    assert calls == [
        ("shift", pytest.approx(300.0)),
        ("skip", pytest.approx(started + 0.5 + 300.0), EVAL_SECONDS),
    ]
    assert loop.engine._binner.add(Sample(401.0, 1.0, 0.5)) is None


def _count_loop(monkeypatch, sample: float, progress: float, samples: int) -> tuple[int, int]:
    """관측 루프를 sample `samples`개 돌려 (진행 통지 수, 조정기 부름 수)를 센다 — 조정은 실제 경로."""
    monkeypatch.setattr(base, "SAMPLE_SECONDS", sample)
    monkeypatch.setattr(base, "PROGRESS_SECONDS", progress)
    loop = Loop(ticks=samples)
    engine = loop.engine
    measured = [0]

    def measure(since_pause: bool = False) -> None:
        measured[0] += 1
        engine.s.speed_mb = 5.0
        engine._sample_seconds = sample
        if measured[0] >= samples:
            engine._monitor_stop.set()

    steps = [0]

    class Counting:
        target = 4

        def step(self, now: float, speed: float) -> None:
            steps[0] += 1

    emits = [0]
    engine.measure_speed = measure
    engine._adjust_threads = FileDownloader._adjust_threads.__get__(engine)
    engine._threads = Counting()
    engine._binner = EvalBinner()
    engine.emit_progress = lambda: emits.__setitem__(0, emits[0] + 1)

    loop.run()

    return emits[0], steps[0]


def test_progress_is_sent_every_progress_interval_not_every_eval_window(monkeypatch) -> None:
    """sample 간격 0.5초 · 통지 간격 0.5초면 sample마다 통지하고, 조정기는 1초 window마다만 불려야 한다.

    sample 6개 -> 진행 통지 6번 · 조정기 3번
    """
    assert _count_loop(monkeypatch, 0.5, 0.5, 6) == (6, 3)


def test_progress_interval_can_be_longer_than_the_sample_interval(monkeypatch) -> None:
    """통지 간격이 sample 간격의 두 배면 sample 둘에 한 번 통지해야 한다.

    sample 간격 0.5초 · 통지 간격 1.0초, sample 6개 -> 진행 통지 3번
    """
    assert _count_loop(monkeypatch, 0.5, 1.0, 6)[0] == 3


def test_current_values_notify_every_quarter_and_adjust_every_second(monkeypatch) -> None:
    """지금 값(sample · 통지 0.25초, window 1초)에서는 sample마다 통지하고 넷마다 조정해야 한다.

    SAMPLE_SECONDS = PROGRESS_SECONDS = 0.25, EVAL_SECONDS = 1.0, sample 8개 -> 통지 8번 · 조정기 2번
    """
    assert (base.SAMPLE_SECONDS, base.PROGRESS_SECONDS, EVAL_SECONDS) == (0.25, 0.25, 1.0)

    assert _count_loop(monkeypatch, 0.25, 0.25, 8) == (8, 2)

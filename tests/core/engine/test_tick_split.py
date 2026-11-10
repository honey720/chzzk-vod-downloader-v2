"""관측 틱 분리 — 표본기 · 1초 판정 묶음 · 진행 통지 (#557).

같음 증명: 로컬 대역 서버로 받으며 남긴 표본 열(tests/fixtures/thread_replay — 치지직 데이터가 아님)을
엔진의 조정 경로(`_adjust_threads` → 묶음 → 조정기)에 다시 넣어, 기록한 목표 열과 한 틱도 다르지
않은지 본다. 표본 간격을 0.5초로 쪼갠 열도 묶음을 거치면 같은 결정이고, 묶음이 없으면 다르다.
시계는 테스트가 정한다 — 실제 시간 · 네트워크를 쓰지 않는다.
"""

import csv
from collections.abc import Callable
from pathlib import Path

import pytest

import core.engine.base as base
from core.engine.thread_control import EVAL_SECONDS, EvalBinner, Sample
from core.models.download_data import DownloadData
from core.sources.file_downloader import FileDownloader
from tests.core.engine.test_monitor_loop import Loop, PausedOnce

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "thread_replay"
SCENES = ("s2-calibrated", "s3-slowmix", "s4-drop", "s5-rise", "s6-throttle")
START_TARGET = 4  # 픽스처 머리의 시작 목표
SPLIT = 0.3  # 0.5초 두 표본으로 쪼갤 때 앞 · 뒤에 얹는 흔들림(± 30%) — 둘의 평균은 원래 속도


class QuietLogger:
    def __getattr__(self, name) -> Callable[..., None]:
        return lambda *args, **kwargs: None


def _rows(scene: str) -> list[tuple[float, float, int]]:
    with open(FIXTURES / f"{scene}.csv", encoding="utf-8") as handle:
        lines = [line for line in handle if not line.startswith("#")]
    return [
        (float(r["now"]), float(r["speed_mb"]), int(r["target"])) for r in csv.DictReader(lines)
    ]


def _engine() -> tuple[FileDownloader, DownloadData, list[float]]:
    data = DownloadData(
        base_url="https://example.invalid/video.mp4",
        vod_url="https://chzzk.naver.com/video/1",
        output_path="unused.part",
        resolution=1080,
        content_type="video",
    )
    data.max_threads = 6996  # 상한이 TARGET_CAP(48)이 되게
    data.adjust_threads = START_TARGET
    engine = FileDownloader(data, QuietLogger())
    clock = [0.0]
    engine._now = lambda: clock[0]
    return engine, data, clock


def _feed(samples: list[tuple[float, float, float]], marks: set[float]) -> list[int]:
    """표본 (시각, 속도, 잰 시간)을 엔진의 조정 경로에 넣고, `marks` 시각마다의 목표를 모은다."""
    engine, data, clock = _engine()
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
    """1초 표본 하나를 0.5초 표본 둘(속도 × 1.3 · × 0.7, 잰 시간 반씩)로 쪼갠다 — 묶으면 원래 속도."""
    previous = rows[0][0] - 1.0
    out = []
    for now, speed, _target in rows:
        half = (now - previous) / 2
        out.append((previous + half, speed * (1 + SPLIT), half))
        out.append((now, speed * (1 - SPLIT), half))
        previous = now
    return out


# ================================================================ 같음 증명


@pytest.mark.parametrize("scene", SCENES)
def test_recorded_samples_replay_to_the_recorded_targets(scene) -> None:
    """기록한 표본 열을 엔진의 조정 경로에 넣으면 목표 열이 기록과 한 틱도 달라서는 안 된다.

    픽스처(로컬 대역 서버 150초) 표본 149개, 표본 간격 1초 -> 표본마다의 목표 == 기록한 목표
    """
    rows = _rows(scene)
    assert len({target for *_, target in rows}) > 1, "전제: 목표가 바뀌는 장면"

    targets = _feed(_whole(rows), {now for now, *_ in rows})

    assert targets == [target for *_, target in rows]


@pytest.mark.parametrize("scene", SCENES)
def test_half_second_samples_decide_the_same_through_the_eval_bin(scene, monkeypatch) -> None:
    """표본 간격 0.5초로 쪼갠 열도 1초 묶음을 거치면 1초 표본과 같은 목표 열이어야 한다.

    SAMPLE_SECONDS = 0.5, 1초 표본을 속도 × 1.3 · × 0.7 반씩으로 쪼갬 -> 1초 시각마다의 목표 == 기록한 목표
    """
    monkeypatch.setattr(base, "SAMPLE_SECONDS", 0.5)
    rows = _rows(scene)

    targets = _feed(_halves(rows), {now for now, *_ in rows})

    assert targets == [target for *_, target in rows]


@pytest.mark.parametrize("scene", SCENES)
def test_half_second_samples_without_the_bin_decide_differently(scene, monkeypatch) -> None:
    """같은 0.5초 열을 묶지 않고(묶음 하나 = 표본 하나) 조정기에 넣으면 목표 열이 달라야 한다.

    SAMPLE_SECONDS = 1.0(묶음 크기 1)인 채 0.5초 표본을 넣음 -> 1초 시각마다의 목표 != 기록한 목표
    """
    monkeypatch.setattr(base, "SAMPLE_SECONDS", 1.0)
    rows = _rows(scene)

    targets = _feed(_halves(rows), {now for now, *_ in rows})

    assert targets != [target for *_, target in rows]


# ================================================================ 1초 묶음


def test_binner_calls_back_only_when_a_second_is_full() -> None:
    """표본 간격 0.25초면 표본 넷째에서만 판정 표본이 나오고 그 값은 받은 양 ÷ 시간이어야 한다.

    0.25초 표본 속도 4 · 8 · 4 · 8 MB/s(시각 1.25 ~ 2.0) -> 셋째까지 None, 넷째에 (2.0, 6.0, 1.0)
    """
    binner = EvalBinner(0.25)
    out = [
        binner.add(Sample(1.0 + 0.25 * (i + 1), speed, 0.25))
        for i, speed in enumerate((4, 8, 4, 8))
    ]

    assert out[:3] == [None, None, None]
    assert out[3] == Sample(2.0, pytest.approx(6.0), pytest.approx(1.0))


def test_binner_weights_the_speed_by_the_time_each_sample_took() -> None:
    """표본마다 잰 시간이 다르면 묶음의 처리량은 속도의 시간 가중 평균이어야 한다.

    0.5초 표본 둘: 10 MB/s · 0.75초, 2 MB/s · 0.25초 -> (10 × 0.75 + 2 × 0.25) ÷ 1.0 = 8.0
    """
    binner = EvalBinner(0.5)
    binner.add(Sample(1.0, 10.0, 0.75))

    assert binner.add(Sample(1.25, 2.0, 0.25)).speed == pytest.approx(8.0)


def test_binner_passes_a_one_second_sample_through_unchanged() -> None:
    """표본 간격이 1초면 묶음은 받은 표본을 그대로 돌려줘야 한다(반올림 없이 지금 엔진과 같은 값).

    EvalBinner(1.0)에 (5.0, 0.1 + 0.2, 1.003) -> 같은 객체
    """
    sample = Sample(5.0, 0.1 + 0.2, 1.003)

    assert EvalBinner(1.0).add(sample) is sample


def test_binner_clear_drops_the_half_filled_bin() -> None:
    """채우던 묶음을 비우면 그 앞의 표본은 다음 묶음에 섞이지 않아야 한다.

    0.5초 표본 하나 → clear → 표본 하나 -> None, 표본 하나 더 -> 뒤 두 표본만의 평균(4.0)
    """
    binner = EvalBinner(0.5)
    binner.add(Sample(1.0, 100.0, 0.5))
    binner.clear()

    assert binner.add(Sample(1.5, 2.0, 0.5)) is None
    assert binner.add(Sample(2.0, 6.0, 0.5)).speed == pytest.approx(4.0)


# ================================================================ 관측 루프


def test_pause_skips_one_eval_bin_whatever_the_sample_interval(monkeypatch) -> None:
    """표본 간격이 0.5초여도 재개 뒤에는 판정 표본 하나(1초)를 버리고 채우던 묶음을 비워야 한다.

    SAMPLE_SECONDS = 0.5, 묶음에 표본 하나가 든 채 300초 일시정지 → 재개
    -> 조정기에 skip(재개 시각, EVAL_SECONDS = 1.0) · 묶음이 비어 다음 0.5초 표본 하나로는 차지 않음
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
    loop.engine._binner = EvalBinner(0.5)
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
    """관측 루프를 표본 `samples`개 돌려 (진행 통지 수, 조정기 부름 수)를 센다 — 조정은 실제 경로."""
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
    engine._binner = EvalBinner(sample)
    engine.emit_progress = lambda: emits.__setitem__(0, emits[0] + 1)

    loop.run()

    return emits[0], steps[0]


def test_progress_is_sent_every_progress_interval_not_every_eval_bin(monkeypatch) -> None:
    """표본 간격 0.5초 · 통지 간격 0.5초면 표본마다 통지하고, 조정기는 1초 묶음마다만 불려야 한다.

    표본 6개 -> 진행 통지 6번 · 조정기 3번
    """
    assert _count_loop(monkeypatch, 0.5, 0.5, 6) == (6, 3)


def test_progress_interval_can_be_longer_than_the_sample_interval(monkeypatch) -> None:
    """통지 간격이 표본 간격의 두 배면 표본 둘에 한 번 통지해야 한다.

    표본 간격 0.5초 · 통지 간격 1.0초, 표본 6개 -> 진행 통지 3번
    """
    assert _count_loop(monkeypatch, 0.5, 1.0, 6)[0] == 3


def test_current_values_sample_adjust_and_notify_once_per_second(monkeypatch) -> None:
    """지금 값(표본 · 묶음 · 통지 모두 1초)에서는 표본마다 조정 한 번 · 통지 한 번이어야 한다.

    SAMPLE_SECONDS = PROGRESS_SECONDS = EVAL_SECONDS = 1.0, 표본 5개 -> 통지 5번 · 조정기 5번
    """
    assert (base.SAMPLE_SECONDS, base.PROGRESS_SECONDS, EVAL_SECONDS) == (1.0, 1.0, 1.0)

    assert _count_loop(monkeypatch, 1.0, 1.0, 5) == (5, 5)

"""실행 루프가 쉬는 시간 — 작업이 끝나면 바로 깨고, 그 밖에는 길어야 0.1초 쉰다 (#376).

루프가 쉰 시간을 테스트 시계로 잰다. 실제 시간으로 재지 않는다 — 루프가 쉬는 두 방법을 모두 같은
시계에 적는다:

- ``time.sleep(x)``(엔진 모듈의 ``tm``): 시계를 x만큼 돌리고 곧바로 돌아온다
- 슬롯이 빈 신호(``_slot_freed``)의 ``wait(x)``: 신호로 끝나면 시계를 돌리지 않고, 신호 없이 끝나면
  시계를 x만큼 돌린다
"""

import threading
import time

import core.engine.base as base_module
import core.sources.m3u8_downloader as m3u8_module
from tests.core.sources import test_m3u8_downloader_run as m3u8_run

# 신호를 실제로 기다리는 상한(초) — 받아 둔 세그먼트의 완료는 곧바로 온다. 이 안에 오지 않으면
# 신호 없이 끝난 것으로 보고 루프가 청한 시간만큼 시계를 돌린다
_SIGNAL_GRACE = 0.2


class _RestClock:
    """실행 루프가 쉰 시간(초)의 합."""

    def __init__(self) -> None:
        self.rested = 0.0

    def rest(self, seconds: float) -> None:
        self.rested += seconds


class _TimeOnClock:
    """엔진 모듈의 ``time`` 대역 — sleep만 시계에 적고 나머지는 진짜 time을 쓴다."""

    def __init__(self, clock: _RestClock) -> None:
        self._clock = clock

    def sleep(self, seconds: float) -> None:
        self._clock.rest(seconds)
        time.sleep(0.001)  # 실제로 쉬지 않으면 루프가 다른 스레드에 차례를 넘기지 않는다

    def __getattr__(self, name: str):
        return getattr(time, name)


class _EventOnClock:
    """``threading.Event`` 대역 — 신호 없이 끝난 기다림만 시계에 적는다."""

    def __init__(self, clock: _RestClock) -> None:
        self._clock = clock
        self._event = threading.Event()
        self.sets = 0  # 신호를 올린 횟수
        self.woken = 0  # 신호로 끝난 기다림의 수
        self._counting = threading.Lock()

    def set(self) -> None:
        with self._counting:  # 작업자 스레드 여럿이 함께 올린다
            self.sets += 1
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        if self._event.wait(_SIGNAL_GRACE):
            self.woken += 1
            return True
        self._clock.rest(timeout or 0.0)
        return False


class _CountingSession(m3u8_run.M3U8Session):
    """세그먼트 요청 수를 세는 가짜 세션."""

    def __init__(self, chunks_per_segment: int, throttle: float = 0.0) -> None:
        super().__init__(chunks_per_segment, throttle)
        self.segment_requests = 0
        self.quick_first = False  # 첫 세그먼트만 곧바로 받게 할지

    def get(self, url, **kwargs):
        if url.endswith(".m4v"):
            self.segment_requests += 1
            if url.endswith("seg_0.m4v") and self.quick_first:
                return m3u8_run.StreamResponse(m3u8_run._segment_content(0, 1), 0.0)
        return super().get(url, **kwargs)


def _engine_on_clock(tmp_path, monkeypatch, session: _CountingSession, workers: int):
    """루프가 쉰 시간을 시계에 적는, 작업자 수가 고정된 m3u8 엔진을 준비한다."""
    engine, data, logger, _output, finished, failures, _merge = m3u8_run._make_engine(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(m3u8_module, "get_thread_session", lambda: session)
    clock = _RestClock()
    monkeypatch.setattr(base_module, "tm", _TimeOnClock(clock))
    engine._slot_freed = _EventOnClock(clock)
    engine._adjust_threads = lambda: None  # 관측 틱이 작업자 수를 바꾸지 않는다
    engine._slow_speed_threshold_kb_s = 0  # 러너 속도에 따른 저속 재큐를 끈다 (#160)
    data.adjust_threads = workers
    return engine, data, logger, finished, failures, clock


def test_run_that_only_uses_stored_segments_does_not_rest_a_tenth_of_a_second_per_turn(
    tmp_path, monkeypatch
):
    """받아 둔 세그먼트만 쓰는 실행은 작업자 수만큼 끝낼 때마다 0.1초를 쉬지 않아야 한다 (#376).

    세그먼트 200개 전부 받아 둔 것(_prefetched), 작업자 4
    -> 완료, 실패 0건, 다시 쓴 세그먼트 200 · 세그먼트 요청 0건,
       루프가 쉰 시간 < 1초 (반복마다 0.1초를 쉬면 200 ÷ 4 × 0.1 = 5초)
    """
    monkeypatch.setattr(m3u8_run, "SEGMENT_COUNT", 200)
    session = _CountingSession(chunks_per_segment=1)
    engine, data, _logger, finished, failures, clock = _engine_on_clock(
        tmp_path, monkeypatch, session, workers=4
    )
    engine._prefetched = {index: 1 for index in range(200)}

    data.model.start()
    thread = m3u8_run._run_in_thread(engine)
    assert finished.wait(timeout=60), "완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    assert failures == []
    assert (data.reused_threads, session.segment_requests) == (200, 0)
    assert clock.rested < 1.0


def test_stop_while_every_worker_is_receiving_ends_the_loop_after_at_most_one_short_rest(
    tmp_path, monkeypatch
):
    """모든 작업자가 받는 중에 중지하면 실행 루프는 길어야 0.1초를 더 쉬고 끝나야 한다 (#376).

    세그먼트 6개, 세그먼트마다 청크 1000개 × 5ms(받는 데 5초), 작업자 4. 루프가 0.2초 이상
    쉰 뒤 중지
    -> 실행이 끝난다, 중지한 뒤 루프가 끝날 때(관측 정지를 처음 부를 때)까지 쉰 시간 ≤ 0.1초, 완료 콜백 없음
    """
    session = _CountingSession(chunks_per_segment=1000, throttle=0.005)
    engine, data, _logger, finished, _failures, clock = _engine_on_clock(
        tmp_path, monkeypatch, session, workers=4
    )
    loop_ended_at: list[float] = []
    stop_monitor = engine._stop_monitor

    def recorded_stop_monitor(monitor):
        loop_ended_at.append(clock.rested)
        return stop_monitor(monitor)

    engine._stop_monitor = recorded_stop_monitor

    data.model.start()
    thread = m3u8_run._run_in_thread(engine)
    deadline = time.monotonic() + 15
    # 작업자 넷이 모두 받는 중이고, 루프가 그동안 쉬며 돌았다
    while session.segment_requests < 4 or clock.rested < 0.2:
        assert time.monotonic() < deadline, (
            "작업자가 모두 받기 시작하지 않았거나 루프가 쉬지 않았다"
        )
        time.sleep(0.005)
    stopped_at = clock.rested
    data.model.stop()
    thread.join(timeout=30)

    assert not thread.is_alive()
    # 관측 정지는 루프가 끝나면 처음 불린다 — 정리 경로에서 한 번 더 불릴 수 있다
    assert loop_ended_at
    assert loop_ended_at[0] - stopped_at <= 0.1 + 1e-9
    assert not finished.is_set()


def test_loop_rests_again_after_a_finished_job_while_the_other_workers_keep_receiving(
    tmp_path, monkeypatch
):
    """작업 하나가 끝나 깬 뒤 다른 작업자들이 받는 동안 실행 루프는 다시 쉬어야 한다 (#376).

    세그먼트 6개, 첫 세그먼트는 청크 1개(곧바로 끝남) · 나머지는 청크 1000개 × 5ms(받는 데 5초),
    작업자 4. 첫 세그먼트가 끝난 뒤 루프가 0.2초 이상 쉴 때까지(최대 5초) 기다렸다가 중지
    -> 루프가 그만큼 쉰다, 신호로 끝난 기다림의 수 ≤ 신호를 올린 횟수
    """
    session = _CountingSession(chunks_per_segment=1000, throttle=0.005)
    session.quick_first = True
    engine, data, _logger, _finished, _failures, clock = _engine_on_clock(
        tmp_path, monkeypatch, session, workers=4
    )
    event = engine._slot_freed

    data.model.start()
    thread = m3u8_run._run_in_thread(engine)
    deadline = time.monotonic() + 15
    while data.completed_threads < 1:
        assert time.monotonic() < deadline, "첫 세그먼트가 끝나지 않았다"
        time.sleep(0.005)
    rested_after_first = clock.rested
    # 쉬지 않고 도는 루프는 다른 테스트까지 느리게 한다 — 기다림이 실패해도 멈추고 거둔 뒤에 단언한다
    rest_deadline = time.monotonic() + 5
    while clock.rested < rested_after_first + 0.2 and time.monotonic() < rest_deadline:
        time.sleep(0.005)
    rested_after = clock.rested - rested_after_first
    data.model.stop()
    thread.join(timeout=30)

    assert not thread.is_alive()
    assert rested_after >= 0.2, "첫 세그먼트가 끝난 뒤 루프가 쉬지 않았다"
    assert event.woken <= event.sets

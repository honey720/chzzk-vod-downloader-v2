"""저속 판정 규칙 (#347) — 시간 창 · 다른 연결과 견주기 · 재시작 이득 · 일시정지 시간 제외 · 응답 닫기.

시계는 엔진의 단조 시계(`_now`)를 가짜로 바꿔 끼운다. 실제 시간은 쓰지 않는다.
"""

import threading

import pytest

import core.downloaders.file_downloader as file_module
import core.downloaders.hls_aes_downloader as hls_module
import core.downloaders.m3u8_downloader as m3u8_module
from core.downloaders.file_downloader import FileDownloader
from core.downloaders.hls_aes_downloader import HlsAesDownloader
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.models.download_data import DownloadData

KB = 1024
MB = 1024 * KB
CHUNK = 8192


class ManualClock:
    """엔진의 단조 시계 대체 — 시험이 직접 옮긴다."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class QuietLogger:
    """엔진이 부르는 로그 메서드를 모두 받아 버린다."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _make_data(output_path: str = "unused.part", content_type: str = "video") -> DownloadData:
    data = DownloadData(
        base_url="https://example.invalid/video.mp4",
        vod_url="https://chzzk.naver.com/video/1",
        output_path=output_path,
        resolution=1080,
        content_type=content_type,
    )
    data.model.start()
    data.threads_progress = [0] * 8
    data.remaining_ranges = []
    return data


@pytest.fixture
def engine():
    """가짜 시계를 끼운 엔진 — 저속 판정 창만 따로 잰다."""
    made = FileDownloader(_make_data(), QuietLogger())
    made.clock = ManualClock()
    made._now = made.clock
    return made


def _feed(watch, points) -> list[bool]:
    """(경과 초, 그때까지 받은 바이트) 열을 차례로 넣고 판정을 모은다."""
    return [watch.is_slow(elapsed, received) for elapsed, received in points]


def _peers(engine, speeds_kb_s, age: float = 0.0) -> None:
    """다른 슬롯(1번부터)이 age초 전에 알린 속도를 놓는다."""
    for slot, speed in enumerate(speeds_kb_s, start=1):
        engine._conn_speeds[slot] = (engine.clock.now - age, speed)


FAST_PEERS = (800.0, 900.0, 1000.0)  # 다른 연결 셋의 속도(KB/s) — 이 연결만 느린 상황을 만든다
BIG = 100 * MB  # 받을 양 — 남은 양이 많아 끊는 쪽이 늘 이득인 크기


def _always_fast_peers(engine) -> None:
    """어느 시각에 물어도 다른 연결 셋이 빠르다고 답하게 한다 — 받기 루프를 통째로 돌리는 시험용."""
    engine._peer_speeds = lambda part_num, now: list(FAST_PEERS)


# ================================================================ 시간 창


def test_slow_watch_does_not_judge_before_the_window_has_passed(engine):
    """응답 시작 뒤 3초가 지나기 전이면 아무리 느려도 저속이 아니어야 한다.

    1 KB/s로 0.5초마다 받음, 경과 0.5 ~ 2.5초 -> 모두 False
    """
    watch = engine._watch_slow(0)

    verdicts = _feed(watch, [(t / 2, int(t / 2 * KB)) for t in range(1, 6)])

    assert verdicts == [False] * 5


def test_slow_watch_judges_slow_when_the_last_window_is_below_the_threshold(engine):
    """응답 시작 뒤 3초가 지났고 최근 3초의 속도가 100 KB/s 미만이면 저속이어야 한다.

    다른 연결 셋은 빠르고 받을 양은 100 MB. 1 KB/s로 받음, 경과 3.0초에 3 KB -> True
    """
    _peers(engine, FAST_PEERS)
    watch = engine._watch_slow(0, expected=BIG)

    verdicts = _feed(watch, [(1.0, 1 * KB), (2.0, 2 * KB), (3.0, 3 * KB)])

    assert verdicts == [False, False, True]


def test_slow_watch_looks_at_the_recent_window_not_the_average_since_the_start(engine):
    """처음 3초를 빠르게 받았어도 그 뒤 3초가 임계 미만이면 저속이어야 한다.

    0~3초: 1 MB/s(누적 3 MB) / 3~6.5초: 10 KB/s
    -> 경과 4.0초(창에 빠른 구간이 남아 있다) False, 경과 6.5초 True
       (그때의 누적 평균은 약 470 KB/s로 임계보다 높다)
    다른 연결 셋은 빠르고 받을 양은 100 MB다
    """
    _peers(engine, FAST_PEERS)
    watch = engine._watch_slow(0, expected=BIG)
    fast = [(t / 2, int(t / 2 * MB)) for t in range(1, 7)]  # 0.5 ~ 3.0초
    slow = [(3.0 + t / 2, 3 * MB + int(t / 2 * 10 * KB)) for t in range(1, 8)]  # 3.5 ~ 6.5초

    verdicts = _feed(watch, fast + slow)

    assert verdicts[: len(fast)] == [False] * len(fast)
    assert verdicts[len(fast) + 1] is False  # 경과 4.0초
    assert verdicts[-1] is True  # 경과 6.5초


def test_slow_watch_ignores_a_one_second_stall_inside_a_fast_transfer(engine):
    """빠르게 받던 연결이 1초 멎었다 이어 받으면 저속이 아니어야 한다.

    1 MB/s로 3초 받음 → 1초 동안 0바이트 → 다시 1 MB/s로 2초
    -> 모든 시점 False
    """
    watch = engine._watch_slow(0)
    before = [(t / 2, int(t / 2 * MB)) for t in range(1, 7)]  # ~3.0초, 3 MB
    after = [(4.0 + t / 2, 3 * MB + int(t / 2 * MB)) for t in range(0, 5)]  # 4.0초부터

    verdicts = _feed(watch, before + after)

    assert verdicts == [False] * len(verdicts)


def test_connection_above_the_threshold_is_not_slow_however_fast_the_others_are(engine):
    """최근 3초의 속도가 임계(100 KB/s) 이상이면, 다른 연결들이 훨씬 빨라도 저속이 아니어야 한다.

    다른 연결 셋은 9000 KB/s, 받을 양 100 MB, 이 연결은 3초 동안 500 KB/s -> False
    """
    _peers(engine, (9000.0, 9000.0, 9000.0))
    watch = engine._watch_slow(0, expected=BIG)

    assert watch.is_slow(3.0, int(500 * KB * 3)) is False


def test_slow_watch_never_judges_when_the_threshold_is_zero(engine):
    """저속 임계가 0이면 어떤 속도도 저속이 아니어야 한다.

    임계 0, 경과 3 · 6 · 9초에 0바이트 -> 모두 False
    """
    engine._slow_speed_threshold_kb_s = 0
    watch = engine._watch_slow(0)

    assert _feed(watch, [(3.0, 0), (6.0, 0), (9.0, 0)]) == [False, False, False]


# ================================================================ 다른 연결과 견주기


@pytest.mark.parametrize(
    ("peer_speeds", "own_speed", "slow"),
    [
        ((20.0, 25.0, 30.0), 15.0, False),  # 중앙 25의 0.3배(7.5) 이상 — 회선 전체가 느리다
        ((20.0, 25.0, 30.0), 2.0, True),  # 중앙의 0.3배 미만 — 이 연결만 유독 느리다
        ((800.0, 900.0, 1000.0), 50.0, True),  # 다른 연결은 빠르다
        ((20.0, 25.0), 2.0, False),  # 견줄 연결이 3개 미만 — 끊지 않는다
        ((), 2.0, False),
    ],
    ids=["모두 느림", "유독 느림", "다른 연결은 빠름", "견줄 연결 둘", "견줄 연결 없음"],
)
def test_slow_watch_compares_with_the_other_connections(engine, peer_speeds, own_speed, slow):
    """임계 미만인 연결은 다른 연결들의 중앙 속도의 0.3배보다도 느릴 때만 저속이어야 한다.

    견줄 연결이 3개 미만이면 저속이 아니다 — 다시 받는 쪽이 얼마나 빠를지 알 수 없다.

    다른 연결들이 방금 알린 속도 peer_speeds(KB/s), 이 연결은 3초 동안 own_speed(KB/s), 받을 양 100 MB
    -> 경과 3.0초의 판정 == slow
    """
    _peers(engine, peer_speeds)
    watch = engine._watch_slow(0, expected=BIG)

    assert watch.is_slow(3.0, int(own_speed * KB * 3)) is slow


def test_slow_watch_does_not_compare_with_connections_that_went_quiet(engine):
    """한참 전에 속도를 알린 연결과는 견주지 않아야 한다.

    다른 연결 셋이 5초 전에 800 · 900 · 1000 KB/s를 알림, 이 연결은 3초 동안 15 KB/s, 받을 양 100 MB
    -> False — 견줄 연결이 없다 (방금 알린 것이었다면 True다)
    """
    _peers(engine, FAST_PEERS, age=5.0)
    watch = engine._watch_slow(0, expected=BIG)

    assert watch.is_slow(3.0, int(15.0 * KB * 3)) is False


def test_slow_watch_publishes_its_speed_for_the_other_connections(engine):
    """판정 창은 판정 전(3초 이전)에도 이 연결의 속도를 알려야 한다.

    슬롯 0이 경과 1.0초에 200 KB를 받음 -> 슬롯 1의 견줄 속도 == [200.0]
    """
    watch = engine._watch_slow(0)

    watch.is_slow(1.0, 200 * KB)

    assert engine._peer_speeds(1, engine.clock.now) == [pytest.approx(200.0)]


def test_starting_a_new_request_clears_the_speed_of_that_slot(engine):
    """요청을 새로 시작하면 그 슬롯이 앞 요청에서 알린 속도가 지워져야 한다.

    슬롯 0이 200 KB/s를 알린 뒤 슬롯 0의 판정 창을 새로 만듦 -> 슬롯 1의 견줄 속도 == []
    """
    engine._watch_slow(0).is_slow(1.0, 200 * KB)

    engine._watch_slow(0)

    assert engine._peer_speeds(1, engine.clock.now) == []


# ================================================================ 재시작 이득


@pytest.mark.parametrize(
    ("resumes", "expected_mb", "received_mb", "own_speed", "slow"),
    [
        # 처음부터 다시 받는 경로(세그먼트): 다시 받을 양 = 전체 10 MB → 10240 ÷ 400 + 1 = 26.6초, ×1.5 = 39.9초
        (False, 10, 7, 90.0, False),  # 그대로 두면 3072 ÷ 90 = 34.1초 — 끊는 쪽이 더 늦다
        (False, 10, 1, 90.0, True),  # 그대로 두면 9216 ÷ 90 = 102.4초
        # 이어받는 경로(mp4): 다시 받을 양 = 남은 3 MB → 3072 ÷ 400 + 1 = 8.7초, ×1.5 = 13.0초
        (True, 10, 7, 90.0, True),  # 그대로 두면 34.1초
    ],
    ids=["세그먼트 · 거의 다 받음", "세그먼트 · 막 시작", "이어받기 · 거의 다 받음"],
)
def test_slow_connection_is_cut_only_when_restarting_finishes_clearly_sooner(
    engine, resumes, expected_mb, received_mb, own_speed, slow
):
    """느린 연결은, 그대로 두는 것보다 끊고 다시 받는 쪽이 1.5배 넘게 빨리 끝날 때만 저속이어야 한다.

    다른 연결 셋은 400 KB/s. 이 연결은 expected_mb 가운데 received_mb를 받았고 최근 3초는 own_speed(KB/s)
    -> 판정 == slow
    """
    _peers(engine, (400.0, 400.0, 400.0))
    watch = engine._watch_slow(0, expected=expected_mb * MB, resumes=resumes)
    received = received_mb * MB
    watch.is_slow(7.0, received - int(own_speed * KB * 3))

    assert watch.is_slow(10.0, received) is slow


def test_slow_connection_is_not_cut_when_the_remaining_size_is_unknown(engine):
    """받을 양을 모르는 응답은 아무리 느려도 저속으로 끊지 않아야 한다.

    다른 연결 셋은 빠름, 받을 양 미상, 3초 동안 1 KB/s -> False
    """
    _peers(engine, FAST_PEERS)
    watch = engine._watch_slow(0)

    assert watch.is_slow(3.0, 3 * KB) is False


def test_restart_cost_counts_one_second_for_the_new_request(engine):
    """끊는 쪽의 시간에는 새 요청의 시작 비용 1초가 들어가야 한다.

    이어받는 경로, 다른 연결 셋은 1000 KB/s, 남은 양 60 KB, 최근 3초는 50 KB/s
    -> False — 그대로 두면 1.2초, 끊으면 (0.06 + 1.0) × 1.5 = 1.59초 (시작 비용이 없다면 0.09초라 끊는다)
    """
    _peers(engine, (1000.0, 1000.0, 1000.0))
    watch = engine._watch_slow(0, expected=10 * MB, resumes=True)
    received = 10 * MB - 60 * KB
    watch.is_slow(7.0, received - 150 * KB)

    assert watch.is_slow(10.0, received) is False


def test_connection_that_received_nothing_in_the_window_is_cut(engine):
    """최근 3초에 한 바이트도 오지 않은 연결은 저속이어야 한다 — 그대로 두면 끝나지 않는다.

    다른 연결 셋은 빠름, 받을 양 1 MB 가운데 900 KB를 받은 뒤 3초 동안 0바이트 -> True
    """
    _peers(engine, FAST_PEERS)
    watch = engine._watch_slow(0, expected=1 * MB)
    watch.is_slow(7.0, 900 * KB)

    assert watch.is_slow(10.0, 900 * KB) is True


class _OneChunkResponse:
    """청크 하나를 주는 응답 — 받을 양을 선언한다."""

    status_code = 206

    def __init__(self, declared: int):
        self.headers = {"Content-Length": str(declared)}

    def raise_for_status(self):
        pass

    def close(self):
        pass

    def iter_content(self, chunk_size=CHUNK):
        yield b"x" * CHUNK


def _watch_arguments(monkeypatch, module, made, call) -> list[tuple]:
    """받기 루프를 한 번 돌려, 판정 창을 만들 때 넘긴 (받을 양, 이어받는가)를 모은다."""
    made.clock = ManualClock()
    made._now = made.clock
    seen: list[tuple] = []
    real = made._watch_slow

    def spy(part_num, expected=None, resumes=False):
        seen.append((expected, resumes))
        return real(part_num, expected, resumes)

    made._watch_slow = spy

    class _Session:
        def get(self, url, **kwargs):
            return _OneChunkResponse(declared=5 * MB)

    monkeypatch.setattr(module, "get_thread_session", lambda: _Session())
    call(made)
    return seen


def test_file_part_tells_the_watch_its_range_size_and_that_it_resumes(tmp_path, monkeypatch):
    """mp4 파트는 판정 창에 요청한 범위의 크기를 받을 양으로, '이어받는다'로 넘겨야 한다.

    범위 0 ~ 40 MB - 1을 요청 -> (40 MB, True)
    """
    output = tmp_path / "part.bin"
    output.write_bytes(b"")
    made = FileDownloader(_make_data(str(output)), QuietLogger())

    seen = _watch_arguments(
        monkeypatch, file_module, made, lambda e: e._download_part(0, 40 * MB - 1, 0, 40 * MB)
    )

    assert seen == [(40 * MB, True)]


def test_resumed_file_part_tells_the_watch_only_what_is_left_of_its_range(tmp_path, monkeypatch):
    """이어받는 mp4 파트는 판정 창에 범위 전체가 아니라 이어받을 나머지를 받을 양으로 넘겨야 한다.

    범위 0 ~ 40 MB - 1 가운데 8 MB를 이미 받아 둠 -> (32 MB, True)
    """
    output = tmp_path / "part.bin"
    output.write_bytes(b"\x00" * (8 * MB))
    made = FileDownloader(_make_data(str(output)), QuietLogger())
    made._part_progress[(0, 40 * MB - 1)] = 8 * MB

    seen = _watch_arguments(
        monkeypatch, file_module, made, lambda e: e._download_part(0, 40 * MB - 1, 0, 40 * MB)
    )

    assert seen == [(32 * MB, True)]


@pytest.mark.parametrize(
    ("module", "engine_class", "content_type"),
    [(m3u8_module, M3U8Downloader, "m3u8"), (hls_module, HlsAesDownloader, "hls_aes")],
    ids=["m3u8", "hls_aes"],
)
def test_segment_tells_the_watch_the_declared_length_and_that_it_starts_over(
    tmp_path, monkeypatch, module, engine_class, content_type
):
    """세그먼트는 판정 창에 응답이 선언한 길이를 받을 양으로, '처음부터 다시 받는다'로 넘겨야 한다.

    Content-Length 5 MB인 응답 -> (5 MB, False)
    """
    made = engine_class(_make_data(str(tmp_path / "out.mp4"), content_type), QuietLogger())
    made.temp_dir = str(tmp_path)
    made.width = 4

    seen = _watch_arguments(
        monkeypatch,
        module,
        made,
        lambda e: e._download_segment(index=3, segment="seg_3", part_num=0, total_ranges=4),
    )

    assert seen == [(5 * MB, False)]


# ================================================================ 일시정지 시간 제외


def test_wait_while_paused_returns_zero_when_not_paused(engine):
    """일시정지 중이 아니면 기다리지 않고 0을 돌려줘야 한다."""
    assert engine._wait_while_paused() == 0.0


def test_wait_while_paused_returns_the_time_spent_paused(engine):
    """일시정지 중이면 풀릴 때까지 기다리고 그 시간을 돌려줘야 한다.

    일시정지 → 다른 스레드가 시계를 40초 옮기고 재개 -> 40.0
    """
    engine.s.model.pause()

    def resume_later():
        engine.clock.now += 40.0
        engine.s.model.resume()

    threading.Timer(0.05, resume_later).start()

    assert engine._wait_while_paused() == pytest.approx(40.0)


class _PausingResponse:
    """청크 사이에 시계를 옮기는 응답 — 둘째 청크 앞에서 일시정지했다가 60초 뒤 재개한다."""

    def __init__(self, engine, chunks: int, seconds_per_chunk: float, chunk_bytes: int = 512 * KB):
        self._engine = engine
        self._chunks = chunks
        self._seconds = seconds_per_chunk
        self._chunk_bytes = chunk_bytes
        self.status_code = 206
        self.headers = {"Content-Length": str(chunks * chunk_bytes)}
        self.closed = 0

    def raise_for_status(self):
        pass

    def close(self):
        self.closed += 1

    def iter_content(self, chunk_size=CHUNK):
        engine = self._engine
        for number in range(self._chunks):
            engine.clock.now += self._seconds
            if number == 1:
                engine.s.model.pause()

                def resume_later():
                    engine.clock.now += 60.0
                    engine.s.model.resume()

                threading.Timer(0.05, resume_later).start()
            yield b"x" * self._chunk_bytes


def test_part_is_not_restarted_for_the_time_it_was_paused(tmp_path, monkeypatch):
    """받는 도중 일시정지한 시간은 저속 판정에서 빠져야 한다.

    0.5초마다 512 KB(1 MB/s)를 받는 파트, 둘째 청크 앞에서 일시정지 → 60초 뒤 재개, 모두 12청크.
    다른 연결 셋은 빠르다(일시정지한 60초를 빼지 않으면 이 연결만 느린 것으로 보여 끊긴다)
    -> 재시작 0건, 파트 완료 1건
    """
    output = tmp_path / "part.bin"
    output.write_bytes(b"")
    data = _make_data(str(output))
    made = FileDownloader(data, QuietLogger())
    made.clock = ManualClock()
    made._now = made.clock
    _always_fast_peers(made)
    response = _PausingResponse(made, chunks=12, seconds_per_chunk=0.5)

    class _Session:
        def get(self, url, **kwargs):
            return response

    monkeypatch.setattr(file_module, "get_thread_session", lambda: _Session())

    made._download_part(0, 12 * 512 * KB - 1, 0, 12 * 512 * KB)

    assert data.restart_threads == 0
    assert data.completed_threads == 1


@pytest.mark.parametrize(
    ("module", "engine_class", "content_type"),
    [(m3u8_module, M3U8Downloader, "m3u8"), (hls_module, HlsAesDownloader, "hls_aes")],
    ids=["m3u8", "hls_aes"],
)
def test_segment_is_not_restarted_for_the_time_it_was_paused(
    tmp_path, monkeypatch, module, engine_class, content_type
):
    """세그먼트를 받는 도중 일시정지한 시간도 저속 판정에서 빠져야 한다.

    0.5초마다 약 512 KB(1 MB/s)를 받는 세그먼트, 둘째 청크 앞에서 일시정지 → 60초 뒤 재개, 모두 12청크.
    다른 연결 셋은 빠르다
    -> 저속 재시작 0건
    """
    data = _make_data(str(tmp_path / "out.mp4"), content_type=content_type)
    made = engine_class(data, QuietLogger())
    made.temp_dir = str(tmp_path)
    made.width = 4
    made.clock = ManualClock()
    made._now = made.clock
    _always_fast_peers(made)
    # 본문 길이가 16의 배수가 아니다 — 다 받은 뒤 암호화 경로가 복호화까지 가지 않고 온전성 검사에서
    # 다시 받기로 빠진다(이 시험은 받는 동안의 저속 판정만 본다)
    response = _PausingResponse(made, chunks=12, seconds_per_chunk=0.5, chunk_bytes=512 * KB + 1)

    class _Session:
        def get(self, url, **kwargs):
            return response

    monkeypatch.setattr(module, "get_thread_session", lambda: _Session())

    made._download_segment(index=3, segment="seg_3", part_num=0, total_ranges=4)

    assert data.restart_threads == 0


# ================================================================ 응답 닫기


class _CrawlingResponse:
    """청크마다 시계를 2초 옮기는 응답 — 늘 저속이다. 닫힌 횟수를 센다."""

    def __init__(self, clock: ManualClock):
        self._clock = clock
        self.status_code = 206
        self.headers = {"Content-Length": str(40 * MB)}
        self.closed = 0

    def raise_for_status(self):
        pass

    def close(self):
        self.closed += 1

    def iter_content(self, chunk_size=CHUNK):
        for _ in range(10):
            self._clock.now += 2.0
            yield b"x" * CHUNK


def _crawl(monkeypatch, module, made, call) -> _CrawlingResponse:
    made.clock = ManualClock()
    made._now = made.clock
    _always_fast_peers(made)
    response = _CrawlingResponse(made.clock)

    class _Session:
        def get(self, url, **kwargs):
            return response

    monkeypatch.setattr(module, "get_thread_session", lambda: _Session())
    call(made)
    return response


def test_file_part_closes_the_response_when_it_restarts_for_slow_speed(tmp_path, monkeypatch):
    """저속으로 재시작하는 파트는 받다 만 응답을 닫아야 한다.

    다른 연결 셋은 빠르다. 청크마다 2초가 흐르는 응답(4 KB/s) -> 재시작 1건, 응답의 close() 1번
    """
    output = tmp_path / "part.bin"
    output.write_bytes(b"")
    data = _make_data(str(output))
    made = FileDownloader(data, QuietLogger())

    response = _crawl(
        monkeypatch, file_module, made, lambda e: e._download_part(0, 40 * MB - 1, 0, 40 * MB)
    )

    assert data.restart_threads == 1
    assert response.closed == 1


def test_m3u8_segment_closes_the_response_when_it_restarts_for_slow_speed(tmp_path, monkeypatch):
    """저속으로 재시작하는 세그먼트는 받다 만 응답을 닫아야 한다.

    다른 연결 셋은 빠르다. 청크마다 2초가 흐르는 응답(받을 양 40 MB) -> 재시작 1건, 응답의 close() 1번
    """
    data = _make_data(str(tmp_path / "out.mp4"), content_type="m3u8")
    made = M3U8Downloader(data, QuietLogger())
    made.temp_dir = str(tmp_path)
    made.width = 4

    response = _crawl(
        monkeypatch,
        m3u8_module,
        made,
        lambda e: e._download_segment(index=7, segment="seg_7.m4v", part_num=0, total_ranges=4),
    )

    assert data.restart_threads == 1
    assert response.closed == 1


def test_hls_aes_segment_closes_the_response_when_it_restarts_for_slow_speed(tmp_path, monkeypatch):
    """저속으로 재시작하는 암호화 세그먼트는 받다 만 응답을 닫아야 한다.

    다른 연결 셋은 빠르다. 청크마다 2초가 흐르는 응답(받을 양 40 MB) -> 재시작 1건, 응답의 close() 1번
    """
    data = _make_data(str(tmp_path / "out.mp4"), content_type="hls_aes")
    made = HlsAesDownloader(data, QuietLogger())
    made.temp_dir = str(tmp_path)
    made.width = 4

    response = _crawl(
        monkeypatch,
        hls_module,
        made,
        lambda e: e._download_segment(index=3, segment="seg_3.ts", part_num=0, total_ranges=4),
    )

    assert data.restart_threads == 1
    assert response.closed == 1

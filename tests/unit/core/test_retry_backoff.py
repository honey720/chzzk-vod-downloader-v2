"""재요청 대기 · 전체 실패 판정 · 실패 통지 위치 (#329).

세 다운로더(m3u8 · 암호화 VOD · 파일)를 가짜 세션으로 돌린다. 재요청 대기와 전체 실패 판정은
엔진의 재요청 시계(``_retry_now``)를 쓰므로, 그 시계를 테스트가 손으로 돌리는 시계로 바꿔
실제 시간을 기다리지 않는다.
"""

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import core.downloaders.base as base_module
import core.downloaders.file_downloader as fd_module
import core.downloaders.hls_aes_downloader as aes_module
import core.downloaders.m3u8_downloader as m3u8_module
import core.models.download_task as task_module
from core.downloaders.base import NETWORK_LOST, NetworkLostError
from core.downloaders.file_downloader import FileDownloader
from core.downloaders.hls_aes_downloader import HlsAesDownloader
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.models.download_data import DownloadData
from core.utils.retry import retry_delay
from tests.unit.core import test_file_downloader_run as file_run
from tests.unit.core import test_hls_aes_downloader as aes_run
from tests.unit.core import test_m3u8_downloader_run as m3u8_run

KINDS = ("m3u8", "hls_aes", "file")
START = 100.0  # 테스트 시계의 처음 값(초)
QUIET = 10.0  # 전체 실패 판정의 무수신 시간(초) — base._ALL_FAILED_QUIET_SECONDS와 같아야 한다
NOMINAL_WAITS = (0.5, 1.0, 2.0, 4.0, 4.0)  # 흔들기 전 재요청 대기(초)
EPSILON = 1e-3  # 시계를 이분 탐색해 찾은 시각의 오차(초)


def test_quiet_constant_matches_the_engine():
    """이 파일이 쓰는 무수신 시간은 엔진의 값과 같아야 한다.

    base._ALL_FAILED_QUIET_SECONDS -> 10.0
    """
    assert base_module._ALL_FAILED_QUIET_SECONDS == QUIET


# ================================================================ 대기 시간 계산


def test_retry_delay_without_jitter_doubles_from_half_a_second_and_stops_at_four():
    """흔들림이 0이면 재요청 대기는 0.5초부터 두 배씩 늘다가 4초에서 멈춰야 한다.

    attempt 1~7, rand=0
    -> 0.5, 1, 2, 4, 4, 4, 4
    """
    waits = [retry_delay(attempt, rand=lambda: 0.0) for attempt in range(1, 8)]

    assert waits == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0, 4.0]


def test_retry_delay_jitter_shortens_by_at_most_a_tenth():
    """흔들림은 대기를 10%까지만 줄이고 늘리지는 않아야 한다.

    attempt 1~5, rand가 0에 가까운 값과 1에 가까운 값, 기본 난수 200번
    -> 모두 [흔들기 전 값 x 0.9, 흔들기 전 값] 안
    """
    draws = [lambda: 0.0, lambda: 0.999999] + [None] * 200
    for attempt, nominal in enumerate(NOMINAL_WAITS, start=1):
        for rand in draws:
            wait = retry_delay(attempt) if rand is None else retry_delay(attempt, rand=rand)
            assert nominal * 0.9 - 1e-9 <= wait <= nominal


# ================================================================ 가짜 서버 · 엔진 준비


class _Clock:
    """테스트가 손으로 돌리는 재요청 시계."""

    def __init__(self):
        self.now = START

    def __call__(self) -> float:
        return self.now


class _Faults:
    """항목마다 몇 번째 요청에 무엇을 던질지 정하고, 요청이 온 시각(테스트 시계)을 적는다.

    fail(key, attempt)이 예외를 돌려주면 그 요청은 그 예외로 실패한다. attempt는 1부터다.
    gated에 든 항목의 응답은 gate를 하나 얻을 때마다 조각 하나를 흘린다.
    """

    def __init__(self, clock: _Clock, fail):
        self.clock = clock
        self.fail = fail
        self.requests: dict[int, list[float]] = {}
        self.gated: set[int] = set()
        self.gate = threading.Semaphore(0)
        self._lock = threading.Lock()

    def check(self, key: int) -> None:
        with self._lock:
            times = self.requests.setdefault(key, [])
            times.append(self.clock.now)
            attempt = len(times)
        error = self.fail(key, attempt)
        if error is not None:
            raise error

    def wrap(self, key: int, response):
        return _Gated(response, self.gate) if key in self.gated else response


class _Gated:
    """응답의 본문을 잘게 나눠, 테스트가 gate를 풀 때마다 한 조각씩 흘리는 응답."""

    PIECES = 64  # 원래 조각 하나를 나누는 수

    def __init__(self, response, gate: threading.Semaphore):
        self._response = response
        self._gate = gate

    def __getattr__(self, name):
        return getattr(self._response, name)

    def iter_content(self, chunk_size=8192):
        for chunk in self._response.iter_content(chunk_size):
            size = max(1, -(-len(chunk) // self.PIECES))
            for offset in range(0, len(chunk), size):
                assert self._gate.acquire(timeout=30)
                yield chunk[offset : offset + size]


class _M3U8Session(m3u8_run.M3U8Session):
    def __init__(self, faults: _Faults):
        super().__init__(chunks_per_segment=3)
        self.faults = faults

    def get(self, url, **kwargs):
        if "seg_" not in url:
            return super().get(url, **kwargs)
        key = int(url.rsplit("seg_", 1)[1].removesuffix(".m4v"))
        self.faults.check(key)
        return self.faults.wrap(key, super().get(url, **kwargs))


class _AesSession(aes_run.FakeSession):
    def __init__(self, faults: _Faults):
        super().__init__(aes_run._playlist_text())
        self.faults = faults

    def get(self, url, **kwargs):
        if "segment-" not in url or not kwargs.get("stream"):
            # 준비 단계의 키 확인(첫 세그먼트를 스트리밍 없이 받는다)은 전송이 아니다
            return super().get(url, **kwargs)
        key = int(url.rsplit("segment-", 1)[1].removesuffix(".ts"))
        self.faults.check(key)
        return self.faults.wrap(key, super().get(url, **kwargs))


class _FileSession(file_run.RangeSession):
    def __init__(self, faults: _Faults):
        super().__init__(file_run.CONTENT)
        self.faults = faults

    def get(self, url, headers=None, **kwargs):
        start = int(headers["Range"].removeprefix("bytes=").split("-")[0])
        key = start // file_run.MB  # 해상도 144의 파트는 1MB다
        self.faults.check(key)
        return self.faults.wrap(key, super().get(url, headers=headers, **kwargs))


class _Logger:
    """로거 대역 — 오류 · 경고 줄만 적고 나머지는 받아 넘긴다."""

    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: None

    def log_error(self, message, exception=None):
        self.errors.append(message)

    def warning(self, message):
        self.warnings.append(message)


@dataclass
class _Rig:
    engine: object
    data: DownloadData
    logger: _Logger
    clock: _Clock
    faults: _Faults
    output: Path
    expected: bytes  # 모든 항목을 받았을 때의 산출물
    keys: tuple[int, ...]  # 항목의 번호 — m3u8 · 암호화 VOD는 세그먼트, 파일은 1MB 파트
    finished: threading.Event = field(default_factory=threading.Event)
    failures: list = field(default_factory=list)
    lock_grabbed: list = field(default_factory=list)  # 실패 콜백이 엔진 락을 잡았는가
    turns: int = 0  # 실행 루프가 꺼낼 항목이 있는지 물은 횟수 — 루프가 돈 횟수다


def _rig(kind: str, tmp_path, monkeypatch, fail=lambda key, attempt: None) -> _Rig:
    """가짜 세션 · 테스트 시계가 연결된, RUNNING 이전 상태의 엔진을 준비한다.

    실패 콜백은 실패를 적은 뒤 엔진 락을 잡아 본다(2초 제한) — 락을 쥔 채 알리면 잡지 못한다.
    """
    clock = _Clock()
    faults = _Faults(clock, fail)
    monkeypatch.setattr(base_module, "remux_stream", m3u8_run._passthrough_remux_stream)
    if kind == "m3u8":
        module, cls, session = m3u8_module, M3U8Downloader, _M3U8Session(faults)
        url, resolution, expected = m3u8_run.BASE_URL, 1080, m3u8_run._expected_output(3)
        keys = tuple(range(m3u8_run.SEGMENT_COUNT))
    elif kind == "hls_aes":
        module, cls, session = aes_module, HlsAesDownloader, _AesSession(faults)
        url, resolution = aes_run.BASE_URL, 144
        keys = tuple(range(aes_run.SEGMENT_COUNT))
        expected = b"".join(aes_run._segment_body(i) for i in keys)
    else:
        module, cls, session = fd_module, FileDownloader, _FileSession(faults)
        url, resolution, expected = "https://example.invalid/video.mp4", 144, file_run.CONTENT
        keys = tuple(range(file_run.TOTAL_SIZE // file_run.MB))
    monkeypatch.setattr(module, "get_thread_session", lambda: session)
    output = tmp_path / "out.mp4"
    data = DownloadData(
        base_url=url,
        vod_url="https://chzzk.naver.com/video/1",
        output_path=str(output),
        resolution=resolution,
        content_type={"m3u8": "m3u8", "hls_aes": "hls_aes", "file": "video"}[kind],
    )
    logger = _Logger()
    rig = _Rig(None, data, logger, clock, faults, output, expected, keys)

    def on_finished():
        data.model.finish()
        rig.finished.set()

    def on_failed(exc):
        rig.failures.append(exc)
        grabbed = rig.engine.lock.acquire(timeout=2)
        rig.lock_grabbed.append(grabbed)
        if grabbed:
            rig.engine.lock.release()

    rig.engine = cls(data, logger, on_finished=on_finished, on_failed=on_failed)
    if kind == "hls_aes":
        rig.engine.set_key_resolver(lambda content, key_uri: aes_run.KEY)
    rig.engine._retry_now = clock
    ask = rig.engine._has_ready_item

    def counted_ask() -> bool:
        rig.turns += 1
        return ask()

    rig.engine._has_ready_item = counted_ask
    rig.engine._slow_speed_threshold_kb_s = 0  # 러너 속도에 따른 저속 재큐를 끈다 (#160)
    return rig


def _start(rig: _Rig) -> threading.Thread:
    rig.data.model.start()
    thread = threading.Thread(target=rig.engine.run, daemon=True)
    thread.start()
    return thread


def _wait(condition, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "조건이 제한 시간 안에 서지 않았다"
        time.sleep(0.005)


def _polls(rig: _Rig, count: int = 1, until=lambda: False) -> None:
    """실행 루프가 count번 더 돌 때까지(최대 1초) 기다린다 — 그동안 루프가 시계를 본다."""
    start = rig.turns
    deadline = time.monotonic() + 1.0
    while rig.turns < start + count and not until() and time.monotonic() < deadline:
        time.sleep(0.002)


def _drive(rig: _Rig, until, step: float = 0.5, limit: float = 60.0) -> None:
    """until()이 참이 될 때까지 테스트 시계를 step씩 돌린다 — 돌릴 때마다 실행 루프를 기다린다."""
    while not until():
        assert rig.clock.now - START <= limit, f"테스트 시계 {limit}초 안에 끝나지 않았다"
        rig.clock.now += step
        _polls(rig, 1, until)


def _others_done(rig: _Rig, target: int):
    """target 말고 모든 항목을 한 번씩 요청해 받았다."""
    others = [key for key in rig.keys if key != target]
    return lambda: (
        all(key in rig.faults.requests for key in others)
        and rig.data.completed_threads >= len(others)
    )


# ================================================================ 재요청 대기


def _earliest_ready(engine, clock: _Clock, failed_at: float) -> float:
    """실패한 뒤 몇 초가 지나야 다시 꺼낼 수 있게 되는지 — 시계를 이분 탐색한다."""
    low, high = 0.0, 8.0
    clock.now = failed_at + high
    assert engine._has_ready_item()
    while high - low > EPSILON / 4:
        middle = (low + high) / 2
        clock.now = failed_at + middle
        if engine._has_ready_item():
            high = middle
        else:
            low = middle
    clock.now = failed_at + high  # 찾은 시각에 시계를 둔다 — 꺼낼 수 있는 쪽이다
    return high


def _segment_engine(tmp_path, monkeypatch, fail) -> _Rig:
    """작업자 메서드를 직접 부를 수 있게 준비한 m3u8 엔진 (실행 루프 없음)."""
    rig = _rig("m3u8", tmp_path, monkeypatch, fail)
    rig.data.model.start()
    rig.data.threads_progress = [0] * 4
    rig.data.remaining_ranges = []
    rig.engine.temp_dir = str(tmp_path)
    rig.engine.width = 1
    return rig


def _fetch(rig: _Rig, index: int) -> None:
    rig.engine._download_segment(
        index=index, segment=f"seg_{index}.m4v", part_num=0, total_ranges=m3u8_run.SEGMENT_COUNT
    )


def test_the_same_segment_is_handed_out_again_after_waits_of_0_5_1_2_4_4_seconds(
    tmp_path, monkeypatch
):
    """같은 세그먼트가 연달아 실패하면 다시 꺼낼 수 있게 되기까지 0.5 · 1 · 2 · 4 · 4초여야 한다.

    seg_2가 연결 오류로 다섯 번 실패하고, 실패할 때마다 다시 꺼낼 수 있게 되는 시각을 찾아 그때 꺼냄
    -> 기다린 시간이 차례로 [0.45, 0.5] · [0.9, 1] · [1.8, 2] · [3.6, 4] · [3.6, 4] 안
    """
    rig = _segment_engine(
        tmp_path, monkeypatch, lambda key, attempt: requests.ConnectionError("끊김")
    )
    waits = []
    for _ in NOMINAL_WAITS:
        _fetch(rig, 2)
        assert rig.data.remaining_ranges == [(2, "seg_2.m4v")]
        failed_at = rig.clock.now
        waits.append(_earliest_ready(rig.engine, rig.clock, failed_at))
        with rig.engine.lock:
            assert rig.engine._take_ready_item() == (2, "seg_2.m4v")

    for wait, nominal in zip(waits, NOMINAL_WAITS, strict=True):
        assert nominal * 0.9 - EPSILON <= wait <= nominal + EPSILON, waits


def test_other_segments_are_handed_out_while_a_failed_one_waits_and_it_goes_first_when_ready(
    tmp_path, monkeypatch
):
    """실패한 세그먼트가 기다리는 동안 다른 세그먼트를 꺼내고, 대기가 끝나면 그것을 먼저 꺼내야 한다.

    seg_2가 연결 오류로 한 번 실패, 대기열에 seg_3 · seg_4를 더함
    -> 실패 직후 꺼낸 것은 seg_3, 0.5초 뒤 꺼낸 것은 seg_2, 그다음은 seg_4
    """
    rig = _segment_engine(
        tmp_path,
        monkeypatch,
        lambda key, attempt: requests.ConnectionError("끊김") if key == 2 else None,
    )
    _fetch(rig, 2)
    rig.data.remaining_ranges.extend([(3, "seg_3.m4v"), (4, "seg_4.m4v")])

    with rig.engine.lock:
        first = rig.engine._take_ready_item()
    rig.clock.now += 0.5
    with rig.engine.lock:
        second = rig.engine._take_ready_item()
        third = rig.engine._take_ready_item()

    assert [first, second, third] == [(3, "seg_3.m4v"), (2, "seg_2.m4v"), (4, "seg_4.m4v")]


class _FakeTime:
    """모델이 일시정지 시간을 재는 시계를 테스트 시계로 바꾼다 (time.time은 그대로)."""

    def __init__(self, clock: _Clock):
        self._clock = clock

    def perf_counter(self) -> float:
        return self._clock.now

    def time(self) -> float:
        return time.time()


def test_wait_stands_still_while_paused_and_resumes_with_what_was_left(tmp_path, monkeypatch):
    """일시정지한 동안은 재요청 대기가 흐르지 않고, 재개하면 남은 대기만큼 더 기다려야 한다.

    seg_2가 연결 오류로 한 번 실패(대기 0.45~0.5초), 0.2초 뒤 일시정지, 100초 뒤 재개.
    엔진의 단조 시계와 모델의 시계를 같은 테스트 시계로 돌림
    -> 일시정지 중 꺼낼 수 없음, 재개 0.2초 뒤 꺼낼 수 없음, 재개 0.31초 뒤 꺼낼 수 있음
    """
    rig = _segment_engine(
        tmp_path, monkeypatch, lambda key, attempt: requests.ConnectionError("끊김")
    )
    del rig.engine._retry_now  # 제품의 재요청 시계(일시정지를 뺀다)를 쓴다
    rig.engine._now = rig.clock
    monkeypatch.setattr(task_module, "time", _FakeTime(rig.clock))
    _fetch(rig, 2)
    failed_at = rig.clock.now

    rig.clock.now = failed_at + 0.2
    rig.data.model.pause()
    rig.clock.now = failed_at + 100.2
    ready_while_paused = rig.engine._has_ready_item()
    rig.data.model.resume()
    rig.clock.now = failed_at + 100.4
    ready_soon_after = rig.engine._has_ready_item()
    rig.clock.now = failed_at + 100.51
    ready_after_the_rest = rig.engine._has_ready_item()

    assert (ready_while_paused, ready_soon_after, ready_after_the_rest) == (False, False, True)


@pytest.mark.parametrize("kind", KINDS)
def test_failed_item_is_not_requested_again_until_its_wait_passes(kind, tmp_path, monkeypatch):
    """오류로 실패한 항목은 대기가 지나기 전에 다시 요청하지 않고, 그동안 다른 항목을 받아야 한다.

    첫 항목이 연결 오류로 한 번 실패하고 다음부터 정상. 테스트 시계를 멈춘 채 실행 루프를 몇 번 돌림
    -> 나머지 항목은 모두 받음, 첫 항목 요청 1회. 시계를 0.5초 돌리면 첫 항목을 다시 받아 완료하고
       산출물은 모든 항목을 받은 것과 같다
    """
    rig = _rig(
        kind,
        tmp_path,
        monkeypatch,
        lambda key, attempt: (
            requests.ConnectionError("끊김") if key == 0 and attempt == 1 else None
        ),
    )
    thread = _start(rig)
    _wait(_others_done(rig, 0))
    _polls(rig, 5)
    requested_while_waiting = len(rig.faults.requests[0])

    rig.clock.now += 0.5
    assert rig.finished.wait(timeout=15), "완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    assert requested_while_waiting == 1
    assert rig.faults.requests[0] == [START, START + 0.5]
    assert rig.failures == []
    assert rig.output.read_bytes() == rig.expected


@pytest.mark.parametrize("kind", KINDS)
def test_stop_while_an_item_waits_ends_the_run_at_once(kind, tmp_path, monkeypatch):
    """재요청을 기다리는 항목이 있을 때 중지하면 기다리지 않고 바로 끝나야 한다.

    첫 항목이 늘 연결 오류. 테스트 시계를 멈춘 채 나머지 항목을 다 받은 뒤 중지
    -> run()이 2초 안에 끝남, 첫 항목 요청 1회, 실패 · 완료 통지 없음
    """
    rig = _rig(
        kind,
        tmp_path,
        monkeypatch,
        lambda key, attempt: requests.ConnectionError("끊김") if key == 0 else None,
    )
    thread = _start(rig)
    _wait(_others_done(rig, 0))
    _polls(rig, 3)

    rig.data.model.stop()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(rig.faults.requests[0]) == 1
    assert rig.failures == [] and not rig.finished.is_set()


# ================================================================ 빠진 항목


@pytest.mark.parametrize("kind", KINDS)
def test_item_that_fails_a_few_times_is_still_in_the_output(kind, tmp_path, monkeypatch):
    """몇 번 실패한 뒤 받아진 항목은 산출물에 들어 있어야 한다.

    둘째 항목이 연결 오류로 세 번 실패한 뒤 정상. 시계를 0.5초씩 돌림
    -> 완료, 둘째 항목 요청 4회, 산출물이 모든 항목을 받은 것과 같다
    """
    rig = _rig(
        kind,
        tmp_path,
        monkeypatch,
        lambda key, attempt: (
            requests.ConnectionError("끊김") if key == 1 and attempt <= 3 else None
        ),
    )
    thread = _start(rig)
    _drive(rig, rig.finished.is_set)
    thread.join(timeout=10)

    assert len(rig.faults.requests[1]) == 4
    assert rig.failures == []
    assert rig.output.read_bytes() == rig.expected


def _http_error(status: int) -> requests.HTTPError:
    return requests.HTTPError(f"HTTP {status}", response=SimpleNamespace(status_code=status))


@pytest.mark.parametrize("kind", KINDS)
def test_item_that_never_arrives_fails_the_download_instead_of_completing_without_it(
    kind, tmp_path, monkeypatch
):
    """끝내 받지 못한 항목이 있으면 그 항목을 뺀 채 완료하지 않고 실패해야 한다.

    둘째 항목이 늘 404. 시계를 0.5초씩 돌림
    -> 실패 통지 1회(HTTPError 404), 완료 없음, 둘째 항목 요청 3회(재큐 2회)
    """
    error = _http_error(404)
    rig = _rig(kind, tmp_path, monkeypatch, lambda key, attempt: error if key == 1 else None)
    thread = _start(rig)
    _drive(rig, lambda: bool(rig.failures))
    thread.join(timeout=10)

    assert rig.failures == [error]
    assert not rig.finished.is_set()
    assert len(rig.faults.requests[1]) == 3


# ================================================================ 전체 실패 판정


@pytest.mark.parametrize("kind", KINDS)
def test_every_request_failing_to_connect_ends_as_network_lost_within_the_quiet_time(
    kind, tmp_path, monkeypatch
):
    """모든 요청이 연결 오류면 무수신 10초를 넘긴 뒤 곧 회선 끊김으로 실패하고 상한까지 끌지 않아야 한다.

    모든 항목 요청이 연결 오류. 시계를 1초씩 돌림
    -> 실패 통지 1회(NetworkLostError, 원인은 연결 오류), 실패 시각은 시작 뒤 10~15초,
       항목마다 요청은 상한(11회)보다 적고 상한 도달 줄이 없다, 완료 없음
    """
    cause = requests.ConnectionError("끊김")
    rig = _rig(kind, tmp_path, monkeypatch, lambda key, attempt: cause)
    thread = _start(rig)
    _drive(rig, lambda: bool(rig.failures), step=1.0)
    failed_after = rig.clock.now - START
    thread.join(timeout=10)

    assert len(rig.failures) == 1
    lost = rig.failures[0]
    assert isinstance(lost, NetworkLostError) and lost.message_key == NETWORK_LOST
    assert lost.__cause__ is cause
    assert QUIET <= lost.quiet_seconds <= failed_after <= QUIET + 4.0 + 1.0  # 대기 상한 + 한 걸음
    assert max(len(times) for times in rig.faults.requests.values()) < 11
    assert not any("requeue limit" in line for line in rig.logger.errors)
    assert not rig.finished.is_set()


@pytest.mark.parametrize("kind", KINDS)
def test_a_request_waiting_for_its_answer_in_a_slot_that_failed_holds_off_network_lost(
    kind, tmp_path, monkeypatch
):
    """실패했던 슬롯이 새로 보낸 요청이 답을 기다리는 동안은 회선 끊김으로 판정하지 않아야 한다.

    모든 항목 요청이 연결 오류. 다만 첫 항목의 둘째 요청(앞서 연결 오류로 실패한 슬롯이 받음)은
    답 없이 서 있다가, 테스트가 풀면 연결 타임아웃으로 실패함. 시계를 1초씩 돌림
    -> 시작 뒤 20초까지(다른 항목은 11초 뒤에도 실패) 실패 통지 없음. 서 있던 요청을 풀면
       실패 통지 1회(NetworkLostError), 그 시각은 푼 시각
    """
    waiting = threading.Event()
    release = threading.Event()

    def fail(key, attempt):
        if key == 0 and attempt == 2:
            waiting.set()
            assert release.wait(timeout=30)
            return requests.ConnectTimeout("응답 없음")
        return requests.ConnectionError("끊김")

    rig = _rig(kind, tmp_path, monkeypatch, fail)
    thread = _start(rig)
    try:
        _drive(rig, waiting.is_set, step=1.0)
        _drive(rig, lambda: rig.clock.now >= START + 2 * QUIET, step=1.0)
        failures_while_waiting = list(rig.failures)
        late_failures = [
            at for key, times in rig.faults.requests.items() if key != 0 for at in times
        ]
        released_at = rig.clock.now
    finally:
        release.set()
    _drive(rig, lambda: bool(rig.failures), step=1.0)
    thread.join(timeout=10)

    assert failures_while_waiting == []
    assert max(late_failures) >= START + QUIET + 1  # 무수신 10초 뒤에도 다른 항목이 실패했다
    assert len(rig.failures) == 1 and isinstance(rig.failures[0], NetworkLostError)
    assert rig.failures[0].quiet_seconds >= released_at - START - 1.0


@pytest.mark.parametrize("kind", KINDS)
def test_failures_while_another_item_is_still_arriving_are_not_network_lost(
    kind, tmp_path, monkeypatch
):
    """한 항목이 계속 연결 오류여도 다른 항목을 받고 있는 동안은 전체 실패로 판정하지 않아야 한다.

    첫 항목이 늘 연결 오류, 둘째 항목은 시계를 1초 돌릴 때마다 한 조각씩 옴. 시계를 20초 돌림
    -> 실패 통지 없음
    """
    rig = _rig(
        kind,
        tmp_path,
        monkeypatch,
        lambda key, attempt: requests.ConnectionError("끊김") if key == 0 else None,
    )
    rig.faults.gated.add(1)
    thread = _start(rig)
    _wait(lambda: 1 in rig.faults.requests and 0 in rig.faults.requests)
    for _ in range(int(2 * QUIET)):
        rig.clock.now += 1.0
        rig.faults.gate.release()
        _polls(rig, 1)
    failures_while_arriving = list(rig.failures)

    rig.data.model.stop()
    rig.faults.gate.release(10**6)
    thread.join(timeout=10)

    assert failures_while_arriving == []
    assert len(rig.faults.requests[0]) >= 5  # 그동안 첫 항목은 계속 실패했다


# ================================================================ 실패 통지 위치


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "error", [_http_error(404), requests.ConnectionError("끊김")], ids=["limit", "network-lost"]
)
def test_failure_callback_can_take_the_engine_lock(kind, error, tmp_path, monkeypatch):
    """실패 콜백이 엔진 락을 잡아도 교착하지 않고 실패로 끝나야 한다 — 통지는 락 밖에서 한다.

    모든 항목 요청이 그 오류(404면 재큐 상한, 연결 오류면 전체 실패 판정). 실패 콜백이 엔진 락을
    2초 제한으로 잡아 봄. 시계를 1초씩 돌림
    -> 콜백이 락을 잡음, run()이 끝남, 실패 통지 1회
    """
    rig = _rig(kind, tmp_path, monkeypatch, lambda key, attempt: error)
    thread = _start(rig)
    _drive(rig, lambda: bool(rig.lock_grabbed), step=1.0)
    thread.join(timeout=10)

    assert rig.lock_grabbed == [True]
    assert not thread.is_alive()
    assert len(rig.failures) == 1

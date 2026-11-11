"""손상된 CDN 캐시의 제한된 재시도와 기존 정상 조각 보존을 검증한다."""

import threading
from collections.abc import Callable, Iterator
from urllib.parse import urlsplit

import pytest
import requests

import core.engine.base as base
import core.sources.m3u8_downloader as module
from tests.core.sources.test_m3u8_downloader_run import (
    M3U8Session,
    RunLogger,
    StreamResponse,
    TextResponse,
    _make_engine,
    _expected_output,
)

HOST = "https://ex-nlive-slitvod-streaming.navercdn.com"


def test_recovery_url_keeps_auth_and_is_idempotent() -> None:
    """복구 주소가 생성되면 인증 경로와 쿼리를 보존하고 반복해도 같아야 한다."""
    url = HOST + "/video/hdntl=test/seg.m4v?signature=test"
    expected = HOST + "/video/hdntl=test//seg.m4v?signature=test"
    assert module._cache_recovery_url(url) == expected
    assert module._cache_recovery_url(expected) == expected


@pytest.mark.parametrize(
    "url", ["https://example.org/a.m4v", HOST.replace("https", "http") + "/a.m4v"]
)
def test_other_servers_are_unchanged(url) -> None:
    """실측하지 않은 서버나 HTTP 주소는 변경되지 않아야 한다."""
    assert module._cache_recovery_url(url) == url


@pytest.mark.parametrize("recover", [True, False])
def test_invalid_cache_uses_validated_retry_and_keeps_limit(tmp_path, monkeypatch, recover) -> None:
    """손상 캐시를 만나면 대체 경로를 검증하고 반복 손상은 한도에서 실패해야 한다."""
    engine, data, logger, output, finished, errors, merges = _make_engine(tmp_path, monkeypatch)
    data.base_url = HOST + "/hls/video.m3u8"
    requested = []

    class Session(M3U8Session):
        def get(self, url, **kwargs) -> StreamResponse | TextResponse:
            if "seg_3" in url:
                requested.append(urlsplit(url).path)
                if not recover or "//seg_3" not in url:
                    return StreamResponse(b"damaged cached payload", 0)
            return super().get(url, **kwargs)

    monkeypatch.setattr(module, "get_thread_session", lambda: Session(3))
    monkeypatch.setattr(base, "_TRANSIENT_ERROR_REQUEUE_LIMIT", 2)
    data.adjust_threads = 1
    data.model.start()
    engine.run()
    assert requested[0] == "/hls/seg_3.m4v"
    assert len(requested) >= 2
    assert all(p == "/hls//seg_3.m4v" for p in requested[1:])
    assert any("Segment 4" in w for w in logger.warnings)
    if recover:
        assert not errors and finished.is_set() and merges == [True]
        assert output.read_bytes() == _expected_output(3)
    else:
        assert errors and not finished.is_set() and not output.exists()
        assert len(requested) <= 3


WORKERS = 4  # 같은 세그먼트를 동시에 받는 작업자 수


class _DamagedTogether(StreamResponse):
    """손상 본문을 다 흘린 뒤 모든 작업자가 모일 때까지 기다리는 응답 — 구조 검사 실패를 한꺼번에 낸다."""

    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__(b"damaged cached payload", 0)
        self._barrier = barrier

    def iter_content(self, chunk_size=8192) -> Iterator[bytes]:
        yield from super().iter_content(chunk_size)
        self._barrier.wait(timeout=10)


def test_workers_failing_the_same_segment_together_mark_and_warn_once(
    tmp_path, monkeypatch
) -> None:
    """여러 작업자가 같은 세그먼트의 손상 본문을 동시에 받으면 복구 표시와 경고는 한 번이어야 한다 (#375).

    작업자 4개가 seg_3(복구 대상 CDN)을 동시에 받고, 손상 본문을 다 받은 뒤 함께 구조 검사에 실패함
    -> 복구 표시는 {3}, 복구 경고 1줄, 재큐는 실패마다 하나(대기열의 seg_3 4개 · 오류 재큐 수 4)
    """
    engine, data, logger, _output, _finished, _errors, _merges = _make_engine(tmp_path, monkeypatch)
    data.base_url = HOST + "/hls/video.m3u8"
    barrier = threading.Barrier(WORKERS)

    class Session(M3U8Session):
        def get(self, url, **kwargs) -> _DamagedTogether:
            return _DamagedTogether(barrier)

    monkeypatch.setattr(module, "get_thread_session", lambda: Session(3))
    engine.temp_dir = str(tmp_path)
    engine.width = 1
    data.threads_progress = [0] * WORKERS
    data.model.start()

    item = (3, "seg_3.m4v")
    workers = [
        threading.Thread(
            target=engine._download_segment,
            kwargs={"index": 3, "segment": item[1], "part_num": part, "total_ranges": 1},
        )
        for part in range(WORKERS)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert not any(worker.is_alive() for worker in workers)

    assert engine._cache_recovery == {3}
    assert len([w for w in logger.warnings if "invalid cached body" in w]) == 1
    assert data.remaining_ranges.count(item) == WORKERS
    assert engine._error_requeues[item] == WORKERS


def test_a_second_run_of_the_same_engine_starts_each_segment_on_its_normal_path(
    tmp_path, monkeypatch
) -> None:
    """같은 엔진을 다시 돌리면 앞 실행에서 복구로 표시된 세그먼트도 처음에는 원래 경로로 받아야 한다 (#375).

    첫 실행: seg_3이 원래 경로 · // 경로 모두 손상 → 재시도 한도로 실패. 둘째 실행(같은 엔진): seg_3이
    원래 경로에서 정상
    -> 둘째 실행의 seg_3 요청은 원래 경로 하나("/hls/seg_3.m4v"), 둘째 실행은 완료된다
    """
    engine, data, logger, output, finished, errors, _merges = _make_engine(tmp_path, monkeypatch)
    data.base_url = HOST + "/hls/video.m3u8"
    damaged = True
    requested = []

    class Session(M3U8Session):
        def get(self, url, **kwargs) -> StreamResponse | TextResponse:
            if "seg_3" in url:
                requested.append(urlsplit(url).path)
                if damaged:
                    return StreamResponse(b"damaged cached payload", 0)
            return super().get(url, **kwargs)

    monkeypatch.setattr(module, "get_thread_session", lambda: Session(3))
    monkeypatch.setattr(base, "_TRANSIENT_ERROR_REQUEUE_LIMIT", 2)
    data.adjust_threads = 1
    data.model.start()
    engine.run()
    assert errors and not finished.is_set()  # 첫 실행은 실패했다
    assert "/hls//seg_3.m4v" in requested  # 첫 실행에서 복구 경로로 표시됐다

    damaged = False
    requested.clear()
    data.model.start()
    engine.run()

    assert requested == ["/hls/seg_3.m4v"]
    assert finished.is_set()
    assert output.read_bytes() == _expected_output(3)


# ================================================================ 손상 응답의 판단 근거 로그 (#375 ④)

CORRUPT_BODY = b"damaged cached payload"  # 22바이트 — 길이는 맞지만 fMP4 구조가 아니다
CORRUPT_HEADERS = {
    "Content-Length": "22",
    "ETag": '"etag-1"',
    "Age": "30",
    "X-Cache": "HIT",
}


class _HeaderedResponse(StreamResponse):
    """헤더를 가진 손상 응답."""

    def __init__(self, body: bytes, headers: dict) -> None:
        super().__init__(body, 0)
        self.headers = headers


def _run_with_corrupt_seg3(
    tmp_path, monkeypatch, *, alternate_ok: bool, headers: dict, limit: int
) -> tuple[RunLogger, threading.Event]:
    """seg_3(복구 대상 CDN)의 원래 경로가 손상 응답을 주는 실행 — // 경로는 alternate_ok면 정상."""
    engine, data, logger, output, finished, errors, _merges = _make_engine(tmp_path, monkeypatch)
    data.base_url = HOST + "/hls/video.m3u8"

    class Session(M3U8Session):
        def get(self, url, **kwargs) -> StreamResponse | TextResponse:
            if "seg_3" in url and not (alternate_ok and "//seg_3" in url):
                return _HeaderedResponse(CORRUPT_BODY, headers)
            return super().get(url, **kwargs)

    monkeypatch.setattr(module, "get_thread_session", lambda: Session(3))
    monkeypatch.setattr(base, "_TRANSIENT_ERROR_REQUEUE_LIMIT", limit)
    data.adjust_threads = 1
    data.model.start()
    engine.run()
    return logger, finished


def _corrupt_lines(logger) -> list[str]:
    return [w for w in logger.warnings if "corrupt response -" in w]


def test_corrupt_response_line_carries_the_response_facts_and_the_recovery(
    tmp_path, monkeypatch
) -> None:
    """손상 응답을 받으면 받은 길이 · Content-Length · ETag · Age · 캐시 상태 · 시도 · 경로 · 복구를 한 줄에 남겨야 한다.

    seg_3의 원래 경로가 22바이트 손상 본문(Content-Length 22 · ETag "etag-1" · Age 30 · X-Cache HIT),
    // 경로는 정상
    -> 줄 하나: "Segment 4: corrupt response - received 22 bytes, Content-Length 22, ETag "etag-1",
       Age 30, cache X-Cache=HIT, attempt 1, path original, recovery switching",
       그 뒤 "Segment 4: valid body after 1 corrupt responses (0 not detailed), path alternate"
    """
    logger, finished = _run_with_corrupt_seg3(
        tmp_path, monkeypatch, alternate_ok=True, headers=CORRUPT_HEADERS, limit=2
    )

    assert finished.is_set()
    assert _corrupt_lines(logger) == [
        'Segment 4: corrupt response - received 22 bytes, Content-Length 22, ETag "etag-1", '
        "Age 30, cache X-Cache=HIT, attempt 1, path original, recovery switching"
    ]
    assert (
        "Segment 4: valid body after 1 corrupt responses (0 not detailed), path alternate"
        in logger.warnings
    )


def test_repeated_corrupt_responses_of_a_segment_are_grouped(tmp_path, monkeypatch) -> None:
    """한 세그먼트의 손상 응답이 되풀이되면 자세한 줄은 원래 경로 · 복구 경로의 첫 것만이고 나머지는 끝 줄에 수로 적혀야 한다.

    seg_3의 원래 경로 · // 경로 모두 손상, 일시 오류 재큐 상한 3(요청 4번)
    -> 자세한 줄 2개(attempt 1 path original recovery switching · attempt 2 path alternate recovery
       already), "Segment 4: giving up after 4 corrupt responses (2 not detailed)"
    """
    logger, finished = _run_with_corrupt_seg3(
        tmp_path, monkeypatch, alternate_ok=False, headers=CORRUPT_HEADERS, limit=3
    )

    assert not finished.is_set()
    lines = _corrupt_lines(logger)
    assert [line.rsplit(", attempt ", 1)[1] for line in lines] == [
        "1, path original, recovery switching",
        "2, path alternate, recovery already",
    ]
    assert "Segment 4: giving up after 4 corrupt responses (2 not detailed)" in logger.warnings


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        (0, ["1, path original, recovery not retried"]),  # 첫 손상 응답에서 상한
        (1, ["1, path original, recovery switching", "2, path alternate, recovery not retried"]),
    ],
)
def test_corrupt_response_that_is_not_retried_says_so_instead_of_a_recovery(
    tmp_path, monkeypatch, limit, expected
) -> None:
    """손상 응답으로 재큐 상한에 닿아 다시 받지 않으면 그 자세한 줄의 복구는 not retried여야 한다 (#375).

    seg_3의 원래 경로 · // 경로 모두 손상, 일시 오류 재큐 상한 0 · 1
    -> 자세한 줄의 시도 · 경로 · 복구 == expected, 완료 없음
    """
    logger, finished = _run_with_corrupt_seg3(
        tmp_path, monkeypatch, alternate_ok=False, headers=CORRUPT_HEADERS, limit=limit
    )

    assert not finished.is_set()
    lines = _corrupt_lines(logger)
    assert [line.rsplit(", attempt ", 1)[1] for line in lines] == expected


def test_corrupt_response_without_headers_marks_each_missing_fact(tmp_path, monkeypatch) -> None:
    """헤더가 없는 손상 응답이면 빠진 항목을 "-"로 적어야 한다.

    seg_3의 원래 경로가 헤더 없는 22바이트 손상 본문, // 경로는 정상
    -> "... received 22 bytes, Content-Length -, ETag -, Age -, cache -, attempt 1, ..."
    """
    logger, _finished = _run_with_corrupt_seg3(
        tmp_path, monkeypatch, alternate_ok=True, headers={}, limit=2
    )

    assert _corrupt_lines(logger)[0].startswith(
        "Segment 4: corrupt response - received 22 bytes, Content-Length -, ETag -, Age -, "
        "cache -, attempt 1,"
    )


def test_corrupt_response_line_has_no_address_and_its_header_tokens_are_redacted_in_the_log_file(
    tmp_path, monkeypatch
) -> None:
    """손상 응답의 줄에는 세그먼트 주소가 없어야 하고, 헤더 값에 섞인 주소의 토큰은 로그 파일에서 가려져야 한다.

    seg_3의 X-Cache 헤더가 "HIT from https://edge.invalid/x?token=SECRET123", // 경로는 정상.
    그 줄을 실제 다운로드 로거(DownloadLogger)로 파일에 씀
    -> 줄에 "seg_3" · CDN 호스트가 없다, 로그 파일에 "SECRET123"이 없고 "edge.invalid"는 있다
    """
    from app.download.logger import DownloadLogger

    headers = {**CORRUPT_HEADERS, "X-Cache": "HIT from https://edge.invalid/x?token=SECRET123"}
    logger, _finished = _run_with_corrupt_seg3(
        tmp_path, monkeypatch, alternate_ok=True, headers=headers, limit=2
    )
    (line,) = _corrupt_lines(logger)
    assert "SECRET123" in line  # 전제: 헤더 값이 줄에 들어갔다 — 가리는 것은 포매터다

    file_logger = DownloadLogger()
    file_logger.warning(line)
    file_logger.save_and_close()
    with open(file_logger.log_file, encoding="utf-8") as f:
        written = f.read()

    assert "seg_3" not in line and "navercdn" not in line
    assert "edge.invalid" in written
    assert "SECRET123" not in written


def test_corrupt_header_value_with_a_line_break_stays_on_one_line(tmp_path, monkeypatch) -> None:
    """헤더 값에 줄바꿈 · 제어 문자가 섞여 와도 손상 응답의 줄은 한 줄이어야 한다.

    seg_3의 ETag 헤더가 '"a"\r\nInjected: 1', // 경로는 정상
    -> 줄에 "\r" · "\n"이 없다, ETag 칸은 '"a"Injected: 1'
    """
    headers = {**CORRUPT_HEADERS, "ETag": '"a"\r\nInjected: 1'}
    logger, _finished = _run_with_corrupt_seg3(
        tmp_path, monkeypatch, alternate_ok=True, headers=headers, limit=2
    )
    (line,) = _corrupt_lines(logger)

    assert "\r" not in line and "\n" not in line
    assert 'ETag "a"Injected: 1, ' in line


def test_normal_responses_leave_no_corrupt_response_line(tmp_path, monkeypatch) -> None:
    """세그먼트가 모두 온전하게 오면 손상 응답의 줄도 묶음의 끝 줄도 없어야 한다.

    복구 대상 CDN, 세그먼트 3개 모두 정상
    -> "corrupt response" · "corrupt responses"가 든 줄 0개, 완료
    """
    engine, data, logger, output, finished, _errors, _merges = _make_engine(tmp_path, monkeypatch)
    data.base_url = HOST + "/hls/video.m3u8"
    monkeypatch.setattr(module, "get_thread_session", lambda: M3U8Session(3))
    data.model.start()
    engine.run()

    assert finished.is_set()
    assert [w for w in logger.warnings if "corrupt response" in w] == []


# ================================================================ 초기화 세그먼트의 캐시 복구 (#375 ⑤)

INIT_RECOVERY_WARNING = "Init segment: invalid cached body; retrying alternate resource path"


def _init_session(damaged: Callable[[str], bool], requested: list[str]) -> type[M3U8Session]:
    """init.m4s 요청의 경로를 `requested`에 적고, `damaged(경로)`가 참이면 손상 본문을 주는 세션."""

    class Session(M3U8Session):
        def get(self, url, **kwargs) -> StreamResponse | TextResponse:
            if url.endswith("init.m4s"):
                path = urlsplit(url).path
                requested.append(path)
                if damaged(path):
                    return TextResponse(content=CORRUPT_BODY)
            return super().get(url, **kwargs)

    return Session


def test_damaged_init_segment_is_fetched_again_on_the_recovery_path(tmp_path, monkeypatch) -> None:
    """복구 대상 CDN의 초기화 세그먼트가 원래 경로에서 손상으로 오면 복구 경로로 다시 받아 완료돼야 한다.

    init.m4s가 원래 경로에서 손상 본문, // 경로에서 정상
    -> init.m4s 요청 경로 ["/hls/init.m4s", "/hls//init.m4s"], 복구 경고 1줄, 완료, 산출물 정상
    """
    engine, data, logger, output, finished, errors, _merges = _make_engine(tmp_path, monkeypatch)
    data.base_url = HOST + "/hls/video.m3u8"
    requested: list[str] = []
    session = _init_session(lambda path: "//" not in path, requested)(3)
    monkeypatch.setattr(module, "get_thread_session", lambda: session)
    data.model.start()
    engine.run()

    assert requested == ["/hls/init.m4s", "/hls//init.m4s"]
    assert logger.warnings.count(INIT_RECOVERY_WARNING) == 1
    assert not errors and finished.is_set()
    assert output.read_bytes() == _expected_output(3)


def test_init_segment_damaged_on_both_paths_fails_at_the_same_limit(tmp_path, monkeypatch) -> None:
    """초기화 세그먼트가 복구 경로에서도 손상이면 경로를 더 바꾸지 않고 같은 상한에서 실패해야 한다.

    init.m4s가 두 경로 모두 손상, 다시 받는 상한 2
    -> 요청 3회(원래 1 · // 2), 복구 경고 1줄, 실패 콜백 1회, 완료 없음
    """
    engine, data, logger, _output, finished, errors, _merges = _make_engine(tmp_path, monkeypatch)
    data.base_url = HOST + "/hls/video.m3u8"
    requested: list[str] = []
    session = _init_session(lambda _path: True, requested)(3)
    monkeypatch.setattr(module, "get_thread_session", lambda: session)
    monkeypatch.setattr(base, "_TRANSIENT_ERROR_REQUEUE_LIMIT", 2)
    data.model.start()
    engine.run()

    assert requested == ["/hls/init.m4s", "/hls//init.m4s", "/hls//init.m4s"]
    assert logger.warnings.count(INIT_RECOVERY_WARNING) == 1
    assert len(errors) == 1 and not finished.is_set()


def test_damaged_init_segment_on_another_server_keeps_its_path(tmp_path, monkeypatch) -> None:
    """복구 대상이 아닌 서버의 초기화 세그먼트가 손상으로 오면 같은 경로로만 다시 받아야 한다.

    다른 서버(example.org), init.m4s가 처음 한 번 손상 · 그다음 정상
    -> 요청 경로 ["/hls/init.m4s", "/hls/init.m4s"], 복구 경고 0줄, 완료
    """
    engine, data, logger, _output, finished, errors, _merges = _make_engine(tmp_path, monkeypatch)
    data.base_url = "https://example.org/hls/video.m3u8"
    requested: list[str] = []
    session = _init_session(lambda _path: len(requested) == 1, requested)(3)
    monkeypatch.setattr(module, "get_thread_session", lambda: session)
    data.model.start()
    engine.run()

    assert requested == ["/hls/init.m4s", "/hls/init.m4s"]
    assert INIT_RECOVERY_WARNING not in logger.warnings
    assert any("Init segment incomplete" in e for e in logger.errors), (
        "전제: 손상을 만나 다시 받았다"
    )
    assert not errors and finished.is_set()


def test_init_segment_body_ending_early_keeps_its_path(tmp_path, monkeypatch) -> None:
    """복구 대상 CDN이어도 초기화 세그먼트 본문이 읽다가 끊기면(구조 검사 전) 같은 경로로 다시 받아야 한다.

    init.m4s 첫 요청의 본문 읽기가 ChunkedEncodingError, 그다음 정상
    -> 요청 경로 ["/hls/init.m4s", "/hls/init.m4s"], 복구 경고 0줄, 완료
    """
    engine, data, logger, _output, finished, errors, _merges = _make_engine(tmp_path, monkeypatch)
    data.base_url = HOST + "/hls/video.m3u8"
    requested: list[str] = []

    class _EndsEarly(TextResponse):
        @property
        def content(self) -> bytes:
            raise requests.exceptions.ChunkedEncodingError("IncompleteRead")

        @content.setter
        def content(self, _value: bytes) -> None:
            pass

    class Session(M3U8Session):
        def get(self, url, **kwargs) -> StreamResponse | TextResponse:
            if url.endswith("init.m4s"):
                requested.append(urlsplit(url).path)
                if len(requested) == 1:
                    return _EndsEarly()
            return super().get(url, **kwargs)

    session = Session(3)
    monkeypatch.setattr(module, "get_thread_session", lambda: session)
    data.model.start()
    engine.run()

    assert requested == ["/hls/init.m4s", "/hls/init.m4s"]
    assert INIT_RECOVERY_WARNING not in logger.warnings
    assert not errors and finished.is_set()

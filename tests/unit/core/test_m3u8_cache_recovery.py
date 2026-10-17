"""손상된 CDN 캐시의 제한된 재시도와 기존 정상 조각 보존을 검증한다."""

import threading
from urllib.parse import urlsplit

import pytest

import core.downloaders.base as base
import core.downloaders.m3u8_downloader as module
from tests.unit.core.test_m3u8_downloader_run import (
    M3U8Session,
    StreamResponse,
    _make_engine,
    _expected_output,
)

HOST = "https://ex-nlive-slitvod-streaming.navercdn.com"


def test_recovery_url_keeps_auth_and_is_idempotent():
    """복구 주소가 생성되면 인증 경로와 쿼리를 보존하고 반복해도 같아야 한다."""
    url = HOST + "/video/hdntl=test/seg.m4v?signature=test"
    expected = HOST + "/video/hdntl=test//seg.m4v?signature=test"
    assert module._cache_recovery_url(url) == expected
    assert module._cache_recovery_url(expected) == expected


@pytest.mark.parametrize(
    "url", ["https://example.org/a.m4v", HOST.replace("https", "http") + "/a.m4v"]
)
def test_other_servers_are_unchanged(url):
    """실측하지 않은 서버나 HTTP 주소는 변경되지 않아야 한다."""
    assert module._cache_recovery_url(url) == url


@pytest.mark.parametrize("recover", [True, False])
def test_invalid_cache_uses_validated_retry_and_keeps_limit(tmp_path, monkeypatch, recover):
    """손상 캐시를 만나면 대체 경로를 검증하고 반복 손상은 한도에서 실패해야 한다."""
    engine, data, logger, output, finished, errors, merges = _make_engine(tmp_path, monkeypatch)
    data.base_url = HOST + "/hls/video.m3u8"
    requested = []

    class Session(M3U8Session):
        def get(self, url, **kwargs):
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

    def __init__(self, barrier: threading.Barrier):
        super().__init__(b"damaged cached payload", 0)
        self._barrier = barrier

    def iter_content(self, chunk_size=8192):
        yield from super().iter_content(chunk_size)
        self._barrier.wait(timeout=10)


def test_workers_failing_the_same_segment_together_mark_and_warn_once(tmp_path, monkeypatch):
    """여러 작업자가 같은 세그먼트의 손상 본문을 동시에 받으면 복구 표시와 경고는 한 번이어야 한다 (#375).

    작업자 4개가 seg_3(복구 대상 CDN)을 동시에 받고, 손상 본문을 다 받은 뒤 함께 구조 검사에 실패함
    -> 복구 표시는 {3}, 복구 경고 1줄, 재큐는 실패마다 하나(대기열의 seg_3 4개 · 오류 재큐 수 4)
    """
    engine, data, logger, _output, _finished, _errors, _merges = _make_engine(tmp_path, monkeypatch)
    data.base_url = HOST + "/hls/video.m3u8"
    barrier = threading.Barrier(WORKERS)

    class Session(M3U8Session):
        def get(self, url, **kwargs):
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
):
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
        def get(self, url, **kwargs):
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

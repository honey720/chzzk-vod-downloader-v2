"""손상된 CDN 캐시의 제한된 재시도와 기존 정상 조각 보존을 검증한다."""

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

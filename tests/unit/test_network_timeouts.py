"""NetworkManager 조회 요청의 타임아웃 검증 (#129).

타임아웃이 없으면 네트워크 단절 시 OS TCP 타임아웃(~47초+)까지 조회가 갇힌다.
모든 조회 호출이 REQUEST_TIMEOUT(connect·read)을 싣는지 가짜 세션으로 확인한다.
다운로드 경로(세그먼트·파트)는 기존 timeout=30이 이미 있어 이 테스트 범위 밖.
"""

import json

import pytest

import content.network as network
from content.network import NetworkManager, REQUEST_TIMEOUT
from tests.mocks.mock_http import MockResponse

COOKIES = {"NID_AUT": "REDACTED", "NID_SES": "REDACTED"}


class RecordingSession:
    """호출 kwargs를 기록하고 준비된 응답을 돌려주는 가짜 세션."""

    def __init__(self, response):
        self._response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


class BytesResponse(MockResponse):
    """content 바이트가 필요한 호출(get_aes_key)용 응답."""

    def __init__(self, content: bytes = b"key", status_code: int = 200):
        super().__init__(status_code=status_code)
        self.content = content


def _install(monkeypatch, response):
    session = RecordingSession(response)
    monkeypatch.setattr(network, "_session", session)
    return session


M3U8_PLAYLIST = "\n".join(
    [
        "#EXTM3U",
        '#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1920x1080',
        "1080/playlist.m3u8",
    ]
)


@pytest.mark.parametrize(
    ("name", "call", "response"),
    [
        (
            "get_video_info",
            lambda: NetworkManager.get_video_info("123", COOKIES),
            MockResponse(text=json.dumps({"content": {}})),
        ),
        (
            "get_clip_info",
            lambda: NetworkManager.get_clip_info("clipUID", COOKIES),
            MockResponse(text=json.dumps({"content": {}})),
        ),
        (
            "get_clip_manifest",
            lambda: NetworkManager.get_clip_manifest("clip-id", COOKIES),
            MockResponse(
                text=json.dumps({"card": {"content": {"error": {"errorCode": "X"}}}})
            ),
        ),
        (
            "get_video_m3u8_base_url",
            lambda: NetworkManager.get_video_m3u8_base_url(
                json.dumps({"media": [{"path": "http://example.invalid/master.m3u8"}]}),
                1080,
                COOKIES,
            ),
            MockResponse(text=M3U8_PLAYLIST),
        ),
    ],
)
def test_metadata_calls_carry_request_timeout(monkeypatch, name, call, response):
    """조회 호출은 모두 REQUEST_TIMEOUT을 실어야 한다."""
    session = _install(monkeypatch, response)

    call()

    assert session.calls, name
    for _url, kwargs in session.calls:
        assert kwargs.get("timeout") == REQUEST_TIMEOUT, name


@pytest.mark.parametrize("method", ["get_video_dash_manifest", "get_video_sea_manifest"])
def test_manifest_calls_carry_request_timeout(monkeypatch, load_mock_response, method):
    """매니페스트 조회도 REQUEST_TIMEOUT을 실어야 한다 (실픽스처 XML 파싱 통과)."""
    xml = load_mock_response("dash_manifest.xml")
    session = _install(monkeypatch, MockResponse(text=xml))

    getattr(NetworkManager, method)("video-id", "in-key", COOKIES)

    assert session.calls
    for _url, kwargs in session.calls:
        assert kwargs.get("timeout") == REQUEST_TIMEOUT


def test_aes_key_keeps_dedicated_timeout(monkeypatch):
    """복호화 키 요청의 기존 timeout=30은 유지된다 (#57 경로, 이 이슈 범위 밖)."""
    session = _install(monkeypatch, BytesResponse())

    NetworkManager.get_aes_key("http://example.invalid/key", COOKIES)

    assert session.calls
    _url, kwargs = session.calls[0]
    assert kwargs.get("timeout") == 30


def test_request_timeout_is_connect_read_pair():
    """connect·read를 모두 지정한다 — 단일 값이면 connect만 걸리는 실수 방지."""
    assert isinstance(REQUEST_TIMEOUT, tuple)
    assert len(REQUEST_TIMEOUT) == 2
    connect, read = REQUEST_TIMEOUT
    assert 0 < connect <= read

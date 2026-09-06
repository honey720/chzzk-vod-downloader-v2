"""서버가 알려준 주소로 인증 쿠키를 보내는 두 지점의 신뢰 검사 게이트.

두 지점: 복호화 키(`get_aes_key` — 플레이리스트의 #EXT-X-KEY URI)와 m3u8 마스터
플레이리스트(`get_video_m3u8_base_url` — playback JSON의 path). 둘 다 스트림 응답에서
온 주소에 쿠키를 싣는다.

거부 케이스는 **세션을 스텁하지 않는다.** 검사가 요청 전에 거부하면 아무 요청도
없고, 검사를 지우면 실제 요청이 나가려다 네트워크 가드(#275)에 걸린다 — 가드가
증인이다(pytest.raises가 다른 예외를 받고, 테스트 끝 단언도 실패한다).

리다이렉트 케이스는 응답 대역으로 302를 만들어 "두 번째 주소에 요청(쿠키)이
갔는가"를 호출 기록으로 잰다 — 예외가 났다는 것만으로는 부족하다.
"""

import json

import pytest
import requests

import app.network as network
from app.network import NetworkManager
from tests.mocks.mock_http import MockResponse

COOKIES = {"NID_AUT": "REDACTED", "NID_SES": "REDACTED"}
KEY_URI = "https://api.chzzk.naver.com/service/v1/encryption/videos/VID/aes_key"
MASTER = "https://vod-example.invalid/glive/master.m3u8"
PLAYLIST = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1920x1080\n1080/playlist.m3u8\n"


def _playback_json(path: str) -> str:
    return json.dumps({"media": [{"path": path}]})


class RedirectingSession:
    """응답 대역 — 첫 주소에는 302(Location)로 답하고, 그 뒤 주소에는 본문으로 답한다.

    `calls`에 (url, cookies) 순서를 남긴다. 리다이렉트 게이트는 이 기록으로
    "두 번째 주소에 쿠키가 갔는가"를 잰다.
    """

    def __init__(self, first_url: str, location: str, final: MockResponse):
        self.first_url = first_url
        self.location = location
        self.final = final
        self.calls: list[tuple[str, dict | None]] = []
        #: 홉별 timeout — 시간 제한은 **kwargs로 흘러가므로 한 홉에서 빠져도 아무것도
        #: 실패하지 않고 앱이 멈춘다. 그래서 따로 기록해 "모든 홉이 받았다"를 단언한다
        self.timeouts: list = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs.get("cookies")))
        self.timeouts.append(kwargs.get("timeout"))
        if url == self.first_url:
            resp = MockResponse(status_code=302)
            resp.headers = {"Location": self.location}
            return resp
        return self.final


class BytesResponse(MockResponse):
    """키 응답 대역 — content 바이트."""

    def __init__(self, content: bytes = b"0123456789abcdef"):
        super().__init__(status_code=200)
        self.content = content
        self.headers = {}


# ============ 거부 — 세션 스텁 없음, 네트워크 가드가 증인 ============


class TestKeyRequestRejectsUntrustedAddress:
    """복호화 키: https가 아니거나 API 호스트가 아니면 요청 없이 거부한다."""

    def test_http_key_uri_is_rejected_before_any_request(self):
        with pytest.raises(requests.exceptions.InvalidURL):
            NetworkManager.get_aes_key(
                "http://api.chzzk.naver.com/service/v1/encryption/videos/VID/aes_key", COOKIES
            )

    def test_other_https_host_is_rejected_before_any_request(self):
        with pytest.raises(requests.exceptions.InvalidURL):
            NetworkManager.get_aes_key("https://keys.example.invalid/aes_key", COOKIES)

    def test_rejection_message_carries_no_path_or_query(self):
        """예외 메시지에는 스킴·호스트만 — 경로·질의(토큰이 섞일 수 있다)는 싣지 않는다."""
        with pytest.raises(requests.exceptions.InvalidURL) as info:
            NetworkManager.get_aes_key("https://keys.example.invalid/aes_key?token=SECRET", COOKIES)
        assert "SECRET" not in str(info.value) and "/aes_key" not in str(info.value)


class TestMasterPlaylistRequestRejectsHttp:
    """m3u8 마스터: https만 강제한다(호스트는 잠그지 않는다)."""

    def test_http_path_is_rejected_before_any_request(self):
        with pytest.raises(requests.exceptions.InvalidURL):
            NetworkManager.get_video_m3u8_base_url(
                _playback_json("http://vod-example.invalid/master.m3u8"), 1080, COOKIES
            )


# ============ 허용 — 세션 대역 ============


class TestTrustedAddressesStillWork:
    def test_key_from_api_host_is_fetched_with_cookies(self, monkeypatch):
        session = RedirectingSession(first_url="<none>", location="", final=BytesResponse())
        monkeypatch.setattr(network, "_session", session)

        key = NetworkManager.get_aes_key(KEY_URI, COOKIES)

        assert key == b"0123456789abcdef"
        assert session.calls == [(KEY_URI, COOKIES)]

    def test_any_https_host_is_allowed_for_the_master_playlist(self, monkeypatch):
        session = RedirectingSession(
            first_url="<none>", location="", final=MockResponse(text=PLAYLIST)
        )
        monkeypatch.setattr(network, "_session", session)

        base_url = NetworkManager.get_video_m3u8_base_url(_playback_json(MASTER), 1080, COOKIES)

        assert base_url == "https://vod-example.invalid/glive/1080/playlist.m3u8"
        assert session.calls == [(MASTER, COOKIES)]


# ============ 리다이렉트 — 두 번째 주소에 쿠키가 갔는가 ============


class TestRedirectHopsAreCheckedBeforeCookiesAreSent:
    """허용된 첫 주소가 302로 다른 곳을 가리켜도, 그 홉이 검사를 못 넘으면 요청 자체가 없다."""

    def test_key_redirect_to_another_host_gets_no_request_and_no_cookies(self, monkeypatch):
        leak_target = "https://keys.example.invalid/aes_key"
        session = RedirectingSession(first_url=KEY_URI, location=leak_target, final=BytesResponse())
        monkeypatch.setattr(network, "_session", session)

        with pytest.raises(requests.exceptions.InvalidURL):
            NetworkManager.get_aes_key(KEY_URI, COOKIES)

        # 두 번째 주소로는 요청이 한 번도 나가지 않았다 — 쿠키가 갈 자리가 없다
        assert session.calls == [(KEY_URI, COOKIES)]
        assert all(url != leak_target for url, _ in session.calls)

    def test_master_playlist_redirect_to_http_gets_no_request_and_no_cookies(self, monkeypatch):
        leak_target = "http://vod-example.invalid/glive/master.m3u8"
        session = RedirectingSession(
            first_url=MASTER, location=leak_target, final=MockResponse(text=PLAYLIST)
        )
        monkeypatch.setattr(network, "_session", session)

        with pytest.raises(requests.exceptions.InvalidURL):
            NetworkManager.get_video_m3u8_base_url(_playback_json(MASTER), 1080, COOKIES)

        assert session.calls == [(MASTER, COOKIES)]
        assert all(url != leak_target for url, _ in session.calls)

    def test_key_redirect_within_the_api_host_is_followed_with_cookies(self, monkeypatch):
        """검사를 넘는 홉(같은 호스트·https)은 따라간다 — 쿠키는 그 홉에도 실린다."""
        hop = "https://api.chzzk.naver.com/service/v1/encryption/videos/VID/aes_key?rev=2"
        session = RedirectingSession(first_url=KEY_URI, location=hop, final=BytesResponse())
        monkeypatch.setattr(network, "_session", session)

        key = NetworkManager.get_aes_key(KEY_URI, COOKIES)

        assert key == b"0123456789abcdef"
        assert session.calls == [(KEY_URI, COOKIES), (hop, COOKIES)]
        # 모든 홉이 시간 제한을 받았다 — 어느 홉에서든 빠지면 앱이 조용히 멈춘다
        assert session.timeouts == [30, 30]

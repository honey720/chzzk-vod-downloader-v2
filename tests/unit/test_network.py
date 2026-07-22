"""content.network의 URL 파싱·DASH 매니페스트 파싱 테스트.

- URL 파싱(TestExtractContentNo): #33에서 허용 범위를 넓힌 **요구 동작 검증** 테스트다.
- DASH 매니페스트(TestGetVideoDashManifest): HTTP 요청 구성·core 파서 위임 검증이다.
  파싱 자체의 박제 테스트는 tests/unit/core/test_dash.py로 이전했다 (#51).
"""

import pytest

import content.network as network
from content.network import NetworkManager
from core.api.dash import parse_dash_manifest
from core.api.url_parser import extract_content_no
from tests.mocks.mock_http import MockResponse


class TestExtractContentNo:
    """extract_content_no의 요구 동작 검증 (#33).

    구현이 core/api/url_parser.py로 이동해 import 경로만 갱신 (#50).

    브라우저 주소창을 그대로 복사한 URL 변형(쿼리스트링·후행 슬래시·www)은 허용하고,
    live URL·다른 도메인·형식이 깨진 URL은 거부해야 한다.
    """

    @pytest.mark.parametrize(
        ("vod_url", "expected"),
        [
            # 기본 형태: video / clips, 스킴 유무
            ("https://chzzk.naver.com/video/1510760", ("video", "1510760")),
            ("http://chzzk.naver.com/video/1510760", ("video", "1510760")),
            ("chzzk.naver.com/video/1510760", ("video", "1510760")),
            ("https://chzzk.naver.com/clips/abcDEF12345", ("clips", "abcDEF12345")),
            ("chzzk.naver.com/clips/abcDEF12345", ("clips", "abcDEF12345")),
            # content_no는 \w+ 이므로 언더스코어는 허용된다
            ("https://chzzk.naver.com/clips/abc_123", ("clips", "abc_123")),
            # 쿼리스트링은 무시하고 파싱한다 (#33)
            ("https://chzzk.naver.com/video/1510760?t=120", ("video", "1510760")),
            ("https://chzzk.naver.com/video/1510760?t=120&utm_source=share", ("video", "1510760")),
            ("https://chzzk.naver.com/clips/abcDEF12345?sharePlatform=web", ("clips", "abcDEF12345")),
            ("chzzk.naver.com/video/1510760?t=120", ("video", "1510760")),  # 스킴 생략 + 쿼리
            # 후행 슬래시를 허용한다 (#33)
            ("https://chzzk.naver.com/video/1510760/", ("video", "1510760")),
            ("https://chzzk.naver.com/video/1510760/?t=120", ("video", "1510760")),  # 슬래시+쿼리
            # www 서브도메인을 허용한다 (#33)
            ("https://www.chzzk.naver.com/video/1510760", ("video", "1510760")),
            ("https://www.chzzk.naver.com/clips/abcDEF12345/", ("clips", "abcDEF12345")),
            # 계속 거부: live URL (녹화 기능 전까지 미지원)
            ("https://chzzk.naver.com/live/channelid00", (None, None)),
            # 계속 거부: 다른 도메인·스킴
            ("https://youtube.com/video/1510760", (None, None)),
            ("https://mchzzk.naver.com/video/1510760", (None, None)),  # 유사 도메인
            ("ftp://chzzk.naver.com/video/1510760", (None, None)),
            # 계속 거부: 형식이 깨진 URL
            ("https://chzzk.naver.com/video/", (None, None)),  # content_no 누락
            ("https://chzzk.naver.com/clips/abc-def", (None, None)),  # 하이픈은 \w 미포함
            ("https://chzzk.naver.com/video/1510760//", (None, None)),  # 이중 슬래시
            ("", (None, None)),  # 빈 문자열
        ],
    )
    def test_extract_content_no(self, vod_url: str, expected: tuple):
        """URL별 (type, content_no) 추출 결과가 요구 동작과 일치해야 한다."""
        assert extract_content_no(vod_url) == expected

    def test_network_manager_delegates_to_core(self):
        """NetworkManager.extract_content_no 시그니처가 유지되고 core 함수에 위임한다 (#50)."""
        assert NetworkManager.extract_content_no(
            "https://chzzk.naver.com/video/1510760"
        ) == ("video", "1510760")


class TestGetVideoDashManifest:
    """NetworkManager.get_video_dash_manifest의 HTTP 경로(요청 구성·위임) 검증.

    XML 파싱 자체는 core/api/dash.py로 이동해 tests/unit/core/test_dash.py에서
    픽스처로 검증한다 (#51). 여기서는 요청 URL·헤더와 core 파서 위임만 확인한다.
    """

    def test_requests_manifest_and_delegates_to_core_parser(
        self, monkeypatch, load_mock_response
    ):
        """요청 URL·헤더 구성을 고정하고, 응답 본문이 core 파서 결과로 반환되는지 확인한다."""
        calls = []
        xml_text = load_mock_response("dash_manifest.xml")

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return MockResponse(text=xml_text)

        monkeypatch.setattr(network._session, "get", fake_get)

        sorted_reps, auto_resolution, auto_base_url = NetworkManager.get_video_dash_manifest(
            "test-video-id", "test-in-key"
        )

        # 요청 URL·헤더 구성도 현재 동작 그대로 고정 (실호출 없음)
        assert calls == [
            (
                "https://apis.naver.com/neonplayer/vodplay/v2/playback/test-video-id"
                "?key=test-in-key",
                {"headers": {"Accept": "application/dash+xml"}},
            )
        ]
        # 반환값은 core 파서에 응답 본문을 그대로 넘긴 결과와 일치해야 한다
        assert (sorted_reps, auto_resolution, auto_base_url) == parse_dash_manifest(xml_text)

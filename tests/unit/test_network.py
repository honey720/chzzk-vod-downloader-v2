"""content.network의 URL 파싱·DASH 매니페스트 파싱 테스트.

- URL 파싱(TestExtractContentNo): #33에서 허용 범위를 넓힌 **요구 동작 검증** 테스트다.
- DASH 매니페스트 파싱(TestGetVideoDashManifest): #27의 현재 동작 보존(박제) 테스트다.
"""

import pytest

import content.network as network
from content.network import NetworkManager
from tests.mocks.mock_http import MockResponse


class TestExtractContentNo:
    """NetworkManager.extract_content_no의 요구 동작 검증 (#33).

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
        assert NetworkManager.extract_content_no(vod_url) == expected


class TestGetVideoDashManifest:
    """NetworkManager.get_video_dash_manifest의 XML 파싱 결과 박제 (HTTP는 mock)."""

    def _patch_get(self, monkeypatch: pytest.MonkeyPatch, xml_text: str) -> list:
        """공유 세션(network._session)의 get을 목으로 바꾸고 호출 기록 리스트를 반환한다."""
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return MockResponse(text=xml_text)

        monkeypatch.setattr(network._session, "get", fake_get)
        return calls

    def test_parses_sorts_and_skips_hls(self, monkeypatch, load_mock_response):
        """해상도 오름차순 정렬, min(width, height) 계산, '/hls/' 항목 스킵을 고정한다."""
        calls = self._patch_get(monkeypatch, load_mock_response("dash_manifest.xml"))

        sorted_reps, auto_resolution, auto_base_url = NetworkManager.get_video_dash_manifest(
            "test-video-id", "test-in-key"
        )

        assert sorted_reps == [
            [480, "https://vod-example.invalid/chzzk/video_480p.mp4"],
            [720, "https://vod-example.invalid/chzzk/video_720p.mp4"],
            [1080, "https://vod-example.invalid/chzzk/video_1080p.mp4"],
        ]
        # auto는 목록 중 가장 높은 해상도
        assert auto_resolution == 1080
        assert auto_base_url == "https://vod-example.invalid/chzzk/video_1080p.mp4"

        # 요청 URL·헤더 구성도 현재 동작 그대로 고정 (실호출 없음)
        assert calls == [
            (
                "https://apis.naver.com/neonplayer/vodplay/v2/playback/test-video-id"
                "?key=test-in-key",
                {"headers": {"Accept": "application/dash+xml"}},
            )
        ]

    def test_portrait_single_representation(self, monkeypatch, load_mock_response):
        """세로 영상(720x1280) 단일 항목: 해상도는 min(width, height)=720으로 계산된다."""
        self._patch_get(monkeypatch, load_mock_response("dash_manifest_portrait.xml"))

        sorted_reps, auto_resolution, auto_base_url = NetworkManager.get_video_dash_manifest(
            "portrait-video-id", "portrait-in-key"
        )

        assert sorted_reps == [[720, "https://vod-example.invalid/chzzk/portrait_720.mp4"]]
        assert auto_resolution == 720
        assert auto_base_url == "https://vod-example.invalid/chzzk/portrait_720.mp4"

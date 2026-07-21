"""content.network의 URL 파싱·DASH 매니페스트 파싱 박제 테스트.

목적은 "올바른 동작" 검증이 아니라 현재 동작의 보존이다 (#27).
현재 결과가 이상해 보여도 그 결과 그대로를 기대값으로 삼는다.
"""

import pytest

import content.network as network
from content.network import NetworkManager
from tests.mocks.mock_http import MockResponse


class TestExtractContentNo:
    """NetworkManager.extract_content_no의 (type, content_no) 결과 박제."""

    @pytest.mark.parametrize(
        ("vod_url", "expected"),
        [
            # 정상 케이스: video / clips, 스킴 유무
            ("https://chzzk.naver.com/video/1510760", ("video", "1510760")),
            ("http://chzzk.naver.com/video/1510760", ("video", "1510760")),
            ("chzzk.naver.com/video/1510760", ("video", "1510760")),
            ("https://chzzk.naver.com/clips/abcDEF12345", ("clips", "abcDEF12345")),
            ("chzzk.naver.com/clips/abcDEF12345", ("clips", "abcDEF12345")),
            # content_no는 \w+ 이므로 언더스코어는 허용된다
            ("https://chzzk.naver.com/clips/abc_123", ("clips", "abc_123")),
            # 경계 케이스: 현재 구현은 fullmatch라서 아래는 전부 (None, None)
            ("https://chzzk.naver.com/video/1510760?t=120", (None, None)),  # 쿼리스트링
            ("https://chzzk.naver.com/video/1510760/", (None, None)),  # 후행 슬래시
            ("https://chzzk.naver.com/clips/abc-def", (None, None)),  # 하이픈은 \w 미포함
            ("https://chzzk.naver.com/live/channelid00", (None, None)),  # live는 미지원
            ("https://chzzk.naver.com/video/", (None, None)),  # content_no 누락
            ("https://www.chzzk.naver.com/video/1510760", (None, None)),  # www 서브도메인
            ("https://youtube.com/video/1510760", (None, None)),  # 다른 도메인
            ("ftp://chzzk.naver.com/video/1510760", (None, None)),  # 다른 스킴
            ("", (None, None)),  # 빈 문자열
        ],
    )
    def test_extract_content_no(self, vod_url: str, expected: tuple):
        """URL별 (type, content_no) 추출 결과를 현재 동작 그대로 고정한다."""
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

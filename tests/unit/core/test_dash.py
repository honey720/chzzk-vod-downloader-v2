"""core.api.dash의 DASH 매니페스트 파싱 박제 테스트.

tests/unit/test_network.py의 HTTP mock 기반 테스트를 core 파서 직접 호출로 전환 (#51).
파싱이 순수 함수가 되어 HTTP mock 없이 픽스처 XML만으로 검증한다. 기대값 무변경 (#27, #38).
"""

import pytest

from core.api.dash import parse_dash_manifest


class TestParseDashManifest:
    """parse_dash_manifest의 XML 파싱 결과 박제."""

    def test_parses_sorts_and_skips_hls(self, load_mock_response):
        """해상도 오름차순 정렬, min(width, height) 계산, '/hls/' 항목 스킵을 고정한다."""
        sorted_reps, auto_resolution, auto_base_url = parse_dash_manifest(
            load_mock_response("dash_manifest.xml")
        )

        assert sorted_reps == [
            [480, "https://vod-example.invalid/chzzk/video_480p.mp4"],
            [720, "https://vod-example.invalid/chzzk/video_720p.mp4"],
            [1080, "https://vod-example.invalid/chzzk/video_1080p.mp4"],
        ]
        # auto는 목록 중 가장 높은 해상도
        assert auto_resolution == 1080
        assert auto_base_url == "https://vod-example.invalid/chzzk/video_1080p.mp4"

    def test_skips_audio_only_representation(self, load_mock_response):
        """width/height 없는 오디오 전용 Representation은 건너뛰어야 한다 (#38).

        픽스처는 videoNo 14158884의 실제 매니페스트 박제본이다: video/mp4 3종(1080/720/144)
        + video/mp2t 3종(BaseURL이 '/hls/'로 끝나 기존 로직이 스킵) + audio/mp4 1종
        (width/height 없음 — 기존에는 여기서 TypeError로 전체 파싱이 실패했다).
        """
        sorted_reps, auto_resolution, auto_base_url = parse_dash_manifest(
            load_mock_response("dash_manifest_audio_only_14158884.xml")
        )

        # 영상 3종만 남는다: 오디오 전용(m4a)·hls 항목은 제외, 해상도 오름차순
        assert [rep[0] for rep in sorted_reps] == [144, 720, 1080]
        assert auto_resolution == 1080
        for _, base_url in sorted_reps:
            assert base_url.endswith(".mp4?_lsu_sa_=REDACTED")
            assert "/hls/" not in base_url
            assert ".m4a" not in base_url
        assert auto_base_url == sorted_reps[-1][1]

    @pytest.mark.parametrize(
        "fixture_name",
        ["dash_manifest_sea_13714380.xml", "dash_manifest_sea_14283698.xml"],
    )
    def test_sea_encrypted_manifest_returns_empty_without_crash(
        self, load_mock_response, fixture_name
    ):
        """AES(SEA) 암호화 매니페스트에서 크래시 없이 빈 결과를 반환해야 한다 (#55).

        픽스처는 멤버십 VOD 13714380·14283698의 실제 매니페스트 박제본이다(서명 REDACTED):
        비디오 Representation 3종은 ContentProtection(urn:mpeg:dash:sea:2012)만 있고
        BaseURL이 없다 — 기존에는 여기서 AttributeError('NoneType' .text)로 전체가 죽었다.
        오디오 1종은 BaseURL이 있으나 width/height가 없어 #38 규칙으로 제외된다.
        결과적으로 사용 가능한 항목이 없으므로 ([], None, None)을 반환한다 (2차 방어선 —
        1차 방어는 worker의 encryptionType 조기 감지).
        """
        sorted_reps, auto_resolution, auto_base_url = parse_dash_manifest(
            load_mock_response(fixture_name)
        )

        assert sorted_reps == []
        assert auto_resolution is None
        assert auto_base_url is None

    def test_portrait_single_representation(self, load_mock_response):
        """세로 영상(720x1280) 단일 항목: 해상도는 min(width, height)=720으로 계산된다."""
        sorted_reps, auto_resolution, auto_base_url = parse_dash_manifest(
            load_mock_response("dash_manifest_portrait.xml")
        )

        assert sorted_reps == [[720, "https://vod-example.invalid/chzzk/portrait_720.mp4"]]
        assert auto_resolution == 720
        assert auto_base_url == "https://vod-example.invalid/chzzk/portrait_720.mp4"

"""core/models/content.py 컨텐츠 모델 단위 테스트 (#61).

ContentType 값 매핑, Content 기본값·id 생성, VideoInfo 불변성을 검증한다.
"""

import dataclasses

import pytest

from core.models.content import Content, ContentType, VideoInfo


class TestContentType:
    def test_values_match_current_strings(self):
        """열거형 값은 현행 코드가 쓰는 문자열('video'/'m3u8'/'clip'/'live')과 같아야 한다."""
        assert ContentType.CHZZK_VIDEO.value == "video"
        assert ContentType.CHZZK_VIDEO_M3U8.value == "m3u8"
        assert ContentType.CHZZK_CLIP.value == "clip"
        assert ContentType.CHZZK_LIVE.value == "live"

    def test_lookup_by_value(self):
        """기존 문자열로 역변환(ContentType(value))이 가능해야 한다."""
        assert ContentType("video") is ContentType.CHZZK_VIDEO
        assert ContentType("m3u8") is ContentType.CHZZK_VIDEO_M3U8
        assert ContentType("clip") is ContentType.CHZZK_CLIP

    def test_unknown_value_raises(self):
        """정의되지 않은 타입 문자열은 ValueError — 에러를 타입 값으로 표현하지 않는다."""
        with pytest.raises(ValueError):
            ContentType("error")


class TestContent:
    def test_minimal_construction_with_defaults(self):
        """필수 필드만으로 생성되고 나머지는 기본값이어야 한다."""
        content = Content(content_type=ContentType.CHZZK_VIDEO, url="https://chzzk.naver.com/video/1")
        assert content.content_type is ContentType.CHZZK_VIDEO
        assert content.url == "https://chzzk.naver.com/video/1"
        assert content.title is None
        assert content.resolution is None
        assert content.encryption_type is None

    def test_id_is_unique_per_instance(self):
        """id는 인스턴스마다 자동 생성되는 고유값이어야 한다."""
        a = Content(content_type=ContentType.CHZZK_CLIP, url="u")
        b = Content(content_type=ContentType.CHZZK_CLIP, url="u")
        assert a.id and b.id
        assert a.id != b.id

    def test_is_single_dataclass_without_subclasses(self):
        """SPEC §4.1: 서브클래스 없는 단일 데이터클래스여야 한다."""
        assert dataclasses.is_dataclass(Content)
        assert Content.__subclasses__() == []


class TestVideoInfo:
    def _make(self) -> VideoInfo:
        return VideoInfo(
            video_id="vid",
            in_key="key",
            adult=False,
            vod_status="ABR_HLS",
            live_rewind_playback_json=None,
            membership_benefit_type=None,
            encryption_type=None,
            metadata={"duration": 1},
        )

    def test_field_access(self):
        """tuple 위치가 아닌 이름으로 필드에 접근할 수 있어야 한다."""
        info = self._make()
        assert info.video_id == "vid"
        assert info.in_key == "key"
        assert info.metadata["duration"] == 1

    def test_result_object_is_immutable(self):
        """조회 결과는 값 객체이므로 필드 재할당이 금지된다(frozen)."""
        info = self._make()
        with pytest.raises(dataclasses.FrozenInstanceError):
            info.video_id = "other"

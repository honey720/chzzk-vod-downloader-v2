"""playback 정보의 트랙 읽기와 마스터 플레이리스트의 변형 짝짓기 (#318)."""

import json

import pytest

from core.api.playback_tracks import (
    STREAM_NOT_FOUND,
    PlaybackTrack,
    StreamSelectionError,
    playback_tracks,
    select_variant,
    select_variant_by_height,
    track_for_resolution,
)
from core.api.representations import track_resolution


def _track(
    name: str | None,
    width: int,
    height: int,
    *,
    fps: float | None = 30.0,
    video: int = 1000000,
    audio: int = 192000,
) -> PlaybackTrack:
    return PlaybackTrack(
        name=name,
        resolution=track_resolution(name, width, height),
        width=width,
        height=height,
        frame_rate=fps,
        video_bitrate=video,
        audio_bitrate=audio,
    )


def _master(*variants: tuple[str, str]) -> str:
    """(STREAM-INF 속성, 주소) 목록으로 마스터 플레이리스트를 만든다."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
    for attributes, uri in variants:
        lines += [f"#EXT-X-STREAM-INF:{attributes}", uri]
    return "\n".join(lines) + "\n"


# ================================================================ 해상도 값


@pytest.mark.parametrize(
    ("name", "width", "height", "expected"),
    [
        ("1080p", 720, 1280, 1080),  # 세로 방송의 원본 트랙 — 이름의 숫자
        ("720p", 720, 1280, 720),
        ("720P", 1280, 720, 720),  # 대문자
        (" 480p ", 852, 480, 480),  # 앞뒤 공백
        ("1080", 1920, 1080, 1080),  # p 없는 숫자(DASH의 resolution 라벨)
        (None, 720, 1280, 720),  # 이름 없음 — 짧은 변
        ("source", 1920, 1080, 1080),  # 숫자p가 아님 — 짧은 변
        ("1080p60", 1920, 1080, 1080),  # 뒤에 다른 글자 — 짧은 변
        ("0p", 1280, 720, 720),  # 0 — 짧은 변
        (1080, 1280, 720, 720),  # 문자열이 아님 — 짧은 변
    ],
)
def test_track_resolution_follows_the_track_name_or_the_short_side(name, width, height, expected):
    """track_resolution은 이름이 `<숫자>p`면 그 숫자를, 아니면 짧은 변을 돌려줘야 한다.

    주석의 경우마다 (이름, 가로, 세로)
    -> 해상도
    """
    assert track_resolution(name, width, height) == expected


# ================================================================ 트랙 읽기


def test_playback_tracks_reads_each_track_in_order():
    """playback_tracks는 첫 media의 encodingTrack을 등장 순서대로 읽어야 한다.

    트랙 둘 — "720p" 720x1280 60fps 3000000+192000, 이름·fps·비트레이트 없는 1280x720
    -> PlaybackTrack 둘. 둘째는 이름 None · 해상도 720(짧은 변) · fps None · 비트레이트 0
    """
    text = json.dumps(
        {
            "media": [
                {
                    "encodingTrack": [
                        {
                            "encodingTrackId": "720p",
                            "videoWidth": 720,
                            "videoHeight": 1280,
                            "videoFrameRate": "60.0",
                            "videoBitRate": 3000000,
                            "audioBitRate": 192000,
                        },
                        {"videoWidth": 1280, "videoHeight": 720},
                    ]
                }
            ]
        }
    )

    assert playback_tracks(text) == [
        PlaybackTrack("720p", 720, 720, 1280, 60.0, 3000000, 192000),
        PlaybackTrack(None, 720, 1280, 720, None, 0, 0),
    ]


def test_playback_tracks_is_empty_without_encoding_tracks():
    """encodingTrack이 없는 playback 정보에서는 빈 목록이어야 한다.

    {"media": [{"path": ...}]}
    -> []
    """
    assert playback_tracks('{"media": [{"path": "https://example.invalid/master.m3u8"}]}') == []


def test_track_for_resolution_keeps_the_higher_video_bitrate_then_the_first():
    """track_for_resolution은 해상도가 같은 트랙 중 영상 비트레이트가 높은 것, 같으면 먼저 나온 것을 골라야 한다.

    1080 셋(영상 4000000 · 6000000 · 6000000), 720 하나
    -> 1080은 둘째 트랙, 720은 넷째 트랙, 480은 None
    """
    tracks = [
        _track("1080p", 1920, 1080, video=4000000),
        _track("1080p", 1920, 1080, video=6000000, fps=60.0),
        _track("1080p", 1920, 1080, video=6000000, fps=30.0),
        _track("720p", 1280, 720),
    ]

    assert track_for_resolution(tracks, 1080) is tracks[1]
    assert track_for_resolution(tracks, 720) is tracks[3]
    assert track_for_resolution(tracks, 480) is None


# ================================================================ 변형 고르기


def test_select_variant_matches_by_size_when_one_variant_has_it():
    """크기가 같은 변형이 하나면 프레임률·BANDWIDTH가 달라도 그 변형을 골라야 한다.

    트랙 1280x720 30fps 1192000, 변형 1280x720(60fps · BANDWIDTH 9) · 1920x1080
    -> "a.m3u8"
    """
    master = _master(
        ("BANDWIDTH=9,RESOLUTION=1280x720,FRAME-RATE=60.00", "a.m3u8"),
        ("BANDWIDTH=9,RESOLUTION=1920x1080,FRAME-RATE=60.00", "b.m3u8"),
    )

    assert select_variant(master, _track("720p", 1280, 720)) == "a.m3u8"


def test_select_variant_compares_the_width_as_well_as_the_height():
    """세로가 같고 가로가 다른 변형은 같은 크기로 보지 않아야 한다.

    트랙 1280x720, 변형 960x720 · 1280x720(프레임률 · BANDWIDTH 선언 없음)
    -> 둘째 변형의 주소
    """
    master = _master(
        ("RESOLUTION=960x720", "narrow.m3u8"),
        ("RESOLUTION=1280x720", "wide.m3u8"),
    )

    assert select_variant(master, _track("720p", 1280, 720)) == "wide.m3u8"


def test_select_variant_does_not_match_a_swapped_size():
    """가로와 세로가 뒤바뀐 변형은 같은 크기로 보지 않아야 한다.

    트랙 720x1280, 변형 1280x720 하나
    -> StreamSelectionError, message_key == STREAM_NOT_FOUND
    """
    master = _master(("BANDWIDTH=1192000,RESOLUTION=1280x720,FRAME-RATE=30.00", "a.m3u8"))

    with pytest.raises(StreamSelectionError) as caught:
        select_variant(master, _track("720p", 720, 1280))

    assert caught.value.message_key == STREAM_NOT_FOUND


def test_select_variant_narrows_same_size_variants_by_frame_rate():
    """크기가 같은 변형이 둘이면 프레임률이 같은 것을 골라야 한다.

    변형 720x1280 60fps · 720x1280 30fps(BANDWIDTH는 둘 다 2692000), 트랙 720x1280 30fps
    ("30.0"과 "30.00"은 같은 값)
    -> 둘째 변형의 주소
    """
    master = _master(
        ("BANDWIDTH=2692000,RESOLUTION=720x1280,FRAME-RATE=60.00", "re-encoded.m3u8"),
        ("BANDWIDTH=2692000,RESOLUTION=720x1280,FRAME-RATE=30.00", "source.m3u8"),
    )

    track = _track("1080p", 720, 1280, fps=30.0, video=2500000, audio=192000)

    assert select_variant(master, track) == "source.m3u8"


def test_select_variant_narrows_same_size_and_frame_rate_by_bandwidth():
    """크기와 프레임률이 같은 변형이 둘이면 BANDWIDTH가 영상 + 오디오 비트레이트인 것을 골라야 한다.

    변형 720x1280 30fps BANDWIDTH 3192000 · 2692000, 트랙 영상 2500000 + 오디오 192000
    -> 둘째 변형의 주소
    """
    master = _master(
        ("BANDWIDTH=3192000,RESOLUTION=720x1280,FRAME-RATE=30.00", "high.m3u8"),
        ("BANDWIDTH=2692000,RESOLUTION=720x1280,FRAME-RATE=30.00", "low.m3u8"),
    )

    track = _track("1080p", 720, 1280, fps=30.0, video=2500000, audio=192000)

    assert select_variant(master, track) == "low.m3u8"


def test_select_variant_skips_the_frame_rate_when_a_variant_does_not_declare_it():
    """크기가 같은 변형 중 FRAME-RATE를 선언하지 않은 것이 있으면 프레임률을 건너뛰고 BANDWIDTH로 골라야 한다.

    변형 720x1280 BANDWIDTH 3192000(FRAME-RATE 없음) · 2692000(30fps), 트랙 60fps 3000000 + 192000
    -> 첫째 변형의 주소
    """
    master = _master(
        ("BANDWIDTH=3192000,RESOLUTION=720x1280", "first.m3u8"),
        ("BANDWIDTH=2692000,RESOLUTION=720x1280,FRAME-RATE=30.00", "second.m3u8"),
    )

    track = _track("720p", 720, 1280, fps=60.0, video=3000000, audio=192000)

    assert select_variant(master, track) == "first.m3u8"


@pytest.mark.parametrize(
    "variants",
    [
        # 크기 · 프레임률 · BANDWIDTH가 모두 같은 변형 둘
        [
            ("BANDWIDTH=2692000,RESOLUTION=720x1280,FRAME-RATE=30.00", "a.m3u8"),
            ("BANDWIDTH=2692000,RESOLUTION=720x1280,FRAME-RATE=30.00", "b.m3u8"),
        ],
        # 크기와 프레임률이 같은 변형 둘 — BANDWIDTH가 맞는 것이 없다
        [
            ("BANDWIDTH=1,RESOLUTION=720x1280,FRAME-RATE=30.00", "a.m3u8"),
            ("BANDWIDTH=2,RESOLUTION=720x1280,FRAME-RATE=30.00", "b.m3u8"),
        ],
        # 크기가 같은 변형 둘 — 프레임률이 맞는 것이 없다
        [
            ("BANDWIDTH=2692000,RESOLUTION=720x1280,FRAME-RATE=60.00", "a.m3u8"),
            ("BANDWIDTH=2692000,RESOLUTION=720x1280,FRAME-RATE=24.00", "b.m3u8"),
        ],
        # 크기가 같은 변형 둘 — 좁힐 값을 아무것도 선언하지 않았다
        [
            ('CODECS="avc1",RESOLUTION=720x1280', "a.m3u8"),
            ('CODECS="avc1",RESOLUTION=720x1280', "b.m3u8"),
        ],
        # 변형이 없다
        [],
    ],
    ids=["identical", "no-bandwidth-match", "no-frame-rate-match", "nothing-declared", "empty"],
)
def test_select_variant_fails_when_the_variant_is_not_determined(variants):
    """변형이 하나로 정해지지 않으면 아무거나 고르지 않고 키 기반 오류로 실패해야 한다.

    주석의 경우마다 마스터 플레이리스트, 트랙 720x1280 30fps 2500000 + 192000
    -> StreamSelectionError, message_key == STREAM_NOT_FOUND
    """
    track = _track("1080p", 720, 1280, fps=30.0, video=2500000, audio=192000)

    with pytest.raises(StreamSelectionError) as caught:
        select_variant(_master(*variants), track)

    assert caught.value.message_key == STREAM_NOT_FOUND


def test_stream_selection_error_is_a_value_error():
    """StreamSelectionError는 ValueError여야 한다.

    StreamSelectionError("x")
    -> ValueError의 인스턴스, str() == "x"
    """
    error = StreamSelectionError("x")

    assert isinstance(error, ValueError)
    assert str(error) == "x"


# ================================================================ 트랙 정보가 없을 때


def test_select_variant_by_height_returns_the_first_variant_with_that_height():
    """select_variant_by_height는 세로값이 그 해상도인 첫 변형의 주소를 돌려줘야 한다.

    변형 1920x1080 둘 · 1280x720 하나, 해상도 1080
    -> 첫째 변형의 주소
    """
    master = _master(
        ("BANDWIDTH=6000000,RESOLUTION=1920x1080", "first.m3u8"),
        ("BANDWIDTH=4000000,RESOLUTION=1920x1080", "second.m3u8"),
        ("BANDWIDTH=3000000,RESOLUTION=1280x720", "third.m3u8"),
    )

    assert select_variant_by_height(master, 1080) == "first.m3u8"


def test_select_variant_by_height_fails_when_no_variant_has_that_height():
    """세로값이 그 해상도인 변형이 없으면 키 기반 오류로 실패해야 한다.

    변형 720x1280 하나, 해상도 720
    -> StreamSelectionError, message_key == STREAM_NOT_FOUND
    """
    master = _master(("BANDWIDTH=3192000,RESOLUTION=720x1280", "a.m3u8"))

    with pytest.raises(StreamSelectionError) as caught:
        select_variant_by_height(master, 720)

    assert caught.value.message_key == STREAM_NOT_FOUND

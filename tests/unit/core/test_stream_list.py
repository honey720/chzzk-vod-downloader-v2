"""마스터 플레이리스트 기준의 해상도 목록 · 원본 판정 · 고른 변형 다시 찾기 (#318)."""

import pytest

from core.api.playback_tracks import (
    STREAM_NOT_FOUND,
    StreamSelectionError,
    list_streams,
    playback_tracks,
    select_stream,
)
from core.api.representations import (
    ORIGINAL_FILE_TAG,
    StreamEntry,
    file_tag,
    is_original,
    shown_frame_rate,
)
from core.models.content import StreamKey
from tests.unit.stream_samples import S1, S2, S4, S5, S9, S11, Sample


def _listed(sample: Sample) -> list[tuple]:
    """목록을 (해상도, 가로, 세로, 원본, 표시할 fps)로 — 오름차순 그대로."""
    entries = list_streams(sample.master, playback_tracks(sample.playback))
    return [
        (e[0], e.stream.width, e.stream.height, is_original(e), shown_frame_rate(e))
        for e in entries
    ]


_LANDSCAPE_REST = [
    (144, 256, 144, False, None),
    (360, 640, 360, False, None),
    (480, 852, 480, False, None),
    (720, 1280, 720, False, 60),
]
_PORTRAIT_REST = [(r, h, w, o, f) for r, w, h, o, f in _LANDSCAPE_REST]


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        (S1, _LANDSCAPE_REST + [(1080, 1920, 1080, True, 60)]),  # 원본 60fps
        (S2, _LANDSCAPE_REST + [(1080, 1920, 1080, True, None)]),  # 원본 30fps
        # 720x1280이 둘 — 다시 인코딩한 60fps, 그리고 원본 30fps(뒤)
        (S4, _PORTRAIT_REST + [(720, 720, 1280, True, None)]),
        (S5, _PORTRAIT_REST + [(1080, 1080, 1920, True, None)]),
        # playback의 원본 트랙은 fps 0 — 크기가 같은 변형이 하나라 원본이고, fps는 마스터의 60
        (S9, _LANDSCAPE_REST + [(1080, 1920, 1080, True, 60)]),
        # playback의 트랙이 마스터에 없다 — 원본 표시 없음, 목록은 마스터 그대로
        (S11, _LANDSCAPE_REST + [(1080, 1920, 1080, False, 60)]),
    ],
    ids=["S1", "S2", "S4", "S5", "S9", "S11"],
)
def test_list_follows_the_master_playlist(sample, expected):
    """해상도 목록은 마스터 플레이리스트의 변형마다 한 항목이고, 원본 여부와 fps는 그 변형의 것이어야 한다.

    주석의 표본마다 playback 정보와 마스터 플레이리스트
    -> (짧은 변, 가로, 세로, 원본, 표시할 fps) 오름차순 — 짧은 변이 같으면 원본이 뒤
    """
    assert _listed(sample) == expected


def test_s4_lists_both_streams_of_the_same_short_side():
    """짧은 변이 같은 두 변형은 합치지 않고 둘 다 목록에 남아야 한다.

    S4(720x1280 변형 둘 — 60fps · 30fps 원본)
    -> 720이 둘이고 마지막 항목(기본 선택)이 30fps 원본
    """
    entries = list_streams(S4.master, playback_tracks(S4.playback))

    same = [e for e in entries if e[0] == 720]

    assert [(is_original(e), e.stream.frame_rate) for e in same] == [(False, 60.0), (True, 30.0)]
    assert entries[-1] is same[-1]


def test_entries_keep_the_list_format():
    """목록 항목은 `[해상도, None]` 리스트와 같아야 한다.

    S1
    -> [[144, None], [360, None], [480, None], [720, None], [1080, None]]
    """
    entries = list_streams(S1.master, playback_tracks(S1.playback))

    assert entries == [[144, None], [360, None], [480, None], [720, None], [1080, None]]


def test_original_is_not_marked_when_same_size_variants_cannot_be_told_apart():
    """원본 트랙과 크기가 같은 변형이 둘인데 프레임률·BANDWIDTH로도 갈리지 않으면 원본을 표시하지 않아야 한다.

    S4의 마스터에서 두 720x1280 변형을 같은 fps·BANDWIDTH로 바꿈
    -> 원본인 항목 0개
    """
    sample = Sample(S4.tracks, S4.variants[:4] + [(720, 1280, 3192000, 60.0)])

    entries = list_streams(sample.master, playback_tracks(sample.playback))

    assert [e for e in entries if is_original(e)] == []


def test_original_among_same_size_variants_is_told_by_bandwidth_when_frame_rates_match():
    """원본 트랙과 크기·프레임률이 같은 변형이 둘이면 BANDWIDTH가 맞는 쪽이 원본이어야 한다.

    S4의 마스터에서 다시 인코딩한 720x1280 변형을 30fps로 바꿈(BANDWIDTH는 그대로)
    -> 원본은 BANDWIDTH가 원본 트랙의 영상 + 오디오인 변형 하나
    """
    sample = Sample(S4.tracks, [(720, 1280, 3192000, 30.0)] + S4.variants[1:])

    entries = list_streams(sample.master, playback_tracks(sample.playback))

    assert [e.stream.bandwidth for e in entries if is_original(e)] == [2692000]


def test_list_skips_variants_without_a_resolution():
    """RESOLUTION이 없는 변형은 목록에 넣지 않아야 한다.

    변형 둘 — RESOLUTION 없는 것(오디오 전용), 1280x720
    -> [[720, None]]
    """
    master = (
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=192000,CODECS="mp4a.40.2"\n'
        "audio.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=3192000,RESOLUTION=1280x720,FRAME-RATE=60.00\n"
        "video.m3u8\n"
    )

    assert list_streams(master, []) == [[720, None]]


# ================================================================ 고른 변형 다시 찾기


@pytest.mark.parametrize(
    ("sample", "position", "variant"),
    [
        (S1, -1, "v4/chunklist.m3u8"),
        (S4, -1, "v4/chunklist.m3u8"),  # 720p(원본) — 30fps 변형
        (S4, -2, "v0/chunklist.m3u8"),  # 720p 60fps — 다시 인코딩한 변형
        (S11, -1, "v4/chunklist.m3u8"),
        (S11, 0, "v3/chunklist.m3u8"),
    ],
    ids=["S1-top", "S4-original", "S4-60fps", "S11-top", "S11-144"],
)
def test_select_stream_finds_the_listed_variant_again(sample, position, variant):
    """목록에서 고른 항목의 스트림 값으로 마스터 플레이리스트에서 같은 변형을 다시 찾아야 한다.

    주석의 경우마다 표본과 목록에서의 위치
    -> 그 변형의 주소
    """
    entries = list_streams(sample.master, playback_tracks(sample.playback))

    assert select_stream(sample.master, entries[position].stream) == variant


@pytest.mark.parametrize(
    "key",
    [
        StreamKey(1080, 1920, 30.0, 6192000),  # 그 크기의 변형이 없다
        StreamKey(720, 1280, 24.0, 2692000),  # 크기가 같은 변형 둘 — 프레임률이 맞는 것이 없다
        StreamKey(720, 1280, None, None),  # 크기가 같은 변형 둘 — 가를 값이 없다
    ],
    ids=["no-size", "no-frame-rate", "nothing-to-tell"],
)
def test_select_stream_fails_with_a_key_when_the_variant_is_not_determined(key):
    """고른 변형을 하나로 다시 찾지 못하면 키 기반 오류로 실패해야 한다.

    S4의 마스터 플레이리스트, 주석의 스트림 값
    -> StreamSelectionError, message_key == STREAM_NOT_FOUND
    """
    with pytest.raises(StreamSelectionError) as caught:
        select_stream(S4.master, key)

    assert caught.value.message_key == STREAM_NOT_FOUND


# ================================================================ 표시 · 파일명


@pytest.mark.parametrize(
    ("frame_rate", "shown"),
    [(60.0, 60), (59.94, 60), (50.0, 50), (49.9, None), (30.0, None), (None, None)],
)
def test_shown_frame_rate_is_the_rounded_value_from_fifty(frame_rate, shown):
    """표시할 프레임률은 50fps 이상일 때만 반올림한 정수이고, 그 아래나 모르는 값은 None이어야 한다.

    주석의 경우마다 선언된 프레임률
    -> 표시할 값
    """
    assert shown_frame_rate(StreamEntry(720, None, frame_rate=frame_rate)) == shown


def test_shown_frame_rate_and_original_of_a_plain_list_are_empty():
    """스트림 정보가 없는 평범한 목록 항목은 표시할 프레임률이 없고 원본이 아니어야 한다.

    [720, "https://v.invalid/720.mp4"]
    -> (None, False)
    """
    entry = [720, "https://v.invalid/720.mp4"]

    assert (shown_frame_rate(entry), is_original(entry)) == (None, False)


@pytest.mark.parametrize(
    ("sample", "tags"),
    [
        (S1, ["", "", "", "", ""]),  # 해상도마다 하나 — 붙이지 않는다
        (S4, ["", "", "", "", ORIGINAL_FILE_TAG]),  # 720이 둘 — 원본 쪽에만
    ],
    ids=["S1", "S4"],
)
def test_file_tag_marks_the_original_only_among_same_short_sides(sample, tags):
    """파일명 표시는 짧은 변이 같은 항목이 둘일 때 원본 쪽에만 붙어야 한다.

    주석의 표본마다 목록의 항목 순서대로
    -> 표시("(원본)" 또는 빈 문자열)
    """
    entries = list_streams(sample.master, playback_tracks(sample.playback))

    assert [file_tag(entries, entry) for entry in entries] == tags

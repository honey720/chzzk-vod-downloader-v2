"""플레이리스트의 세그먼트 길이와 구간 → 세그먼트 범위 단위 테스트 (#309).

대상: core/api/hls.py의 durations · duration, core/utils/hls_ranges.py의 selection_segments.

핵심 계약:
- 세그먼트 길이는 #EXTINF에서 읽고, 영상 길이는 그 합이다
- 구간의 시작·끝 시각이 놓인 세그먼트를 #EXTINF 누적으로 찾는다
- 시작 쪽은 하나 앞의 세그먼트부터 받는다
"""

import pytest

from core.api.hls import HlsPlaylist, parse_media_playlist
from core.models.plan import TimeRange
from core.models.ts_index import SegmentSpan
from core.utils.hls_ranges import selection_segments


def _playlist(durations: list[str]) -> str:
    """세그먼트 길이 목록으로 만든 플레이리스트 본문."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4", "#EXT-X-MEDIA-SEQUENCE:0"]
    for number, duration in enumerate(durations):
        lines += [f"#EXTINF:{duration},", f"segment-{number:06d}.ts"]
    return "\n".join([*lines, "#EXT-X-ENDLIST"])


# 4초 세그먼트 넷과 2.5초 세그먼트 하나 — 시작 시각 0 · 4 · 8 · 12 · 16, 길이 18.5초
FIVE = parse_media_playlist(_playlist(["4.000", "4.000", "4.000", "4.000", "2.500"]))


# ================================================================ 세그먼트 길이


def test_parse_media_playlist_reads_segment_durations():
    """parse_media_playlist는 세그먼트마다 #EXTINF 길이를 같은 순서로 돌려줘야 한다.

    #EXTINF = 3.840 · 3.840 · 1.200
    -> durations == (3.84, 3.84, 1.2)
    """
    playlist = parse_media_playlist(_playlist(["3.840", "3.840", "1.200"]))

    assert playlist.durations == pytest.approx((3.84, 3.84, 1.2))
    assert len(playlist.durations) == len(playlist.segments)


def test_parse_media_playlist_reads_extinf_with_title_or_integer():
    """parse_media_playlist는 #EXTINF에 제목이 붙거나 길이가 정수여도 길이를 읽어야 한다.

    "#EXTINF:4,", "#EXTINF:2.5,제목"
    -> durations == (4.0, 2.5)
    """
    text = "\n".join(["#EXTM3U", "#EXTINF:4,", "a.ts", "#EXTINF:2.5,제목", "b.ts"])

    assert parse_media_playlist(text).durations == (4.0, 2.5)


def test_parse_media_playlist_gives_zero_to_segment_without_extinf():
    """parse_media_playlist는 #EXTINF가 없는 세그먼트의 길이를 0.0으로 돌려줘야 한다.

    a.ts(#EXTINF 4.0) · b.ts(#EXTINF 없음) · c.ts(#EXTINF 2.0)
    -> durations == (4.0, 0.0, 2.0)
    """
    text = "\n".join(["#EXTM3U", "#EXTINF:4.0,", "a.ts", "b.ts", "#EXTINF:2.0,", "c.ts"])

    assert parse_media_playlist(text).durations == (4.0, 0.0, 2.0)


def test_playlist_duration_is_sum_of_extinf():
    """HlsPlaylist.duration은 세그먼트 수 × 목표 길이가 아니라 #EXTINF의 합이어야 한다.

    #EXTINF = 4.000 · 4.000 · 4.000 · 4.000 · 2.500 (TARGETDURATION 4)
    -> duration == 18.5
    """
    assert FIVE.duration == pytest.approx(18.5)


def test_playlist_built_without_durations_reports_zero_duration():
    """HlsPlaylist는 durations 없이 만들면 빈 튜플과 길이 0.0을 돌려줘야 한다.

    HlsPlaylist(segments=("a.ts",))
    -> durations == (), duration == 0.0
    """
    playlist = HlsPlaylist(segments=("a.ts",))

    assert playlist.durations == ()
    assert playlist.duration == 0.0


# ================================================================ 구간 → 세그먼트


def test_selection_segments_starts_one_segment_before_the_covering_segment():
    """selection_segments는 구간이 놓인 세그먼트보다 하나 앞의 세그먼트부터 받도록 돌려줘야 한다.

    세그먼트 시작 0 · 4 · 8 · 12 · 16초, 구간 9.0~10.0초 (세그먼트 2 안)
    -> first == 1, last == 2, cover_first == 2, cover_last == 2
    """
    assert selection_segments(FIVE, TimeRange(9.0, 10.0)) == SegmentSpan(
        first=1, last=2, cover_first=2, cover_last=2
    )


def test_selection_segments_starts_at_first_segment_when_nothing_precedes():
    """selection_segments는 구간이 첫 세그먼트에 놓이면 첫 세그먼트부터 받도록 돌려줘야 한다.

    구간 1.0~2.0초 (세그먼트 0 안)
    -> first == 0, last == 0
    """
    span = selection_segments(FIVE, TimeRange(1.0, 2.0))

    assert (span.first, span.last) == (0, 0)


def test_selection_segments_covers_every_segment_the_range_touches():
    """selection_segments는 구간이 여러 세그먼트에 걸치면 끝 시각이 놓인 세그먼트까지 돌려줘야 한다.

    구간 5.0~13.0초 (세그먼트 1 ~ 3)
    -> first == 0, last == 3, cover_first == 1, cover_last == 3
    """
    assert selection_segments(FIVE, TimeRange(5.0, 13.0)) == SegmentSpan(
        first=0, last=3, cover_first=1, cover_last=3
    )


def test_selection_segments_puts_boundary_time_in_the_segment_that_starts_there():
    """selection_segments는 세그먼트 경계와 같은 시각을 그 시각에 시작하는 세그먼트에 놓아야 한다.

    구간 8.0~12.0초 (8.0은 세그먼트 2의 시작, 12.0은 세그먼트 3의 시작)
    -> cover_first == 2, cover_last == 3, first == 1
    """
    span = selection_segments(FIVE, TimeRange(8.0, 12.0))

    assert (span.first, span.cover_first, span.cover_last) == (1, 2, 3)


def test_selection_segments_follows_uneven_segment_durations():
    """selection_segments는 세그먼트 길이가 고르지 않아도 #EXTINF 누적으로 세그먼트를 찾아야 한다.

    #EXTINF = 2 · 2 · 2 · 6 (시작 0 · 2 · 4 · 6초), 구간 5.0~5.5초
    -> cover_first == 2 (4초마다 나눴다면 1)
    """
    playlist = parse_media_playlist(_playlist(["2.000", "2.000", "2.000", "6.000"]))

    assert selection_segments(playlist, TimeRange(5.0, 5.5)).cover_first == 2


def test_selection_segments_clamps_end_to_last_segment():
    """selection_segments는 구간의 끝이 플레이리스트 길이를 넘으면 마지막 세그먼트까지 돌려줘야 한다.

    플레이리스트 18.5초, 구간 17.0~30.0초
    -> last == 4
    """
    assert selection_segments(FIVE, TimeRange(17.0, 30.0)).last == 4


def test_selection_segments_rejects_start_at_or_after_duration():
    """selection_segments는 구간의 시작이 플레이리스트 길이와 같거나 뒤이면 ValueError를 내야 한다.

    플레이리스트 18.5초, 구간 18.5~19.0초
    -> ValueError
    """
    with pytest.raises(ValueError):
        selection_segments(FIVE, TimeRange(18.5, 19.0))


def test_selection_segments_rejects_playlist_without_durations():
    """selection_segments는 플레이리스트에 세그먼트 길이가 없으면 ValueError를 내야 한다.

    HlsPlaylist(segments=("a.ts", "b.ts")) — durations 없음
    -> ValueError
    """
    with pytest.raises(ValueError):
        selection_segments(HlsPlaylist(segments=("a.ts", "b.ts")), TimeRange(0.0, 1.0))

"""구간 → 세그먼트 범위와 프레임(core/utils/fmp4_sections.py) 단위 테스트 (#309).

ffmpeg로는 만들기 어려운 모양의 입력을 다룬다. 입력은 tests/unit/core/fmp4_builder.py가
상자를 직접 조립한 합성 fMP4다 — 영상 10fps(timescale 1000, 샘플 길이 100틱), 오디오
timescale 8000 · 샘플 길이 1024틱. 받기부터 구간 파일까지의 경로는 test_m3u8_sections.py가 본다.
"""

from core.api.fmp4 import parse_init_segment, parse_media_segment
from core.api.hls import parse_media_playlist
from core.models.plan import TimeRange
from core.utils.fmp4_sections import plan_fmp4_sections
from tests.unit.core.fmp4_builder import (
    KEY,
    NON_KEY,
    Fragment,
    InitTrack,
    Run,
    Sample,
    Traf,
    init_segment,
    media_segment,
)

VIDEO = 1  # 영상 트랙 번호
AUDIO = 2  # 오디오 트랙 번호


def _segment(number: int, *, video: bool = True, audio_samples: int = 8) -> bytes:
    """number번째 1초 세그먼트 — 영상 10프레임(첫 프레임이 키프레임)과 오디오."""
    trafs = []
    if video:
        frames = [
            Sample(duration=100, size=10, flags=KEY if n == 0 else NON_KEY) for n in range(10)
        ]
        trafs.append(Traf(VIDEO, [Run(frames)], decode_time=number * 1000))
    sound = [Sample(duration=1024, size=7) for _ in range(audio_samples)]
    trafs.append(Traf(AUDIO, [Run(sound)], decode_time=number * 8192))
    return media_segment([Fragment(trafs)])


def _playlist(durations: list[float]):
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", '#EXT-X-MAP:URI="init.mp4"']
    for number, duration in enumerate(durations):
        lines += [f"#EXTINF:{duration:.6f},", f"seg-{number}.m4s"]
    return parse_media_playlist("\n".join([*lines, "#EXT-X-ENDLIST"]))


def test_section_ending_at_the_video_length_skips_a_last_segment_without_video():
    """끝이 영상 길이와 같은 구간은 마지막 세그먼트에 영상이 없으면 그 앞 세그먼트의 마지막 프레임에서 끝나야 한다.

    세그먼트 셋 — 0 · 1은 영상 10프레임씩과 오디오, 2는 오디오뿐. #EXTINF 1.0 · 1.0 · 0.5 (길이 2.5초),
    구간 0.5 ~ 2.5초
    -> 끝 프레임의 PTS == 1.9초(둘째 세그먼트의 마지막 프레임), 받는 세그먼트 0~2
    """
    init = parse_init_segment(
        init_segment(
            [
                InitTrack(VIDEO, b"vide", 1000, trex=(100, 10, NON_KEY)),
                InitTrack(AUDIO, b"soun", 8000, codec=b"mp4a", trex=(1024, 7, KEY)),
            ]
        )
    )
    segments = [
        parse_media_segment(_segment(0), init),
        parse_media_segment(_segment(1), init),
        parse_media_segment(_segment(2, video=False, audio_samples=4), init),
    ]

    sections = plan_fmp4_sections(
        _playlist([1.0, 1.0, 0.5]), init, [TimeRange(0.5, 2.5)], segments.__getitem__
    )

    assert len(sections) == 1
    assert (sections[0].first_pts, sections[0].last_pts) == (0.5, 1.9)
    assert (sections[0].first_segment, sections[0].last_segment) == (0, 2)

"""moov 색인의 값과, 그 값에서 나오는 구간 계획 · 부분 mp4 · 컷의 프레임 정보 (#309).

색인이 표를 **무엇에 담는지**(튜플 · 배열)를 모르는 계약 게이트다 — 값만 본다. 색인의 표를
연속 배열로 바꾸기 전에 세웠고, 기대값은 바꾸기 전의 코드가 낸 값이다. 표는 순회해서
바이트로 싼 뒤 지문(SHA-256 앞 16자)으로 견준다.

입력은 조립기(``mp4_builder``)로 만든 mp4 하나다 — 29.97fps 240프레임(재정렬 I P B B,
30프레임마다 키프레임, 청크 15프레임), 48kHz 오디오 376샘플, 샘플 크기는 씨앗 309의 난수다.
"""

import hashlib
import random
import struct
from fractions import Fraction

import pytest

from core.api.mp4 import read_mp4_head
from core.models.cut import CutPiece, CutPlan
from core.models.plan import TimeRange
from core.utils.hybrid_cut import cut_frames_from_mp4, plan_cut
from core.utils.mp4_partial import build_head, plan_partial
from core.utils.mp4_ranges import sections_download_size, selection_byte_ranges
from tests.unit.core.mp4_builder import TrackSpec, build_mp4

SELECTIONS = (
    TimeRange(0.5, 2.0),
    TimeRange(3.0, 3.1),
    TimeRange(1.9, 4.5),  # 첫 구간과 겹친다
    TimeRange(7.0, 8.008),  # 영상 끝까지
)


def _source() -> bytes:
    generator = random.Random(309)
    frames = 240
    video = TrackSpec(
        handler=b"vide",
        timescale=30000,
        deltas=[1001] * frames,
        sizes=[generator.randrange(200, 4000) for _ in range(frames)],
        chunks=[15] * 16,
        composition=[1001, 3003, 0, 0] * 60,
        sync=list(range(1, frames + 1, 30)),
        edits=[(8008, 1001)],
    )
    samples = 376
    audio = TrackSpec(
        handler=b"soun",
        timescale=48000,
        deltas=[1024] * samples,
        sizes=[generator.randrange(100, 600) for _ in range(samples)],
        chunks=[23] * 16 + [8],
        edits=[(8008, 1024)],
    )
    return build_mp4([video, audio]).data


@pytest.fixture(scope="module")
def head():
    data = _source()
    assert hashlib.sha256(data).hexdigest()[:16] == "670670fd6252e088", "전제: 입력이 그대로다"
    return read_mp4_head(lambda offset, size: data[offset : offset + size])


def _digest(values, code: str) -> str:
    """값들을 순회해 리틀엔디언 double("d") 또는 64비트 정수("q")로 싼 바이트의 지문."""
    values = list(values)
    return hashlib.sha256(struct.pack(f"<{len(values)}{code}", *values)).hexdigest()[:16]


def test_index_scalars_are_unchanged(head):
    """색인의 프레임 수 · 길이 · 프레임률 · 키프레임은 바꾸기 전의 값이어야 한다.

    -> 240프레임, 8.008초, 30000/1001, 키프레임 [0, 29, 60, 89, 120, 149, 180, 209]
    """
    index = head.index

    assert len(index.frame_pts) == len(index.frame_samples) == 240
    assert index.duration == 8.008
    assert index.fps == Fraction(30000, 1001)
    assert list(index.keyframes) == [0, 29, 60, 89, 120, 149, 180, 209]


@pytest.mark.parametrize(
    ("track", "expected"),
    [
        (
            "video",
            {
                "times": "431b046060bea174",
                "decode_times": "8df559a0851e580a",
                "durations": "ad7c5f3000fea75d",
                "offsets": "4d11cfbac90acc79",
                "sizes": "15c4a0616f1cd033",
                "chunks": ([0, 15, 30, 45], 16),
                "sync": ([0, 30, 60, 90], 8),
            },
        ),
        (
            "audio",
            {
                "times": "951f6cc295a66281",
                "decode_times": "951f6cc295a66281",
                "durations": "8ad5d2e713bd8313",
                "offsets": "c3adc8cb31f99f7a",
                "sizes": "70b58a68618350a8",
                "chunks": ([0, 23, 46, 69], 17),
                "sync": ([0, 1, 2, 3], 376),
            },
        ),
    ],
)
def test_track_tables_are_unchanged(head, track, expected):
    """트랙의 샘플 표(시각 · DTS · 길이 · 위치 · 크기 · 청크 · 동기 샘플)는 바꾸기 전의 값이어야 한다.

    표마다 값들을 순회해 싼 바이트의 지문이 기대값과 같다. 청크 · 동기 샘플은 앞 넷과 개수.
    """
    table = getattr(head.index, track)

    assert _digest(table.times, "d") == expected["times"]
    assert _digest(table.decode_times, "d") == expected["decode_times"]
    assert _digest(table.durations, "d") == expected["durations"]
    assert _digest(table.offsets, "q") == expected["offsets"]
    assert _digest(table.sizes, "q") == expected["sizes"]
    assert (list(table.chunk_starts)[:4], len(table.chunk_starts)) == expected["chunks"]
    assert (list(table.sync_samples)[:4], len(table.sync_samples)) == expected["sync"]


def test_frame_order_is_unchanged(head):
    """표시 순서의 프레임 PTS와 그 프레임의 샘플 번호는 바꾸기 전의 값이어야 한다."""
    assert _digest(head.index.frame_pts, "d") == "cf80ce921baf31c4"
    assert _digest(head.index.frame_samples, "q") == "cbbb907e8a8db3cb"


@pytest.mark.parametrize(
    ("selection", "ranges", "keyframe", "first_frame", "last_frame"),
    [
        (SELECTIONS[0], ((4918, 199964),), 0, 15, 60),
        (SELECTIONS[1], ((161804, 276878),), 60, 90, 93),
        (SELECTIONS[2], ((81877, 381112),), 29, 57, 135),
        (SELECTIONS[3], ((459718, 623878),), 180, 210, 239),
    ],
)
def test_selection_byte_ranges_are_unchanged(
    head, selection, ranges, keyframe, first_frame, last_frame
):
    """구간의 바이트 범위 · 시작 키프레임 · 첫 프레임 · 끝 프레임은 바꾸기 전의 값이어야 한다."""
    picked = selection_byte_ranges(head.index, selection)

    assert picked.ranges == ranges
    assert picked.total_size == ranges[0][1] - ranges[0][0] + 1
    assert (picked.keyframe, picked.first_frame, picked.last_frame) == (
        keyframe,
        first_frame,
        last_frame,
    )


def test_partial_layout_and_head_bytes_are_unchanged(head):
    """구간 넷의 받을 크기 · 부분 파일의 배치 · 머리 바이트는 바꾸기 전의 값이어야 한다.

    -> 받을 크기 540,356바이트, 머리 4,918바이트, 범위 둘(겹치는 구간은 합쳐진다),
       머리 바이트의 SHA-256이 기대값과 같다
    """
    index = head.index
    spans = [
        span for selection in SELECTIONS for span in selection_byte_ranges(index, selection).ranges
    ]

    layout = plan_partial(index, spans)

    assert sections_download_size(index, SELECTIONS) == 540356
    assert layout.head_size == 4918
    assert layout.ranges == ((4918, 381112), (459718, 623878))
    assert layout.starts == (4918, 381113)
    assert layout.size == 545274
    assert (
        hashlib.sha256(build_head(head.data, layout, index.moov_range)).hexdigest()
        == "68d8728d73f973f4b236b5817ab968bddace6671659dd63fd6c596b344a3c9ea"
    )


def test_cut_frames_and_cut_plan_are_unchanged(head):
    """색인에서 뽑은 컷의 프레임 정보와 프레임 15~100의 컷 계획은 바꾸기 전의 값이어야 한다."""
    frames = cut_frames_from_mp4(head.index)

    assert _digest(frames.frame_pts, "d") == "cf80ce921baf31c4"
    assert _digest(frames.frame_dts, "d") == "b8e302679b450752"
    assert list(frames.keyframes) == [0, 29, 60, 89, 120, 149, 180, 209]
    assert (frames.timescale, frames.frame_duration) == (30000, 0.03336666666666667)
    assert (frames.audio_start, frames.audio_end, frames.audio_bitrate) == (0.0, 8.0, 134)
    assert plan_cut(frames, 15, 100) == CutPlan(
        first=15,
        last=100,
        pieces=(
            CutPiece(kind="head", first=15, end=29),
            CutPiece(kind="mid", first=29, end=89),
            CutPiece(kind="tail", first=89, end=101),
        ),
    )

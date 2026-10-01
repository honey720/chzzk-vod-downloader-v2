"""MPEG-TS 세그먼트 해석(core/api/mpegts.py) 단위 테스트 (#309).

핵심 계약:
- 영상 PES의 PTS·DTS와 키프레임을 프레임별로 읽는다. PES가 여러 패킷에 걸쳐도 같다
- 색인의 시각은 VOD 시작(origin) = 0이고, 프레임은 PTS 순서다
- 33비트 랩어라운드를 풀어 시각이 이어진다

입력은 tests/unit/core/ts_builder.py가 패킷을 직접 조립한 합성 TS다.
"""

import pytest

from core.api.mpegts import (
    TS_INVALID,
    TS_UNSUPPORTED,
    TsError,
    build_ts_index,
    parse_ts,
    ts_origin,
)
from core.models.ts_index import TsStreams
from core.utils.timecode import snap_to_frame
from tests.unit.core.ts_builder import (
    VIDEO_PID,
    Frame,
    audio_pes,
    build_ts,
    packetize,
    pat_packet,
    pes,
    pmt_packet,
    short_pmt_packet,
    video_frame,
)

TICK = 3000  # 30fps 한 프레임의 틱 수 (90,000 ÷ 30)
WRAP = 1 << 33  # 33비트 타임스탬프가 0으로 돌아가는 값


def _reordered_frames(base: int = 0) -> list[Frame]:
    """디코드 순서 I P B B — 표시 순서는 I B B P. DTS는 PTS보다 한 프레임 앞선다."""
    return [
        Frame(pts=base + 1 * TICK, dts=base + 0 * TICK, idr=True),
        Frame(pts=base + 4 * TICK, dts=base + 1 * TICK),
        Frame(pts=base + 2 * TICK, dts=base + 2 * TICK),
        Frame(pts=base + 3 * TICK, dts=base + 3 * TICK),
    ]


# ================================================================ parse_ts


def test_parse_ts_reads_pts_and_dts_in_decode_order():
    """parse_ts는 영상 PES마다 PTS와 DTS를 파일에 나온 순서로 돌려줘야 한다.

    디코드 순서 I P B B, PTS = 3000·12000·6000·9000, DTS = 0·3000·6000·9000
    -> video_pts == (3000, 12000, 6000, 9000), video_dts == (0, 3000, 6000, 9000)
    """
    streams = parse_ts(build_ts(_reordered_frames()))

    assert streams.video_pts == (3000, 12000, 6000, 9000)
    assert streams.video_dts == (0, 3000, 6000, 9000)


def test_parse_ts_uses_pts_as_dts_when_pes_has_no_dts():
    """parse_ts는 PES에 DTS가 없으면 DTS를 PTS와 같게 돌려줘야 한다.

    PTS = 3000 · 6000, DTS 없음
    -> video_dts == (3000, 6000)
    """
    streams = parse_ts(build_ts([Frame(pts=3000, idr=True), Frame(pts=6000)]))

    assert streams.video_dts == (3000, 6000)


def test_parse_ts_marks_idr_frames_as_keyframes():
    """parse_ts는 랜덤 액세스 표시가 없어도 IDR NAL이 있는 프레임을 키프레임으로 돌려줘야 한다.

    프레임 4개 중 0번과 2번에 IDR NAL, 랜덤 액세스 표시는 모두 꺼짐
    -> video_keyframes == (0, 2)
    """
    frames = [Frame(pts=n * TICK, idr=n in (0, 2)) for n in range(4)]

    assert parse_ts(build_ts(frames)).video_keyframes == (0, 2)


def test_parse_ts_marks_random_access_frames_as_keyframes():
    """parse_ts는 IDR NAL이 없어도 랜덤 액세스 표시가 켜진 프레임을 키프레임으로 돌려줘야 한다.

    프레임 3개 중 1번의 적응 필드에 랜덤 액세스 표시, IDR NAL 없음
    -> video_keyframes == (1,)
    """
    frames = [Frame(pts=n * TICK, random_access=n == 1) for n in range(3)]

    assert parse_ts(build_ts(frames)).video_keyframes == (1,)


def test_parse_ts_finds_idr_nal_beyond_the_first_packet():
    """parse_ts는 IDR NAL이 PES의 첫 패킷 밖에 있어도 키프레임으로 돌려줘야 한다.

    IDR NAL 앞에 400바이트를 채워 PES가 3패킷 이상에 걸치는 프레임
    -> video_keyframes == (0,)
    """
    streams = parse_ts(build_ts([Frame(pts=3000, idr=True, filler=400), Frame(pts=6000)]))

    assert streams.video_keyframes == (0,)


def test_parse_ts_reads_timestamps_split_across_packets():
    """parse_ts는 PES 머리가 두 패킷에 걸쳐 있어도 PTS와 DTS를 읽어야 한다.

    첫 패킷의 본문을 10바이트로 줄여 PTS·DTS 칸이 다음 패킷에 놓인 PES
    -> video_pts == (12000,), video_dts == (9000,)
    """
    data = (
        pat_packet()
        + pmt_packet()
        + video_frame(Frame(pts=12000, dts=9000, idr=True), first_payload=10)
    )

    streams = parse_ts(data)

    assert streams.video_pts == (12000,)
    assert streams.video_dts == (9000,)


def test_parse_ts_reads_hevc_irap_as_keyframe():
    """parse_ts는 HEVC 스트림에서 IRAP NAL(종류 16~23)이 있는 프레임을 키프레임으로 돌려줘야 한다.

    PMT stream_type 0x24, 0번 프레임의 NAL 종류 19(IDR_W_RADL), 1번 프레임의 NAL 종류 1
    -> video_keyframes == (0,)
    """
    frames = [
        Frame(pts=3000, nal=b"\x00\x00\x01\x26\x01"),  # 종류 19 → 머리 바이트 19 << 1
        Frame(pts=6000, nal=b"\x00\x00\x01\x02\x01"),  # 종류 1 → 머리 바이트 1 << 1
    ]

    assert parse_ts(build_ts(frames, video_type=0x24)).video_keyframes == (0,)


def test_parse_ts_reads_audio_pes_timestamps():
    """parse_ts는 오디오 PES마다 PTS를 돌려줘야 한다.

    오디오 PES 3개, PTS = 0 · 1920 · 3840
    -> audio_pts == (0, 1920, 3840)
    """
    streams = parse_ts(build_ts([Frame(pts=3000, idr=True)], audio=[0, 1920, 3840]))

    assert streams.audio_pts == (0, 1920, 3840)


def test_parse_ts_reads_concatenated_segments():
    """parse_ts는 세그먼트 둘을 이어 붙인 bytes에서 두 세그먼트의 프레임을 모두 돌려줘야 한다.

    프레임 4개짜리 세그먼트 둘(각자 PAT·PMT로 시작)
    -> 프레임 8개, 키프레임 (0, 4)
    """
    data = build_ts(_reordered_frames()) + build_ts(_reordered_frames(base=4 * TICK))

    streams = parse_ts(data)

    assert len(streams.video_pts) == 8
    assert streams.video_keyframes == (0, 4)


@pytest.mark.parametrize(
    "damage",
    [
        lambda data: data[:-1],  # 길이가 188의 배수가 아니다
        lambda data: data[:188] + b"\x00" + data[189:],  # 둘째 패킷의 동기 바이트가 틀리다
        lambda data: b"",  # 비어 있다
    ],
    ids=["length", "sync", "empty"],
)
def test_parse_ts_rejects_broken_packets(damage):
    """parse_ts는 패킷 경계나 동기 바이트가 맞지 않으면 손상 키로 TsError를 내야 한다.

    길이를 1바이트 줄임 · 둘째 패킷의 첫 바이트를 0으로 바꿈 · 빈 bytes
    -> message_key == TS_INVALID
    """
    with pytest.raises(TsError) as info:
        parse_ts(damage(build_ts(_reordered_frames())))

    assert info.value.message_key == TS_INVALID


def test_parse_ts_rejects_pmt_section_cut_short():
    """parse_ts는 PMT 섹션이 스트림 목록 앞의 고정 칸보다 짧으면 IndexError가 아니라 손상 키로 TsError를 내야 한다.

    섹션 본문이 3바이트뿐인 PMT
    -> message_key == TS_INVALID
    """
    with pytest.raises(TsError) as info:
        parse_ts(pat_packet() + short_pmt_packet())

    assert info.value.message_key == TS_INVALID


def test_parse_ts_rejects_segment_without_video_stream():
    """parse_ts는 PMT에 영상 스트림이 없으면 미지원 키로 TsError를 내야 한다.

    PMT에 오디오 스트림만 있는 TS
    -> message_key == TS_UNSUPPORTED
    """
    data = pat_packet() + pmt_packet(video_pid=None) + audio_pes(0)

    with pytest.raises(TsError) as info:
        parse_ts(data)

    assert info.value.message_key == TS_UNSUPPORTED


def test_parse_ts_rejects_video_pes_without_pts():
    """parse_ts는 영상 PES에 PTS가 없으면 미지원 키로 TsError를 내야 한다.

    PTS·DTS 플래그가 모두 꺼진 영상 PES
    -> message_key == TS_UNSUPPORTED
    """
    bare = bytearray(pes(0xE0, 3000, None, b"\x00\x00\x01\x65" + b"\xaa" * 20))
    bare[7] = 0x00  # PTS_DTS_flags를 끈다
    data = pat_packet() + pmt_packet() + packetize(VIDEO_PID, bytes(bare))

    with pytest.raises(TsError) as info:
        parse_ts(data)

    assert info.value.message_key == TS_UNSUPPORTED


# ================================================================ ts_origin


def test_ts_origin_is_earliest_pts_across_video_and_audio():
    """ts_origin은 영상과 오디오를 통틀어 가장 이른 PTS를 돌려줘야 한다.

    영상 PTS = 6000 · 9000, 오디오 PTS = 2580 · 4500
    -> 2580
    """
    streams = TsStreams(
        video_pts=(6000, 9000), video_dts=(3000, 6000), video_keyframes=(0,), audio_pts=(2580, 4500)
    )

    assert ts_origin(streams) == 2580


def test_ts_origin_picks_value_before_wraparound():
    """ts_origin은 세그먼트 안에서 값이 0으로 돌아가도 그 직전의 값을 가장 이른 것으로 돌려줘야 한다.

    영상 PTS = 2^33 − 3000 · 0 · 3000
    -> 2^33 − 3000
    """
    streams = TsStreams(
        video_pts=(WRAP - 3000, 0, 3000),
        video_dts=(WRAP - 3000, 0, 3000),
        video_keyframes=(0,),
        audio_pts=(),
    )

    assert ts_origin(streams) == WRAP - 3000


# ================================================================ build_ts_index


def test_build_ts_index_orders_frames_by_pts():
    """build_ts_index는 프레임을 DTS가 아니라 PTS 순서로 놓아야 한다.

    디코드 순서 I P B B, PTS = 3000·12000·6000·9000, origin = 3000
    -> frame_pts = 0, 1/30, 2/30, 3/30초 / frame_samples == (0, 2, 3, 1)
    """
    index = build_ts_index(parse_ts(build_ts(_reordered_frames())), origin=3000)

    assert index.frame_pts == pytest.approx([0.0, 1 / 30, 2 / 30, 3 / 30])
    assert index.frame_samples == (0, 2, 3, 1)


def test_build_ts_index_keeps_decode_times_in_decode_order():
    """build_ts_index는 DTS를 디코드 순서 그대로 초로 돌려줘야 한다.

    DTS = 0·3000·6000·9000, origin = 3000
    -> decode_times = −1/30, 0, 1/30, 2/30초
    """
    index = build_ts_index(parse_ts(build_ts(_reordered_frames())), origin=3000)

    assert index.decode_times == pytest.approx([-1 / 30, 0.0, 1 / 30, 2 / 30])


def test_build_ts_index_maps_keyframes_to_display_order():
    """build_ts_index는 키프레임을 표시 순서의 프레임 번호로 돌려줘야 한다.

    디코드 순서 I P B B 두 묶음 (키프레임은 디코드 0번·4번)
    -> keyframes == (0, 4)
    """
    frames = _reordered_frames() + _reordered_frames(base=4 * TICK)

    index = build_ts_index(parse_ts(build_ts(frames)), origin=3000)

    assert index.keyframes == (0, 4)


def test_build_ts_index_measures_time_from_origin():
    """build_ts_index는 origin을 0초로 삼아 그 뒤 세그먼트의 시각을 돌려줘야 한다.

    origin = 126000, 세그먼트의 첫 PTS = 126000 + 90000 × 100 + 3000
    -> frame_pts[0] == 100 + 1/30초
    """
    base = 126000 + 90000 * 100
    index = build_ts_index(parse_ts(build_ts(_reordered_frames(base=base))), origin=126000)

    assert index.frame_pts[0] == pytest.approx(100 + 1 / 30)


def test_build_ts_index_unwraps_33bit_timestamps():
    """build_ts_index는 타임스탬프가 2^33에서 0으로 돌아가도 시각을 이어서 돌려줘야 한다.

    프레임 4개, PTS = 2^33 − 6000 · 2^33 − 3000 · 0 · 3000 (DTS 같음), origin = 2^33 − 6000
    -> frame_pts = 0, 1/30, 2/30, 3/30초
    """
    stamps = [(WRAP - 6000 + n * TICK) % WRAP for n in range(4)]
    frames = [Frame(pts=stamp, dts=stamp, idr=n == 0) for n, stamp in enumerate(stamps)]

    index = build_ts_index(parse_ts(build_ts(frames)), origin=WRAP - 6000)

    assert index.frame_pts == pytest.approx([0.0, 1 / 30, 2 / 30, 3 / 30])


def test_build_ts_index_unwraps_pts_that_wraps_before_its_dts():
    """build_ts_index는 DTS는 2^33 직전이고 PTS만 0을 넘어간 프레임의 시각을 이어서 돌려줘야 한다.

    DTS = 2^33 − 3000, PTS = 3000 (DTS보다 6000틱 뒤), origin = 2^33 − 3000
    -> frame_pts[0] == 6000/90000초
    """
    frames = [Frame(pts=3000, dts=WRAP - 3000, idr=True)]

    index = build_ts_index(parse_ts(build_ts(frames)), origin=WRAP - 3000)

    assert index.frame_pts[0] == pytest.approx(6000 / 90000)


def test_build_ts_index_reads_segment_after_origin_wrapped():
    """build_ts_index는 origin 뒤에 랩어라운드가 지난 세그먼트의 시각을 origin 기준으로 돌려줘야 한다.

    origin = 2^33 − 90000 × 10 (랩어라운드 10초 전), 세그먼트의 첫 PTS = 90000 × 5 (랩어라운드 5초 뒤)
    -> frame_pts[0] == 15초
    """
    frames = [Frame(pts=90000 * 5, dts=90000 * 5, idr=True)]

    index = build_ts_index(parse_ts(build_ts(frames)), origin=WRAP - 90000 * 10)

    assert index.frame_pts[0] == pytest.approx(15.0)


def test_build_ts_index_reads_dts_slightly_before_origin_as_negative():
    """build_ts_index는 첫 DTS가 origin보다 조금 앞서면 26.5시간 뒤가 아니라 음수 시각으로 돌려줘야 한다.

    origin = 3000 (가장 이른 PTS), 첫 DTS = 0
    -> decode_times[0] == −1/30초
    """
    index = build_ts_index(parse_ts(build_ts(_reordered_frames())), origin=3000)

    assert index.decode_times[0] == pytest.approx(-1 / 30)


def test_build_ts_index_uses_expected_start_to_count_wraps():
    """build_ts_index는 expected_start를 받으면 랩어라운드 횟수를 그 시각에 가장 가깝게 맞춰야 한다.

    origin = 0, 세그먼트의 첫 PTS = 90000 (원시 값으로는 1초), expected_start = 2^33/90000 + 1초 (약 26.5시간 뒤)
    -> frame_pts[0] == 2^33/90000 + 1초
    """
    frames = [Frame(pts=90000, dts=90000, idr=True)]
    later = WRAP / 90000 + 1

    index = build_ts_index(parse_ts(build_ts(frames)), origin=0, expected_start=later)

    assert index.frame_pts[0] == pytest.approx(later)


def test_build_ts_index_converts_audio_pts():
    """build_ts_index는 오디오 PES의 PTS도 origin 기준 초로 돌려줘야 한다.

    오디오 PTS = 3000 · 4920, origin = 3000
    -> audio_pts = 0, 1920/90000초
    """
    streams = parse_ts(build_ts([Frame(pts=3000, idr=True)], audio=[3000, 4920]))

    index = build_ts_index(streams, origin=3000)

    assert index.audio_pts == pytest.approx([0.0, 1920 / 90000])


def test_build_ts_index_rejects_streams_without_video_frames():
    """build_ts_index는 영상 프레임이 없으면 손상 키로 TsError를 내야 한다.

    영상 프레임 0개
    -> message_key == TS_INVALID
    """
    empty = TsStreams(video_pts=(), video_dts=(), video_keyframes=(), audio_pts=(0,))

    with pytest.raises(TsError) as info:
        build_ts_index(empty, origin=0)

    assert info.value.message_key == TS_INVALID


def test_build_ts_index_result_feeds_snap_to_frame():
    """build_ts_index의 frame_pts는 snap_to_frame에 그대로 넣을 수 있어야 한다.

    30fps 프레임 4개(0 ~ 0.1초), 명목 시각 0.07초
    -> 프레임 2
    """
    index = build_ts_index(parse_ts(build_ts(_reordered_frames())), origin=3000)

    assert snap_to_frame(0.07, index.frame_pts, 30, "start") == 2

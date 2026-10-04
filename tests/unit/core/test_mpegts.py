"""MPEG-TS 세그먼트 해석(core/api/mpegts.py) 단위 테스트 (#309).

핵심 계약:
- 영상 PES의 PTS·DTS와 키프레임을 프레임별로 읽는다. PES가 여러 패킷에 걸쳐도 같다
- 색인의 시각은 VOD 시작(origin) = 0이고, 프레임은 PTS 순서다
- 33비트 랩어라운드를 풀어 시각이 이어진다
- 프레임 길이는 PTS 간격에서, 오디오가 끝나는 시각은 마지막 오디오 PES에 든 프레임 수로 구한다
- 이어지는 세그먼트의 영상 시각 범위는 틈도 겹침도 없이 맞닿는다

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
    ts_video_span,
)
from core.models.ts_index import TsIndex, TsStreams
from core.utils.timecode import snap_to_frame
from tests.unit.core.ts_builder import (
    VIDEO_PID,
    Frame,
    adts_frame,
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


# ================================================================ 프레임 길이 · 오디오의 끝 · 시각 범위
#
# 암호화 VOD의 세그먼트에서 본 구조를 옮긴 합성 세그먼트다(값은 실제 것이 아니다).
# - 영상 60fps · 세그먼트 4초(240프레임) · GOP 2초 · B프레임(재정렬 지연 1프레임)
# - 오디오 48kHz AAC(ADTS) · PES 하나에 24프레임(512ms) · 세그먼트의 마지막 PES만 더 적다
# - VOD의 0초는 첫 오디오 PTS이고, 첫 세그먼트의 영상은 그보다 42.667ms 늦게 시작한다

FRAME_60 = 1500  # 60fps 한 프레임의 틱 수 (90,000 ÷ 60)
AAC_FRAME = 1920  # 48kHz AAC 한 프레임의 틱 수 (1024 × 90,000 ÷ 48,000)
ORIGIN = 90_000  # 합성 VOD의 0초 — 첫 세그먼트의 첫 오디오 PTS(원시 틱)
VIDEO_LEAD = 3840  # 첫 세그먼트에서 영상이 오디오보다 늦게 시작하는 틱 수 (42.667ms)
SEGMENT_FRAMES = 240  # 4초
SEGMENT_TICKS = SEGMENT_FRAMES * FRAME_60

# 세그먼트마다 오디오 PES에 든 프레임 수 — 마지막 PES만 24보다 적다
AUDIO_COUNTS = (
    (24,) * 7 + (21,),  # 189프레임 = 4.032초
    (24,) * 7 + (20,),  # 188프레임
    (24,) * 7 + (19,),  # 187프레임
)


def _gop_frames(first_pts: int, count: int, frame_ticks: int = FRAME_60, gop: int = 120) -> list:
    """디코드 순서의 영상 프레임 — GOP마다 I, 그 뒤로 (P, B) 쌍, 끝에 P. 재정렬 지연은 한 프레임이다.

    표시 순서 n번째 프레임의 PTS는 first_pts + n × frame_ticks이고, DTS는 디코드 순서대로
    (first_pts − frame_ticks)부터 한 프레임씩 는다.
    """
    order = []
    for start in range(0, count, gop):
        size = min(gop, count - start)
        order.append(start)
        for pair in range(1, size - 1, 2):
            order += [start + pair + 1, start + pair]
        if size > 1 and size % 2 == 0:
            order.append(start + size - 1)
    assert sorted(order) == list(range(count))
    return [
        Frame(
            pts=first_pts + display * frame_ticks,
            dts=first_pts - frame_ticks + position * frame_ticks,
            idr=display % gop == 0,
        )
        for position, display in enumerate(order)
    ]


def _segment_audio(number: int) -> list[tuple[int, int]]:
    """number번째 합성 세그먼트의 오디오 PES — (PTS, 든 프레임 수). 앞 세그먼트의 오디오에 이어진다."""
    pts = ORIGIN + sum(sum(counts) for counts in AUDIO_COUNTS[:number]) * AAC_FRAME
    found = []
    for count in AUDIO_COUNTS[number]:
        found.append((pts, count))
        pts += count * AAC_FRAME
    return found


def _segment_video_start(number: int) -> int:
    """number번째 합성 세그먼트의 첫 영상 PTS(원시 틱)."""
    return ORIGIN + VIDEO_LEAD + number * SEGMENT_TICKS


def _segment(number: int) -> bytes:
    """number번째 합성 세그먼트(0 · 1 · 2)."""
    frames = _gop_frames(_segment_video_start(number), SEGMENT_FRAMES)
    return build_ts(frames, adts=_segment_audio(number))


def _index_of(number: int) -> TsIndex:
    return build_ts_index(parse_ts(_segment(number)), ORIGIN)


def test_synthetic_segments_carry_the_observed_layout():
    """합성 세그먼트는 옮기려던 구조여야 한다 — 영상이 오디오보다 늦게 시작하고, 키프레임이 2초마다 있고, 재정렬 지연이 한 프레임이다.

    합성 세그먼트 0 · 1 · 2
    -> (첫 영상 PTS − 첫 오디오 PTS) = 3840 · 960 · 0틱(42.667 · 10.667 · 0ms),
       세그먼트마다 프레임 240개 · 키프레임 0 · 120번째 · 키프레임의 (PTS − DTS) = 1500틱
    """
    leads = []
    for number in range(3):
        streams = parse_ts(_segment(number))
        leads.append(min(streams.video_pts) - streams.audio_pts[0])
        index = build_ts_index(streams, ORIGIN)
        assert len(index.frame_pts) == SEGMENT_FRAMES
        assert index.keyframes == (0, 120)
        assert streams.video_pts[0] - streams.video_dts[0] == FRAME_60

    assert leads == [3840, 960, 0]


def test_parse_ts_counts_adts_frames_in_each_audio_pes():
    """parse_ts는 ADTS 오디오 PES마다 든 프레임 수와 표본화율을 읽어야 한다.

    합성 세그먼트 0 — 오디오 PES 8개, 프레임 수 24 × 7 + 21, 48kHz. PES 하나가 여러 패킷에 걸친다
    -> audio_frames == (24, 24, 24, 24, 24, 24, 24, 21), audio_sample_rate == 48000
    """
    streams = parse_ts(_segment(0))

    assert streams.audio_frames == AUDIO_COUNTS[0]
    assert len(streams.audio_frames) == len(streams.audio_pts)
    assert streams.audio_sample_rate == 48000


def test_parse_ts_counts_every_aac_frame_inside_one_adts_frame():
    """parse_ts는 ADTS 프레임 하나에 AAC 프레임이 여럿 들었으면 머리에 적힌 수만큼 세야 한다.

    오디오 PES 하나 — AAC 프레임 2개짜리 ADTS 프레임 3개
    -> audio_frames == (6,)
    """
    data = build_ts([Frame(pts=3000, idr=True)])
    data += audio_pes(0, payload=adts_frame(blocks=2) * 3)

    assert parse_ts(data).audio_frames == (6,)


@pytest.mark.parametrize(
    ("audio_type", "payload"),
    [
        (0x0F, b"\xbb" * 30),  # ADTS 스트림인데 본문이 ADTS 머리로 시작하지 않는다
        (0x0F, b"\x00" + adts_frame() * 4),  # 머리 앞에 다른 바이트가 있다
        (0x11, adts_frame() * 4),  # LATM 스트림 — 본문이 ADTS처럼 보여도 세지 않는다
    ],
    ids=["not-adts-body", "offset-body", "latm-stream"],
)
def test_audio_end_is_unknown_when_frames_cannot_be_counted(audio_type, payload):
    """오디오 PES의 프레임 수를 읽을 수 없으면 프레임 수는 0이고 오디오가 끝나는 시각은 None이어야 한다.

    주석의 오디오 PES 하나(PTS 3000), 영상 프레임 하나
    -> audio_frames == (0,), audio_end is None, audio_pts는 그대로 읽힌다
    """
    data = pat_packet() + pmt_packet(audio_type=audio_type)
    data += video_frame(Frame(pts=3000, idr=True)) + audio_pes(3000, payload=payload)

    streams = parse_ts(data)
    index = build_ts_index(streams, origin=3000)

    assert streams.audio_frames == (0,)
    assert index.audio_pts == (0.0,)
    assert index.audio_end is None


def test_audio_end_is_none_without_audio():
    """오디오 PES가 없으면 오디오가 끝나는 시각은 None이어야 한다.

    영상 프레임만 든 TS
    -> audio_end is None
    """
    index = build_ts_index(parse_ts(build_ts(_reordered_frames())), origin=3000)

    assert index.audio_end is None


@pytest.mark.parametrize("number", [0, 1])
def test_audio_end_meets_the_first_audio_pts_of_the_next_segment(number):
    """세그먼트의 오디오가 끝나는 시각은 다음 세그먼트의 첫 오디오 PTS와 같아야 한다.

    합성 세그먼트 number와 number + 1 (마지막 오디오 PES의 프레임 수 21 · 20)
    -> audio_end == 다음 세그먼트의 audio_pts[0] == (다음 세그먼트의 첫 오디오 PTS − ORIGIN) ÷ 90,000
    """
    index = _index_of(number)
    following = _index_of(number + 1)
    expected = (_segment_audio(number + 1)[0][0] - ORIGIN) / 90_000

    assert index.audio_end == pytest.approx(expected, abs=1e-9)
    assert following.audio_pts[0] == pytest.approx(expected, abs=1e-9)


def test_audio_end_of_joined_segments_is_the_end_of_the_last_one():
    """세그먼트를 이어 붙인 것의 오디오가 끝나는 시각은 마지막 세그먼트의 것이어야 한다.

    합성 세그먼트 0 · 1을 이은 bytes
    -> audio_frames == 두 세그먼트의 것을 이은 것,
       audio_end == (세그먼트 2의 첫 오디오 PTS − ORIGIN) ÷ 90,000
    """
    streams = parse_ts(_segment(0) + _segment(1))
    index = build_ts_index(streams, ORIGIN)

    assert streams.audio_frames == AUDIO_COUNTS[0] + AUDIO_COUNTS[1]
    assert index.audio_end == pytest.approx((_segment_audio(2)[0][0] - ORIGIN) / 90_000, abs=1e-9)


@pytest.mark.parametrize(
    ("frame_ticks", "expected"),
    [(1500, 1 / 60), (3000, 1 / 30), (3600, 1 / 25)],
    ids=["60fps", "30fps", "25fps"],
)
def test_frame_duration_is_measured_from_pts_intervals(frame_ticks, expected):
    """프레임 길이는 표시 순서로 이웃한 프레임의 PTS 간격에서 재야 한다.

    B프레임이 든 24프레임(GOP 12), 프레임 간격은 주석의 틱
    -> frame_duration == 주석의 값
    """
    frames = _gop_frames(ORIGIN, 24, frame_ticks=frame_ticks, gop=12)

    index = build_ts_index(parse_ts(build_ts(frames)), ORIGIN)

    assert index.frame_duration == pytest.approx(expected, abs=1e-12)


def test_frame_duration_ignores_the_longer_interval_of_a_missing_frame():
    """프레임이 빠진 자리의 긴 간격은 프레임 길이에 들어가지 않아야 한다.

    60fps 프레임 6개에서 넷째를 뺀 것 — 간격 1500 · 1500 · 3000 · 1500틱
    -> frame_duration == 1/60
    """
    frames = [Frame(pts=n * FRAME_60, idr=n == 0) for n in (0, 1, 2, 4, 5)]

    index = build_ts_index(parse_ts(build_ts(frames)), origin=0)

    assert index.frame_duration == pytest.approx(1 / 60, abs=1e-12)


def test_frame_duration_is_none_for_a_single_frame():
    """프레임이 하나뿐이면 프레임 길이는 None이어야 한다.

    영상 프레임 하나
    -> frame_duration is None
    """
    index = build_ts_index(parse_ts(build_ts([Frame(pts=3000, idr=True)])), origin=3000)

    assert index.frame_duration is None


def test_video_spans_of_consecutive_segments_meet_without_gap_or_overlap():
    """이어지는 세그먼트의 영상 시각 범위는 앞의 끝과 뒤의 시작이 같아야 한다.

    합성 세그먼트 0 · 1 · 2 (각 240프레임, 60fps)
    -> 범위 == (세그먼트의 첫 영상 PTS − ORIGIN) ÷ 90,000 부터 4초, 앞의 끝 == 뒤의 시작
    """
    spans = [ts_video_span(_index_of(number)) for number in range(3)]

    for number, (begin, end) in enumerate(spans):
        expected = (_segment_video_start(number) - ORIGIN) / 90_000
        assert begin == pytest.approx(expected, abs=1e-9)
        assert end - begin == pytest.approx(4.0, abs=1e-9)
    assert spans[0][1] == pytest.approx(spans[1][0], abs=1e-9)
    assert spans[1][1] == pytest.approx(spans[2][0], abs=1e-9)


def test_video_span_of_a_30fps_segment_ends_one_frame_after_the_last_pts():
    """영상 시각 범위의 끝은 마지막 PTS에 그 영상의 프레임 길이를 더한 값이어야 한다.

    30fps 프레임 30개(0초부터)
    -> 범위 == (0.0, 1.0)
    """
    frames = _gop_frames(ORIGIN, 30, frame_ticks=3000, gop=30)

    begin, end = ts_video_span(build_ts_index(parse_ts(build_ts(frames)), ORIGIN))

    assert (begin, end) == (pytest.approx(0.0, abs=1e-9), pytest.approx(1.0, abs=1e-9))


def test_video_span_of_a_single_frame_needs_a_frame_duration():
    """프레임이 하나뿐인 색인의 시각 범위는 프레임 길이를 받아야 구할 수 있어야 한다.

    영상 프레임 하나(0초)
    -> 길이 없이: TsError(TS_UNSUPPORTED) / 길이 1/60: (0.0, 1/60)
    """
    index = build_ts_index(parse_ts(build_ts([Frame(pts=3000, idr=True)])), origin=3000)

    with pytest.raises(TsError) as info:
        ts_video_span(index)
    assert info.value.message_key == TS_UNSUPPORTED
    assert ts_video_span(index, 1 / 60) == (0.0, pytest.approx(1 / 60))

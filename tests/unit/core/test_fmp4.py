"""fMP4 세그먼트 해석(core/api/fmp4.py) 단위 테스트 (#309).

핵심 계약:
- 초기화 세그먼트에서 트랙의 timescale·코덱·편집 목록·trex 기본값을 읽는다
- 미디어 세그먼트의 moof에서 샘플별 DTS·PTS·길이·크기·키프레임을 읽는다.
  값은 trun → tfhd → trex 순으로 찾는다
- 여러 세그먼트를 이은 색인의 시각은 편집 목록을 적용한 값이고 VOD 시작 = 0이다
- mdat 본문 없이 세그먼트의 앞부분만으로 읽을 수 있다

입력은 tests/unit/core/fmp4_builder.py가 상자를 직접 조립한 합성 fMP4다.
"""

import dataclasses
import struct
import tracemalloc
from fractions import Fraction

import pytest

import core.api.fmp4 as fmp4_module
from core.api.fmp4 import (
    build_fmp4_index,
    fmp4_origin,
    parse_init_segment,
    parse_media_segment,
    read_media_segment,
    scan_moof,
)
from core.api.mp4 import (
    MP4_INVALID,
    MP4_TOO_LONG,
    MP4_TRUNCATED,
    MP4_UNSUPPORTED,
    Mp4Error,
)
from core.models.fmp4_index import Fmp4Init
from core.utils.timecode import snap_to_frame
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
from tests.unit.core.mp4_builder import box

VIDEO = 1  # 영상 트랙 번호
AUDIO = 2  # 오디오 트랙 번호
TICK = 100  # 영상 한 프레임의 틱 수 (timescale 1000, 10fps)


def _init(video_edits=((1200, TICK),), video_trex=(TICK, 50, NON_KEY), audio=True):
    """영상(timescale 1000) · 오디오(timescale 8000) 초기화 세그먼트의 해석 결과."""
    tracks = [
        InitTrack(
            VIDEO, b"vide", 1000, edits=list(video_edits) if video_edits else None, trex=video_trex
        )
    ]
    if audio:
        tracks.append(InitTrack(AUDIO, b"soun", 8000, codec=b"mp4a", trex=(1024, 7, KEY)))
    return parse_init_segment(init_segment(tracks))


def _reordered(first_flags: int | None = KEY) -> Run:
    """디코드 순서 I P B B — composition offset 100 · 300 · 0 · 0 (표시 순서 I B B P)."""
    return Run([Sample(composition=c) for c in (100, 300, 0, 0)], first_sample_flags=first_flags)


def _video_segment(decode_time: int = 0) -> bytes:
    return media_segment([Fragment([Traf(VIDEO, [_reordered()], decode_time=decode_time)])])


# ================================================================ 초기화 세그먼트


def test_parse_init_segment_reads_track_basics():
    """parse_init_segment는 트랙의 번호·종류·코덱·timescale을 돌려줘야 한다.

    영상 트랙 1(avc1, timescale 1000), 오디오 트랙 2(mp4a, timescale 8000)
    -> video = (1, "vide", "avc1", 1000), audio = (2, "soun", "mp4a", 8000)
    """
    init = _init()

    assert (init.video.track_id, init.video.handler, init.video.codec, init.video.timescale) == (
        1,
        "vide",
        "avc1",
        1000,
    )
    assert (init.audio.track_id, init.audio.handler, init.audio.codec, init.audio.timescale) == (
        2,
        "soun",
        "mp4a",
        8000,
    )


def test_parse_init_segment_reads_trex_defaults():
    """parse_init_segment는 trex의 기본 길이·크기·플래그를 트랙마다 돌려줘야 한다.

    영상 trex = (100, 50, NON_KEY), 오디오 trex = (1024, 7, KEY)
    -> 같은 값
    """
    init = _init()

    assert (init.video.default_duration, init.video.default_size, init.video.default_flags) == (
        100,
        50,
        NON_KEY,
    )
    assert (init.audio.default_duration, init.audio.default_size, init.audio.default_flags) == (
        1024,
        7,
        KEY,
    )


def test_parse_init_segment_reads_edit_list():
    """parse_init_segment는 편집 목록의 빈 편집 길이와 media_time을 돌려줘야 한다.

    영상 elst = [(250, -1), (1200, 100)] (무비 timescale 1000)
    -> empty_edit == 1/4초, media_time == 100
    """
    init = _init(video_edits=((250, -1), (1200, 100)))

    assert (init.video.empty_edit, init.video.media_time) == (Fraction(1, 4), 100)


def test_parse_init_segment_reads_video_only_stream():
    """parse_init_segment는 오디오 트랙이 없으면 audio를 None으로 돌려줘야 한다.

    영상 트랙만 있는 초기화 세그먼트
    -> audio is None
    """
    assert _init(audio=False).audio is None


def test_parse_init_segment_rejects_moov_without_mvex():
    """parse_init_segment는 moov에 mvex가 없으면 미지원 키로 Mp4Error를 내야 한다.

    mvex 없는 moov
    -> message_key == MP4_UNSUPPORTED
    """
    with pytest.raises(Mp4Error) as info:
        parse_init_segment(init_segment([InitTrack(VIDEO, b"vide", 1000)], mvex=False))

    assert info.value.message_key == MP4_UNSUPPORTED


def test_parse_init_segment_rejects_stream_without_video_track():
    """parse_init_segment는 영상 트랙이 없으면 미지원 키로 Mp4Error를 내야 한다.

    오디오 트랙만 있는 초기화 세그먼트
    -> message_key == MP4_UNSUPPORTED
    """
    with pytest.raises(Mp4Error) as info:
        parse_init_segment(init_segment([InitTrack(AUDIO, b"soun", 8000, codec=b"mp4a")]))

    assert info.value.message_key == MP4_UNSUPPORTED


def test_parse_init_segment_rejects_bytes_without_moov():
    """parse_init_segment는 moov가 없으면 손상 키로 Mp4Error를 내야 한다.

    ftyp 상자만 있는 bytes
    -> message_key == MP4_INVALID
    """
    with pytest.raises(Mp4Error) as info:
        parse_init_segment(box(b"ftyp", b"iso5" + bytes(4)))

    assert info.value.message_key == MP4_INVALID


# ================================================================ 미디어 세그먼트


def test_parse_media_segment_adds_composition_offset_to_decode_time():
    """parse_media_segment는 PTS를 DTS + composition offset으로 돌려줘야 한다.

    디코드 순서 I P B B, 길이 100(trex 기본값), composition offset = 100 · 300 · 0 · 0, tfdt = 0
    -> decode_times == (0, 100, 200, 300), presentation_times == (100, 400, 200, 300)
    """
    segment = parse_media_segment(_video_segment(), _init())

    assert segment.video.decode_times == (0, 100, 200, 300)
    assert segment.video.presentation_times == (100, 400, 200, 300)


def test_parse_media_segment_starts_decode_time_at_tfdt():
    """parse_media_segment는 DTS를 0이 아니라 tfdt의 값부터 누적해야 한다.

    tfdt = 90000, 길이 100
    -> decode_times == (90000, 90100, 90200, 90300)
    """
    segment = parse_media_segment(_video_segment(decode_time=90000), _init())

    assert segment.video.decode_times == (90000, 90100, 90200, 90300)


@pytest.mark.parametrize("version", [0, 1])
def test_parse_media_segment_reads_both_tfdt_versions(version):
    """parse_media_segment는 tfdt가 32비트(버전 0)든 64비트(버전 1)든 시작 DTS를 읽어야 한다.

    tfdt = 4,000,000,000 (버전 0 · 1)
    -> decode_times[0] == 4,000,000,000
    """
    traf = Traf(VIDEO, [_reordered()], decode_time=4_000_000_000, tfdt_version=version)

    segment = parse_media_segment(media_segment([Fragment([traf])]), _init())

    assert segment.video.decode_times[0] == 4_000_000_000


def test_parse_media_segment_reads_64bit_tfdt_beyond_32_bits():
    """parse_media_segment는 2^32를 넘는 64비트 tfdt를 그대로 돌려줘야 한다.

    tfdt = 2^32 + 500 (버전 1)
    -> decode_times[0] == 2^32 + 500
    """
    traf = Traf(VIDEO, [_reordered()], decode_time=(1 << 32) + 500)

    segment = parse_media_segment(media_segment([Fragment([traf])]), _init())

    assert segment.video.decode_times[0] == (1 << 32) + 500


def test_parse_media_segment_falls_back_to_trex_defaults():
    """parse_media_segment는 trun과 tfhd에 값이 없으면 trex의 기본 길이·크기를 써야 한다.

    trex = (100, 50, …), tfhd 기본값 없음, trun에 길이·크기 없음
    -> durations == (100,) × 4, sizes == (50,) × 4
    """
    segment = parse_media_segment(_video_segment(), _init())

    assert segment.video.durations == (100,) * 4
    assert segment.video.sizes == (50,) * 4


def test_parse_media_segment_prefers_tfhd_defaults_over_trex():
    """parse_media_segment는 tfhd에 기본값이 있으면 trex 대신 그 값을 써야 한다.

    trex = (100, 50, …), tfhd = (기본 길이 200, 기본 크기 70)
    -> durations == (200,) × 4, sizes == (70,) × 4, decode_times == (0, 200, 400, 600)
    """
    traf = Traf(VIDEO, [_reordered()], default_duration=200, default_size=70)

    segment = parse_media_segment(media_segment([Fragment([traf])]), _init())

    assert segment.video.durations == (200,) * 4
    assert segment.video.sizes == (70,) * 4
    assert segment.video.decode_times == (0, 200, 400, 600)


def test_parse_media_segment_prefers_trun_values_over_defaults():
    """parse_media_segment는 trun에 샘플별 길이·크기가 있으면 기본값 대신 그 값을 써야 한다.

    tfhd 기본 길이 200, trun 샘플별 길이 = 90 · 110, 크기 = 11 · 13
    -> durations == (90, 110), sizes == (11, 13), decode_times == (0, 90)
    """
    run = Run([Sample(duration=90, size=11), Sample(duration=110, size=13)])
    traf = Traf(VIDEO, [run], default_duration=200)

    segment = parse_media_segment(media_segment([Fragment([traf])]), _init())

    assert segment.video.durations == (90, 110)
    assert segment.video.sizes == (11, 13)
    assert segment.video.decode_times == (0, 90)


def test_parse_media_segment_marks_first_sample_from_first_sample_flags():
    """parse_media_segment는 first_sample_flags가 키프레임이면 기본 플래그가 아니어도 첫 샘플을 키프레임으로 돌려줘야 한다.

    trex 기본 플래그 = NON_KEY, trun first_sample_flags = KEY, 샘플별 플래그 없음
    -> sync_samples == (0,)
    """
    segment = parse_media_segment(_video_segment(), _init())

    assert segment.video.sync_samples == (0,)


def test_parse_media_segment_uses_default_flags_without_first_sample_flags():
    """parse_media_segment는 first_sample_flags도 샘플별 플래그도 없으면 모든 샘플에 기본 플래그를 써야 한다.

    trex 기본 플래그 = NON_KEY, trun에 플래그 없음
    -> sync_samples == ()
    """
    traf = Traf(VIDEO, [_reordered(first_flags=None)])

    segment = parse_media_segment(media_segment([Fragment([traf])]), _init())

    assert segment.video.sync_samples == ()


def test_parse_media_segment_prefers_tfhd_default_flags_over_trex():
    """parse_media_segment는 tfhd에 기본 플래그가 있으면 trex 대신 그 값을 써야 한다.

    trex 기본 플래그 = NON_KEY, tfhd 기본 플래그 = KEY, trun에 플래그 없음
    -> sync_samples == (0, 1, 2, 3)
    """
    traf = Traf(VIDEO, [_reordered(first_flags=None)], default_flags=KEY)

    segment = parse_media_segment(media_segment([Fragment([traf])]), _init())

    assert segment.video.sync_samples == (0, 1, 2, 3)


def test_parse_media_segment_reads_per_sample_flags():
    """parse_media_segment는 trun에 샘플별 플래그가 있으면 그 값으로 키프레임을 돌려줘야 한다.

    샘플별 플래그 = KEY · NON_KEY · KEY · NON_KEY
    -> sync_samples == (0, 2)
    """
    run = Run([Sample(flags=flag) for flag in (KEY, NON_KEY, KEY, NON_KEY)])

    segment = parse_media_segment(media_segment([Fragment([Traf(VIDEO, [run])])]), _init())

    assert segment.video.sync_samples == (0, 2)


def test_parse_media_segment_reads_signed_composition_offset():
    """parse_media_segment는 trun 버전 1의 음수 composition offset을 부호 있는 값으로 읽어야 한다.

    trun 버전 1, composition offset = 0 · −100, tfdt = 1000, 길이 100
    -> presentation_times == (1000, 1000)
    """
    run = Run([Sample(composition=0), Sample(composition=-100)], version=1)

    segment = parse_media_segment(
        media_segment([Fragment([Traf(VIDEO, [run], decode_time=1000)])]), _init()
    )

    assert segment.video.presentation_times == (1000, 1000)


def test_parse_media_segment_joins_every_moof_in_the_segment():
    """parse_media_segment는 세그먼트에 moof가 둘이면 두 moof의 샘플을 모두 돌려줘야 한다.

    moof 둘 — 각각 영상 4샘플, tfdt = 0 · 400
    -> fragments == 2, decode_times == (0, 100 … 700), sync_samples == (0, 4)
    """
    data = media_segment([
        Fragment([Traf(VIDEO, [_reordered()], decode_time=0)]),
        Fragment([Traf(VIDEO, [_reordered()], decode_time=400)]),
    ])  # fmt: skip

    segment = parse_media_segment(data, _init())

    assert segment.fragments == 2
    assert segment.video.decode_times == tuple(range(0, 800, 100))
    assert segment.video.sync_samples == (0, 4)


def test_parse_media_segment_continues_decode_time_across_truns():
    """parse_media_segment는 traf 하나에 trun이 둘이면 둘째 trun의 DTS를 첫째에 이어서 돌려줘야 한다.

    trun 둘 — 각각 2샘플, 길이 100, tfdt = 1000
    -> decode_times == (1000, 1100, 1200, 1300)
    """
    runs = [Run([Sample(composition=0), Sample(composition=0)]) for _ in range(2)]

    segment = parse_media_segment(
        media_segment([Fragment([Traf(VIDEO, runs, decode_time=1000)])]), _init()
    )

    assert segment.video.decode_times == (1000, 1100, 1200, 1300)


def test_parse_media_segment_separates_tracks():
    """parse_media_segment는 traf의 트랙 번호로 영상과 오디오 샘플을 나눠 돌려줘야 한다.

    moof 하나에 영상 traf(4샘플)와 오디오 traf(3샘플, 길이 1024는 trex 기본값, tfdt = 2048)
    -> 영상 4샘플, 오디오 decode_times == (2048, 3072, 4096), 오디오 sync_samples == (0, 1, 2)
    """
    audio = Traf(AUDIO, [Run([Sample(size=7), Sample(size=7), Sample(size=7)])], decode_time=2048)
    data = media_segment([Fragment([Traf(VIDEO, [_reordered()]), audio])])

    segment = parse_media_segment(data, _init())

    assert len(segment.video.decode_times) == 4
    assert segment.audio.decode_times == (2048, 3072, 4096)
    assert segment.audio.sync_samples == (0, 1, 2)


def test_parse_media_segment_ignores_unknown_track():
    """parse_media_segment는 초기화 세그먼트에 없는 트랙 번호의 traf를 건너뛰어야 한다.

    트랙 9의 traf와 영상 traf
    -> 영상 4샘플, 오디오 0샘플
    """
    data = media_segment([Fragment([Traf(9, [_reordered()]), Traf(VIDEO, [_reordered()])])])

    segment = parse_media_segment(data, _init())

    assert len(segment.video.decode_times) == 4
    assert segment.audio.decode_times == ()


def test_parse_media_segment_rejects_traf_without_tfdt():
    """parse_media_segment는 traf에 tfdt가 없으면 미지원 키로 Mp4Error를 내야 한다.

    tfdt 없는 영상 traf
    -> message_key == MP4_UNSUPPORTED
    """
    data = media_segment([Fragment([Traf(VIDEO, [_reordered()], decode_time=None)])])

    with pytest.raises(Mp4Error) as info:
        parse_media_segment(data, _init())

    assert info.value.message_key == MP4_UNSUPPORTED


# ================================================================ 샘플 수 방어


def test_parse_media_segment_rejects_sample_count_that_runs_past_trun():
    """parse_media_segment는 trun의 샘플 수가 그 상자에 들어가는 수보다 많으면 뒤 상자를 읽지 않고 거부해야 한다.

    샘플별 composition offset 4개짜리 trun의 sample_count를 5로 바꿈 (뒤에 다른 상자가 이어진다)
    -> message_key == MP4_INVALID
    """
    fragment = Fragment([Traf(VIDEO, [_reordered()])], extra=box(b"free", bytes(16)))
    data = bytearray(media_segment([fragment]))
    struct.pack_into(">I", data, data.find(b"trun") + 8, 5)

    with pytest.raises(Mp4Error) as info:
        parse_media_segment(bytes(data), _init())

    assert info.value.message_key == MP4_INVALID


@pytest.mark.parametrize(
    ("limit", "passes"),
    [(8, True), (7, False)],  # 영상 8샘플 — 상한과 같다 · 상한 + 1
    ids=["at-limit", "over"],
)
def test_parse_media_segment_applies_sample_limit_across_moofs(monkeypatch, limit, passes):
    """parse_media_segment는 세그먼트 안 여러 moof의 샘플 수 합이 상한과 같으면 받고 넘으면 길이 초과 키로 거부해야 한다.

    moof 둘 × 영상 4샘플 = 8샘플, 영상 상한 8 · 7
    -> 통과 · MP4_TOO_LONG
    """
    monkeypatch.setattr(fmp4_module, "_MAX_SAMPLES", {b"vide": limit, b"soun": 100})
    data = media_segment(
        [Fragment([Traf(VIDEO, [_reordered()], decode_time=n * 400)]) for n in range(2)]
    )

    if passes:
        assert len(parse_media_segment(data, _init()).video.decode_times) == 8
    else:
        with pytest.raises(Mp4Error) as info:
            parse_media_segment(data, _init())
        assert info.value.message_key == MP4_TOO_LONG


def test_parse_media_segment_rejects_huge_default_only_run_before_allocating(monkeypatch):
    """parse_media_segment는 샘플별 칸이 없는 trun의 샘플 수가 상한을 넘으면 샘플을 펼치지 않고 거부해야 한다.

    샘플별 칸 없는 trun의 sample_count = 2,000,000, 영상 상한 1,000,000
    -> message_key == MP4_TOO_LONG, 최대 메모리 사용량 < 1MiB
    """
    monkeypatch.setattr(fmp4_module, "_MAX_SAMPLES", {b"vide": 1_000_000, b"soun": 100})
    data = bytearray(media_segment([Fragment([Traf(VIDEO, [Run([])])])]))
    struct.pack_into(">I", data, data.find(b"trun") + 8, 2_000_000)
    init = _init()

    tracemalloc.start()
    try:
        with pytest.raises(Mp4Error) as info:
            parse_media_segment(bytes(data), init)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert info.value.message_key == MP4_TOO_LONG
    assert peak < 1024 * 1024


def _default_only_segment(counts: list[int]) -> bytes:
    """샘플마다의 칸이 없는 영상 trun이 counts개 든 세그먼트 — 샘플 수 칸을 counts로 고쳐 쓴다."""
    data = bytearray(media_segment([Fragment([Traf(VIDEO, [Run([]) for _ in counts])])]))
    at = -1
    for count in counts:
        at = data.find(b"trun", at + 1)
        struct.pack_into(">I", data, at + 8, count)
    return bytes(data)


@pytest.mark.parametrize(
    ("counts", "passes"),
    [([1200], True), ([1201], False), ([700, 500], True), ([700, 501], False)],
    ids=["at-limit", "over", "two-runs-at-limit", "two-runs-over"],
)
def test_parse_media_segment_limits_default_only_runs_to_one_segment_worth(counts, passes):
    """parse_media_segment는 샘플마다의 칸이 없는 trun들의 샘플 수 합이 세그먼트 하나의 분량(120초)을 넘으면 길이 초과 키로 거부해야 한다.

    영상 timescale 1000, 기본 샘플 길이 100틱 -> 120초 = 1200샘플. trun의 샘플 수는 주석의 값
    -> 1200개까지 통과 · 넘으면 MP4_TOO_LONG (트랙 전체 상한 5,184,000보다 한참 아래다)
    """
    data = _default_only_segment(counts)

    if passes:
        assert len(parse_media_segment(data, _init()).video.decode_times) == sum(counts)
    else:
        with pytest.raises(Mp4Error) as info:
            parse_media_segment(data, _init())
        assert info.value.message_key == MP4_TOO_LONG


def test_parse_media_segment_rejects_small_moof_declaring_millions_before_allocating():
    """parse_media_segment는 작은 moof가 샘플마다의 칸 없이 수백만 샘플을 선언하면 펼치지 않고 거부해야 한다.

    샘플별 칸 없는 trun의 sample_count = 2,000,000 — 트랙 전체 상한(5,184,000) 안이다
    -> message_key == MP4_TOO_LONG, 최대 메모리 사용량 < 1MiB
    """
    data = _default_only_segment([2_000_000])
    init = _init()

    tracemalloc.start()
    try:
        with pytest.raises(Mp4Error) as info:
            parse_media_segment(data, init)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert info.value.message_key == MP4_TOO_LONG
    assert peak < 1024 * 1024


def test_parse_media_segment_rejects_default_only_run_without_a_sample_duration():
    """parse_media_segment는 샘플마다의 칸이 없는 trun에 기본 샘플 길이도 없으면 손상 키로 거부해야 한다.

    trex 기본 길이 0, tfhd 기본값 없음, 샘플별 칸 없는 trun의 sample_count = 5
    -> message_key == MP4_INVALID
    """
    data = _default_only_segment([5])

    with pytest.raises(Mp4Error) as info:
        parse_media_segment(data, _init(video_trex=(0, 50, NON_KEY)))

    assert info.value.message_key == MP4_INVALID


# ================================================================ 앞부분만으로 읽기


def test_scan_moof_reports_where_the_first_mdat_starts():
    """scan_moof는 세그먼트 전체를 받으면 첫 mdat가 시작하는 위치와 끝나는 위치를 돌려줘야 한다.

    styp · moof · mdat (moof 하나)
    -> complete, moof_end == mdat 상자의 위치, next_offset == 세그먼트 길이
    """
    data = _video_segment()

    scan = scan_moof(data)

    assert scan.complete is True
    assert scan.moof_end == data.find(b"mdat") - 4
    assert scan.next_offset == len(data)


def test_scan_moof_is_complete_once_the_mdat_header_is_in_view():
    """scan_moof는 mdat 머리 8바이트까지만 받아도 complete로 돌려줘야 한다.

    세그먼트의 앞 (mdat 위치 + 8)바이트
    -> complete, moof_end == mdat 상자의 위치
    """
    data = _video_segment()
    mdat = data.find(b"mdat") - 4

    scan = scan_moof(data[: mdat + 8])

    assert (scan.complete, scan.moof_end) == (True, mdat)


def test_scan_moof_asks_for_more_when_moof_is_cut_short():
    """scan_moof는 받은 bytes가 moof 도중에 끝나면 complete가 아니고 mdat가 시작할 위치를 돌려줘야 한다.

    세그먼트의 앞 40바이트 (styp와 moof 머리까지)
    -> complete 아님, moof_end == mdat 상자의 위치, next_offset is None
    """
    data = _video_segment()

    scan = scan_moof(data[:40])

    assert (scan.complete, scan.moof_end, scan.next_offset) == (False, data.find(b"mdat") - 4, None)


def test_scan_moof_points_at_second_fragment():
    """scan_moof는 moof가 둘인 세그먼트에서 next_offset으로 둘째 moof의 위치를 돌려줘야 한다.

    styp · moof · mdat · moof · mdat
    -> next_offset == 둘째 moof 상자의 위치 (세그먼트 길이보다 작다)
    """
    data = media_segment(
        [Fragment([Traf(VIDEO, [_reordered()], decode_time=n * 400)]) for n in range(2)]
    )

    scan = scan_moof(data)

    assert scan.next_offset == data.rfind(b"moof") - 4
    assert scan.next_offset < len(data)


def test_parse_media_segment_reads_prefix_that_ends_at_moof_end():
    """parse_media_segment는 mdat 본문 없이 moof가 끝나는 위치까지만 받아도 전체를 받은 것과 같은 샘플을 돌려줘야 한다.

    세그먼트 전체 · data[:moof_end]
    -> 영상 샘플이 같음
    """
    data = _video_segment(decode_time=90000)
    init = _init()

    prefix = parse_media_segment(data[: scan_moof(data).moof_end], init)

    assert prefix.video == parse_media_segment(data, init).video


def test_parse_media_segment_rejects_moof_cut_short():
    """parse_media_segment는 bytes가 moof 도중에 끝나면 손상 키로 Mp4Error를 내야 한다.

    세그먼트의 앞 40바이트
    -> message_key == MP4_INVALID
    """
    with pytest.raises(Mp4Error) as info:
        parse_media_segment(_video_segment()[:40], _init())

    assert info.value.message_key == MP4_INVALID


# ================================================================ 색인


def test_fmp4_origin_is_earliest_presented_time_across_tracks():
    """fmp4_origin은 편집 목록을 적용한 시각으로 영상과 오디오를 통틀어 가장 이른 값을 돌려줘야 한다.

    영상: 빈 편집 0.25초, media_time 100, 첫 PTS 100 → 0.25초 / 오디오: 첫 PTS 800 (timescale 8000) → 0.1초
    -> 1/10초
    """
    init = _init(video_edits=((250, -1), (1200, 100)))
    audio = Traf(AUDIO, [Run([Sample(size=7), Sample(size=7)])], decode_time=800)
    segment = parse_media_segment(
        media_segment([Fragment([Traf(VIDEO, [_reordered()]), audio])]), init
    )

    assert fmp4_origin(init, segment) == Fraction(1, 10)


def test_build_fmp4_index_orders_frames_by_presentation_time():
    """build_fmp4_index는 프레임을 PTS 순서로 놓고 편집 목록의 media_time을 빼야 한다.

    디코드 순서 I P B B, PTS = 100 · 400 · 200 · 300, media_time 100, origin 0
    -> frame_pts = 0.0, 0.1, 0.2, 0.3초 / frame_samples == (0, 2, 3, 1) / keyframes == (0,)
    """
    init = _init(audio=False)
    segment = parse_media_segment(_video_segment(), init)

    index = build_fmp4_index(init, [segment], Fraction(0))

    assert index.frame_pts == pytest.approx([0.0, 0.1, 0.2, 0.3])
    assert index.frame_samples == (0, 2, 3, 1)
    assert index.keyframes == (0,)


def test_build_fmp4_index_keeps_decode_times_in_decode_order():
    """build_fmp4_index는 DTS를 디코드 순서 그대로 편집 목록을 적용한 초로 돌려줘야 한다.

    DTS = 0 · 100 · 200 · 300, media_time 100
    -> decode_times = −0.1, 0.0, 0.1, 0.2초
    """
    init = _init(audio=False)

    index = build_fmp4_index(init, [parse_media_segment(_video_segment(), init)], Fraction(0))

    assert index.decode_times == pytest.approx([-0.1, 0.0, 0.1, 0.2])


def test_build_fmp4_index_applies_empty_edit():
    """build_fmp4_index는 영상에 빈 편집이 있으면 그 길이만큼 영상을 늦게 놓아야 한다.

    영상 elst = [(250, -1), (1200, 100)], origin 0
    -> frame_pts[0] == 0.25초
    """
    init = _init(video_edits=((250, -1), (1200, 100)), audio=False)

    index = build_fmp4_index(init, [parse_media_segment(_video_segment(), init)], Fraction(0))

    assert index.frame_pts[0] == pytest.approx(0.25)


def test_build_fmp4_index_measures_time_from_origin():
    """build_fmp4_index는 origin을 0초로 삼아 그 뒤 세그먼트의 시각을 돌려줘야 한다.

    origin 2초, 세그먼트의 tfdt = 12000 (첫 PTS 12100, media_time 100 → 12초)
    -> frame_pts[0] == 10초
    """
    init = _init(audio=False)
    segment = parse_media_segment(_video_segment(decode_time=12000), init)

    index = build_fmp4_index(init, [segment], Fraction(2))

    assert index.frame_pts[0] == pytest.approx(10.0)


def test_build_fmp4_index_joins_segments():
    """build_fmp4_index는 여러 세그먼트의 프레임을 이어서 키프레임을 전체 프레임 번호로 돌려줘야 한다.

    세그먼트 둘 — 각각 4프레임, tfdt = 0 · 400
    -> 프레임 8개, frame_pts = 0.0 … 0.7초, keyframes == (0, 4)
    """
    init = _init(audio=False)
    segments = [parse_media_segment(_video_segment(decode_time=n * 400), init) for n in range(2)]

    index = build_fmp4_index(init, segments, Fraction(0))

    assert index.frame_pts == pytest.approx([n / 10 for n in range(8)])
    assert index.keyframes == (0, 4)


def test_build_fmp4_index_leaves_out_frames_hidden_by_edit_list():
    """build_fmp4_index는 PTS가 media_time보다 앞선 영상 샘플을 frame_pts에 넣지 않아야 한다.

    PTS = 100 · 400 · 200 · 300, media_time 250
    -> 프레임 2개 (PTS 300 · 400 → 0.05초 · 0.15초)
    """
    init = _init(video_edits=((1200, 250),), audio=False)

    index = build_fmp4_index(init, [parse_media_segment(_video_segment(), init)], Fraction(0))

    assert index.frame_pts == pytest.approx([0.05, 0.15])


def test_build_fmp4_index_converts_audio_samples():
    """build_fmp4_index는 오디오 샘플의 PTS를 origin 기준 초로 돌려줘야 한다.

    오디오 timescale 8000, tfdt = 8000, 샘플 길이 1024, origin 0.5초
    -> audio_pts = 0.5, 0.628초
    """
    init = _init()
    audio = Traf(AUDIO, [Run([Sample(size=7), Sample(size=7)])], decode_time=8000)
    segment = parse_media_segment(
        media_segment([Fragment([Traf(VIDEO, [_reordered()]), audio])]), init
    )

    index = build_fmp4_index(init, [segment], Fraction(1, 2))

    assert index.audio_pts == pytest.approx([0.5, 0.628])


def test_build_fmp4_index_rejects_segments_without_video_frames():
    """build_fmp4_index는 표시되는 영상 프레임이 없으면 손상 키로 Mp4Error를 내야 한다.

    영상 샘플이 없는 세그먼트
    -> message_key == MP4_INVALID
    """
    init = _init()
    audio_only = parse_media_segment(
        media_segment([Fragment([Traf(AUDIO, [Run([Sample(size=7)])])])]), init
    )

    with pytest.raises(Mp4Error) as info:
        build_fmp4_index(init, [audio_only], Fraction(0))

    assert info.value.message_key == MP4_INVALID


def test_build_fmp4_index_result_feeds_snap_to_frame():
    """build_fmp4_index의 frame_pts는 snap_to_frame에 그대로 넣을 수 있어야 한다.

    10fps 프레임 4개(0 ~ 0.3초), 명목 시각 0.22초
    -> 프레임 2
    """
    init = _init(audio=False)
    index = build_fmp4_index(init, [parse_media_segment(_video_segment(), init)], Fraction(0))

    assert snap_to_frame(0.22, index.frame_pts, 10, "start") == 2


# ================================================================ 리뷰 반영


@pytest.mark.parametrize("size", [2, 5, 7])
def test_scan_moof_rejects_mdat_smaller_than_its_header(size):
    """scan_moof는 mdat의 크기 칸이 머리(8바이트)보다 작으면 next_offset을 돌려주지 않고 손상 키로 거부해야 한다.

    mdat의 크기 칸을 2 · 5 · 7로 바꾼 세그먼트
    -> message_key == MP4_INVALID
    """
    data = bytearray(_video_segment())
    struct.pack_into(">I", data, data.find(b"mdat") - 4, size)

    with pytest.raises(Mp4Error) as info:
        scan_moof(bytes(data))

    assert info.value.message_key == MP4_INVALID


def test_scan_moof_accepts_mdat_that_runs_to_end_of_file():
    """scan_moof는 mdat의 크기 칸이 0(끝까지)이면 받고 next_offset을 None으로 돌려줘야 한다.

    mdat의 크기 칸을 0으로 바꾼 세그먼트
    -> complete, moof_end == mdat 상자의 위치, next_offset is None
    """
    data = bytearray(_video_segment())
    mdat = data.find(b"mdat") - 4
    struct.pack_into(">I", data, mdat, 0)

    scan = scan_moof(bytes(data))

    assert (scan.complete, scan.moof_end, scan.next_offset) == (True, mdat, None)


def test_parse_init_segment_rejects_video_and_audio_sharing_a_track_id():
    """parse_init_segment는 영상과 오디오의 트랙 번호가 같으면 손상 키로 Mp4Error를 내야 한다.

    영상 트랙 1, 오디오 트랙 1
    -> message_key == MP4_INVALID
    """
    tracks = [InitTrack(1, b"vide", 1000), InitTrack(1, b"soun", 8000, codec=b"mp4a")]

    with pytest.raises(Mp4Error) as info:
        parse_init_segment(init_segment(tracks))

    assert info.value.message_key == MP4_INVALID


def test_parse_media_segment_rejects_init_whose_tracks_share_an_id():
    """parse_media_segment는 직접 만든 Fmp4Init의 영상·오디오 트랙 번호가 같으면 손상 키로 Mp4Error를 내야 한다.

    정상 초기화 세그먼트의 오디오 track_id를 영상과 같은 1로 바꾼 Fmp4Init
    -> message_key == MP4_INVALID
    """
    init = _init()
    clashing = Fmp4Init(
        video=init.video, audio=dataclasses.replace(init.audio, track_id=init.video.track_id)
    )

    with pytest.raises(Mp4Error) as info:
        parse_media_segment(_video_segment(), clashing)

    assert info.value.message_key == MP4_INVALID


# ================================================================ 읽기 함수로 moof만 읽기 (#309)


def _logged_reader(data: bytes, log: list[tuple[int, int]]):
    """bytes를 파일처럼 읽어 주는 읽기 함수 — 실제로 돌려준 (위치, 길이)를 log에 남긴다."""

    def read(offset: int, size: int) -> bytes:
        found = data[offset : offset + size]
        log.append((offset, len(found)))
        return found

    return read


def test_read_media_segment_matches_parse_without_reading_mdat_bodies():
    """read_media_segment는 mdat 본문을 읽지 않고 parse_media_segment와 같은 결과를 내야 한다.

    moof 둘짜리 세그먼트(영상 4샘플씩), mdat 본문 100,000바이트씩
    -> 결과 == parse_media_segment(전체), 읽은 바이트의 합 < 2,000 (세그먼트는 20만 바이트가 넘는다)
    """
    data = media_segment(
        [
            Fragment([Traf(VIDEO, [_reordered()], decode_time=n * 400)], mdat_size=100_000)
            for n in range(2)
        ]
    )
    init = _init()
    log: list[tuple[int, int]] = []

    segment = read_media_segment(_logged_reader(data, log), init)

    assert segment == parse_media_segment(data, init)
    assert segment.fragments == 2
    assert len(data) > 200_000
    assert sum(length for _offset, length in log) < 2_000


def test_read_media_segment_stops_at_an_mdat_that_runs_to_the_end():
    """read_media_segment는 크기 0(끝까지)인 mdat를 만나면 그 앞의 moof까지만 읽어야 한다.

    moof 하나짜리 세그먼트의 mdat 크기 칸을 0으로 고쳐 씀
    -> 영상 4샘플, moof 1개
    """
    data = bytearray(_video_segment())
    struct.pack_into(">I", data, data.find(b"mdat") - 4, 0)

    segment = read_media_segment(_logged_reader(bytes(data), []), _init())

    assert (len(segment.video.decode_times), segment.fragments) == (4, 1)


@pytest.mark.parametrize("damage", ["oversized", "truncated", "undersized"])
def test_read_media_segment_rejects_a_damaged_box(monkeypatch, damage):
    """read_media_segment는 mdat가 아닌 상자가 상한보다 크거나 잘렸거나 머리보다 작으면 손상 키로 거부해야 한다.

    oversized: 상한을 16바이트로 낮춤 / truncated: moof 중간에서 자른 bytes / undersized: moof의 크기 칸을 4로 고침
    -> message_key == MP4_INVALID
    """
    data = bytearray(_video_segment())
    moof = data.find(b"moof") - 4
    if damage == "oversized":
        monkeypatch.setattr(fmp4_module, "_MAX_KEPT_BOX_BYTES", 16)
    elif damage == "truncated":
        del data[moof + 40 :]
    else:
        struct.pack_into(">I", data, moof, 4)

    with pytest.raises(Mp4Error) as info:
        read_media_segment(_logged_reader(bytes(data), []), _init())

    assert info.value.message_key == MP4_INVALID


@pytest.mark.parametrize(
    ("cut", "truncated"),
    [(0, False), (1, True), (30, True), (70, True)],
    ids=["whole", "one-byte-short", "inside-mdat", "inside-mdat-header"],
)
def test_read_media_segment_tells_a_truncated_segment_when_given_its_size(cut, truncated):
    """read_media_segment는 세그먼트의 크기를 받으면 상자들이 말하는 끝이 그 크기와 다른 세그먼트를 잘림 키로 거부해야 한다.

    moof 하나 · mdat 본문 64바이트인 세그먼트에서 끝 cut바이트를 뺀 것, total = 남은 길이
    -> 0이면 영상 4샘플, 그 밖은 Mp4Error(MP4_TRUNCATED)
    """
    data = _video_segment()
    data = data[: len(data) - cut]
    reader = _logged_reader(data, [])

    if truncated:
        with pytest.raises(Mp4Error) as info:
            read_media_segment(reader, _init(), len(data))
        assert info.value.message_key == MP4_TRUNCATED
    else:
        assert len(read_media_segment(reader, _init(), len(data)).video.decode_times) == 4


def test_read_media_segment_accepts_a_truncated_mdat_without_a_size():
    """read_media_segment는 크기를 받지 않으면 mdat가 잘린 세그먼트에서도 그 앞의 moof를 읽어야 한다.

    mdat 본문 도중에서 잘린 세그먼트, total 없음(앞부분만 받은 경우)
    -> 영상 4샘플
    """
    data = _video_segment()[:-30]

    assert len(read_media_segment(_logged_reader(data, []), _init()).video.decode_times) == 4

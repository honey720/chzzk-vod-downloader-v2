"""mp4의 moov 찾기·해석·받기(core/api/mp4.py) 단위 테스트 (#178).

핵심 계약:
- 프레임 PTS는 ctts와 편집 목록을 적용한 표시 시각이고, 가장 먼저 표시되는 샘플이 0이다
- 샘플 위치는 stsc 청크 구간 + stco/co64 + stsz로 계산한다
- moov가 mdat 뒤에 있어도 상자 크기를 따라 건너뛰어 찾는다
- 조각난 mp4는 키가 붙은 예외로 거부한다

입력은 tests/unit/core/mp4_builder.py가 상자를 직접 조립한 합성 mp4다.
"""

import struct
import tracemalloc
from fractions import Fraction

import pytest
import requests

import core.api.mp4 as mp4_module
from core.api.mp4 import (
    MP4_FRAGMENTED,
    MP4_INVALID,
    MP4_MOOV_NOT_FOUND,
    MP4_RANGE_MISMATCH,
    MP4_RANGE_NOT_SUPPORTED,
    MP4_TOO_LONG,
    MP4_UNSUPPORTED,
    Mp4Error,
    fetch_mp4_index,
    parse_moov,
    read_mp4_index,
    scan_top_level,
)
from core.utils.timecode import snap_to_frame
from tests.unit.core.mp4_builder import audio_spec, box, build_mp4, video_spec


def _reader(data: bytes, log: list[tuple[int, int]] | None = None):
    """bytes를 파일처럼 읽어 주는 읽기 함수 — 요청을 log에 남긴다."""

    def read(offset: int, size: int) -> bytes:
        if log is not None:
            log.append((offset, size))
        return data[offset : offset + size]

    return read


# ================================================================ 프레임 시각


def test_parse_moov_orders_frames_by_presentation_time():
    """parse_moov는 ctts를 적용해 프레임 PTS를 표시 순서로 돌려줘야 한다.

    디코드 순서 I P B B × 3, ctts=[100, 300, 0, 0] × 3, 샘플 길이 100, timescale 1000
    -> frame_pts = 0.0, 0.1 … 1.1 / frame_samples = (0,2,3,1, 4,6,7,5, 8,10,11,9)
    """
    index = parse_moov(build_mp4([video_spec(), audio_spec()]).moov)

    assert index.frame_pts == pytest.approx([n / 10 for n in range(12)])
    assert index.frame_samples == (0, 2, 3, 1, 4, 6, 7, 5, 8, 10, 11, 9)


def test_parse_moov_subtracts_edit_list_media_time():
    """parse_moov는 편집 목록의 media_time을 빼서 첫 프레임을 0초에 놓아야 한다.

    영상 첫 PTS = 100틱, elst media_time = 100
    -> frame_pts[0] == 0.0
    """
    index = parse_moov(build_mp4([video_spec(), audio_spec()]).moov)

    assert index.frame_pts[0] == 0.0


def test_parse_moov_keeps_video_delay_from_empty_edit():
    """parse_moov는 영상에 빈 편집이 있으면 그 길이만큼 영상을 늦게 시작시켜야 한다.

    영상 elst = [(250, -1), (1200, 100)] (무비 timescale 1000), 오디오는 0초 시작
    -> frame_pts[0] == 0.25
    """
    video = video_spec(edits=[(250, -1), (1200, 100)])

    index = parse_moov(build_mp4([video, audio_spec()]).moov)

    assert index.frame_pts[0] == pytest.approx(0.25)
    assert index.audio.times[1] == pytest.approx(0.0)


def test_parse_moov_sets_zero_at_earliest_presented_sample():
    """parse_moov는 두 트랙 모두 늦게 시작하면 가장 먼저 표시되는 샘플을 0초로 삼아야 한다.

    영상 빈 편집 250, 오디오 빈 편집 400 (무비 timescale 1000)
    -> frame_pts[0] == 0.0, 오디오 첫 표시 샘플 == 0.15
    """
    video = video_spec(edits=[(250, -1), (1200, 100)])
    audio = audio_spec(edits=[(400, -1), (1920, 1024)])

    index = parse_moov(build_mp4([video, audio]).moov)

    assert index.frame_pts[0] == pytest.approx(0.0)
    assert index.audio.times[1] == pytest.approx(0.15)


def test_parse_moov_marks_samples_hidden_by_edit_list_as_negative():
    """parse_moov는 편집 목록이 가린 오디오 샘플의 시각을 음수로 돌려줘야 한다.

    오디오 샘플 길이 1024틱(8000Hz), elst media_time = 1024
    -> times[0] == -0.128, times[1] == 0.0
    """
    index = parse_moov(build_mp4([video_spec(), audio_spec()]).moov)

    assert index.audio.times[0] == pytest.approx(-0.128)
    assert index.audio.times[1] == pytest.approx(0.0)


def test_parse_moov_reads_keyframes_from_stss():
    """parse_moov는 stss의 샘플 번호(1부터)를 키프레임의 프레임 번호로 바꿔야 한다.

    stss = [1, 5, 9]
    -> keyframes == (0, 4, 8), video.sync_samples == (0, 4, 8)
    """
    index = parse_moov(build_mp4([video_spec(), audio_spec()]).moov)

    assert index.keyframes == (0, 4, 8)
    assert index.video.sync_samples == (0, 4, 8)


def test_parse_moov_treats_every_sample_as_sync_without_stss():
    """parse_moov는 stss가 없는 트랙의 모든 샘플을 단독 디코드 가능으로 돌려줘야 한다.

    오디오 16샘플, stss 없음
    -> audio.sync_samples == (0 … 15)
    """
    index = parse_moov(build_mp4([video_spec(), audio_spec()]).moov)

    assert index.audio.sync_samples == tuple(range(16))


def test_parse_moov_reports_duration_and_frame_rate():
    """parse_moov는 마지막 프레임이 끝나는 시각과 timescale ÷ 샘플 길이를 돌려줘야 한다.

    12프레임 × 100틱, timescale 1000
    -> duration == 1.2, fps == 10
    """
    index = parse_moov(build_mp4([video_spec(), audio_spec()]).moov)

    assert index.duration == pytest.approx(1.2)
    assert index.fps == Fraction(10)


def test_parse_moov_frame_rate_keeps_exact_ratio():
    """parse_moov는 프레임률을 timescale과 샘플 길이의 정확한 비로 돌려줘야 한다.

    timescale 11988, 샘플 길이 400
    -> fps == 2997/100
    """
    video = video_spec(
        timescale=11988,
        deltas=[400] * 12,
        composition=[400, 1200, 0, 0] * 3,
        edits=[(400, 400)],
    )

    index = parse_moov(build_mp4([video]).moov)

    assert index.fps == Fraction(2997, 100)


def test_parse_moov_result_feeds_snap_to_frame():
    """parse_moov의 frame_pts와 fps는 snap_to_frame에 그대로 넣을 수 있어야 한다.

    명목 시각 0.52초, 10fps
    -> 프레임 5
    """
    index = parse_moov(build_mp4([video_spec(), audio_spec()]).moov)

    assert snap_to_frame(0.52, index.frame_pts, index.fps, "start") == 5


def test_parse_moov_reads_video_only_file():
    """parse_moov는 오디오 트랙이 없으면 audio를 None으로 돌려줘야 한다.

    영상 트랙만 있는 moov
    -> audio is None, 프레임 12개
    """
    index = parse_moov(build_mp4([video_spec()]).moov)

    assert index.audio is None
    assert len(index.frame_pts) == 12


# ================================================================ 샘플 위치


@pytest.mark.parametrize("co64", [False, True])
def test_parse_moov_locates_every_sample_in_the_file(co64):
    """parse_moov가 돌려준 샘플 위치·크기로 파일을 읽으면 그 샘플의 바이트가 나와야 한다.

    영상 청크 [3, 3, 2, 4]샘플(stsc 구간 3개), 오디오 청크 [4, 4, 4, 4], 청크는 번갈아 놓임, stco · co64
    -> 모든 샘플에서 data[offset : offset + size] == 조립기가 쓴 바이트
    """
    built = build_mp4([video_spec(), audio_spec()], co64=co64)

    index = parse_moov(built.moov)

    for handler, track in ((b"vide", index.video), (b"soun", index.audio)):
        assert list(track.offsets) == built.sample_offsets[handler]
        for sample, (offset, size) in enumerate(zip(track.offsets, track.sizes)):
            assert built.data[offset : offset + size] == built.sample_bytes(handler, sample, size)


def test_parse_moov_reads_uniform_sample_size():
    """parse_moov는 stsz의 고정 크기 칸이 0이 아니면 모든 샘플에 그 크기를 줘야 한다.

    오디오 stsz: sample_size=7, sample_count=16
    -> sizes == (7,) × 16
    """
    index = parse_moov(build_mp4([video_spec(), audio_spec()]).moov)

    assert index.audio.sizes == (7,) * 16


def test_parse_moov_reads_chunk_offsets_beyond_four_gigabytes():
    """parse_moov는 co64의 4GB를 넘는 청크 위치를 그대로 돌려줘야 한다.

    co64 청크 위치에 5,000,000,000을 더한 moov
    -> offsets[0] > 2**32, 이웃 샘플 간격은 샘플 크기와 같음
    """
    built = build_mp4([video_spec()], co64=True)
    moov = bytearray(built.moov)
    table = moov.find(b"co64") + 4  # 버전·플래그의 시작
    count = struct.unpack_from(">I", moov, table + 4)[0]
    offsets = struct.unpack_from(f">{count}Q", moov, table + 8)
    struct.pack_into(f">{count}Q", moov, table + 8, *(o + 5_000_000_000 for o in offsets))

    index = parse_moov(bytes(moov))

    assert index.video.offsets[0] == built.sample_offsets[b"vide"][0] + 5_000_000_000
    assert index.video.offsets[1] - index.video.offsets[0] == index.video.sizes[0]


# ================================================================ moov 찾기


def test_scan_top_level_finds_moov_before_mdat():
    """scan_top_level은 moov가 앞에 있으면 그 위치와 크기를 돌려줘야 한다.

    ftyp · moov · mdat
    -> moov == (조립기의 moov 위치, moov 크기)
    """
    built = build_mp4([video_spec(), audio_spec()])

    scan = scan_top_level(built.data[:64])

    assert scan.moov == (built.moov_offset, len(built.moov))


def test_scan_top_level_points_past_mdat_when_moov_is_not_in_view():
    """scan_top_level은 mdat 머리만 보여도 그 크기로 다음 상자 위치를 돌려줘야 한다.

    ftyp · mdat · moov 중 앞 40바이트만
    -> moov is None, next_offset == moov 위치
    """
    built = build_mp4([video_spec(), audio_spec()], moov_first=False)

    scan = scan_top_level(built.data[:40])

    assert scan.moov is None
    assert scan.next_offset == built.moov_offset


def test_scan_top_level_reads_64bit_box_size():
    """scan_top_level은 크기 칸이 1이면 뒤따르는 64비트 크기로 다음 상자 위치를 계산해야 한다.

    mdat 머리가 16바이트(64비트 크기)인 ftyp · mdat · moov 중 앞 48바이트만
    -> next_offset == moov 위치
    """
    built = build_mp4([video_spec()], moov_first=False, large_mdat_header=True)

    scan = scan_top_level(built.data[:48])

    assert scan.next_offset == built.moov_offset


def test_read_mp4_index_skips_mdat_body_when_moov_is_at_the_end(monkeypatch):
    """read_mp4_index는 moov가 mdat 뒤에 있으면 mdat 본문을 건너뛰어 moov만 읽어야 한다.

    ftyp(24B) · mdat · moov, 한 번에 24바이트씩 읽도록 줄임
    -> 요청 = [(0, 24), (24, 24), (moov 위치, 24), (moov 위치 + 24, 남은 크기)]
    """
    monkeypatch.setattr(mp4_module, "_FIRST_READ_BYTES", 24)
    monkeypatch.setattr(mp4_module, "_HEADER_READ_BYTES", 24)
    built = build_mp4([video_spec(), audio_spec()], moov_first=False)
    log: list[tuple[int, int]] = []

    index = read_mp4_index(_reader(built.data, log))

    assert log == [
        (0, 24),
        (24, 24),
        (built.moov_offset, 24),
        (built.moov_offset + 24, len(built.moov) - 24),
    ]
    assert list(index.video.offsets) == built.sample_offsets[b"vide"]


def test_read_mp4_index_reads_front_moov_in_one_request():
    """read_mp4_index는 moov가 첫 읽기 안에 다 들어오면 요청 한 번으로 끝내야 한다.

    ftyp · moov · mdat (전체가 첫 읽기 크기보다 작음)
    -> 요청 1회, 프레임 12개
    """
    built = build_mp4([video_spec(), audio_spec()])
    log: list[tuple[int, int]] = []

    index = read_mp4_index(_reader(built.data, log))

    assert len(log) == 1
    assert len(index.frame_pts) == 12


def test_read_mp4_index_raises_when_file_has_no_moov():
    """read_mp4_index는 파일 끝까지 moov가 없으면 못 찾음 키로 Mp4Error를 내야 한다.

    ftyp · mdat만 있는 파일
    -> message_key == MP4_MOOV_NOT_FOUND
    """
    built = build_mp4([video_spec()], include_moov=False)

    with pytest.raises(Mp4Error) as info:
        read_mp4_index(_reader(built.data))

    assert info.value.message_key == MP4_MOOV_NOT_FOUND


# ================================================================ 거부


def test_scan_top_level_rejects_moof():
    """scan_top_level은 최상위에 moof가 있으면 조각난 mp4 키로 Mp4Error를 내야 한다.

    ftyp · moof · moov · mdat
    -> message_key == MP4_FRAGMENTED
    """
    built = build_mp4([video_spec()], top_level_extra=box(b"moof", bytes(8)))

    with pytest.raises(Mp4Error) as info:
        scan_top_level(built.data)

    assert info.value.message_key == MP4_FRAGMENTED


def test_parse_moov_rejects_mvex():
    """parse_moov는 moov 안에 mvex가 있으면 조각난 mp4 키로 Mp4Error를 내야 한다.

    moov = mvhd · trak · mvex
    -> message_key == MP4_FRAGMENTED
    """
    built = build_mp4([video_spec()], mvex=True)

    with pytest.raises(Mp4Error) as info:
        parse_moov(built.moov)

    assert info.value.message_key == MP4_FRAGMENTED


def test_parse_moov_rejects_table_cut_short():
    """parse_moov는 표의 항목 수보다 데이터가 짧으면 손상 키로 Mp4Error를 내야 한다.

    stsz의 sample_count를 12에서 5000으로 바꾼 moov
    -> message_key == MP4_INVALID
    """
    moov = bytearray(build_mp4([video_spec()]).moov)
    struct.pack_into(">I", moov, moov.find(b"stsz") + 12, 5000)

    with pytest.raises(Mp4Error) as info:
        parse_moov(bytes(moov))

    assert info.value.message_key == MP4_INVALID


def test_parse_moov_rejects_entry_count_that_runs_past_its_box():
    """parse_moov는 표의 항목 수가 그 상자에 들어가는 수보다 많으면 뒤 상자를 읽지 않고 거부해야 한다.

    stss의 항목 수를 3에서 4로 바꾼 moov (상자에는 3개만 들어 있고 바로 뒤에 다음 상자가 온다)
    -> message_key == MP4_INVALID
    """
    moov = bytearray(build_mp4([video_spec()]).moov)
    struct.pack_into(">I", moov, moov.find(b"stss") + 8, 4)

    with pytest.raises(Mp4Error) as info:
        parse_moov(bytes(moov))

    assert info.value.message_key == MP4_INVALID


def test_parse_moov_rejects_sample_count_mismatch():
    """parse_moov는 stts와 stsz의 샘플 수가 다르면 손상 키로 Mp4Error를 내야 한다.

    stts 11샘플, stsz 12샘플
    -> message_key == MP4_INVALID
    """
    video = video_spec(deltas=[100] * 11)

    with pytest.raises(Mp4Error) as info:
        parse_moov(build_mp4([video]).moov)

    assert info.value.message_key == MP4_INVALID


def test_parse_moov_rejects_file_without_video_track():
    """parse_moov는 영상 트랙이 없으면 미지원 키로 Mp4Error를 내야 한다.

    오디오 트랙만 있는 moov
    -> message_key == MP4_UNSUPPORTED
    """
    with pytest.raises(Mp4Error) as info:
        parse_moov(build_mp4([audio_spec()]).moov)

    assert info.value.message_key == MP4_UNSUPPORTED


def test_parse_moov_rejects_edit_list_with_two_segments():
    """parse_moov는 편집 목록에 구간이 둘이면 미지원 키로 Mp4Error를 내야 한다.

    영상 elst = [(600, 100), (600, 700)]
    -> message_key == MP4_UNSUPPORTED
    """
    video = video_spec(edits=[(600, 100), (600, 700)])

    with pytest.raises(Mp4Error) as info:
        parse_moov(build_mp4([video]).moov)

    assert info.value.message_key == MP4_UNSUPPORTED


# ================================================================ 샘플 수 방어

# 개수 칸에 써넣을 큰 값 — 펼치면 리스트만 16MB를 넘는다
HUGE_RUN = 2_000_000
# 펼치지 않고 거부했다면 넘지 않을 메모리 사용량(바이트)
SMALL_PEAK = 1024 * 1024


def _peak_memory_of_rejected_parse(moov: bytes) -> tuple[str, int]:
    """parse_moov가 낸 Mp4Error의 키와, 그동안의 최대 메모리 사용량(바이트)."""
    tracemalloc.start()
    try:
        with pytest.raises(Mp4Error) as info:
            parse_moov(moov)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return info.value.message_key, peak


def test_parse_moov_rejects_stts_run_total_before_expanding_it():
    """parse_moov는 stts의 개수 합이 샘플 수와 다르면 구간을 펼치지 않고 손상 키로 거부해야 한다.

    stts 첫 구간의 개수를 12에서 2,000,000으로 바꾼 moov (stsz는 12샘플)
    -> message_key == MP4_INVALID, 최대 메모리 사용량 < 1MiB
    """
    moov = bytearray(build_mp4([video_spec()]).moov)
    struct.pack_into(">I", moov, moov.find(b"stts") + 12, HUGE_RUN)

    key, peak = _peak_memory_of_rejected_parse(bytes(moov))

    assert key == MP4_INVALID
    assert peak < SMALL_PEAK


def test_parse_moov_rejects_ctts_run_total_before_expanding_it():
    """parse_moov는 ctts의 개수 합이 샘플 수와 다르면 구간을 펼치지 않고 손상 키로 거부해야 한다.

    ctts 첫 구간의 개수를 1에서 2,000,000으로 바꾼 moov (stsz는 12샘플)
    -> message_key == MP4_INVALID, 최대 메모리 사용량 < 1MiB
    """
    moov = bytearray(build_mp4([video_spec()]).moov)
    struct.pack_into(">I", moov, moov.find(b"ctts") + 12, HUGE_RUN)

    key, peak = _peak_memory_of_rejected_parse(bytes(moov))

    assert key == MP4_INVALID
    assert peak < SMALL_PEAK


@pytest.mark.parametrize(
    ("limits", "expected_key"),
    [
        ({b"vide": 12, b"soun": 16}, None),  # 두 트랙 모두 상한과 같다
        ({b"vide": 11, b"soun": 16}, MP4_TOO_LONG),  # 영상이 상한 + 1
        ({b"vide": 12, b"soun": 15}, MP4_TOO_LONG),  # 오디오가 상한 + 1
    ],
    ids=["at-limit", "video-over", "audio-over"],
)
def test_parse_moov_applies_sample_limit_per_track(monkeypatch, limits, expected_key):
    """parse_moov는 트랙의 샘플 수가 그 트랙의 상한과 같으면 받고 하나라도 넘으면 길이 초과 키로 거부해야 한다.

    영상 12샘플 · 오디오 16샘플, 상한 (12, 16) · (11, 16) · (12, 15)
    -> 통과 · MP4_TOO_LONG · MP4_TOO_LONG
    """
    monkeypatch.setattr(mp4_module, "_MAX_SAMPLES", limits)
    moov = build_mp4([video_spec(), audio_spec()]).moov

    if expected_key is None:
        assert len(parse_moov(moov).frame_pts) == 12
    else:
        with pytest.raises(Mp4Error) as info:
            parse_moov(moov)
        assert info.value.message_key == expected_key


def test_parse_moov_rejects_uniform_stsz_count_over_limit_before_allocating(monkeypatch):
    """parse_moov는 고정 크기 stsz의 샘플 수가 상한을 넘으면 크기 목록을 만들지 않고 거부해야 한다.

    오디오 stsz(고정 크기 7)의 sample_count를 16에서 2,000,000으로 바꾼 moov, 오디오 상한 1,000,000
    -> message_key == MP4_TOO_LONG, 최대 메모리 사용량 < 1MiB
    """
    monkeypatch.setattr(mp4_module, "_MAX_SAMPLES", {b"vide": 100, b"soun": 1_000_000})
    built = build_mp4([video_spec(), audio_spec()])
    moov = bytearray(built.moov)
    audio_stsz = moov.rfind(b"stsz")  # 오디오 트랙이 뒤에 있다
    struct.pack_into(">I", moov, audio_stsz + 12, HUGE_RUN)

    key, peak = _peak_memory_of_rejected_parse(bytes(moov))

    assert key == MP4_TOO_LONG
    assert peak < SMALL_PEAK


def test_sample_limits_cover_twenty_four_hours():
    """샘플 수 상한은 24시간 분량의 60fps 영상과 48kHz AAC 오디오여야 한다.

    60 × 86,400 / 48,000 ÷ 1,024 × 86,400
    -> 영상 5,184,000, 오디오 4,050,000
    """
    assert mp4_module._MAX_SAMPLES == {b"vide": 5_184_000, b"soun": 4_050_000}


# ================================================================ 받기 (가짜 응답)


class FakeResponse:
    """requests 응답 흉내 — 본문을 몇 바이트 내줬는지 기록한다."""

    def __init__(self, status_code: int, body: bytes, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.bytes_handed = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self._body), chunk_size):
            chunk = self._body[start : start + chunk_size]
            self.bytes_handed += len(chunk)
            yield chunk

    @property
    def content(self) -> bytes:
        self.bytes_handed = len(self._body)
        return self._body


class RangeServer:
    """범위 요청에 파일 조각으로 답하는 가짜 세션 — 받은 요청을 기록한다.

    Args:
        content_range: (요청 시작, 요청 끝, 파일 크기) → Content-Range 값. None을 돌려주면
            머리를 싣지 않는다. 주지 않으면 실제로 보낸 범위를 정직하게 적는다
        body: 보낼 조각 → 실제로 보낼 본문. 주지 않으면 조각 그대로 보낸다
    """

    def __init__(self, data: bytes, status_code: int = 206, content_range=None, body=None):
        self.data = data
        self.status_code = status_code
        self.content_range = content_range
        self.body = body
        self.calls: list[tuple[str, dict]] = []
        self.responses: list[FakeResponse] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append((url, kwargs))
        first, last = (int(x) for x in kwargs["headers"]["Range"].removeprefix("bytes=").split("-"))
        total = len(self.data)
        if self.status_code == 206 and first >= total:
            response = FakeResponse(416, b"")
        elif self.status_code == 206:
            piece = self.data[first : last + 1]
            if self.content_range is not None:
                value = self.content_range(first, last, total)
            else:
                value = f"bytes {first}-{first + len(piece) - 1}/{total}"
            headers = {} if value is None else {"Content-Range": value}
            response = FakeResponse(206, self.body(piece) if self.body else piece, headers)
        else:
            response = FakeResponse(self.status_code, self.data)
        self.responses.append(response)
        return response


def test_fetch_mp4_index_requests_ranges_and_builds_index(monkeypatch):
    """fetch_mp4_index는 범위 요청으로 받은 moov로 색인을 만들어야 한다.

    ftyp · mdat · moov를 주는 가짜 서버, 한 번에 24바이트씩 읽도록 줄임
    -> Range 머리 = bytes=0-23, bytes=24-47, moov 위치부터 … / 프레임 12개
    """
    monkeypatch.setattr(mp4_module, "_FIRST_READ_BYTES", 24)
    monkeypatch.setattr(mp4_module, "_HEADER_READ_BYTES", 24)
    built = build_mp4([video_spec(), audio_spec()], moov_first=False)
    server = RangeServer(built.data)
    monkeypatch.setattr(mp4_module, "get_thread_session", lambda: server)

    index = fetch_mp4_index("https://example.invalid/video.mp4")

    assert [kwargs["headers"]["Range"] for _, kwargs in server.calls] == [
        "bytes=0-23",
        "bytes=24-47",
        f"bytes={built.moov_offset}-{built.moov_offset + 23}",
        f"bytes={built.moov_offset + 24}-{built.moov_offset + len(built.moov) - 1}",
    ]
    assert len(index.frame_pts) == 12


def test_fetch_mp4_index_sends_timeout_and_no_cookies(monkeypatch):
    """fetch_mp4_index는 요청마다 타임아웃을 주고 쿠키는 싣지 않아야 한다.

    가짜 서버에 요청
    -> 모든 요청의 timeout == 모듈의 _REQUEST_TIMEOUT, cookies 인자 없음, 주소는 받은 그대로
    """
    built = build_mp4([video_spec(), audio_spec()])
    server = RangeServer(built.data)
    monkeypatch.setattr(mp4_module, "get_thread_session", lambda: server)

    fetch_mp4_index("https://example.invalid/video.mp4")

    assert server.calls
    for url, kwargs in server.calls:
        assert url == "https://example.invalid/video.mp4"
        assert kwargs["timeout"] == mp4_module._REQUEST_TIMEOUT
        assert "cookies" not in kwargs


def test_fetch_mp4_index_rejects_full_response_without_reading_body(monkeypatch):
    """fetch_mp4_index는 서버가 범위 요청에 200으로 답하면 본문을 읽지 않고 거부해야 한다.

    모든 요청에 200과 파일 전체로 답하는 가짜 서버
    -> message_key == MP4_RANGE_NOT_SUPPORTED, 요청 1회, 본문 읽지 않음
    """
    built = build_mp4([video_spec(), audio_spec()])
    server = RangeServer(built.data, status_code=200)
    monkeypatch.setattr(mp4_module, "get_thread_session", lambda: server)

    with pytest.raises(Mp4Error) as info:
        fetch_mp4_index("https://example.invalid/video.mp4")

    assert info.value.message_key == MP4_RANGE_NOT_SUPPORTED
    assert len(server.responses) == 1
    assert server.responses[0].bytes_handed == 0


def test_fetch_mp4_index_stops_reading_when_body_exceeds_granted_range(monkeypatch):
    """fetch_mp4_index는 본문이 Content-Range가 말한 길이를 넘으면 끝까지 읽지 않고 거부해야 한다.

    Content-Range는 정직하게 적고 본문 뒤에 4MiB를 덧붙여 보내는 가짜 서버
    -> message_key == MP4_RANGE_MISMATCH, 내준 바이트 < 본문 전체
    """
    built = build_mp4([video_spec(), audio_spec()])
    padding = bytes(4 * 1024 * 1024)
    server = RangeServer(built.data, body=lambda piece: piece + padding)
    monkeypatch.setattr(mp4_module, "get_thread_session", lambda: server)

    with pytest.raises(Mp4Error) as info:
        fetch_mp4_index("https://example.invalid/video.mp4")

    assert info.value.message_key == MP4_RANGE_MISMATCH
    assert 0 < server.responses[0].bytes_handed < len(built.data) + len(padding)


def test_fetch_mp4_index_rejects_body_shorter_than_granted_range(monkeypatch):
    """fetch_mp4_index는 본문이 Content-Range가 말한 길이보다 짧으면 거부해야 한다.

    Content-Range는 정직하게 적고 본문의 마지막 3바이트를 빼고 보내는 가짜 서버
    -> message_key == MP4_RANGE_MISMATCH
    """
    built = build_mp4([video_spec(), audio_spec()])
    server = RangeServer(built.data, body=lambda piece: piece[:-3])
    monkeypatch.setattr(mp4_module, "get_thread_session", lambda: server)

    with pytest.raises(Mp4Error) as info:
        fetch_mp4_index("https://example.invalid/video.mp4")

    assert info.value.message_key == MP4_RANGE_MISMATCH


@pytest.mark.parametrize(
    "content_range",
    [
        lambda first, last, total: f"bytes {first + 1}-{last}/{total}",  # 시작만 다르다
        lambda first, last, total: f"bytes {first}-{last + 5}/{total}",  # 끝이 요청보다 뒤다
        lambda first, last, total: (
            f"bytes {first}-{last - 5}/{total}"
        ),  # 끝이 앞인데 파일 끝도 아니다
        lambda first, last, total: f"bytes {first}-{last - 5}/*",  # 끝이 앞인데 전체 크기를 모른다
        lambda first, last, total: f"items {first}-{last}/{total}",  # 단위가 bytes가 아니다
        lambda first, last, total: None,  # 머리가 없다
    ],
    ids=["start", "end-after", "end-before", "end-before-unknown-total", "unit", "missing"],
)
def test_fetch_mp4_index_rejects_content_range_that_differs_from_request(
    monkeypatch, content_range
):
    """fetch_mp4_index는 206의 Content-Range가 요청한 범위와 다르면 본문을 읽지 않고 거부해야 한다.

    요청 bytes=0-23 (파일은 그보다 길다), Content-Range를 6가지로 틀리게 적는 가짜 서버
    -> message_key == MP4_RANGE_MISMATCH, 요청 1회, 내준 바이트 0
    """
    monkeypatch.setattr(mp4_module, "_FIRST_READ_BYTES", 24)
    built = build_mp4([video_spec(), audio_spec()])
    server = RangeServer(built.data, content_range=content_range)
    monkeypatch.setattr(mp4_module, "get_thread_session", lambda: server)

    with pytest.raises(Mp4Error) as info:
        fetch_mp4_index("https://example.invalid/video.mp4")

    assert info.value.message_key == MP4_RANGE_MISMATCH
    assert len(server.responses) == 1
    assert server.responses[0].bytes_handed == 0


def test_fetch_mp4_index_accepts_exact_range_with_unknown_total(monkeypatch):
    """fetch_mp4_index는 Content-Range의 전체 크기가 "*"여도 범위가 요청과 같으면 받아야 한다.

    ftyp · mdat · moov, 한 번에 24바이트씩 읽어 모든 요청이 파일 안에 들어감, Content-Range = "bytes 시작-끝/*"
    -> 프레임 12개
    """
    monkeypatch.setattr(mp4_module, "_FIRST_READ_BYTES", 24)
    monkeypatch.setattr(mp4_module, "_HEADER_READ_BYTES", 24)
    built = build_mp4([video_spec(), audio_spec()], moov_first=False)
    server = RangeServer(
        built.data, content_range=lambda first, last, total: f"bytes {first}-{last}/*"
    )
    monkeypatch.setattr(mp4_module, "get_thread_session", lambda: server)

    index = fetch_mp4_index("https://example.invalid/video.mp4")

    assert len(index.frame_pts) == 12


def test_fetch_mp4_index_accepts_range_cut_at_end_of_file(monkeypatch):
    """fetch_mp4_index는 파일이 요청보다 먼저 끝나 끝이 파일의 마지막 바이트인 응답을 받아야 한다.

    파일 전체가 첫 요청(1MiB)보다 작음, Content-Range = "bytes 0-(파일 크기−1)/파일 크기"
    -> 요청 1회, 프레임 12개
    """
    built = build_mp4([video_spec(), audio_spec()])
    server = RangeServer(built.data)
    monkeypatch.setattr(mp4_module, "get_thread_session", lambda: server)

    index = fetch_mp4_index("https://example.invalid/video.mp4")

    assert len(server.calls) == 1
    assert len(index.frame_pts) == 12


def test_fetch_mp4_index_propagates_http_error(monkeypatch):
    """fetch_mp4_index는 서버가 오류 상태로 답하면 requests 예외를 그대로 내야 한다.

    모든 요청에 403으로 답하는 가짜 서버
    -> requests.HTTPError
    """
    built = build_mp4([video_spec(), audio_spec()])
    server = RangeServer(built.data, status_code=403)
    monkeypatch.setattr(mp4_module, "get_thread_session", lambda: server)

    with pytest.raises(requests.HTTPError):
        fetch_mp4_index("https://example.invalid/video.mp4")


# ================================================================ moov의 위치 · 청크 (#309)


@pytest.mark.parametrize("moov_first", [True, False], ids=["front", "back"])
def test_read_mp4_index_reports_where_the_moov_is(moov_first):
    """read_mp4_index는 파일에서 moov를 찾은 (시작, 끝) 바이트를 moov_range에 실어야 한다.

    ftyp · moov · mdat 와 ftyp · mdat · moov
    -> moov_range == (조립기가 놓은 moov의 위치, 그 위치 + moov 길이 − 1)
    """
    built = build_mp4([video_spec(), audio_spec()], moov_first=moov_first)

    index = read_mp4_index(_reader(built.data))

    assert index.moov_range == (built.moov_offset, built.moov_offset + len(built.moov) - 1)


def test_parse_moov_leaves_moov_range_empty():
    """parse_moov는 moov bytes만 받았으므로 moov_range를 채우지 않아야 한다.

    합성 mp4의 moov bytes
    -> moov_range is None
    """
    built = build_mp4([video_spec(), audio_spec()])

    assert parse_moov(built.moov).moov_range is None


def test_parse_moov_reports_first_sample_of_every_chunk():
    """parse_moov는 트랙마다 청크의 첫 샘플 인덱스를 chunk_starts에 실어야 한다.

    영상 청크별 샘플 수 [3, 3, 2, 4], 오디오 [4, 4, 4, 4]
    -> 영상 chunk_starts == (0, 3, 6, 8), 오디오 == (0, 4, 8, 12)
    """
    built = build_mp4([video_spec(), audio_spec()])

    index = parse_moov(built.moov)

    assert index.video.chunk_starts == (0, 3, 6, 8)
    assert index.audio.chunk_starts == (0, 4, 8, 12)


# ================================================================ 선언된 오디오 비트레이트 (#309)


def _descriptor(tag: int, body: bytes, long_length: bool) -> bytes:
    """esds 안의 서술자 하나 — 꼬리표 + 길이 + 본문. long_length면 길이를 4바이트로 쓴다."""
    length = bytes([0x80, 0x80, 0x80, len(body)]) if long_length else bytes([len(body)])
    return bytes([tag]) + length + body


def _esds(
    max_rate: int,
    avg_rate: int,
    *,
    long_length: bool = False,
    es_flags: int = 0,
    optional: bytes = b"",
) -> bytes:
    """esds 상자 — ES 서술자(0x03) 안에 디코더 설정 서술자(0x04)가 든다."""
    config = bytes([0x40, 0x15]) + bytes(3) + struct.pack(">II", max_rate, avg_rate)
    es = struct.pack(">HB", 1, es_flags) + optional + _descriptor(0x04, config, long_length)
    return box(b"esds", bytes(4) + _descriptor(0x03, es, long_length))


def _btrt(max_rate: int, avg_rate: int) -> bytes:
    """btrt 상자 — 버퍼 크기(0) · maxBitrate · avgBitrate."""
    return box(b"btrt", struct.pack(">III", 0, max_rate, avg_rate))


def _audio_stsd(*children: bytes, entries: int = 1, version: int = 0) -> bytes:
    """mp4a 샘플 엔트리 하나가 든 stsd의 본문(버전·플래그부터)."""
    # 예약(6) · 데이터 참조 번호(2) · 버전(2) · 개정(2) · 제작자(4) · 채널(2) · 샘플 크기(2) ·
    # 압축 번호(2) · 패킷 크기(2) · 샘플률(4)
    fixed = bytes(6) + struct.pack(">HHHIHHHHI", 1, version, 0, 0, 2, 16, 0, 0, 48000 << 16)
    payload = fixed + b"".join(children)
    return (
        bytes(4)
        + struct.pack(">I", entries)
        + struct.pack(">I4s", 8 + len(payload), b"mp4a")
        + payload
    )


@pytest.mark.parametrize(
    ("stsd", "expected"),
    [
        (_audio_stsd(_esds(192_000, 128_000)), 128_000),
        (
            _audio_stsd(_esds(192_000, 128_000, long_length=True)),
            128_000,
        ),  # ffmpeg가 쓰는 길이 모양
        # ES 서술자의 선택 칸(dependsOn_ES_ID 2바이트)이 앞에 있다
        (_audio_stsd(_esds(192_000, 128_000, es_flags=0x80, optional=bytes(2))), 128_000),
        (_audio_stsd(_esds(192_000, 128_000), _btrt(0, 96_000)), 128_000),  # esds가 앞선다
        (_audio_stsd(_esds(160_000, 0), _btrt(170_000, 96_000)), 96_000),  # esds avg 0 → btrt avg
        (_audio_stsd(_btrt(170_000, 96_000)), 96_000),
        (_audio_stsd(_esds(160_000, 0)), 160_000),  # avg가 어디에도 없다 → max
        (_audio_stsd(_esds(0, 0), _btrt(170_000, 0)), 170_000),
        (_audio_stsd(), None),  # esds도 btrt도 없다
        (_audio_stsd(_esds(0, 0)), None),
        (_audio_stsd(_esds(192_000, 128_000), entries=0), None),
        (_audio_stsd(_esds(192_000, 128_000), version=3), None),  # 모르는 엔트리 버전
        (
            _audio_stsd(box(b"esds", bytes(4) + bytes([0x03, 5, 0, 1, 0, 0x04, 20]))),
            None,
        ),  # 잘린 esds
        (_audio_stsd(box(b"esds", bytes(4) + bytes([0x05, 2, 0, 0]))), None),  # 다른 서술자로 시작
    ],
    ids=[
        "esds-avg",
        "esds-long-length",
        "esds-optional-field",
        "esds-before-btrt",
        "btrt-avg-when-esds-avg-is-zero",
        "btrt-only",
        "esds-max",
        "btrt-max",
        "nothing",
        "all-zero",
        "no-entry",
        "unknown-version",
        "truncated-esds",
        "wrong-descriptor",
    ],
)
def test_declared_bitrate_reads_the_sample_entry(stsd, expected):
    """_declared_bitrate는 오디오 샘플 엔트리의 esds avg · btrt avg · esds max · btrt max 순서로 0이 아닌 첫 값을 돌려주고, 없거나 읽지 못하면 None을 돌려줘야 한다.

    주석의 경우마다 손으로 조립한 stsd 본문 (값은 bit/s)
    -> 기대값
    """
    assert mp4_module._declared_bitrate(stsd, (0, len(stsd))) == expected


def test_parse_moov_leaves_declared_bitrate_empty_without_a_sample_entry():
    """parse_moov는 샘플 엔트리가 없는 트랙의 declared_bitrate를 None으로 두고 해석에 실패하지 않아야 한다.

    합성 mp4 — stsd의 항목 수 0
    -> 오디오·영상 declared_bitrate is None
    """
    index = parse_moov(build_mp4([video_spec(), audio_spec()]).moov)

    assert (index.video.declared_bitrate, index.audio.declared_bitrate) == (None, None)


def test_composition_offsets_reads_negative_values_and_expands_runs():
    """_composition_offsets는 ctts의 값을 부호 있는 수로 읽고, 개수만큼 펼쳐 샘플마다 하나씩 돌려줘야 한다.

    손으로 조립한 ctts 본문 — (개수 2, 값 -100) · (개수 1, 값 0) · (개수 1, 값 300), 샘플 4개
    -> [-100, -100, 0, 300]
    """
    ctts = bytes(4) + struct.pack(">I", 3) + struct.pack(">IiIiIi", 2, -100, 1, 0, 1, 300)

    assert list(mp4_module._composition_offsets(ctts, (0, len(ctts)), 4)) == [-100, -100, 0, 300]


# ================================================================ 표를 배열로 통째로 읽기 (#309)


def test_read_table_reads_big_endian_values_of_each_width():
    """_read_table은 빅엔디언 32비트(부호 없음 · 있음) · 64비트 값을 그 개수만큼 읽어야 한다.

    바이트 00 00 00 01 · FF FF FF FE · 00 00 00 01 00 00 00 00
    -> "I" 둘 == [1, 4294967294], "i" 둘 == [1, -2], 뒤 8바이트의 "Q" 하나 == [4294967296]
    """
    data = struct.pack(">IIQ", 1, 0xFFFFFFFE, 1 << 32)

    assert list(mp4_module._read_table(data, 0, 2, "I")) == [1, 0xFFFFFFFE]
    assert list(mp4_module._read_table(data, 0, 2, "i")) == [1, -2]
    assert list(mp4_module._read_table(data, 8, 1, "Q")) == [1 << 32]


def test_read_table_rejects_a_table_that_runs_past_the_data():
    """_read_table은 표가 바이트의 끝을 넘으면 손상 키로 거부해야 한다.

    8바이트에서 32비트 값 3개(12바이트)를 읽음 -> Mp4Error(MP4_INVALID)
    """
    with pytest.raises(Mp4Error) as info:
        mp4_module._read_table(bytes(8), 0, 3, "I")

    assert info.value.message_key == MP4_INVALID


def test_expand_runs_repeats_each_value_by_its_count():
    """_expand_runs는 (개수, 값) 구간을 샘플마다 값 하나씩으로 펴야 한다 — 개수 0인 구간은 사라진다.

    개수 [2, 0, 3] · 값 [7, 8, 9] -> [7, 7, 9, 9, 9]
    """
    import array

    runs, values = array.array("I", [2, 0, 3]), array.array("I", [7, 8, 9])

    assert list(mp4_module._expand_runs(runs, values, "q")) == [7, 7, 9, 9, 9]


def test_parse_moov_reads_chunks_that_hold_one_sample_each():
    """parse_moov는 청크마다 샘플이 하나인 트랙의 샘플 위치를 조립기가 쓴 위치 그대로 읽어야 한다.

    영상 12샘플을 청크 12개에(청크당 1샘플), 오디오는 표준 재료
    -> 영상 offsets == 조립기가 기록한 샘플 위치, chunk_starts == 0 … 11
    """
    built = build_mp4([video_spec(chunks=[1] * 12), audio_spec()])

    index = parse_moov(built.moov)

    assert list(index.video.offsets) == built.sample_offsets[b"vide"]
    assert list(index.video.chunk_starts) == list(range(12))


def test_parse_moov_does_not_hold_many_times_the_index_while_parsing():
    """parse_moov가 해석하는 동안 쓰는 메모리의 최고치는 다 만든 색인의 3배를 넘지 않아야 한다.

    영상 12,000샘플(재정렬 I P B B) + 오디오 9,000샘플을 tracemalloc 아래에서 해석
    -> 해석 중 최고 ÷ 해석이 끝난 뒤 남은 양 < 3 (표를 샘플마다 파이썬 객체로 풀어 리스트에 담으면 5배를 넘는다)
    """
    frames, samples = (
        12_000,
        9_000,
    )  # 조립기가 샘플 수의 제곱으로 느려진다 — 이 크기에서도 비는 같다
    video = video_spec(
        deltas=[100] * frames,
        sizes=[40] * frames,
        chunks=[30] * (frames // 30),
        composition=[100, 300, 0, 0] * (frames // 4),
        sync=list(range(1, frames + 1, 60)),
    )
    audio = audio_spec(deltas=[1024] * samples, sizes=[7] * samples, chunks=[25] * (samples // 25))
    moov = build_mp4([video, audio]).moov

    tracemalloc.start()
    try:
        index = parse_moov(moov)
        held, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(index.frame_pts) == frames
    assert peak / held < 3, f"최고 {peak / 1e6:.1f}MB · 색인 {held / 1e6:.1f}MB"


def test_parse_moov_gives_each_sample_its_own_duration_when_they_differ():
    """샘플 길이가 고르지 않은 트랙은 샘플마다 제 길이(틱 ÷ timescale)를 가져야 한다.

    영상 12샘플의 길이(틱) [100, 100, 150, 150, 150, 90, 100, 100, 100, 100, 100, 110], timescale 1000
    -> video.durations == 그 값을 1000으로 나눈 것(구간 다섯 — 값이 바뀔 때마다 새 구간이다)
    """
    deltas = [100, 100, 150, 150, 150, 90, 100, 100, 100, 100, 100, 110]

    index = parse_moov(build_mp4([video_spec(deltas=deltas), audio_spec()]).moov)

    assert list(index.video.durations) == [delta / 1000 for delta in deltas]

"""mp4의 moov 찾기·해석·받기(core/api/mp4.py) 단위 테스트 (#178).

핵심 계약:
- 프레임 PTS는 ctts와 편집 목록을 적용한 표시 시각이고, 가장 먼저 표시되는 샘플이 0이다
- 샘플 위치는 stsc 청크 구간 + stco/co64 + stsz로 계산한다
- moov가 mdat 뒤에 있어도 상자 크기를 따라 건너뛰어 찾는다
- 조각난 mp4는 키가 붙은 예외로 거부한다

입력은 tests/unit/core/mp4_builder.py가 상자를 직접 조립한 합성 mp4다.
"""

import struct
from fractions import Fraction

import pytest
import requests

import core.api.mp4 as mp4_module
from core.api.mp4 import (
    MP4_FRAGMENTED,
    MP4_INVALID,
    MP4_MOOV_NOT_FOUND,
    MP4_RANGE_NOT_SUPPORTED,
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


# ================================================================ 받기 (가짜 응답)


class FakeResponse:
    """requests 응답 흉내 — 본문을 읽었는지 기록한다."""

    def __init__(self, status_code: int, body: bytes):
        self.status_code = status_code
        self._body = body
        self.body_read = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    @property
    def content(self) -> bytes:
        self.body_read = True
        return self._body


class RangeServer:
    """범위 요청에 파일 조각으로 답하는 가짜 세션 — 받은 요청을 기록한다."""

    def __init__(self, data: bytes, status_code: int = 206):
        self.data = data
        self.status_code = status_code
        self.calls: list[tuple[str, dict]] = []
        self.responses: list[FakeResponse] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append((url, kwargs))
        first, last = (int(x) for x in kwargs["headers"]["Range"].removeprefix("bytes=").split("-"))
        if self.status_code == 206 and first >= len(self.data):
            response = FakeResponse(416, b"")
        elif self.status_code == 206:
            response = FakeResponse(206, self.data[first : last + 1])
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
    assert server.responses[0].body_read is False


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

"""HLS fMP4에서 플레이리스트·초기화 세그먼트·moof만 받기(core/api/hls_fmp4.py) 단위 테스트 (#309).

핵심 계약:
- 세그먼트는 moof가 든 앞부분만 범위 요청으로 받는다. moof 하나에 요청 한 번이다
- 받은 moof로 해석한 결과는 세그먼트 전체를 해석한 결과와 같다
- 서버가 범위 요청에 200으로 전체를 보내면 그 본문을 그대로 해석한다
- 한 번 받은 세그먼트는 다시 요청하지 않는다

입력은 합성 fMP4다(tests/unit/core/fmp4_builder.py). 요청은 범위 요청에 답하는 호스트
(tests/unit/core/range_host.py — 소켓을 열지 않는다)가 받는다.
"""

import pytest

import core.api.hls_fmp4 as hls_fmp4_module
from core.api.fmp4 import parse_init_segment, parse_media_segment
from core.api.hls_fmp4 import (
    HLS_NOT_FMP4,
    fetch_fmp4_head,
    fetch_segment_moofs,
    segment_frames,
)
from core.api.mp4 import MP4_INVALID, Mp4Error
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
from tests.unit.core.range_host import RangeHost

TICK = 100  # 영상 샘플 하나의 길이(틱) — timescale 1000에서 0.1초
INIT = init_segment([InitTrack(1, b"vide", 1000, trex=(TICK, 50, NON_KEY))])


def _fragment(decode_time: int, samples: int, mdat_size: int = 64) -> Fragment:
    """샘플 samples개짜리 영상 moof 하나 — 첫 샘플만 키프레임이다."""
    run = Run([Sample(duration=TICK, size=10) for _ in range(samples)], first_sample_flags=KEY)
    return Fragment([Traf(1, [run], decode_time=decode_time)], mdat_size=mdat_size)


def _playlist(*lines: str) -> bytes:
    return "\n".join(["#EXTM3U", *lines, "#EXT-X-ENDLIST"]).encode("utf-8")


def _host(monkeypatch, files: dict[str, bytes]) -> RangeHost:
    host = RangeHost(files)
    monkeypatch.setattr(hls_fmp4_module, "get_thread_session", host.session)
    return host


def _ranged(host: RangeHost) -> list[str]:
    """호스트에 온 범위 요청의 Range 머리 — 온 순서대로."""
    return [header for _method, _name, header in host.requests if header is not None]


# ================================================================ 플레이리스트 · 초기화 세그먼트


def test_fetch_fmp4_head_requests_playlist_and_init_once_each(monkeypatch):
    """fetch_fmp4_head는 플레이리스트와 초기화 세그먼트를 한 번씩 받아 해석해야 한다.

    EXT-X-MAP "init.mp4"와 세그먼트 둘이 있는 플레이리스트
    -> 요청 == [플레이리스트, 초기화 세그먼트], init_data == 초기화 세그먼트 bytes, 세그먼트는 아직 없다
    """
    playlist = _playlist(
        '#EXT-X-MAP:URI="init.mp4"', "#EXTINF:1.0,", "a.m4s", "#EXTINF:1.0,", "b.m4s"
    )
    host = _host(monkeypatch, {"v/media.m3u8": playlist, "v/init.mp4": INIT})

    head = fetch_fmp4_head(host.url("v/media.m3u8"))

    assert [name for _m, name, _h in host.requests] == ["v/media.m3u8", "v/init.mp4"]
    assert head.playlist.segments == ("a.m4s", "b.m4s")
    assert head.init_data == INIT
    assert head.init == parse_init_segment(INIT)
    assert head.segments == {}


def test_fetch_fmp4_head_rejects_playlist_without_init_segment(monkeypatch):
    """fetch_fmp4_head는 EXT-X-MAP이 없는 플레이리스트를 fMP4가 아니라는 키로 거부해야 한다.

    세그먼트만 있는 플레이리스트(MPEG-TS 모양)
    -> Mp4Error(HLS_NOT_FMP4)
    """
    host = _host(monkeypatch, {"v/media.m3u8": _playlist("#EXTINF:1.0,", "a.ts")})

    with pytest.raises(Mp4Error) as info:
        fetch_fmp4_head(host.url("v/media.m3u8"))

    assert info.value.message_key == HLS_NOT_FMP4


def test_fetch_fmp4_head_rejects_playlist_whose_init_segment_changes(monkeypatch):
    """fetch_fmp4_head는 EXT-X-MAP이 중간에 바뀌는 플레이리스트를 거부해야 한다.

    EXT-X-MAP "one.mp4" 뒤에 세그먼트, 다시 EXT-X-MAP "two.mp4" 뒤에 세그먼트
    -> Mp4Error(HLS_NOT_FMP4), 초기화 세그먼트는 요청하지 않는다
    """
    playlist = _playlist(
        '#EXT-X-MAP:URI="one.mp4"', "#EXTINF:1.0,", "a.m4s",
        '#EXT-X-MAP:URI="two.mp4"', "#EXTINF:1.0,", "b.m4s",
    )  # fmt: skip
    host = _host(monkeypatch, {"v/media.m3u8": playlist, "v/one.mp4": INIT, "v/two.mp4": INIT})

    with pytest.raises(Mp4Error) as info:
        fetch_fmp4_head(host.url("v/media.m3u8"))

    assert info.value.message_key == HLS_NOT_FMP4
    assert [name for _m, name, _h in host.requests] == ["v/media.m3u8"]


# ================================================================ moof


def test_fetch_segment_moofs_reads_single_moof_in_one_request(monkeypatch):
    """fetch_segment_moofs는 moof가 하나인 세그먼트를 범위 요청 한 번으로 읽고 전체 해석과 같은 결과를 내야 한다.

    styp · moof(샘플 4) · mdat(200,000바이트 — 첫 요청 크기보다 크다)
    -> 범위 요청 1건(bytes=0-65535), 결과 == parse_media_segment(세그먼트 전체)
    """
    init = parse_init_segment(INIT)
    segment = media_segment([_fragment(0, 4, mdat_size=200_000)])
    host = _host(monkeypatch, {"seg.m4s": segment})

    found = fetch_segment_moofs(host.url("seg.m4s"), init)

    assert _ranged(host) == ["bytes=0-65535"]
    assert found == parse_media_segment(segment, init)


def test_fetch_segment_moofs_follows_every_moof_in_the_segment(monkeypatch):
    """fetch_segment_moofs는 mdat 뒤에 moof가 더 있으면 그 위치부터 이어 읽어 모두 해석해야 한다.

    styp · moof(샘플 3) · mdat(100,000) · moof(샘플 2) · mdat(100,000)
    -> 범위 요청 2건(둘째는 첫 mdat 바로 뒤에서 시작), 결과 == 세그먼트 전체 해석(샘플 5, moof 2)
    """
    init = parse_init_segment(INIT)
    fragments = [_fragment(0, 3, mdat_size=100_000), _fragment(300, 2, mdat_size=100_000)]
    segment = media_segment(fragments)
    second = len(media_segment(fragments[:1]))  # 첫 moof · mdat이 끝나는 위치
    host = _host(monkeypatch, {"seg.m4s": segment})

    found = fetch_segment_moofs(host.url("seg.m4s"), init)

    assert _ranged(host) == ["bytes=0-65535", f"bytes={second}-{second + 65535}"]
    assert found == parse_media_segment(segment, init)
    assert (found.fragments, len(found.video.decode_times)) == (2, 5)


def test_fetch_segment_moofs_asks_again_when_moof_is_longer_than_first_read(monkeypatch):
    """fetch_segment_moofs는 moof가 첫 요청보다 길면 요청을 키워 다시 받아야 한다.

    첫 요청 크기를 64바이트로 줄임, moof(샘플 40)는 그보다 길다
    -> 범위 요청 2건 이상, 결과 == 세그먼트 전체 해석
    """
    monkeypatch.setattr(hls_fmp4_module, "_MOOF_READ_BYTES", 64)
    init = parse_init_segment(INIT)
    segment = media_segment([_fragment(0, 40, mdat_size=5_000)])
    host = _host(monkeypatch, {"seg.m4s": segment})

    found = fetch_segment_moofs(host.url("seg.m4s"), init)

    assert len(_ranged(host)) >= 2
    assert found == parse_media_segment(segment, init)


def test_fetch_segment_moofs_uses_whole_body_when_server_ignores_range(monkeypatch):
    """fetch_segment_moofs는 서버가 범위 요청에 200으로 전체를 보내면 그 본문을 해석하고 더 요청하지 않아야 한다.

    호스트가 Range를 무시, styp · moof · mdat(100,000) · moof · mdat(100,000)
    -> 요청 1건, 결과 == 세그먼트 전체 해석(moof 2)
    """
    init = parse_init_segment(INIT)
    segment = media_segment(
        [_fragment(0, 3, mdat_size=100_000), _fragment(300, 2, mdat_size=100_000)]
    )
    host = _host(monkeypatch, {"seg.m4s": segment})
    host.ignore_range = True

    found = fetch_segment_moofs(host.url("seg.m4s"), init)

    assert len(host.requests) == 1
    assert found == parse_media_segment(segment, init)
    assert found.fragments == 2


def test_fetch_segment_moofs_rejects_segment_without_moof(monkeypatch):
    """fetch_segment_moofs는 moof가 없는 세그먼트를 손상 키로 거부해야 한다.

    styp만 있는 세그먼트
    -> Mp4Error(MP4_INVALID)
    """
    host = _host(monkeypatch, {"seg.m4s": media_segment([])})

    with pytest.raises(Mp4Error) as info:
        fetch_segment_moofs(host.url("seg.m4s"), parse_init_segment(INIT))

    assert info.value.message_key == MP4_INVALID


def test_segment_frames_requests_each_segment_only_once(monkeypatch):
    """segment_frames는 한 번 받은 세그먼트를 보관해 두고 다시 요청하지 않아야 한다.

    세그먼트 둘인 플레이리스트, 0번을 두 번 · 1번을 한 번 요청
    -> 범위 요청은 a.m4s 1건 · b.m4s 1건, 두 번째 호출은 같은 객체를 돌려준다
    """
    playlist = _playlist(
        '#EXT-X-MAP:URI="init.mp4"', "#EXTINF:0.4,", "a.m4s", "#EXTINF:0.2,", "b.m4s"
    )
    files = {
        "v/media.m3u8": playlist,
        "v/init.mp4": INIT,
        "v/a.m4s": media_segment([_fragment(0, 4)]),
        "v/b.m4s": media_segment([_fragment(400, 2)]),
    }
    host = _host(monkeypatch, files)
    url = host.url("v/media.m3u8")
    head = fetch_fmp4_head(url)

    first = segment_frames(head, url, 0)
    again = segment_frames(head, url, 0)
    other = segment_frames(head, url, 1)

    assert again is first
    assert [name for _m, name, header in host.requests if header] == ["v/a.m4s", "v/b.m4s"]
    assert len(other.video.decode_times) == 2
    assert sorted(head.segments) == [0, 1]

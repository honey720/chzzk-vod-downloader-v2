"""HLS fMP4의 플레이리스트 · 초기화 세그먼트 · 세그먼트 받기(core/api/hls_fmp4.py) 단위 테스트 (#309).

핵심 계약:
- 플레이리스트와 초기화 세그먼트는 한 번씩 받는다. EXT-X-MAP이 없거나 바뀌면 거부한다
- 세그먼트는 전체 요청으로만 받는다 — 범위 요청을 보내지 않는다
- 세그먼트 안의 moof를 모두 읽는다(사이에 다른 상자가 있어도)
- 받은 세그먼트는 segment_dir에 엔진과 같은 이름으로 두고, 잘린 것은 두지 않는다
- 같은 세그먼트를 두 번 요청하지 않는다

입력은 tests/unit/core/fmp4_builder.py가 조립한 합성 fMP4이고, 요청은 소켓 없는 호스트
(tests/unit/core/range_host.py)가 답한다.
"""

import os
from types import SimpleNamespace

import pytest

import core.api.hls_fmp4 as hls_fmp4_module
from core.api.fmp4 import parse_init_segment, parse_media_segment
from core.api.hls_fmp4 import (
    HLS_NOT_FMP4,
    download_segment,
    fetch_fmp4_head,
    read_segment_file,
    segment_file_name,
    segment_frames,
)
from core.api.mp4 import Mp4Error
from core.downloaders.integrity import TruncatedSegmentError
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


# ================================================================ 세그먼트


def _two_moof_segment() -> bytes:
    """moof 둘짜리 세그먼트 — styp · moof · mdat · emsg · moof · mdat (영상 4샘플 + 2샘플)."""
    return (
        media_segment([_fragment(0, 4)])
        + box(b"emsg", bytes(40))
        + media_segment([_fragment(400, 2)], styp=False)
    )


def _vod(monkeypatch, segments: dict[str, bytes]) -> tuple[RangeHost, str]:
    """세그먼트들을 순서대로 적은 플레이리스트와 함께 내주는 호스트와 플레이리스트 주소."""
    lines = ['#EXT-X-MAP:URI="init.mp4"']
    for name in segments:
        lines += ["#EXTINF:0.6,", name]
    files = {"v/media.m3u8": _playlist(*lines), "v/init.mp4": INIT}
    files.update({f"v/{name}": data for name, data in segments.items()})
    host = _host(monkeypatch, files)
    return host, host.url("v/media.m3u8")


def test_segment_frames_reads_every_moof_with_one_whole_request(monkeypatch, tmp_path):
    """segment_frames는 세그먼트를 범위 머리 없는 요청 한 번으로 받아 그 안의 moof를 모두 읽어야 한다.

    moof 둘(사이에 emsg)인 세그먼트 하나 — 영상 4샘플 + 2샘플
    -> 세그먼트 요청 1건(Range 머리 없음), 영상 6샘플, moof 2개, 결과 == parse_media_segment(전체)
    """
    data = _two_moof_segment()
    host, url = _vod(monkeypatch, {"a.m4s": data})
    head = fetch_fmp4_head(url, str(tmp_path / "segments"))

    segment = segment_frames(head, url, 0)

    assert [(n, h) for _m, n, h in host.requests if n.endswith(".m4s")] == [("v/a.m4s", None)]
    assert (len(segment.video.decode_times), segment.fragments) == (6, 2)
    assert segment == parse_media_segment(data, head.init)


def test_segment_frames_keeps_the_segment_in_the_segment_folder(monkeypatch, tmp_path):
    """segment_frames는 받은 세그먼트를 segment_dir에 엔진과 같은 이름으로 두고 stored에 적어야 한다.

    세그먼트 셋인 플레이리스트, segment_dir 지정, 1번(둘째) 세그먼트를 읽음
    -> segment_dir에 "2.m4v" 하나(내용 == 세그먼트 bytes), head.stored == {1}
    """
    segments = {name: media_segment([_fragment(n * 400, 4)]) for n, name in enumerate("abc")}
    _host_, url = _vod(monkeypatch, segments)
    folder = tmp_path / "segments"
    head = fetch_fmp4_head(url, str(folder))

    segment_frames(head, url, 1)

    assert os.listdir(folder) == ["2.m4v"]
    assert (folder / "2.m4v").read_bytes() == segments["b"]
    assert head.stored == {1}


def test_segment_frames_keeps_nothing_without_a_segment_folder(monkeypatch, tmp_path):
    """segment_frames는 segment_dir이 없으면 프레임 정보만 읽고 본문은 남기지 않아야 한다.

    segment_dir 없이 만든 Fmp4Head, 0번 세그먼트를 읽음
    -> 영상 4샘플, head.stored가 비어 있다
    """
    _host_, url = _vod(monkeypatch, {"a.m4s": media_segment([_fragment(0, 4)])})
    head = fetch_fmp4_head(url)

    segment = segment_frames(head, url, 0)

    assert len(segment.video.decode_times) == 4
    assert head.stored == set()


def test_segment_frames_requests_each_segment_only_once(monkeypatch, tmp_path):
    """segment_frames는 한 번 받은 세그먼트를 보관해 두고 다시 요청하지 않아야 한다.

    세그먼트 둘인 플레이리스트, 0번을 두 번 · 1번을 한 번 요청
    -> 세그먼트 요청은 a.m4s 1건 · b.m4s 1건, 두 번째 호출은 같은 객체를 돌려준다
    """
    segments = {
        "a.m4s": media_segment([_fragment(0, 4)]),
        "b.m4s": media_segment([_fragment(400, 2)]),
    }
    host, url = _vod(monkeypatch, segments)
    head = fetch_fmp4_head(url, str(tmp_path / "segments"))

    first = segment_frames(head, url, 0)
    again = segment_frames(head, url, 0)
    other = segment_frames(head, url, 1)

    assert again is first
    assert [name for _m, name, _h in host.requests if name.endswith(".m4s")] == [
        "v/a.m4s",
        "v/b.m4s",
    ]
    assert len(other.video.decode_times) == 2
    assert sorted(head.segments) == [0, 1]


@pytest.mark.parametrize("folder", [True, False], ids=["with-folder", "without-folder"])
def test_segment_frames_rejects_a_truncated_segment_and_keeps_no_file(
    monkeypatch, tmp_path, folder
):
    """segment_frames는 서버가 잘린 세그먼트를 주면 잘림 오류로 실패하고 그 파일을 남기지 않아야 한다.

    mdat 도중에서 잘린 세그먼트(상자가 말하는 끝 > 본문 길이)
    -> TruncatedSegmentError, segment_dir에 파일 없음, head.stored · head.segments가 비어 있다
    """
    whole = media_segment([_fragment(0, 4, mdat_size=4000)])
    host, url = _vod(monkeypatch, {"a.m4s": whole[:-1000]})
    target = tmp_path / "segments"
    head = fetch_fmp4_head(url, str(target) if folder else None)

    with pytest.raises(TruncatedSegmentError):
        segment_frames(head, url, 0)

    assert not target.exists() or os.listdir(target) == []
    assert (head.stored, head.segments) == (set(), {})


def test_segment_frames_never_sends_a_range_request_to_a_truncating_cache(monkeypatch, tmp_path):
    """segment_frames는 범위 요청에 잘린 본문을 주고 그것을 캐시하는 서버에서도 온전한 세그먼트를 받아야 한다.

    truncating_cache를 켠 호스트, 세그먼트 둘을 읽음
    -> Range 머리가 든 요청 0건, 캐시에 잘린 본문 없음, 받아 둔 파일 == 세그먼트 bytes
    """
    segments = {"a.m4s": _two_moof_segment(), "b.m4s": media_segment([_fragment(600, 4)])}
    host, url = _vod(monkeypatch, segments)
    host.truncating_cache = True
    folder = tmp_path / "segments"
    head = fetch_fmp4_head(url, str(folder))

    segment_frames(head, url, 0)
    segment_frames(head, url, 1)

    assert _ranged(host) == []
    assert host.truncated() == []
    assert (folder / "1.m4v").read_bytes() == segments["a.m4s"]
    assert (folder / "2.m4v").read_bytes() == segments["b.m4s"]


def test_truncating_cache_host_poisons_a_file_after_a_range_request(monkeypatch):
    """truncating_cache를 켠 호스트는 캐시에 없는 파일의 범위 요청에 200과 잘린 본문으로 답하고, 그 뒤의 전체 요청에도 잘린 본문을 줘야 한다.

    1,000바이트 파일 둘. a: 범위 0-99 요청 뒤 전체 요청 / b: 전체 요청 뒤 범위 0-99 요청 뒤 전체 요청
    -> a: (200, 100바이트) · (200, 100바이트) / b: (200, 1000) · (206, 100) · (200, 1000)
    """
    host = _host(monkeypatch, {"a": bytes(1000), "b": bytes(1000)})
    host.truncating_cache = True
    session = host.session()
    ranged = {"Range": "bytes=0-99"}

    def ask(name: str, headers: dict | None = None) -> tuple[int, int]:
        response = session.get(host.url(name), headers=headers or {})
        return response.status_code, len(response.content)

    assert [ask("a", ranged), ask("a")] == [(200, 100), (200, 100)]
    assert [ask("b"), ask("b", ranged), ask("b")] == [(200, 1000), (206, 100), (200, 1000)]
    assert host.truncated() == ["a"]


def test_read_segment_file_reads_the_frames_of_a_stored_segment(tmp_path):
    """read_segment_file은 받아 둔 세그먼트 파일의 프레임 정보를 돌려줘야 한다.

    moof 둘짜리 세그먼트를 그대로 쓴 파일
    -> parse_media_segment와 같은 결과
    """
    data = _two_moof_segment()
    init = parse_init_segment(INIT)
    (tmp_path / "whole.m4v").write_bytes(data)

    assert read_segment_file(str(tmp_path / "whole.m4v"), init) == parse_media_segment(data, init)


@pytest.mark.parametrize(
    ("count", "index", "expected"),
    [
        (6, 0, "1.m4v"),
        (6, 5, "6.m4v"),
        (19960, 28, "00029.m4v"),
        (10, 9, "10.m4v"),
        (10, 0, "01.m4v"),
    ],
)
def test_segment_file_name_matches_the_name_the_engine_writes(count, index, expected):
    """segment_file_name은 세그먼트 번호(1부터)를 세그먼트 수의 자릿수만큼 0으로 채운 이름을 돌려줘야 한다.

    주석의 경우마다 (세그먼트 수, 인덱스)
    -> 기대한 파일 이름
    """
    assert segment_file_name(count, index) == expected


def test_download_segment_rejects_a_body_shorter_than_content_length(monkeypatch, tmp_path):
    """download_segment는 받은 길이가 서버가 말한 Content-Length와 다르면 잘림 오류로 실패하고 파일을 남기지 않아야 한다.

    Content-Length 100인데 본문은 60바이트인 응답
    -> TruncatedSegmentError, 폴더에 파일 없음(받는 중이던 .part도 없다)
    """

    class Short:
        headers = {"Content-Length": "100"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield bytes(60)

    session = SimpleNamespace(get=lambda url, **kwargs: Short())
    monkeypatch.setattr(hls_fmp4_module, "get_thread_session", lambda: session)

    with pytest.raises(TruncatedSegmentError):
        download_segment("https://range.test/a.m4s", str(tmp_path / "1.m4v"))

    assert os.listdir(tmp_path) == []


def test_download_segment_rejects_a_body_that_ends_right_after_a_moof(monkeypatch, tmp_path):
    """download_segment는 본문이 moof 바로 뒤에서 끝나면 잘림 오류로 실패하고 파일을 남기지 않아야 한다.

    moof 하나 · mdat 하나인 세그먼트에서 mdat를 통째로 뺀 본문(상자 크기의 합 == 본문 길이)
    -> TruncatedSegmentError, 폴더에 파일 없음
    """
    whole = media_segment([_fragment(0, 4, mdat_size=64)])
    body = whole[: whole.rindex(b"mdat") - 4]  # mdat 머리(크기 4바이트 + 종류)가 시작하는 자리
    host = _host(monkeypatch, {"a.m4s": body})

    with pytest.raises(TruncatedSegmentError):
        download_segment(host.url("a.m4s"), str(tmp_path / "1.m4v"))

    assert os.listdir(tmp_path) == []

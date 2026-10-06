"""구간 입력의 기준값 조회 — 전달 방식마다 무엇을 받아 프레임률 · 길이를 정하는가 (#309).

`app.section_basis.probe_section_basis`가 컨텐츠 타입마다 부르는 조회 함수를 대역으로 바꿔,
어떤 주소 · 어떤 값이 넘어가는지와 무엇을 돌려주는지를 잰다. 네트워크는 타지 않는다.
"""

from fractions import Fraction
from types import SimpleNamespace

import pytest

import app.section_basis as section_basis
from app.section_basis import SectionBasis, SectionBasisError, probe_section_basis
from core.models.content import ContentType, StreamKey


def _item(content_type: str, **extra) -> SimpleNamespace:
    """조회가 읽는 칸만 가진 카드 데이터 대역."""
    values = {
        "content_type": content_type,
        "vod_url": "https://chzzk.naver.com/video/1",
        "base_url": "https://media.invalid/stream",
        "resolution": 1080,
        "stream": None,
    }
    values.update(extra)
    return SimpleNamespace(**values)


def test_encoded_vod_reads_the_frame_rate_and_length_from_the_moov(monkeypatch):
    """인코딩 완료 VOD는 고른 해상도의 mp4에서 moov를 받아 그 값을 돌려줘야 한다.

    moov 색인: fps 60000/1001, 길이 1234.5초
    -> SectionBasis(60000/1001, 1234.5), 조회한 주소 == base_url
    """
    asked = []

    def fetch(url):
        asked.append(url)
        index = SimpleNamespace(fps=Fraction(60000, 1001), duration=1234.5)
        return SimpleNamespace(index=index)

    monkeypatch.setattr(section_basis, "fetch_mp4_head", fetch)

    basis = probe_section_basis(_item("video"))

    assert basis == SectionBasis(fps=Fraction(60000, 1001), duration=1234.5)
    assert asked == ["https://media.invalid/stream"]


def test_replay_uses_the_picked_variant_and_its_declared_frame_rate(monkeypatch):
    """인코딩 전 다시보기는 고른 변형의 플레이리스트에서 읽고, 선언된 프레임률을 넘겨야 한다.

    고른 변형 StreamKey(1920, 1080, 60.0, 6336000), 마스터의 선언값 60
    -> 변형 해석에 그 StreamKey가 넘어간다
    -> 프레임률 정하기에 선언값 60과 첫 세그먼트가 넘어간다
    -> 세그먼트를 둘 폴더를 주지 않는다(받은 본문을 버린다)
    -> SectionBasis(60, 3599.983)
    """
    stream = StreamKey(1920, 1080, 60.0, 6336000)
    seen = {}

    def resolve(content):
        seen["content"] = content
        return "https://media.invalid/1080.m3u8", Fraction(60)

    def fetch_head(*args, **kwargs):
        seen["head_args"] = (args, kwargs)
        return SimpleNamespace(init="init", playlist="playlist")

    def frames(head, url, index):
        seen.setdefault("segments", []).append((url, index))
        return f"segment{index}"

    def choose(init, segments, declared):
        seen["choose"] = (init, segments, declared)
        return SimpleNamespace(rate=Fraction(60))

    def timeline(playlist, init, segment_at):
        seen["last"] = segment_at(7)
        return SimpleNamespace(duration=3599.983)

    monkeypatch.setattr(section_basis, "resolve_m3u8_variant", resolve)
    monkeypatch.setattr(section_basis, "fetch_fmp4_head", fetch_head)
    monkeypatch.setattr(section_basis, "segment_frames", frames)
    monkeypatch.setattr(section_basis, "choose_frame_rate", choose)
    monkeypatch.setattr(section_basis, "fmp4_timeline", timeline)

    basis = probe_section_basis(_item("m3u8", stream=stream))

    assert basis == SectionBasis(fps=Fraction(60), duration=3599.983)
    content = seen["content"]
    assert content.content_type is ContentType.CHZZK_VIDEO_M3U8
    assert (content.stream, content.resolution) == (stream, 1080)
    assert seen["head_args"] == (("https://media.invalid/1080.m3u8",), {})
    assert seen["choose"] == ("init", ["segment0"], Fraction(60))
    assert seen["segments"] == [
        ("https://media.invalid/1080.m3u8", 0),
        ("https://media.invalid/1080.m3u8", 7),
    ]


def test_encrypted_vod_decrypts_with_the_resolved_key_and_keeps_it_out_of_the_result(monkeypatch):
    """암호화 VOD는 리졸버로 받은 키로 세그먼트를 읽고, 돌려주는 값에 키가 없어야 한다.

    매니페스트의 선언값 {base_url: 30}, 키 b"K" * 16
    -> 세그먼트 읽기에 그 키가 넘어간다, 프레임률 정하기에 선언값 30이 넘어간다
    -> 세그먼트를 둘 폴더를 주지 않는다
    -> SectionBasis(30, 100.0) — 칸은 fps · duration 둘뿐이다
    """
    key = b"K" * 16
    seen = {}
    base_url = "https://media.invalid/stream"

    def fetch_head(*args, **kwargs):
        seen["head_args"] = (args, kwargs)
        return SimpleNamespace(playlist="playlist")

    def streams(head, url, index, used_key):
        seen.setdefault("keys", []).append(used_key)
        return f"segment{index}"

    def choose(segments, declared):
        seen["choose"] = (segments, declared)
        return SimpleNamespace(rate=Fraction(30))

    def timeline(playlist, segment_at, fps):
        segment_at(3)
        return SimpleNamespace(duration=100.0)

    network = SimpleNamespace(
        extract_content_no=lambda url: ("video", "1"),
        get_video_info=lambda number, cookies: SimpleNamespace(video_id="id", in_key="in"),
        get_video_frame_rates=lambda video_id, in_key, cookies: {base_url: Fraction(30)},
    )
    monkeypatch.setattr(section_basis, "fetch_ts_head", fetch_head)
    monkeypatch.setattr(section_basis, "ts_key_uri", lambda url, head: "https://key.invalid/k")
    monkeypatch.setattr(section_basis, "resolve_aes_key", lambda content, uri: key)
    monkeypatch.setattr(section_basis, "segment_streams", streams)
    monkeypatch.setattr(section_basis, "choose_ts_frame_rate", choose)
    monkeypatch.setattr(section_basis, "ts_timeline", timeline)
    monkeypatch.setattr(section_basis, "NetworkManager", network)

    basis = probe_section_basis(_item("hls_aes"))

    assert basis == SectionBasis(fps=Fraction(30), duration=100.0)
    assert vars(basis) == {"fps": Fraction(30), "duration": 100.0}
    assert seen["head_args"] == ((base_url,), {})
    assert seen["keys"] == [key, key]
    assert seen["choose"] == (["segment0"], Fraction(30))


def test_encrypted_vod_measures_the_frame_rate_when_the_manifest_cannot_be_read(monkeypatch):
    """선언된 프레임률을 읽지 못하면 선언값 없이(None) 첫 세그먼트에서 재게 해야 한다.

    영상 정보 조회가 예외를 던짐
    -> 프레임률 정하기에 넘어간 선언값 == None, 조회는 실패하지 않는다
    """
    seen = {}

    def broken(*args):
        raise RuntimeError("조회 실패(대역)")

    def choose(segments, declared):
        seen["declared"] = declared
        return SimpleNamespace(rate=Fraction(60))

    network = SimpleNamespace(extract_content_no=lambda url: ("video", "1"), get_video_info=broken)
    monkeypatch.setattr(section_basis, "fetch_ts_head", lambda url: SimpleNamespace(playlist="p"))
    monkeypatch.setattr(section_basis, "ts_key_uri", lambda url, head: "https://key.invalid/k")
    monkeypatch.setattr(section_basis, "resolve_aes_key", lambda content, uri: b"K" * 16)
    monkeypatch.setattr(section_basis, "segment_streams", lambda head, url, index, key: "segment")
    monkeypatch.setattr(section_basis, "choose_ts_frame_rate", choose)
    monkeypatch.setattr(
        section_basis, "ts_timeline", lambda playlist, at, fps: SimpleNamespace(duration=5.0)
    )
    monkeypatch.setattr(section_basis, "NetworkManager", network)

    basis = probe_section_basis(_item("hls_aes"))

    assert seen == {"declared": None}
    assert basis == SectionBasis(fps=Fraction(60), duration=5.0)


@pytest.mark.parametrize("content_type", ["clip", "live", ""])
def test_types_without_sections_are_refused(content_type):
    """구간 기능이 없는 타입은 조회하지 않고 SectionBasisError여야 한다.

    content_type = "clip" · "live" · ""
    -> SectionBasisError
    """
    with pytest.raises(SectionBasisError):
        probe_section_basis(_item(content_type))

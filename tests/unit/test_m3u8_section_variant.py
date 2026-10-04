"""구간 다운로드가 읽는 변형 — 목록에서 고른 변형과 같아야 한다 (#309 · #318).

구간 다운로드는 받기 전에 그 변형의 플레이리스트와 세그먼트를 읽어 구간을 정한다. 읽는
변형이 다운로드가 받는 변형과 다르면 다른 영상의 프레임 시각으로 구간을 정하게 된다.

입력은 tests/unit/stream_samples.py의 합성 표본이다. 요청은 보내지 않는다.
"""

from fractions import Fraction
from types import SimpleNamespace

import pytest

import app.download_resolvers as resolvers
import app.network as network
import scripts.headless_download as headless
from app.network import NetworkManager
from core.api.mp4 import MP4_INVALID, Mp4Error
from core.models.content import Content, ContentType, StreamKey, VideoInfo
from tests.mocks.mock_http import MockResponse
from tests.unit.stream_samples import S4, S5, Sample, variant_url

VOD_URL = "https://chzzk.naver.com/video/123"

# (표본, 변형 번호) — S5는 세로 방송, S4는 짧은 변이 같은 변형이 둘(3번 60fps · 4번 원본 30fps)
CASES = [(S5, number) for number in range(len(S5.variants))] + [
    (S4, number) for number in range(len(S4.variants))
]
IDS = [f"S5-v{number}" for number in range(len(S5.variants))] + [
    f"S4-v{number}" for number in range(len(S4.variants))
]


def _stream_key(sample: Sample, number: int) -> StreamKey:
    """표본의 number번째 변형을 목록에서 골랐을 때 항목이 드는 값."""
    width, height, bandwidth, frame_rate = sample.variants[number]
    return StreamKey(width, height, frame_rate, bandwidth)


@pytest.fixture
def serve(monkeypatch):
    """영상 정보 조회와 마스터 플레이리스트 요청에 표본으로 답하게 한다."""

    def install(sample: Sample) -> None:
        info = VideoInfo(
            video_id=None,
            in_key=None,
            adult=False,
            vod_status=None,
            live_rewind_playback_json=sample.playback,
            membership_benefit_type=None,
            encryption_type=None,
            metadata={},
        )
        monkeypatch.setattr(resolvers, "_load_cookies", lambda: {})
        monkeypatch.setattr(NetworkManager, "get_video_info", lambda content_no, cookies: info)
        monkeypatch.setattr(
            network._session, "get", lambda url, **kwargs: MockResponse(text=sample.master)
        )

    return install


@pytest.mark.parametrize(("sample", "number"), CASES, ids=IDS)
def test_section_resolver_reads_the_variant_picked_in_the_list(serve, sample, number):
    """구간 해석이 읽는 주소는 목록에서 고른 변형의 주소이고, 다운로드가 받는 주소와 같아야 한다.

    주석의 표본마다 number번째 변형을 고른 Content(해상도 = 짧은 변, stream = 그 변형의 값)
    -> resolve_m3u8_variant == (그 변형의 주소, 그 변형의 FRAME-RATE),
       resolve_m3u8_base_url == 그 변형의 주소
    """
    serve(sample)
    width, height, _bandwidth, frame_rate = sample.variants[number]
    content = Content(
        content_type=ContentType.CHZZK_VIDEO_M3U8,
        url=VOD_URL,
        resolution=min(width, height),
        stream=_stream_key(sample, number),
    )

    assert resolvers.resolve_m3u8_variant(content) == (variant_url(number), Fraction(frame_rate))
    assert resolvers.resolve_m3u8_base_url(content) == variant_url(number)


@pytest.mark.parametrize(("sample", "number"), CASES, ids=IDS)
def test_headless_section_resolution_reads_the_variant_of_the_item(
    monkeypatch, serve, sample, number
):
    """헤드리스의 구간 해석은 아이템이 고른 변형의 플레이리스트를 받아야 한다.

    주석의 표본마다 number번째 변형을 고른 아이템(해상도 = 짧은 변, stream = 그 변형의 값)
    -> fetch_fmp4_head가 받는 주소 == 그 변형의 주소
    """
    serve(sample)
    width, height, _bandwidth, _frame_rate = sample.variants[number]
    item = SimpleNamespace(
        vod_url=VOD_URL, resolution=min(width, height), stream=_stream_key(sample, number)
    )
    fetched = []

    def fetch_fmp4_head(url, segment_dir=None):
        fetched.append(url)
        raise Mp4Error(MP4_INVALID, "여기서 멈춘다 — 주소만 본다")

    monkeypatch.setattr(headless, "fetch_fmp4_head", fetch_fmp4_head)

    assert headless._resolve_fmp4_sections(item, ["00:00:01:00-00:00:02:00"]) is None
    assert fetched == [variant_url(number)]

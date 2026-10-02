"""인코딩 전 다시보기 — 마스터 플레이리스트 기준의 목록부터 다운로드 주소 · 파일명까지 (#318).

입력은 tests/unit/stream_samples.py의 합성 표본이다. 요청은 보내지 않는다 — 마스터
플레이리스트 응답만 세션 대역으로 바꾼다.
"""

import logging
import os

import pytest
import requests

import app.download_resolvers as resolvers
import app.network as network
from app.network import REQUEST_TIMEOUT, NetworkManager
from app.viewmodels.data import ContentItem
from core.api.playback_tracks import STREAM_NOT_FOUND, StreamSelectionError
from core.models.content import Content, ContentType, StreamKey, VideoInfo
from core.services.metadata_service import fetch_video
from core.utils.paths import build_output_path
from tests.mocks.mock_http import MockResponse
from tests.unit.stream_samples import MASTER_URL, S1, S2, S4, S5, S9, S11, Sample, variant_url

COOKIES = {"NID_AUT": "REDACTED", "NID_SES": "REDACTED"}
VOD_URL = "https://chzzk.naver.com/video/1"


class _Session:
    """마스터 플레이리스트 요청에 주어진 본문으로 답하고 요청을 기록한다."""

    def __init__(self, monkeypatch, text: str = "", *, error: Exception | None = None):
        self.calls: list[tuple] = []
        self._text = text
        self._error = error
        monkeypatch.setattr(network._session, "get", self._get)

    def _get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._error is not None:
            raise self._error
        return MockResponse(text=self._text)


class _Api:
    """영상 정보만 대역이고 나머지는 NetworkManager 그대로인 조회 묶음."""

    get_video_m3u8_manifest = staticmethod(NetworkManager.get_video_m3u8_manifest)
    get_video_m3u8_streams = staticmethod(NetworkManager.get_video_m3u8_streams)

    def __init__(self, sample: Sample):
        self._sample = sample

    def get_video_info(self, video_no, cookies):
        return VideoInfo(
            video_id="video-id",
            in_key=None,
            adult=False,
            vod_status="NONE",
            live_rewind_playback_json=self._sample.playback,
            membership_benefit_type=None,
            encryption_type=None,
            metadata={"title": "방송", "duration": 1},
        )


def _fetch(sample: Sample):
    """조회 서비스를 거쳐 (해상도 목록, 자동 해상도, 자동 주소)를 얻는다."""
    result, _encrypted = fetch_video(VOD_URL, "1", COOKIES, "downloads", _Api(sample))
    return result[2], result[3], result[4]


def _item(sample: Sample, tmp_path) -> ContentItem:
    """조회 결과로 카드 아이템을 만든다 — 제품이 만드는 것과 같은 인자다."""
    reps, resolution, base_url = _fetch(sample)
    return ContentItem(
        VOD_URL,
        {"title": "방송"},
        reps,
        resolution,
        base_url,
        str(tmp_path),
        "m3u8",
        sample.playback,
    )


# ================================================================ 불러올 때


def test_loading_requests_the_master_playlist_once_with_cookies(monkeypatch):
    """다시보기를 불러올 때 마스터 플레이리스트를 쿠키와 함께 한 번 받아야 한다.

    S1
    -> 요청 1건 — 주소는 playback 정보의 path, 쿠키 · 리다이렉트 끔 · 조회 타임아웃
    """
    session = _Session(monkeypatch, S1.master)

    _fetch(S1)

    assert session.calls == [
        (MASTER_URL, {"cookies": COOKIES, "allow_redirects": False, "timeout": REQUEST_TIMEOUT})
    ]


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        (S1, [(144, None), (360, None), (480, None), (720, 60), (1080, 60)]),
        (S2, [(144, None), (360, None), (480, None), (720, 60), (1080, None)]),
        (S4, [(144, None), (360, None), (480, None), (720, 60), (720, None)]),
        (S5, [(144, None), (360, None), (480, None), (720, 60), (1080, None)]),
        (S9, [(144, None), (360, None), (480, None), (720, 60), (1080, 60)]),
        (S11, [(144, None), (360, None), (480, None), (720, 60), (1080, 60)]),
    ],
    ids=["S1", "S2", "S4", "S5", "S9", "S11"],
)
def test_loaded_list_carries_the_short_side_and_the_shown_frame_rate(monkeypatch, sample, expected):
    """불러온 목록의 해상도는 짧은 변이고, 표시할 프레임률은 마스터 플레이리스트의 값이어야 한다.

    주석의 표본마다
    -> (해상도, 표시할 fps) 오름차순, 자동 선택은 마지막 항목의 해상도
    """
    _Session(monkeypatch, sample.master)

    reps, resolution, base_url = _fetch(sample)

    assert [(rep[0], ContentItem.rep_frame_rate(rep)) for rep in reps] == expected
    assert (resolution, base_url) == (expected[-1][0], None)


@pytest.mark.parametrize(
    ("sample", "originals"),
    [
        (S1, [False, False, False, False, True]),
        (S2, [False, False, False, False, True]),
        (S4, [False, False, False, False, True]),  # 30fps 변형이 원본
        (S5, [False, False, False, False, True]),
        (S9, [False, False, False, False, True]),  # playback의 fps · 비트레이트가 0이어도
        (S11, [False, False, False, False, False]),  # playback의 트랙이 마스터에 없다
    ],
    ids=["S1", "S2", "S4", "S5", "S9", "S11"],
)
def test_loaded_list_marks_the_original_stream(monkeypatch, sample, originals):
    """불러온 목록의 원본 표시는 playback의 원본 트랙과 짝인 변형 하나에만 있어야 한다.

    주석의 표본마다
    -> 목록 순서대로의 원본 여부
    """
    _Session(monkeypatch, sample.master)

    reps, _resolution, _base_url = _fetch(sample)

    assert [ContentItem.rep_is_original(rep) for rep in reps] == originals


@pytest.mark.parametrize(
    "error",
    [requests.ConnectionError("boom"), requests.Timeout("slow")],
    ids=["connection", "timeout"],
)
def test_loading_falls_back_to_the_playback_tracks_when_the_master_cannot_be_fetched(
    monkeypatch, caplog, error
):
    """마스터 플레이리스트를 받지 못하면 playback 정보의 트랙으로 목록을 만들고 경고를 남겨야 한다.

    S1, 마스터 플레이리스트 요청이 주석의 예외로 실패
    -> [[144, None], [360, None], [480, None], [720, None], [1080, None]](스트림 정보 없음),
       조회 서비스 로거의 WARNING 1건
    """
    _Session(monkeypatch, error=error)

    with caplog.at_level(logging.WARNING):
        reps, resolution, _base_url = _fetch(S1)

    assert reps == [[144, None], [360, None], [480, None], [720, None], [1080, None]]
    assert [getattr(rep, "stream", None) for rep in reps] == [None] * 5
    assert resolution == 1080
    warnings = [r for r in caplog.records if r.name == "core.services.metadata_service"]
    assert [r.levelno for r in warnings] == [logging.WARNING]


def test_loading_falls_back_when_the_master_has_no_variant(monkeypatch, caplog):
    """마스터 플레이리스트에 변형이 없으면 playback 정보의 트랙으로 목록을 만들고 경고를 남겨야 한다.

    S1의 playback 정보, 변형이 없는 마스터 플레이리스트("#EXTM3U")
    -> 해상도 [144, 360, 480, 720, 1080], 조회 서비스 로거의 WARNING 1건
    """
    _Session(monkeypatch, "#EXTM3U\n")

    with caplog.at_level(logging.WARNING):
        reps, _resolution, _base_url = _fetch(S1)

    assert [rep[0] for rep in reps] == [144, 360, 480, 720, 1080]
    warnings = [r for r in caplog.records if r.name == "core.services.metadata_service"]
    assert [r.levelno for r in warnings] == [logging.WARNING]


# ================================================================ 기본 선택 · 파일명


@pytest.mark.parametrize(
    ("sample", "resolution", "stream", "tag"),
    [
        (S1, 1080, StreamKey(1920, 1080, 60.0, 6192000), ""),
        (S4, 720, StreamKey(720, 1280, 30.0, 2692000), "(원본)"),  # 720 둘 중 원본
        (S11, 1080, StreamKey(1920, 1080, 60.0, 8300000), ""),
    ],
    ids=["S1", "S4", "S11"],
)
def test_default_selection_is_the_largest_short_side_and_the_original_among_equals(
    monkeypatch, tmp_path, sample, resolution, stream, tag
):
    """기본 선택은 짧은 변이 가장 큰 항목이고, 같으면 원본이어야 한다.

    주석의 표본마다
    -> 아이템의 해상도 · 스트림 값 · 파일명 표시
    """
    _Session(monkeypatch, sample.master)

    item = _item(sample, tmp_path)

    assert (item.resolution, item.stream, item.resolution_tag) == (resolution, stream, tag)


@pytest.mark.parametrize(
    ("sample", "names"),
    [
        (
            S1,
            ["방송 144p.mp4", "방송 360p.mp4", "방송 480p.mp4", "방송 720p.mp4", "방송 1080p.mp4"],
        ),
        (
            S4,
            [
                "방송 144p.mp4",
                "방송 360p.mp4",
                "방송 480p.mp4",
                "방송 720p.mp4",  # 720p 60fps
                "방송 720p(원본).mp4",
            ],
        ),
    ],
    ids=["S1", "S4"],
)
def test_file_names_tell_the_two_streams_of_the_same_short_side_apart(
    monkeypatch, tmp_path, sample, names
):
    """파일명은 `{제목} {짧은 변}p.mp4`이고, 짧은 변이 같은 항목이 둘일 때 원본 쪽에만 `(원본)`이 붙어야 한다.

    주석의 표본마다 목록의 항목을 차례로 고름
    -> 파일명
    """
    _Session(monkeypatch, sample.master)
    item = _item(sample, tmp_path)

    made = []
    for rep in item.unique_reps:
        item.select_rep(rep)
        path = build_output_path(str(tmp_path), item.title, item.resolution, item.resolution_tag)
        made.append(os.path.basename(path))

    assert made == names


# ================================================================ 다운로드를 시작할 때


@pytest.mark.parametrize(
    ("sample", "variants"),
    [
        (S1, [3, 2, 1, 0, 4]),
        (S4, [3, 2, 1, 0, 4]),  # 720p 60fps → 변형 0, 720p(원본) → 변형 4
        (S9, [3, 2, 1, 0, 4]),
        (S11, [3, 2, 1, 0, 4]),  # playback의 트랙과 무관하게 마스터의 변형을 받는다
    ],
    ids=["S1", "S4", "S9", "S11"],
)
def test_each_listed_entry_downloads_its_own_variant(monkeypatch, sample, variants):
    """목록의 항목을 고르면 다운로드 주소는 그 항목이 가리키는 변형의 것이어야 한다.

    주석의 표본마다 목록의 항목 순서대로 (해상도, 스트림 값)으로 주소를 해석
    -> 변형 번호 순서
    """
    _Session(monkeypatch, sample.master)
    reps, _resolution, _base_url = _fetch(sample)

    urls = [
        NetworkManager.get_video_m3u8_base_url(sample.playback, rep[0], COOKIES, rep.stream)
        for rep in reps
    ]

    assert urls == [variant_url(number) for number in variants]


def test_download_fails_with_a_key_when_the_selected_variant_is_gone(monkeypatch):
    """다운로드를 시작할 때 고른 변형이 마스터 플레이리스트에 없으면 키 기반 오류로 실패해야 한다.

    S4에서 고른 720p(원본)의 스트림 값, 다시 받은 마스터 플레이리스트는 S1의 것
    -> StreamSelectionError, message_key == STREAM_NOT_FOUND
    """
    _Session(monkeypatch, S4.master)
    reps, _resolution, _base_url = _fetch(S4)
    _Session(monkeypatch, S1.master)

    with pytest.raises(StreamSelectionError) as caught:
        NetworkManager.get_video_m3u8_base_url(S4.playback, 720, COOKIES, reps[-1].stream)

    assert caught.value.message_key == STREAM_NOT_FOUND


@pytest.mark.parametrize(
    ("resolution", "variant"), [(144, 3), (360, 2), (480, 1), (720, 0), (1080, 4)]
)
def test_download_without_a_stream_value_finds_the_height_when_the_tracks_do_not_fit(
    monkeypatch, resolution, variant
):
    """스트림 값 없이 해상도만으로 받을 때, playback의 트랙과 맞는 변형이 없으면 세로값이 그 해상도인 변형을 받아야 한다.

    S11(playback은 세로 트랙, 마스터는 가로 변형), 해상도마다
    -> 세로값이 그 해상도인 변형의 주소
    """
    _Session(monkeypatch, S11.master)

    base_url = NetworkManager.get_video_m3u8_base_url(S11.playback, resolution, COOKIES)

    assert base_url == variant_url(variant)


def test_resolver_hands_the_selected_stream_to_the_lookup(monkeypatch):
    """다운로드 시작 때의 주소 해석은 Content에 실린 스트림 값을 그대로 넘겨야 한다.

    Content(resolution=720, stream=S4의 720p(원본) 값)
    -> get_video_m3u8_base_url(playback, 720, 쿠키, 그 스트림 값)
    """
    stream = StreamKey(720, 1280, 30.0, 2692000)
    seen: list[tuple] = []
    monkeypatch.setattr(resolvers, "_load_cookies", lambda: COOKIES)
    monkeypatch.setattr(
        NetworkManager,
        "get_video_info",
        staticmethod(lambda video_no, cookies: _Api(S4).get_video_info(video_no, cookies)),
    )
    monkeypatch.setattr(
        NetworkManager,
        "get_video_m3u8_base_url",
        staticmethod(lambda *args: seen.append(args) or "https://example.invalid/x.m3u8"),
    )
    content = Content(
        content_type=ContentType.CHZZK_VIDEO_M3U8, url=VOD_URL, resolution=720, stream=stream
    )

    resolvers.resolve_m3u8_base_url(content)

    assert seen == [(S4.playback, 720, COOKIES, stream)]

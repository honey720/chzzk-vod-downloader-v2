"""세로 방송 다시보기의 해상도 목록과 스트림 선택 (#318).

세로 방송은 가로 < 세로이고, 크기가 같은 트랙이 둘이다 — 다시 인코딩한 "720p"와 원본
"1080p"가 모두 720x1280이다. 입력은 그 구조만 옮긴 합성 값이다. 요청은 보내지 않는다.

가로 방송의 기준은 test_m3u8_track_selection.py에 있다 — 수정 전 코드에서 박제한 것이라
그 파일은 바꾸지 않고 입력을 만드는 함수만 빌려 쓴다.
"""

import os

import pytest

import app.network as network
from app.network import NetworkManager
from core.api.playback_tracks import STREAM_NOT_FOUND, StreamSelectionError
from core.utils.paths import build_output_path
from tests.mocks.mock_http import MockResponse
from tests.unit.test_m3u8_track_selection import (
    LANDSCAPE,
    master_playlist,
    playback_json,
    variant_url,
)

# (encodingTrackId, 가로, 세로, videoBitRate, audioBitRate, videoFrameRate)
PORTRAIT = [
    ("720p", 720, 1280, 3000000, 192000, "60.0"),
    ("480p", 480, 852, 1500000, 192000, "30.0"),
    ("360p", 360, 640, 600000, 96000, "30.0"),
    ("144p", 144, 256, 128000, 64000, "30.0"),
    ("1080p", 720, 1280, 2500000, 192000, "30.0"),  # 원본 트랙 — 720p 트랙과 크기가 같다
]


@pytest.fixture
def serve_master(monkeypatch):
    """마스터 플레이리스트 요청에 주어진 본문으로 답하게 한다."""

    def install(text: str) -> None:
        monkeypatch.setattr(network._session, "get", lambda url, **kwargs: MockResponse(text=text))

    return install


def test_portrait_list_shows_both_tracks_of_the_same_size():
    """세로 방송의 해상도 목록에는 크기가 같은 두 트랙이 이름의 해상도로 따로 나와야 한다.

    PORTRAIT(720x1280 트랙 둘 — "720p" · "1080p")
    -> [[144, None], [360, None], [480, None], [720, None], [1080, None]], 자동 (1080, None)
    """
    reps, auto_resolution, auto_base_url = NetworkManager.get_video_m3u8_manifest(
        playback_json(PORTRAIT)
    )

    assert reps == [[144, None], [360, None], [480, None], [720, None], [1080, None]]
    assert (auto_resolution, auto_base_url) == (1080, None)


@pytest.mark.parametrize(
    ("resolution", "track_number"),
    [(144, 3), (360, 2), (480, 1), (720, 0), (1080, 4)],
)
def test_each_portrait_resolution_selects_its_own_stream(serve_master, resolution, track_number):
    """세로 방송에서 목록의 해상도를 고르면 그 트랙의 플레이리스트 주소가 나와야 한다.

    PORTRAIT의 playback 정보와 마스터 플레이리스트, 목록의 해상도마다
    -> 표에서 그 이름인 트랙의 주소 (720은 60fps 트랙, 1080은 원본 트랙)
    """
    serve_master(master_playlist(PORTRAIT))

    base_url = NetworkManager.get_video_m3u8_base_url(playback_json(PORTRAIT), resolution, {})

    assert base_url == variant_url(track_number)


@pytest.mark.parametrize(("resolution", "track_number"), [(720, 0), (1080, 4)])
def test_same_size_tracks_are_told_apart_whatever_the_order_in_the_master(
    serve_master, resolution, track_number
):
    """크기가 같은 두 트랙은 마스터 플레이리스트에서의 순서와 관계없이 제 변형을 골라야 한다.

    PORTRAIT의 playback 정보, 변형의 순서를 뒤집은 마스터 플레이리스트
    -> 720은 60fps 변형, 1080은 30fps 변형의 주소
    """
    lines = master_playlist(PORTRAIT).splitlines()
    pairs = [lines[n : n + 2] for n in range(2, len(lines), 2)]
    reversed_master = "\n".join(lines[:2] + [line for pair in reversed(pairs) for line in pair])
    serve_master(reversed_master + "\n")

    base_url = NetworkManager.get_video_m3u8_base_url(playback_json(PORTRAIT), resolution, {})

    assert base_url == variant_url(track_number)


def test_portrait_file_names_carry_the_track_name(tmp_path):
    """세로 방송의 파일명은 `{제목} {트랙 이름의 해상도}p.mp4`여야 한다.

    PORTRAIT, 제목 "방송"
    -> 방송 144p.mp4 · 방송 360p.mp4 · 방송 480p.mp4 · 방송 720p.mp4 · 방송 1080p.mp4
    """
    reps, _auto_resolution, _auto_base_url = NetworkManager.get_video_m3u8_manifest(
        playback_json(PORTRAIT)
    )

    names = [os.path.basename(build_output_path(str(tmp_path), "방송", rep[0])) for rep in reps]

    assert names == [
        "방송 144p.mp4",
        "방송 360p.mp4",
        "방송 480p.mp4",
        "방송 720p.mp4",
        "방송 1080p.mp4",
    ]


def test_selection_fails_with_a_key_when_neither_the_track_size_nor_the_height_fits(serve_master):
    """고른 트랙과 크기가 같은 변형도, 세로값이 그 해상도인 변형도 없으면 키 기반 오류로 실패해야 한다.

    PORTRAIT의 playback 정보, 1920x1080 변형 하나뿐인 마스터 플레이리스트, 해상도 720
    -> StreamSelectionError, message_key == STREAM_NOT_FOUND
    """
    serve_master(master_playlist([LANDSCAPE[4]]))

    with pytest.raises(StreamSelectionError) as caught:
        NetworkManager.get_video_m3u8_base_url(playback_json(PORTRAIT), 720, {})

    assert caught.value.message_key == STREAM_NOT_FOUND


def test_selection_fails_with_a_key_when_two_variants_fit_the_track(serve_master):
    """고른 트랙에 맞는 변형이 둘이면 먼저 나온 것을 고르지 않고 키 기반 오류로 실패해야 한다.

    PORTRAIT의 playback 정보, 원본 트랙의 변형이 두 번 나오는 마스터 플레이리스트, 해상도 1080
    -> StreamSelectionError, message_key == STREAM_NOT_FOUND
    """
    serve_master(master_playlist(PORTRAIT + [PORTRAIT[4]]))

    with pytest.raises(StreamSelectionError) as caught:
        NetworkManager.get_video_m3u8_base_url(playback_json(PORTRAIT), 1080, {})

    assert caught.value.message_key == STREAM_NOT_FOUND


def test_selection_falls_back_to_the_height_when_the_playback_has_no_such_track(serve_master):
    """playback 정보에 그 해상도의 트랙이 없으면 세로값이 그 해상도인 첫 변형을 골라야 한다.

    encodingTrack이 없는 playback 정보, LANDSCAPE의 마스터 플레이리스트, 해상도 480
    -> 표에서 세로가 480인 트랙의 주소
    """
    serve_master(master_playlist(LANDSCAPE))
    playback = '{"media": [{"path": "https://example.invalid/live/master.m3u8"}]}'

    base_url = NetworkManager.get_video_m3u8_base_url(playback, 480, {})

    assert base_url == variant_url(1)

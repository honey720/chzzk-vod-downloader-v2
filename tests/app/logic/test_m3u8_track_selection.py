"""인코딩 전 다시보기의 해상도 목록과 스트림 선택 (#318).

playback 정보의 트랙과 마스터 플레이리스트의 변형을 같은 표에서 만든 합성 입력으로 잰다.
요청은 보내지 않는다 — 마스터 플레이리스트 응답만 세션 대역으로 바꾼다.
"""

import json
import os

import pytest

import app.network as network
from app.network import NetworkManager
from app.viewmodels.download_viewmodel import _failure_message_key
from core.chzzk.playback_tracks import STREAM_NOT_FOUND, StreamSelectionError
from core.utils.paths import build_output_path
from tests.mocks.mock_http import MockResponse

MASTER_URL = "https://example.invalid/live/master.m3u8"

# (encodingTrackId, 가로, 세로, videoBitRate, audioBitRate, videoFrameRate)
LANDSCAPE = [
    ("720p", 1280, 720, 3000000, 192000, "60.0"),
    ("480p", 852, 480, 1500000, 192000, "30.0"),
    ("360p", 640, 360, 600000, 96000, "30.0"),
    ("144p", 256, 144, 128000, 64000, "30.0"),
    ("1080p", 1920, 1080, 6000000, 192000, "60.0"),  # 원본 트랙
]


def playback_json(tracks: list[tuple]) -> str:
    """트랙 표로 playback 정보를 만든다."""
    return json.dumps(
        {
            "media": [
                {
                    "path": MASTER_URL,
                    "encodingTrack": [
                        {
                            "encodingTrackId": name,
                            "videoWidth": width,
                            "videoHeight": height,
                            "videoBitRate": video,
                            "audioBitRate": audio,
                            "videoFrameRate": fps,
                        }
                        for name, width, height, video, audio, fps in tracks
                    ],
                }
            ]
        }
    )


def master_playlist(tracks: list[tuple]) -> str:
    """같은 표로 마스터 플레이리스트를 만든다 — n번째 트랙의 주소는 ``v<n>/chunklist.m3u8``."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
    for number, (_name, width, height, video, audio, fps) in enumerate(tracks):
        lines.append(
            f"#EXT-X-STREAM-INF:BANDWIDTH={video + audio},"
            f'CODECS="avc1.640028,mp4a.40.2",RESOLUTION={width}x{height},'
            f"FRAME-RATE={float(fps):.2f}"
        )
        lines.append(f"v{number}/chunklist.m3u8")
    return "\n".join(lines) + "\n"


def variant_url(number: int) -> str:
    """표의 n번째 트랙을 고르면 나와야 하는 주소."""
    return f"https://example.invalid/live/v{number}/chunklist.m3u8"


@pytest.fixture
def serve_master(monkeypatch):
    """마스터 플레이리스트 요청에 주어진 본문으로 답하게 한다."""

    def install(text: str) -> None:
        monkeypatch.setattr(network._session, "get", lambda url, **kwargs: MockResponse(text=text))

    return install


class TestLandscapeBroadcastIsUnchanged:
    """가로 방송의 목록 · 선택 · 파일명은 #318 수정 전과 같다 — 수정 전 코드에서 얻은 값이다."""

    def test_list_has_one_entry_per_track_in_ascending_order(self):
        """가로 방송의 해상도 목록은 트랙마다 하나씩 오름차순이고 자동 선택은 가장 높은 해상도여야 한다.

        LANDSCAPE(트랙 5개)
        -> [[144, None], [360, None], [480, None], [720, None], [1080, None]], 자동 (1080, None)
        """
        reps, auto_resolution, auto_base_url = NetworkManager.get_video_m3u8_manifest(
            playback_json(LANDSCAPE)
        )

        assert reps == [[144, None], [360, None], [480, None], [720, None], [1080, None]]
        assert (auto_resolution, auto_base_url) == (1080, None)

    @pytest.mark.parametrize(
        ("resolution", "track_number"),
        [(144, 3), (360, 2), (480, 1), (720, 0), (1080, 4)],
    )
    def test_each_listed_resolution_selects_its_own_stream(
        self, serve_master, resolution, track_number
    ):
        """가로 방송에서 목록의 해상도를 고르면 그 트랙의 플레이리스트 주소가 나와야 한다.

        LANDSCAPE의 playback 정보와 마스터 플레이리스트, 목록의 해상도마다
        -> 표에서 그 해상도인 트랙의 주소
        """
        serve_master(master_playlist(LANDSCAPE))

        base_url = NetworkManager.get_video_m3u8_base_url(playback_json(LANDSCAPE), resolution, {})

        assert base_url == variant_url(track_number)

    def test_file_names_carry_the_listed_resolution(self, tmp_path):
        """가로 방송의 파일명은 `{제목} {목록의 해상도}p.mp4`여야 한다.

        LANDSCAPE, 제목 "방송"
        -> 방송 144p.mp4 · 방송 360p.mp4 · 방송 480p.mp4 · 방송 720p.mp4 · 방송 1080p.mp4
        """
        reps, _auto_resolution, _auto_base_url = NetworkManager.get_video_m3u8_manifest(
            playback_json(LANDSCAPE)
        )

        names = [os.path.basename(build_output_path(str(tmp_path), "방송", rep[0])) for rep in reps]

        assert names == [
            "방송 144p.mp4",
            "방송 360p.mp4",
            "방송 480p.mp4",
            "방송 720p.mp4",
            "방송 1080p.mp4",
        ]


class TestMasterEndingWithATagFailsWithAKey:
    """맞는 STREAM-INF 줄이 마지막 줄이라 주소 줄이 없으면 키가 있는 선택 실패로 끝난다 (#333)."""

    # 852x480 변형(주소 있음) 뒤에 주소 줄 없는 1280x720 태그로 끝나는 마스터
    MASTER = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1692000,RESOLUTION=852x480,FRAME-RATE=30.00\n"
        "v0/chunklist.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=3192000,RESOLUTION=1280x720,FRAME-RATE=60.00"
    )

    @pytest.mark.parametrize(
        "tracks",
        [
            [("480p", 852, 480, 1500000, 192000, "30.0")],  # 720 트랙 없음 — 세로값으로 찾는다
            [  # 720 트랙은 있으나 맞는 변형이 없음 — 세로값으로 다시 찾는다
                ("480p", 852, 480, 1500000, 192000, "30.0"),
                ("720p", 1280, 720, 3000000, 192000, "60.0"),
            ],
        ],
        ids=["no-track", "track-without-variant"],
    )
    def test_selecting_720_fails_with_the_stream_not_found_key(self, serve_master, tracks):
        """마스터가 주소 줄 없는 720 태그로 끝나면 720을 고를 때 STREAM_NOT_FOUND 키의 선택 실패여야 한다.

        MASTER, playback 트랙(480만 · 480과 720), 해상도 720
        -> StreamSelectionError, 실패 안내 키 == STREAM_NOT_FOUND
        """
        serve_master(self.MASTER)

        with pytest.raises(StreamSelectionError) as caught:
            NetworkManager.get_video_m3u8_base_url(playback_json(tracks), 720, {})

        assert _failure_message_key(caught.value) == STREAM_NOT_FOUND
